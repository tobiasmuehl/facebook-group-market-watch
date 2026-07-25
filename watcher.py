#!/usr/bin/env python3
"""Facebook Group Market Watch — CLI.

    watcher sample        pull real posts from your groups (no LLM, no DB)
    watcher test          run watch.py over those posts and show what happens
    watcher run           the actual watcher
    watcher rematch       re-apply matches() to posts you already parsed (free)
    watcher label         build a golden set for benchmarking
    watcher bench         compare models on the golden set
    watcher web           browse what you've matched
    watcher doctor        check your setup before blaming the code

Start with `sample`, then `test`. Don't run `run` until `test` looks right.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

# Explicit path, not a search. Bare load_dotenv() walks up the directory tree
# and will happily load an unrelated .env from a parent folder.
load_dotenv(Path(__file__).parent / ".env")

from src import db, dedup, llm, notify  # noqa: E402

SAMPLE_PATH = Path("data/sample.json")
GOLDEN_PATH = Path("data/golden.json")


def load_watch():
    """Import watch.py fresh. Kept in a function so `test` picks up edits."""
    import importlib

    import watch

    return importlib.reload(watch)


def schema_hint(model_cls: Any) -> str:
    """Field list handed to the model, derived from watch.Item so the two can't
    drift apart."""
    lines = []
    for name, field in model_cls.model_fields.items():
        annotation = str(field.annotation).replace("typing.", "").replace("<class '", "").replace("'>", "")
        annotation = annotation.replace("Optional[", "").rstrip("]").replace(" | None", "")
        note = f"  // {field.description}" if field.description else ""
        lines.append(f'  "{name}": {annotation} or null{note}')
    return "{\n" + ",\n".join(lines) + "\n}"


# ── the four gates ───────────────────────────────────────────────────────────

class Pipeline:
    def __init__(self, watch_mod: Any, *, use_db: bool = True):
        self.w = watch_mod
        self.use_db = use_db
        self.hint = schema_hint(watch_mod.Item)
        self.classifier_on = os.getenv("CLASSIFIER_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        self.dedup_on = os.getenv("DEDUP_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
        self.lookback = int(os.getenv("DEDUP_LOOKBACK_DAYS", "45"))
        self.min_shared = int(os.getenv("DEDUP_MIN_SHARED_IMAGES", "2"))
        self.min_ratio = float(os.getenv("DEDUP_MIN_OVERLAP_RATIO", "0.6"))
        # In-memory dedup state, used when there's no DB (i.e. `watcher test`).
        self._seen_text: dict[str, str] = {}
        self._seen_images: list[tuple[str, list[str]]] = []

    # gate 0
    def prefilter(self, text: str) -> tuple[bool, str]:
        # NFKC first. Facebook sellers love mathematical-alphanumeric fonts —
        # "𝟲𝟱,𝟬𝟬𝟬 𝗧𝗛𝗕 𝗔𝘀𝗼𝗸𝗲" is a different string to "65,000 THB Asoke" and
        # would sail past every keyword you wrote. NFKC folds them to ASCII.
        lowered = unicodedata.normalize("NFKC", text or "").lower()
        rules = getattr(self.w, "PREFILTER", {}) or {}
        for word in rules.get("exclude", []):
            if word.lower() in lowered:
                return False, f"excluded by {word!r}"
        include = rules.get("include", [])
        if include and not any(word.lower() in lowered for word in include):
            return False, "no include keyword"
        return True, ""

    # dedup — before any LLM call, so reposts cost nothing
    def check_duplicate(self, post: dict[str, Any]) -> tuple[str | None, str, list[str]]:
        """Must run single-threaded: it's stateful, and two reposts in the same
        batch have to be able to see each other. `pre_gates` guarantees that.
        """
        thash = dedup.text_hash(post.get("raw_text")) or ""
        sigs = dedup.image_signatures(post.get("image_urls"))
        if not self.dedup_on:
            return None, thash, sigs

        # In-memory state covers the current batch; the DB covers earlier runs.
        # Both are needed — a batch containing two copies of the same listing
        # would otherwise slip through, since neither is written to the DB yet.
        if thash:
            prior = self._seen_text.get(thash)
            if not prior and self.use_db:
                prior = db.find_duplicate_by_text(thash, self.lookback)
            if prior and prior != post["post_url"]:
                return prior, thash, sigs

        if sigs:
            candidates = list(self._seen_images)
            if self.use_db:
                candidates += db.recent_image_sigs(self.lookback)
            prior = dedup.find_image_duplicate(
                sigs,
                [(u, s) for u, s in candidates if u != post["post_url"]],
                min_shared=self.min_shared,
                min_ratio=self.min_ratio,
            )
            if prior:
                return prior, thash, sigs

        if thash:
            self._seen_text.setdefault(thash, post["post_url"])
        if sigs:
            self._seen_images.append((post["post_url"], sigs))
        return None, thash, sigs

    def pre_gates(self, post: dict[str, Any]) -> dict[str, Any] | None:
        """The free gates. Returns a verdict if the post is dropped, else None
        and stashes the dedup signatures on the post for the LLM stage."""
        ok, reason = self.prefilter(post.get("raw_text") or "")
        if not ok:
            return {"stage": db.STAGE_KEYWORD, "reason": reason, "parsed": None, "matched": False}

        duplicate, thash, sigs = self.check_duplicate(post)
        post["_text_hash"], post["_image_sigs"] = thash, sigs
        if duplicate:
            return {"stage": db.STAGE_DEDUP, "reason": f"repost of {duplicate}",
                    "parsed": None, "matched": False, "text_hash": thash, "image_sigs": sigs}
        return None

    def llm_gates(self, post: dict[str, Any]) -> dict[str, Any]:
        """The paid gates. Safe to run in parallel.

        Raises QuotaExceeded straight through — that's a stop-the-run signal,
        not a per-post failure.
        """
        text = post.get("raw_text") or ""
        thash = post.get("_text_hash", "")
        sigs = post.get("_image_sigs", [])

        if self.classifier_on:
            criteria = getattr(self.w, "CLASSIFY_CRITERIA", "") or ""
            if criteria and not llm.classify(text, criteria):
                return {"stage": db.STAGE_CLASSIFY, "reason": "classifier said no",
                        "parsed": None, "matched": False, "text_hash": thash, "image_sigs": sigs}

        raw = llm.extract(text, self.w.PROMPT, self.hint)
        # Validate against watch.Item. A schema mismatch becomes a failed post,
        # never a silently wrong row.
        item = self.w.Item.model_validate(raw)

        matched, why = self.w.matches(item)
        return {
            "stage": db.STAGE_PARSED,
            "reason": why,
            "parsed": item.model_dump(),
            "matched": bool(matched),
            "item": item,
            "text_hash": thash,
            "image_sigs": sigs,
        }

    def process(self, post: dict[str, Any]) -> dict[str, Any]:
        """All four gates for a single post. Used by tests; the CLI goes
        through `_map` so dedup stays single-threaded."""
        return self.pre_gates(post) or self.llm_gates(post)


def _map(pipeline: Pipeline, posts: list[dict[str, Any]], on_result) -> None:
    """Run posts through the pipeline.

    The free gates run sequentially first — dedup is stateful, and running it
    in worker threads means two copies of the same listing in one batch never
    see each other and both get alerted.

    Only the LLM gates are parallelised, and only when the backend allows it.

    QuotaExceeded aborts everything — see the docstring on llm.QuotaExceeded.
    """
    survivors: list[dict[str, Any]] = []
    for post in posts:
        verdict = pipeline.pre_gates(post)
        if verdict is None:
            survivors.append(post)
        else:
            on_result(post, verdict)

    workers = llm.concurrency()

    def one(post: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        try:
            return post, pipeline.llm_gates(post)
        except llm.QuotaExceeded:
            raise
        except llm.ParseFailed as exc:
            return post, {"stage": db.STAGE_FAILED, "reason": str(exc)[:300],
                          "parsed": None, "matched": False}
        except Exception as exc:  # noqa: BLE001 - one bad post must not kill the run
            return post, {"stage": db.STAGE_FAILED, "reason": f"{type(exc).__name__}: {exc}"[:300],
                          "parsed": None, "matched": False}

    if workers == 1:
        for post in survivors:
            on_result(*one(post))
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for post, verdict in pool.map(one, survivors):
            on_result(post, verdict)


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_sample(args: argparse.Namespace) -> int:
    """Pull real posts so you (or your agent) can see what you're working with."""
    from src.scrapers import apify

    w = load_watch()
    groups = [args.group] if args.group else w.GROUPS
    print(f"Sampling {args.per_group} posts from {len(groups)} group(s)...")
    posts = apify.sample(groups, per_group=args.per_group)

    if not posts:
        # Don't clobber a good sample with an empty one, and don't exit 0 —
        # a caller (or an agent) needs to see that this failed.
        print("\nGot nothing. The errors above say why; the usual causes are an "
              "expired Apify actor rental, a group you haven't joined, or a bad token.")
        print("See TROUBLESHOOTING.md, or use Chrome DevTools MCP instead (AGENTS.md).")
        return 1

    SAMPLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE_PATH.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(posts)} posts to {SAMPLE_PATH}")
    print("Read them, then write watch.py to match what's actually in there.")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run watch.py against sampled posts. No DB writes. This is the loop you
    iterate in until the results look right."""
    if not SAMPLE_PATH.exists():
        print(f"No {SAMPLE_PATH}. Run `watcher sample` first, or drop a JSON list of "
              '{"post_url","group_id","raw_text","image_urls"} objects there.')
        return 1

    posts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[: args.limit]
    w = load_watch()
    pipeline = Pipeline(w, use_db=False)
    print(f"Running {len(posts)} posts through watch.py "
          f"(backend={llm.backend()}, classifier={'on' if pipeline.classifier_on else 'off'})\n")

    counts: dict[str, int] = {}
    matched_rows: list[tuple[dict, dict]] = []

    def show(post: dict[str, Any], verdict: dict[str, Any]) -> None:
        stage = verdict["stage"]
        counts[stage] = counts.get(stage, 0) + 1
        if stage == db.STAGE_PARSED and verdict["matched"]:
            counts["MATCH"] = counts.get("MATCH", 0) + 1
            matched_rows.append((post, verdict))
        snippet = " ".join((post.get("raw_text") or "").split())[:70]
        mark = "MATCH " if verdict["matched"] else "      "
        print(f"{mark}[{stage:8}] {snippet:<72} | {verdict['reason'][:60]}")

    try:
        _map(pipeline, posts, show)
    except llm.QuotaExceeded as exc:
        print(f"\nOut of credit / over limit: {exc}")
        return 1

    print("\n" + "-" * 100)
    print("where posts ended up: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    if args.show_matches and matched_rows:
        print("\nwhat you'd have been alerted about:\n")
        for post, verdict in matched_rows[:10]:
            print(w.notify_text(verdict["item"], post["post_url"], post.get("raw_text", "")))
            print("-" * 40)

    if not counts.get("MATCH"):
        print("\nNothing matched. Either your criteria are too tight, or the sample has "
              "nothing you want in it. Check the reasons above before loosening anything.")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Scrape, parse, match, alert. The real thing."""
    from src.scrapers import apify

    db.init()
    w = load_watch()
    worker = f"{socket.gethostname()}-{os.getpid()}"
    retention = int(os.getenv("DATA_RETENTION_DAYS", "90"))

    def next_interval() -> int:
        """Minutes until the next cycle.

        watch.py can define scrape_interval_minutes() to vary this by time of
        day — worth it when scraping costs money and the groups are asleep at
        4am. Everything else just uses the env var.
        """
        hook = getattr(w, "scrape_interval_minutes", None)
        if callable(hook):
            return max(1, int(hook()))
        return int(os.getenv("SCRAPE_INTERVAL_MINUTES", "30"))

    while True:
        started = datetime.now(timezone.utc)
        print(f"\n=== cycle {started:%Y-%m-%d %H:%M:%S} UTC ===")

        if not args.no_scrape:
            print("scraping:")
            summary = apify.scrape(w.GROUPS)
            print(f"  -> {summary['new']} new posts")

        pipeline = Pipeline(w, use_db=True)
        processed = 0
        alerts = 0
        quota_hit = False

        while True:
            batch = db.claim_pending(worker, limit=args.batch)
            if not batch:
                break

            def handle(post: dict[str, Any], verdict: dict[str, Any]) -> None:
                nonlocal processed, alerts
                processed += 1
                db.set_stage(
                    post["post_url"], verdict["stage"], verdict["reason"],
                    parsed=verdict.get("parsed"), matched=verdict.get("matched"),
                    text_hash=verdict.get("text_hash"), image_sigs=verdict.get("image_sigs"),
                )
                if not verdict.get("matched") or not verdict.get("item"):
                    return
                if db.already_notified(post["post_url"]):
                    return
                text = w.notify_text(verdict["item"], post["post_url"], post.get("raw_text", ""))
                if notify.enabled():
                    if notify.send(text):
                        db.record_notification(post["post_url"])
                        alerts += 1
                else:
                    # No Telegram configured — still record it so `watcher web`
                    # shows it and you don't get re-alerted after you set one up.
                    db.record_notification(post["post_url"])
                    alerts += 1
                print(f"  MATCH {verdict['reason']} -> {post['post_url']}")

            try:
                _map(pipeline, batch, handle)
            except llm.QuotaExceeded as exc:
                # Hand the claims back so these posts are retried after a top-up
                # rather than being lost.
                db.release_claims(worker)
                print(f"  out of credit / over limit: {exc}")
                print("  posts left pending; they'll be retried next run")
                quota_hit = True
                break

            if args.once and processed >= args.batch:
                break

        purged = db.purge(retention)
        stats = db.stats()
        print(f"processed={processed} alerts={alerts} purged={purged}")
        print("db: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))

        if args.once:
            return 1 if quota_hit else 0
        interval = next_interval()
        print(f"sleeping {interval}m")
        time.sleep(interval * 60)


def cmd_rematch(args: argparse.Namespace) -> int:
    """Re-apply matches() to already-parsed posts. Costs nothing.

    This is what makes gate 2 and gate 3 worth keeping separate: change your
    mind about price or size, and re-decide over your whole history for free.
    """
    db.init()
    w = load_watch()
    posts = db.parsed_posts(limit=args.limit)
    changed = 0
    now_matching = 0

    for post in posts:
        try:
            item = w.Item.model_validate(post["parsed"])
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {post['post_url']}: parsed data no longer fits Item ({exc})")
            continue
        matched, why = w.matches(item)
        if bool(matched) != bool(post.get("matched")):
            changed += 1
        if matched:
            now_matching += 1
        db.set_stage(post["post_url"], db.STAGE_PARSED, why, matched=bool(matched))

    print(f"re-checked {len(posts)} posts: {now_matching} now match ({changed} changed)")
    if changed and args.notify:
        print("note: --notify not implemented on purpose — re-alerting your whole "
              "history is almost never what you want. Use `watcher web` to review.")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    """Build a golden set: extract N sampled posts, save for hand-correction.

    Correct the values in data/golden.json by hand (or have your agent do it
    against the raw text), then use `watcher bench` to compare models against it.
    """
    if not SAMPLE_PATH.exists():
        print(f"No {SAMPLE_PATH}. Run `watcher sample` first.")
        return 1

    posts = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))[: args.limit]
    w = load_watch()
    pipeline = Pipeline(w, use_db=False)
    pipeline.dedup_on = False  # keep every post; a golden set wants variety

    rows: list[dict[str, Any]] = []

    def collect(post: dict[str, Any], verdict: dict[str, Any]) -> None:
        rows.append({
            "post_url": post["post_url"],
            "raw_text": post.get("raw_text", ""),
            "expected": verdict.get("parsed"),
            "stage": verdict["stage"],
        })
        print(f"  [{verdict['stage']}] {post['post_url']}")

    try:
        _map(pipeline, posts, collect)
    except llm.QuotaExceeded as exc:
        print(f"out of credit: {exc}")
        return 1

    GOLDEN_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(rows)} rows to {GOLDEN_PATH}")
    print("Now CORRECT the `expected` values by hand against `raw_text`. "
          "An uncorrected golden set only measures agreement with one model, not accuracy.")
    return 0


def _openrouter_prices() -> dict[str, tuple[float, float]]:
    """(prompt, completion) USD per token, live from OpenRouter. Empty on failure."""
    import urllib.request

    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as response:
            data = json.loads(response.read())
    except Exception:
        return {}
    out = {}
    for row in data.get("data", []):
        pricing = row.get("pricing") or {}
        try:
            out[row["id"]] = (float(pricing.get("prompt", 0)), float(pricing.get("completion", 0)))
        except (TypeError, ValueError):
            continue
    return out


def cmd_bench(args: argparse.Namespace) -> int:
    """Compare models on your golden set. The answer to 'is this model good enough'."""
    if not GOLDEN_PATH.exists():
        print(f"No {GOLDEN_PATH}. Run `watcher label` first, then correct it by hand.")
        return 1
    if llm.backend() != "openrouter":
        print(f"bench compares models, so it needs LLM_BACKEND=openrouter (you have {llm.backend()}).")
        return 1

    golden = [r for r in json.loads(GOLDEN_PATH.read_text(encoding="utf-8")) if r.get("expected")]
    if not golden:
        print("Golden set has no rows with `expected` filled in.")
        return 1

    w = load_watch()
    hint = schema_hint(w.Item)
    fields = list(w.Item.model_fields)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    prices = _openrouter_prices()
    original = os.environ.get("LLM_MODEL")
    results = []

    for model in models:
        os.environ["LLM_MODEL"] = model
        hits = total = failures = 0
        latencies: list[float] = []
        chars_in = chars_out = 0

        print(f"\n{model}")
        for row in golden:
            start = time.monotonic()
            try:
                raw = llm.extract(row["raw_text"], w.PROMPT, hint)
                got = w.Item.model_validate(raw).model_dump()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {type(exc).__name__}")
                continue
            latencies.append(time.monotonic() - start)
            chars_in += len(w.PROMPT) + len(hint) + len(row["raw_text"])
            chars_out += len(json.dumps(got))
            for field in fields:
                total += 1
                if got.get(field) == row["expected"].get(field):
                    hits += 1
            print(".", end="", flush=True)

        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0.0
        # ~4 chars per token is close enough to rank models by cost.
        prompt_price, completion_price = prices.get(model, (0.0, 0.0))
        per_post = (
            (chars_in / 4 * prompt_price + chars_out / 4 * completion_price) / max(1, len(latencies))
        )
        results.append({
            "model": model,
            "accuracy": hits / total if total else 0.0,
            "p50_s": p50,
            "failures": failures,
            "usd_per_1k": per_post * 1000,
        })

    if original is None:
        os.environ.pop("LLM_MODEL", None)
    else:
        os.environ["LLM_MODEL"] = original

    print(f"\n\n{'model':<40} {'field acc':>10} {'p50':>8} {'fails':>6} {'$/1k posts':>11}")
    print("-" * 80)
    for row in sorted(results, key=lambda r: -r["accuracy"]):
        cost = f"${row['usd_per_1k']:.3f}" if row["usd_per_1k"] else "?"
        print(f"{row['model']:<40} {row['accuracy']:>9.1%} {row['p50_s']:>7.1f}s "
              f"{row['failures']:>6} {cost:>11}")
    print("\nField accuracy counts exact matches against your corrected golden set, "
          "every field weighted equally. Check which fields a model gets wrong before "
          "picking on the number alone.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the setup. Run this before assuming something is broken."""
    ok = True

    def check(label: str, passed: bool, hint: str = "") -> None:
        nonlocal ok
        print(f"  {'ok  ' if passed else 'FAIL'}  {label}")
        if not passed:
            ok = False
            if hint:
                print(f"        {hint}")

    print("watch.py")
    try:
        w = load_watch()
        check("imports", True)
        check(f"{len(w.GROUPS)} group(s) configured", bool(w.GROUPS), "GROUPS is empty in watch.py")
        check(f"{len(w.Item.model_fields)} field(s) on Item", bool(w.Item.model_fields))
        required = [n for n, f in w.Item.model_fields.items() if f.is_required()]
        check("all Item fields optional", not required,
              f"required fields will fail on most posts: {required}")
        check("matches() returns (bool, str)",
              isinstance(w.matches(w.Item()), tuple) and len(w.matches(w.Item())) == 2)
    except Exception as exc:  # noqa: BLE001
        check(f"imports ({exc})", False)
        return 1

    print(f"\nLLM backend: {llm.backend()}")
    kind = llm.backend()
    if kind == "openrouter":
        check("OPENROUTER_API_KEY set", bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
              "get one at https://openrouter.ai/keys")
        check(f"model = {os.getenv('LLM_MODEL', 'deepseek/deepseek-v4-flash')}", True)
    else:
        import shutil

        binary = "claude" if kind == "claude-cli" else "codex"
        check(f"`{binary}` on PATH", shutil.which(binary) is not None,
              f"install it, or set LLM_BACKEND=openrouter")
    on = os.getenv("CLASSIFIER_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    print(f"        stage 1 classifier: {'on' if on else 'off'}")

    print("\nscraping")
    check("APIFY_API_TOKEN set", bool(os.getenv("APIFY_API_TOKEN", "").strip()),
          "get one at https://console.apify.com/account/integrations "
          "(or use Chrome DevTools MCP for sampling, see AGENTS.md)")

    print("\ntelegram")
    if notify.enabled():
        check("credentials set", True)
        if args.send_test:
            check("test message delivered", notify.send_test())
    else:
        print("        not configured — alerts off, browse with `watcher web`")

    print("\ndatabase")
    try:
        db.init()
        stats = db.stats()
        check(f"{db.db_path()} readable", True)
        print("        " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    except Exception as exc:  # noqa: BLE001
        check(f"database ({exc})", False)

    print("\n" + ("all good" if ok else "see failures above; TROUBLESHOOTING.md covers most of them"))
    return 0 if ok else 1


def cmd_web(args: argparse.Namespace) -> int:
    from src.web import serve

    db.init()
    serve(args.port)
    return 0


# ── entry ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watcher", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sample", help="pull real posts, no LLM, no DB")
    p.add_argument("--per-group", type=int, default=25)
    p.add_argument("--group", help="one group URL instead of all of them")
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("test", help="run watch.py over sampled posts")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--show-matches", action="store_true", help="print the alerts you'd get")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("run", help="scrape, parse, match, alert")
    p.add_argument("--once", action="store_true", help="one cycle then exit (use with cron)")
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--no-scrape", action="store_true", help="process the backlog, don't fetch")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("rematch", help="re-apply matches() to parsed posts (free)")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--notify", action="store_true")
    p.set_defaults(func=cmd_rematch)

    p = sub.add_parser("label", help="build a golden set for benchmarking")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("bench", help="compare models on the golden set")
    p.add_argument("--models", default="deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("doctor", help="check the setup")
    p.add_argument("--send-test", action="store_true", help="send a test Telegram message")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("web", help="browse matched posts")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_web)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
