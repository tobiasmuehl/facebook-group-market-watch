# Onboarding script — read this first

You are a coding agent. Someone cloned this template because they want to be
told when a specific thing shows up in specific Facebook groups. Your job is to
configure it for them and prove it works before they spend any money.

**You edit exactly one file: `watch.py`.** Everything else is plumbing. If you
find yourself editing `src/`, stop — you've misread the design.

**Use `uv` for everything Python.** `uv sync` to install, `uv run python ...` to
run, `uv add <pkg>` if you genuinely need a new dependency. Never `pip install`,
never `python -m venv`, never activate the venv by hand — `uv run` handles it.
Dependencies live in `pyproject.toml` and are locked in `uv.lock`; both are
committed and both should stay that way.

Follow the steps in order. Don't skip step 0.

---

## Step 0 — Ask how much explanation they want

Ask this first, before anything else:

> How do you want to do this?
>
> **A. Just set it up.** I pick sensible defaults and only ask what I can't
>    figure out. You'll see the result, not the decisions.
>
> **B. Explain as we go.** I'll tell you what each choice does in a sentence
>    or two and give you a recommendation you can override. *(most people)*
>
> **C. Show me the knobs.** Full breakdown of the pipeline, costs, and
>    tradeoffs at each step.

Their answer sets your verbosity for the **whole session**, including later
debugging. In A, fix problems quietly and report the outcome. In C, explain what
broke and why.

**Tier A defaults — apply these without asking:**

- Apify for scraping
- `deepseek/deepseek-v4-flash` for extraction
- Stage 1 classifier **off**
- Keyword prefilter derived by you from the sampled posts
- Telegram if they want alerts on their phone, otherwise `watcher web` only

---

## Step 1 — What are they hunting?

Ask, in their words:

1. **What are you looking for?** (an apartment, a specific car, tickets, a sofa)
2. **Which Facebook groups?** URLs. They must already be a member of private ones.
3. **What makes one worth telling you about?** Price ceiling, size, condition,
   location, whatever — this becomes `matches()`.
4. **How do you want to be told?** Telegram, or just a local web page.

Don't over-interview. You'll learn more from the actual posts in step 3 than
from any amount of asking.

### Offer the Bangkok preset

If they mention **Bangkok**, or an apartment/condo anywhere in Thailand, offer
this before asking for group URLs:

> I have a ready-made set of 23 Bangkok property groups that were used in
> production for exactly this. Want to start from those?
>
> - **Everything** — 23 groups, highest volume
> - **Owner-only** — 14 groups where agents are banned, so you deal with the
>   owner directly. Fewer posts, better prices, no agent fee. *(good default)*
> - **General only** — 9 high-volume agent groups
>
> And are you **renting** or **buying**?

Then:

```bash
cp examples/bangkok_apartments.py watch.py
```

Set `LOOKING_FOR` to `"rent"` or `"buy"`, `GROUPS` to `GROUPS_OWNER`,
`GROUPS_GENERAL`, or the full default, and adjust `MAX_RENT_THB` /
`MAX_BUY_THB`, `MIN_SIZE_SQM`, and the soi/area sets to what they told you.

**Tell them these four things. They are not optional.**

1. **These groups produce thousands of posts a day.** That's the whole reason
   the preset is configured the way it is.

2. **Use DeepSeek.** Set `LLM_MODEL=deepseek/deepseek-v4-flash`. At this volume
   a frontier model costs real money for no benefit — extraction is a
   formatting task. If they doubt it, `watcher label && watcher bench` settles
   it with numbers instead of opinion.

3. **Turn the classifier on** — `CLASSIFIER_ENABLED=true`. This is the one
   preset where it's clearly worth it. Above ~500 posts/day the cheap yes/no
   gate pays for itself, and these groups are thick with posts that look right
   to a keyword filter and aren't.

4. **Start broad, refine later.** Set a generous budget and a wide area list on
   day one. A day of real alerts teaches more than an hour of guessing
   thresholds. When they want it tighter they just tell you — *"too many
   studios"*, *"only Thonglor now"*, *"bonus for high floors"* — you edit
   `watch.py`, then run `watcher rematch`, which re-decides their whole history
   for free. Say this out loud: **tightening criteria costs nothing**, so
   there's no reason to over-think them up front.

The preset already includes a Bangkok-time scrape schedule
(`scrape_interval_minutes()`): every 15 min during office hours, backing off to
150 min overnight. Apify bills per run, and nobody posts at 4am.

Honest caveat to pass on: Bangkok groups mostly don't separate sale from
rental — the two biggest are named "condo for sale **and** rent" — so the real
filtering happens on the extracted `is_rental` field, not by picking groups.

They still need to **join those groups on Facebook** before anything can scrape
them. Say so explicitly; it's the most common reason a first run returns nothing.

You can skip the step 3–4 sampling and prompt work with this preset — the prompt
is already written for Thai/English mixed posts, Thai numerals (หมื่น/แสน/ล้าน),
Sukhumvit soi rules, and BTS proximity. **Do not skip step 5.**

### The scoring system

This preset scores rather than just filters: hard requirements reject outright,
then surviving listings earn points for location, quality of life, amenities,
size, and dealing with an owner directly. `ALERT_THRESHOLD` starts at 0 — alert
on everything that qualifies — and they raise it once they've seen a few days of
real output.

When they want different priorities, edit the weights in `score()`, not the
plumbing. The soi and area sets at the top of the file are **example values** —
replace them with the sois the user actually cares about.

---

## Step 2 — Pick ONE LLM backend

Ask one question:

> Do you already have Claude Code or Codex logged in on this machine?

- **Yes** → `LLM_BACKEND=claude-cli` (or `codex-cli`). No API key. Capped by
  their subscription limits, so it suits under roughly 100 posts/day.
- **No** → `LLM_BACKEND=openrouter`. Costs about **$0.14 per 1000 posts** with
  `deepseek/deepseek-v4-flash`. Needs a card.

**Never mix backends.** Not one backend for classification and another for
extraction, not a fallback chain. Two auth systems and two rate-limit regimes is
the fastest way to make this thing unmaintainable. If a CLI backend runs into
subscription limits, switch `LLM_BACKEND` wholesale to `openrouter`.

Write `.env` from `.env.example`.

---

## Step 3 — Get real posts BEFORE writing anything

This is the step that makes the difference. Do not write `watch.py` from
imagination — write it after reading what's actually in their groups.

**If you have the Chrome DevTools MCP server, use it.** It drives their real
Chrome with their real Facebook login. No Apify token needed, so they can see
this working before paying for anything. Open each group, read ~25 recent posts,
and write `data/sample.json`:

```json
[{"post_url": "...", "group_id": "...", "raw_text": "...", "image_urls": [], "posted_at": null}]
```

Local laptop only — from a VPS this gets checkpointed immediately.

**Otherwise**, have them get an Apify token
(<https://console.apify.com/account/integrations>), put `GROUPS` in `watch.py`,
and run:

```bash
uv run python watcher.py sample
```

Either way: **read `data/sample.json` properly before continuing.** You are
looking for what language posts are in, how prices are written, what the spam
looks like, and which fields actually exist. A prompt written against real posts
beats a prompt written from a description every single time.

---

## Step 4 — Write `watch.py`

Six things, all in that one file:

| | What it does | Cost/1k posts |
|---|---|---|
| `GROUPS` | which groups | — |
| `PREFILTER` | gate 0 — substring include/exclude | free |
| `CLASSIFY_CRITERIA` | gate 1 — cheap yes/no LLM (off by default) | ~$0.03 |
| `PROMPT` + `Item` | gate 2 — pull the fields out | ~$0.14 |
| `matches()` | gate 3 — their actual criteria | free |
| `notify_text()` | the Telegram message | — |

Pick the closest file in `examples/` (`used_cars.py`, `concert_tickets.py`,
`furniture.py`) and adapt it. The default `watch.py` is an apartment example.

Rules that matter:

- **Every `Item` field must be optional with a default.** Models return null for
  anything a post doesn't mention. A required field fails on almost every post.
- **Keep `PREFILTER["include"]` loose.** A post dropped there is never seen
  again. It's a cost saver, not the real filter. When unsure, leave it empty.
- **Extract generously, filter narrowly.** Put everything interesting in `Item`;
  do the judging in `matches()`. That's what makes `watcher rematch` free.
- **`matches()` returns `(bool, reason)`.** The reason is stored either way, so
  rejected posts explain themselves.
- **Never add regex field extraction anywhere.** If a field comes out wrong,
  fix `PROMPT` or add a field to `Item`. Regex fallbacks caused repeated
  regressions in the codebase this template came from. Don't reintroduce them.
- **Don't let the prompt infer from group membership.** "It's in an owners-only
  group, so it's from an owner" is exactly the reasoning that produced bad data
  before. Require explicit evidence in the post text.

---

## Step 5 — Calibrate (this is the loop)

```bash
uv run python watcher.py test --show-matches
```

No database writes, costs a few cents. You get one line per post showing which
gate it died at and why, then the alerts they'd have received.

Show the user that table. Then iterate:

- Wanted posts dying at `keyword` → loosen `PREFILTER["include"]`
- Wanted posts dying at `parsed` with a bad reason → fix `matches()`
- Fields coming out wrong or null → fix `PROMPT`, add examples of the hard cases
- Everything matching → `matches()` is too loose, they'll get spammed
- Nothing matching → read the reasons before loosening anything

Repeat until the user agrees with the output. **Do not move on early.** This
loop is cheap; a badly-tuned watcher that alerts on everything gets muted and
abandoned within a day.

Then check the setup end to end:

```bash
uv run python watcher.py doctor --send-test
```

---

## Step 6 — Turn it on

```bash
uv run python watcher.py run --once     # one full cycle, real alerts
uv run python watcher.py run            # continuous
```

Tell them how to keep it running (tmux, launchd, systemd, or `--once` on a
cron — see README) and how to browse results (`watcher web`).

Tell them the two commands they'll actually use later:

- `watcher rematch` — changed your mind about criteria? Re-decides your whole
  history for free. No new LLM calls.
- `watcher test` — always run this after editing `watch.py`.

---

## When something breaks

**Read `TROUBLESHOOTING.md` first.** It's symptom → cause → fix, built from real
bugs in the production system this came from. Run `watcher doctor` before
theorising.

The one thing not in there: if the user says results are wrong, slow, or too
expensive and you've already tuned the prompt, benchmark instead of guessing:

```bash
uv run python watcher.py label      # extract 30 sampled posts
# hand-correct data/golden.json against the raw text — an uncorrected golden
# set only measures agreement with one model, not accuracy
uv run python watcher.py bench --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro
```

---

## Turning on the classifier (gate 1)

Off by default, and usually should stay off. Turn it on when:

- they're seeing **more than ~500 posts/day**, or
- extraction uses an expensive or vision model (the gap becomes 5–20x)

At flash prices it saves about $0.11 per 1000 posts, which is nothing. Its real
value is semantic: "looking for a 2BR in Thonglor" and "2BR in Thonglor for
rent" are nearly identical to a keyword filter and obviously different to a
model. Set `CLASSIFIER_ENABLED=true` and write `CLASSIFY_CRITERIA`.

## Photos

DeepSeek is text-only. Images are always scraped, stored, and shown in Telegram
and the web UI regardless. Only if the user needs the model to **judge** photos
(furniture condition, car damage) do they need a vision model in `LLM_MODEL` —
and at that point the classifier earns its keep.
