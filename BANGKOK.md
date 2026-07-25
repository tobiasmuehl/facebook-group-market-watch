# Bangkok property — the preset

You're here because the user said yes to the Bangkok question in `AGENTS.md`.
Read this whole file before doing anything else. It pre-decides most of the
remaining setup, and several of its answers **override** the general advice in
`AGENTS.md`.

```bash
cp examples/bangkok_apartments.py watch.py
```

---

## Why you can trust this preset

This is not a guess at what Bangkok posts look like. The prompt, the classifier
criteria, the scoring weights and the dedup thresholds all come from a system
that ran continuously for months on these exact 23 groups and processed **over
100,000 listings**.

That means, concretely: the extraction prompt has already met the edge cases you
would otherwise discover one at a time — Thai and English mixed inside one
sentence, prices written as `35k` / `35,000` / `3 หมื่น` / `2.5 ล้าน`, soi
numbers on roads that aren't Sukhumvit, agents claiming to be owners, and spam
written in mathematical-alphanumeric Unicode specifically to defeat keyword
filters. The model choice was settled by benchmark, not preference: on 30
hand-labelled EN/TH listings DeepSeek beat GPT-5.2 on field accuracy (0.859 vs
0.826 F1) at roughly a seventh of the cost. That run is from February 2026 and
tested the previous model generation — re-run `watcher label` / `watcher bench`
if you want a current answer rather than a historical one.

**So don't rewrite the prompt.** Adjust the constants at the top of the file —
budget, sois, areas, `LOOKING_FOR`, `ALERT_THRESHOLD` — and calibrate at Step 5.
If extraction genuinely looks wrong on their posts, measure it with
`watcher label` / `watcher bench` before changing anything.

---

## What's already decided (do not re-ask)

| Step in AGENTS.md | Normally | Here |
|---|---|---|
| 2 — LLM backend | ask if they have Claude Code / Codex | **OpenRouter, always** |
| 2 — model | `deepseek/deepseek-v4-flash` | same, and don't upgrade without benchmarking |
| gate 1 classifier | off by default | **on** |
| 3 — sample real posts | required before writing the prompt | skip, the prompt is written |
| 4 — write `watch.py` | write from scratch | copy the preset, edit the constants |
| 5 — calibrate | required | **still required, do not skip** |

### Why OpenRouter and not a local CLI backend

These 23 groups produce **thousands of posts a day**. That is the single most
important fact about this preset.

`claude-cli` and `codex-cli` run one subprocess per post against a subscription
with usage limits. At Bangkok volume they will hit those limits within hours,
stall the watcher, and eat the user's Claude Code or Codex allowance that they
presumably want for actual work. Do not offer them here, even if the user has
Claude Code logged in and open in front of you.

```
LLM_BACKEND=openrouter
LLM_MODEL=deepseek/deepseek-v4-flash
CLASSIFIER_ENABLED=true
CLASSIFIER_MODEL=deepseek/deepseek-v4-flash
```

At ~$0.14 per 1000 posts this is a few dollars a month. A frontier model on the
same volume costs real money for no benefit — extraction is a formatting task,
not a reasoning one. If the user doubts it, don't argue, measure:

```bash
uv run python watcher.py label     # extracts 30 posts
# hand-correct data/golden.json against raw_text
uv run python watcher.py bench --models deepseek/deepseek-v4-flash,deepseek/deepseek-v4-pro
```

### Why the classifier is on here

Off by default elsewhere because at flash prices it only saves ~$0.11/1000
posts. At this volume it pays for itself, and its real value is semantic: these
groups are thick with agency ads, new-development campaigns, and people
*looking* rather than *offering* — all of which look correct to a keyword
filter. `CLASSIFY_CRITERIA` in the preset already covers these.

---

## The group package

23 real groups that ran in production. Offer three options:

- **Everything** — 23 groups, highest volume
- **Owner-only** — 14 groups where agents are banned, so the poster is the
  actual owner. Fewer posts, better prices, no agent fee. **Recommended
  default.** Set `GROUPS = GROUPS_OWNER`.
- **General only** — 9 high-volume agent groups. `GROUPS = GROUPS_GENERAL`

Then ask: **renting or buying?** → `LOOKING_FOR = "rent"` or `"buy"`.

**They must join the groups on Facebook first.** No scraper can see a private
group you're not a member of. Say this out loud — it is the most common reason
a first run returns nothing, and it is invisible from the error message.

### The buy/rent caveat

Bangkok groups mostly do **not** separate sale from rental — the two largest are
literally named "condo for sale AND rent". So `LOOKING_FOR` is not implemented by
picking groups; it filters on the extracted `is_rental` field. Tell the user
this, so they don't expect the group lists to do the work.

The split that *is* reliable is owner-only vs general.

---

## The schedule

The preset defines `scrape_interval_minutes()`, which the watcher calls instead
of the flat `SCRAPE_INTERVAL_MINUTES` env var. Bangkok time (UTC+7):

| Hours (ICT) | Interval | Why |
|---|---|---|
| 07–09 | 60 min | early listings trickle in |
| 09–18 | 15 min | office hours, most posts land here |
| 18–21 | 45 min | evening tail |
| 21–23 | 60 min | winding down |
| 23–07 | 150 min | nobody posts at 4am |

Apify bills per actor run and 23 groups is a lot of runs, so this is the
difference between a few dollars and a surprise. Leave it alone unless the user
asks. Deleting the function reverts to the flat env var.

---

## The scoring system

The preset scores rather than just filtering. Hard requirements reject outright;
survivors earn points:

- **location** 0–5 — a specific Sukhumvit soi beats a neighbourhood name, which
  beats a station. Station scoring is derived from the same area sets, so the
  same place can't score differently depending on which field the model filled in.
- **quality of life** — quiet, natural light, balcony, kitchen, view, no
  flooding, near a 7-11
- **amenities** — pool, gym, oven, dishwasher, parking, pets
- **size** — bonus above the minimum
- **owner** +1 — dealing direct is worth real money in agent fees

`ALERT_THRESHOLD` starts at **0** — alert on everything that passes the hard
requirements. Raise it only after the user has seen a few days of real output.

**The soi and area sets at the top of the file are EXAMPLE VALUES.** Replace them
with the sois the user actually cares about. Ask where they want to live and map
it onto `PREMIUM_SOIS` / `GOOD_SOIS` / `OK_SOIS` and the area sets.

One deliberate design point worth preserving if you edit `hard_requirements()`:
it rejects on **stated-and-wrong** values, never on **missing** ones. Most
Bangkok posts omit most fields; rejecting on absence throws away the majority of
real listings.

---

## Start broad, refine by talking

Say this to the user explicitly — it's the single most useful habit here:

> Set a generous budget and a wide area list today. Let it run. A day of real
> alerts teaches you more than an hour of guessing thresholds. When you want it
> tighter, just tell me — *"too many studios"*, *"only Thonglor now"*, *"add a
> bonus for high floors"* — and I'll edit `watch.py`.

Then:

```bash
uv run python watcher.py rematch
```

which re-applies `matches()` to every post already parsed, **for free, with no
new LLM calls**. Only changes to `PROMPT` or `Item` need re-extraction.

**Tightening criteria costs nothing.** That's why there's no reason to
over-think thresholds up front, and why you should push back if the user wants
to spend an hour tuning before seeing any data.

---

## What the preset already handles

Don't rebuild any of this:

- English, Thai, and posts that mix both mid-sentence
- Thai numerals — หมื่น (10k), แสน (100k), ล้าน (1M)
- Thai rent/sale cues — เช่า / ให้เช่า / ขาย
- Owner cues — เจ้าของ, เจ้าของปล่อยเอง (and an explicit instruction *not* to
  infer ownership from which group the post came from)
- Demand-post cues — หาห้อง, ต้องการเช่า, ใครมีห้อง, มองหา
- Areas in Thai script — ทองหล่อ, เอกมัย, พร้อมพงษ์, พระโขนง, อ่อนนุช
- Sukhumvit soi rules — only extract a soi number when it's explicitly tied to
  Sukhumvit, since soi numbers on other roads are unrelated
- BTS/MRT proximity, including metres→minutes conversion
- Mathematical-alphanumeric Unicode (`𝟲𝟱,𝟬𝟬𝟬 𝗔𝘀𝗼𝗸𝗲`), which property spam uses
  constantly — the watcher NFKC-normalizes before keyword matching

---

## Known ceiling

Shared/repost wrapper posts come back from the Apify actor with the wrapper's
text and an empty attachment list, so they extract poorly. Not fixable from
here. If the user asks why some posts have no details, this is why.

---

## Set the expectation before handing off

Bangkok groups are busy, but the watcher still only sees posts made **since it
started running** — there's no back-catalogue. Tell them to leave it running for
a few days before judging it. With the schedule above, a full day covers the
09:00–18:00 window where most listings land, so by day three they'll have a real
sample to tune against.

This matters more here than elsewhere: good Bangkok listings, especially in the
owner groups, are gone within hours. A watcher that only runs while their laptop
is awake misses most of the market. Help them set up launchd/systemd or a cheap
VPS at handoff rather than leaving a terminal open.

---

## Then continue

Go to **Step 5 (Calibrate)** in `AGENTS.md`. Do not skip it — the preset gives
you a working starting point, not a finished one. `watcher test --show-matches`
against their sois and budget is what turns it into theirs.
