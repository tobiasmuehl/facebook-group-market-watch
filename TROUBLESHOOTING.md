# Troubleshooting

Run `uv run python watcher.py doctor` first. It catches most of the setup
problems below before you start theorising.

Most of these are real failures from the production system this template was
extracted from, not hypotheticals.

---

## Nothing is happening

**You just set it up and there's almost nothing there.**
Expected. This only sees posts made *since it started running* — there is no
back-catalogue to import. Leave it running for a few days and it fills up. Don't
retune your filters on day-one data: a quiet first evening usually means the
groups were quiet. When you do change criteria, `watcher rematch` re-decides
everything already collected for free.

**It was finding things, then went quiet for hours.**
Check the process is still alive and that the machine didn't sleep. A laptop
that suspends overnight misses the whole overnight market, and in these groups
good listings go within hours. See "Keeping it running" in the README.

**No alerts at all, and `watcher run` says `processed=0`.**
No posts reached the queue. Run `watcher run --once` and look at the scraping
lines — if every group says `cooldown`, that's working as designed. Wait, or
lower `GROUP_COOLDOWN_MINUTES`.

**`watcher test` shows everything dying at `keyword`.**
`PREFILTER["include"]` is too tight. It's a substring match, so `"2BR"` won't
match `"2 BR"`. When in doubt, set `"include": []` and let the LLM decide.

**Everything dies at `parsed` with the same reason.**
That's `matches()` rejecting them, and the reason column tells you which check.
This is usually correct — read a few of the rejected posts before loosening it.

**Nothing matches, but you can see matching posts in the group by hand.**
Almost always the prefilter. Set `"include": []`, rerun `watcher test`, and see
if they come through. If they do, your keywords were wrong; if they don't, your
`PROMPT` isn't extracting the fields `matches()` depends on.

**Alerts stopped after working fine.**
Check `watcher run` output for `out of credit / over limit`. Posts are left
pending on purpose so they're retried after you top up — nothing was lost.

**Posts appear hours late.**
`SCRAPE_INTERVAL_MINUTES` plus `GROUP_COOLDOWN_MINUTES` is your real latency
floor. For time-sensitive things (tickets) set both to 10 and expect a bigger
Apify bill.

**You're missing posts entirely.**
Busy groups outrun `POSTS_PER_CYCLE`. The watcher already fetches a deeper batch
when every post it sees is new — if you see `all N new — fetching 50 deep` in the
logs regularly, raise `POSTS_PER_CYCLE` or shorten the interval.

---

## Results are wrong

**Fields are null on posts that clearly state them.**
Your `PROMPT` doesn't mention the field, or mentions it ambiguously. Add the
awkward cases explicitly: `"35k" means 35000`. Re-run `watcher test` — it's the
whole point of that command.

**`ValidationError` on nearly every post.**
A field on `Item` is required. Every field must be optional with a default —
models return null for anything a post doesn't mention:

```python
price: float | None = None    # correct
price: float                  # will fail on most posts
```

**A boolean is true when it shouldn't be.**
The model is inferring from context instead of evidence. Say so explicitly in
`PROMPT`: *"Set `is_owner` true ONLY if the post explicitly says the poster is
the owner. Do not infer this from the group name."* Inferring "it's in an
owners-only group so it must be from an owner" was a real, repeated bug.

**A field is wrong in a strange, specific way.**
Someone added regex extraction somewhere. Don't. Every field comes from the LLM;
if it's wrong, fix `PROMPT` or add a field to `Item`. Regex fallbacks caused
repeated regressions in the system this came from.

**Empty or nonsense rows in the database.**
Shouldn't be possible — parsing fails closed, and a failed post is stored as
`stage=failed` with the error, not as a half-parsed row. If you're seeing them,
something is writing to the DB outside the pipeline.

**Timestamps are hours off.**
Everything is stored in UTC. If you're displaying them elsewhere, convert there.

**A post came through with no text and no photos.**
It's a share/repost wrapper. The Apify actor returns the wrapper's text and an
empty attachment list for those, so there's nothing to extract. Known ceiling,
not fixable from here.

**Results were fine, then got worse for no reason.**
The model changed underneath you, or your groups changed. Benchmark instead of
guessing — see *Picking a model* below.

---

## Duplicates

**The same item alerts over and over.**
Agents repost the same listing daily under a new URL. Check `DEDUP_ENABLED=true`,
then widen: raise `DEDUP_LOOKBACK_DAYS`, lower `DEDUP_MIN_SHARED_IMAGES` to 1, or
lower `DEDUP_MIN_OVERLAP_RATIO` to 0.4.

**Two identical Telegram messages for one post.**
Two `watcher run` processes. Kill one. Claims are leased so this shouldn't
happen, but a 30-minute lease means a hung process can look free.

**Reposts with no photos aren't caught.**
Text dedup requires byte-identical text after normalization. Reposters who
retype the caption get through. Nothing cheap fixes this — the alternative is
fuzzy matching, which produces false negatives on genuinely different listings.

---

## Too expensive

**Apify bill higher than expected.**
Apify charges per actor run: `groups × (60 / SCRAPE_INTERVAL_MINUTES) × 24` runs
a day. Raise the interval, drop groups that never produce matches, or raise
`GROUP_COOLDOWN_MINUTES`.

**Apify bill spiked suddenly.**
Something restarted in a loop. The per-group cooldown lives in the database and
survives restarts specifically to prevent this — check `GROUP_COOLDOWN_MINUTES`
isn't 0.

**LLM bill higher than expected.**
Tighten `PREFILTER` (free), or turn on the classifier — `CLASSIFIER_ENABLED=true`
plus a good `CLASSIFY_CRITERIA` swaps a $0.14/1k extraction for a $0.03/1k yes/no
on most posts. Only worth it above ~500 posts/day.

**You want to change your criteria without paying again.**
You don't have to. `watcher rematch` re-runs `matches()` over everything you've
already parsed, for free. Only `PROMPT` and `Item` changes need re-extraction.

---

## Too slow

**Each cycle takes forever.**
Raise `LLM_CONCURRENCY` (openrouter only — CLI backends are serialized on
purpose; parallel subprocesses shred subscription limits).

**429 / rate limited.**
Lower `LLM_CONCURRENCY`.

**CLI backend is slow.**
It is — a subprocess per post. That's the tradeoff for not needing an API key.
Under ~100 posts/day it doesn't matter. Above that, switch to `openrouter`.

**Out of subscription limit on `claude-cli` / `codex-cli`.**
Switch `LLM_BACKEND=openrouter` wholesale. Don't move only one stage across —
running two backends at once doubles the ways this can break.

---

## Picking a model

When results are wrong, slow, or expensive and you've already tuned `PROMPT`,
measure instead of guessing:

```bash
uv run python watcher.py label    # extracts 30 sampled posts into data/golden.json
# Correct the `expected` values by hand against `raw_text`.
uv run python watcher.py bench --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro
```

You get field accuracy, p50 latency, and $/1k posts per model.

An uncorrected golden set measures agreement with one model, not accuracy — the
hand-correction is the entire value of this. Thirty posts is enough to separate a
model that works from one that doesn't.

---

## Setup errors

**`You must rent a paid Actor in order to run it after its free trial has expired`**
Your Apify token is fine — the *actor* needs renting. Open the link in the error
and rent it, pick a different actor (see the alternatives listed in
`src/scrapers/apify.py`), or skip Apify entirely and use Chrome DevTools MCP for
sampling (`BROWSER_SCRAPING.md`). This is the most common first-run failure.

**`watcher sample` wrote 0 posts**
Read the per-group errors above it. Usually the actor rental above, or you
aren't a member of the group. It exits non-zero and leaves any previous
`data/sample.json` untouched.

**`APIFY_API_TOKEN is not set`**
`.env` isn't being loaded. Run from the repo root. In Docker, the env var has to
be passed through to the container — a missing passthrough line was a real
production outage.

**`OPENROUTER_API_KEY is not set`**
Same, or you're on `LLM_BACKEND=openrouter` when you meant a CLI backend.

**`claude cli exit 1` / `codex cli exit 1`**
The CLI isn't logged in, or its flags changed. These aren't supported API
surfaces. Check `claude --help`, or switch to `LLM_BACKEND=openrouter`.

**`unknown LLM_BACKEND=...`**
Exactly one of `openrouter`, `claude-cli`, `codex-cli`.

**Telegram messages never arrive.**
`watcher doctor --send-test`. Usually the chat id — you have to message the bot
first, then read the id from `getUpdates`. Group chat ids are negative.

**`No module named pytest`**
`uv sync --group dev`, then `uv run python -m pytest`.

**Scraper lands on a login page** *(browser backend only)*
Session went stale. Rerun `scrapers/browser/login.py`. Expect this every couple
of weeks — see `BROWSER_SCRAPING.md`.

**Checkpoint or captcha** *(browser backend only)*
Run with `--headful` and solve it by hand. There is no reliable automated path
through these; that's why Apify is the default.
