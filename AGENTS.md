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
- Stage 1 classifier **off** (on for Bangkok or any high-volume watch)
- Keyword prefilter derived by you from the sampled posts
- Telegram if they want alerts on their phone, otherwise `watcher web` only

Tier A does **not** mean skipping Step 1 — the Bangkok question changes which
backend you're allowed to pick, so ask it regardless of tier.

---

## Step 1 — Is this about Bangkok property?

Ask this second, before anything else about what they're hunting:

> Are you looking for a place in **Bangkok** — to rent or to buy?

**If yes → open [`BANGKOK.md`](BANGKOK.md) and follow it now.** It's a complete
preset: 23 real groups that ran in production, a points-scoring system,
Thai/English extraction, and a cost-aware Bangkok-time schedule.

It also **pre-decides several answers below, and overrides them.** Most
importantly it forces `LLM_BACKEND=openrouter` — these groups produce thousands
of posts a day, which would shred a Claude Code or Codex subscription within
hours. So read `BANGKOK.md` *before* Step 2, not after. It tells you where to
rejoin this file.

**If no** → carry on with Step 1b below. Nothing in `BANGKOK.md` applies.

---

## Step 1b — What are they hunting?

Ask, in their words:

1. **What are you looking for?** (an apartment, a specific car, tickets, a sofa)
2. **Which Facebook groups?** URLs. They must already be a member of private ones.
3. **What makes one worth telling you about?** Price ceiling, size, condition,
   location, whatever — this becomes `matches()`.
4. **How do you want to be told?** Telegram, or just a local web page.

Don't over-interview. You'll learn more from the actual posts in step 3 than
from any amount of asking.

**Volume check — ask this now, it changes Step 2.** Roughly how busy are these
groups? If the answer is more than a few hundred posts a day, treat it like
Bangkok: OpenRouter only, and turn the classifier on.

### If they said Bangkok

You should already be in [`BANGKOK.md`](BANGKOK.md) — go back to Step 1. Every
Bangkok-specific decision lives there: the 23-group package, rent vs buy, the
forced OpenRouter backend, the classifier, the scrape schedule, the scoring
system, and the Thai-language handling that is already written for you.

---

## Step 2 — Pick ONE LLM backend

**Skip this if they said Bangkok** — `BANGKOK.md` already forced
`LLM_BACKEND=openrouter`. Don't reopen it.

**Skip the CLI options for any high-volume watch** (more than a few hundred
posts a day, from the volume check in Step 1b). Go straight to OpenRouter and
say why: a subscription-backed CLI runs one subprocess per post and will hit its
usage limits, stalling the watcher and burning the allowance they want for work.

Otherwise, ask one question:

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

**Skip this if they said Bangkok** — the preset's prompt is already written
against real posts from those groups. Go to Step 5.

This is the step that makes the difference. Do not write `watch.py` from
imagination — write it after reading what's actually in their groups.

**Use Apify. This is the default and you should steer them to it.** Have them
get a token (<https://console.apify.com/account/integrations>), put `GROUPS` in
`watch.py`, and run:

```bash
uv run python watcher.py sample
```

It's a two-minute setup, it works the same on their laptop and on a server, and
it's the only option that can run unattended afterwards — which is the entire
point of this tool. It costs a little money. Say so plainly and move on; don't
talk them into a free option that costs them an evening.

**Chrome DevTools MCP is the fallback, not the first choice.** Only reach for it
when they don't want to create an Apify account yet, or they want to see this
working before paying for anything. It drives their real Chrome with their real
Facebook login, so you open each group, read ~25 recent posts, and write
`data/sample.json` yourself:

```json
[{"post_url": "...", "group_id": "...", "raw_text": "...", "image_urls": [], "posted_at": null}]
```

Its limits are real: local laptop only (a VPS gets checkpointed immediately),
it's slower because you're driving a browser by hand, and **it cannot run
unattended** — so they'd still need Apify for Step 6. If you use it for
sampling, tell them that up front.

Either way: **read `data/sample.json` properly before continuing.** You are
looking for what language posts are in, how prices are written, what the spam
looks like, and which fields actually exist. A prompt written against real posts
beats a prompt written from a description every single time.

---

## Step 4 — Write `watch.py`

**Skip this if they said Bangkok** — copy the preset per `BANGKOK.md` and edit
its constants instead. Go to Step 5.

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

### Set the expectation before you hand off

**Say this explicitly. It is the difference between someone keeping this running
and deleting it on day one.**

> This only sees posts made **since it started running**. There's no back-catalogue
> to pull in — each cycle fetches the newest posts and stops. So the first run
> will look sparse, and that's normal, not broken.
>
> Leave it running in the background for **a few days**. The database fills up as
> new listings appear, and it gets genuinely useful once there's a few days of
> history in it. The first day is the worst day this will ever be.

Two consequences worth spelling out for them:

- **Don't judge the filters on day one.** A quiet first evening means the groups
  were quiet, not that `matches()` is wrong. Wait for a real sample before
  tightening or loosening anything — and when they do want changes,
  `watcher rematch` re-decides the whole accumulated history for free.
- **Uptime is the feature.** A watcher that runs 2 hours a day misses 90% of the
  market, and in these groups the good listings go within hours. If they're on a
  laptop that sleeps, say so now and help them pick launchd/systemd or a cheap
  VPS rather than leaving a terminal open.

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
