"""LLM calls. One backend for every stage — never mix.

    LLM_BACKEND=openrouter   pay per token
    LLM_BACKEND=claude-cli   your existing `claude` login
    LLM_BACKEND=codex-cli    your existing `codex` login

Two entry points: classify() -> bool, extract() -> dict.

Fail-closed on purpose. If the model errors or returns unparseable output we
raise instead of storing a half-parsed row — garbage in the DB is much harder
to notice than a failed post sitting in the retry queue.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_ATTEMPTS = 2

# Substrings that mean "you are out of budget/limit", not "this post is weird".
# On these we stop the whole run and hand claims back, so posts get retried
# after you top up instead of being marked failed and forgotten.
_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient credit",
    "quota exceeded",
    "rate limit reached",
    "usage limit",
    "credit balance",
    "402",
    "429",
)


class QuotaExceeded(Exception):
    """Out of credit or over a subscription limit. Stop the run, keep posts pending."""


class ParseFailed(Exception):
    """Model call or JSON parse failed for this post. Retryable."""


def backend() -> str:
    return os.getenv("LLM_BACKEND", "openrouter").strip().lower()


def concurrency() -> int:
    """CLI backends are serialized — parallel subprocesses shred subscription limits."""
    if backend() != "openrouter":
        return 1
    return max(1, int(os.getenv("LLM_CONCURRENCY", "4")))


def _is_quota_error(exc: Exception) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(marker in blob for marker in _QUOTA_MARKERS)


# ── JSON out of messy model output ───────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in fences, prose, or both, even when told not to.
    """
    if not text or not text.strip():
        raise ParseFailed("empty model response")

    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)]
    candidates.append(text)

    for candidate in candidates:
        start = candidate.find("{")
        if start < 0:
            continue
        # Walk to the matching close brace so trailing prose doesn't break json.loads.
        depth, in_string, escaped = 0, False, False
        for i, ch in enumerate(candidate[start:], start):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
    raise ParseFailed(f"no JSON object in response: {text[:200]!r}")


# ── backends ─────────────────────────────────────────────────────────────────

def _openrouter(system: str, user: str, model: str) -> str:
    from openai import OpenAI

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set (see .env)")
    client = OpenAI(api_key=key, base_url="https://openrouter.ai/api/v1", timeout=120, max_retries=0)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0,
        # No reasoning: extraction is a formatting task and reasoning tokens
        # are billed as output. Raise it only if `watcher bench` says it helps.
        extra_body={"reasoning": {"enabled": False}},
    )
    return resp.choices[0].message.content or ""


def _claude_cli(system: str, user: str, model: str) -> str:
    """Shell out to the local `claude` CLI, using its existing login.

    Not a supported API surface — flags drift between releases. If this breaks,
    set LLM_BACKEND=openrouter. Verified against the flags in `claude --help`.
    """
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system,
        "--strict-mcp-config",  # don't load the user's MCP servers; slow, unneeded
    ]
    proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"claude cli exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    # --output-format json returns an envelope; the model text is in `result`.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    if isinstance(envelope, dict):
        if envelope.get("is_error"):
            raise RuntimeError(f"claude cli error: {str(envelope.get('result'))[:300]}")
        return str(envelope.get("result", ""))
    return proc.stdout


def _codex_cli(system: str, user: str, model: str) -> str:
    """Shell out to the local `codex` CLI, using its existing login.

    Same caveat as claude-cli: unsupported surface, flags drift.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_file = Path(tmp) / "last.txt"
        cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check",
               "--ephemeral", "-o", str(out_file)]
        if model:
            cmd += ["-m", model]
        proc = subprocess.run(
            cmd, input=f"{system}\n\n---\n\n{user}",
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"codex cli exit {proc.returncode}: {proc.stderr.strip()[:300]}")
        if out_file.exists():
            return out_file.read_text()
        return proc.stdout


def _call(system: str, user: str, *, classifier: bool) -> str:
    kind = backend()
    if kind == "openrouter":
        model = os.getenv("CLASSIFIER_MODEL" if classifier else "LLM_MODEL", "") or "deepseek/deepseek-v4-flash"
        return _openrouter(system, user, model)
    if kind == "claude-cli":
        model = os.getenv("CLAUDE_CLASSIFIER_MODEL", "haiku") if classifier else os.getenv("CLAUDE_MODEL", "sonnet")
        return _claude_cli(system, user, model)
    if kind == "codex-cli":
        model = os.getenv("CODEX_CLASSIFIER_MODEL", "") if classifier else os.getenv("CODEX_MODEL", "")
        return _codex_cli(system, user, model)
    raise RuntimeError(f"unknown LLM_BACKEND={kind!r} (openrouter | claude-cli | codex-cli)")


def _call_with_retry(system: str, user: str, *, classifier: bool) -> str:
    last: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return _call(system, user, classifier=classifier)
        except Exception as exc:
            if _is_quota_error(exc):
                raise QuotaExceeded(str(exc)) from exc
            last = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)
    raise ParseFailed(f"all {MAX_ATTEMPTS} attempts failed: {last}")


# ── public API ───────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """You are a fast binary classifier for Facebook group posts.
Answer ONLY with JSON: {"relevant": true} or {"relevant": false}.
When genuinely unsure, answer true — a later, more careful step will look again."""


def classify(post_text: str, criteria: str) -> bool:
    """Gate 1. Cheap yes/no on whether a post is worth a full extraction."""
    system = f"{_CLASSIFY_SYSTEM}\n\nRelevant means: {criteria}"
    raw = _call_with_retry(system, post_text, classifier=True)
    return bool(extract_json(raw).get("relevant", True))


def extract(post_text: str, prompt: str, schema_hint: str) -> dict[str, Any]:
    """Gate 2. Full extraction into your watch.Item fields.

    Returns a raw dict; the caller validates it against watch.Item so a schema
    mismatch surfaces as a failed post rather than a silently wrong row.
    """
    system = (
        f"{prompt}\n\n"
        f"Return ONLY a JSON object with exactly these fields:\n{schema_hint}\n\n"
        "Use null for anything the post does not state. Never guess. "
        "Never invent a value to fill a field."
    )
    return extract_json(_call_with_retry(system, post_text, classifier=False))
