# Facebook Group Market Watch

Get told when the thing you're hunting shows up in a Facebook group.

In a lot of places, Facebook groups *are* the marketplace — apartments, cars,
tickets, furniture, equipment. The good stuff goes in minutes and there's no
search, no alerts, and no filters. This watches the groups for you and messages
you when something matches.

**Point your coding agent at this repo and say "set this up for me."** It reads
[`AGENTS.md`](AGENTS.md), asks you a handful of questions, looks at real posts
from your groups, writes the config, and shows you what you'd have been alerted
about before you commit to anything.

Works with Claude Code, Codex, Cursor, or any agent that can read a repo and run
commands.

---

## Doing it yourself

Dependencies are managed with [uv](https://docs.astral.sh/uv/). If you don't
have it: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```bash
uv sync                       # creates .venv from uv.lock
cp .env.example .env          # add your keys
$EDITOR watch.py              # what you're hunting

uv run python watcher.py sample            # get real posts from your groups
uv run python watcher.py test --show-matches   # see what would happen
uv run python watcher.py run                   # go
```

---

## How it works

Every post goes through four gates. Each one that drops a post records why, so
nothing disappears silently.

| | Gate | Cost / 1k posts | Catches |
|---|---|---|---|
| 0 | keyword prefilter | free | obvious noise |
| 1 | cheap LLM classifier *(off by default)* | ~$0.03 | semantic noise keywords can't |
| 2 | LLM extraction | ~$0.14 | pulls the fields out |
| 3 | `matches()` — plain Python | free | your actual criteria |

Gates 2 and 3 are separate on purpose. Extract generously, filter narrowly —
then changing your mind about price or size costs nothing:

```bash
uv run python watcher.py rematch   # re-decides your whole history, free
```

---

## You edit one file

`watch.py`. That's it — groups, keywords, the extraction prompt, the fields you
want, your criteria, and the message format. Everything else is plumbing.

```python
GROUPS = ["https://www.facebook.com/groups/..."]

PREFILTER = {"include": ["for rent", "condo"], "exclude": ["hiring"]}

PROMPT = """Extract apartment rental details..."""

class Item(BaseModel):
    price: float | None = None
    bedrooms: float | None = None
    area: str | None = None

def matches(item):
    if item.price and item.price > 60_000:
        return False, f"too expensive: {item.price:,.0f}"
    return True, f"{item.price:,.0f} in {item.area}"
```

The config format is Python because the thing configuring it is usually a
language model, and models write Python better than they fill in YAML schemas.

See [`examples/`](examples/) for cars, tickets, and furniture.

### Looking in Bangkok?

There's a ready-made config with **23 real Facebook property groups** — the set
this template was extracted from, after running in production for months:

```bash
cp examples/bangkok_apartments.py watch.py
```

Set `LOOKING_FOR = "rent"` or `"buy"`, your budget, and your sois. It handles
Thai/English mixed posts, Thai numerals (หมื่น/แสน/ล้าน), Sukhumvit soi rules,
BTS/MRT proximity, owner-vs-agent detection, and demand-post filtering. It scores
listings on points — location, quiet, light, balcony, kitchen, amenities, size,
owner — instead of a flat yes/no, and includes a Bangkok-time scrape schedule
that backs off overnight so Apify doesn't bill you to watch an empty feed.

**These groups produce thousands of posts a day.** Use
`LLM_MODEL=deepseek/deepseek-v4-flash` and set `CLASSIFIER_ENABLED=true`. A
frontier model on this volume costs real money for no gain.

**Start broad, then refine.** Generous budget, wide areas, let it run a day. When
you want it tighter, tell your coding agent — *"too many studios"*, *"only
Thonglor"*, *"add a bonus for high floors"* — and run `watcher rematch`, which
re-decides your whole history for free. Tightening costs nothing, so don't
agonise over thresholds up front.

14 of the 23 are **owner-only groups** where agents are banned — fewer posts,
better prices, no agent fee. Set `GROUPS = GROUPS_OWNER` for just those.

You have to join the groups on Facebook first; no scraper can see a private
group you're not in.

---

## Setup

### Scraping

**Apify** is the default and what you should use.
Get a token at <https://console.apify.com/account/integrations>.

Can't or won't? [`BROWSER_SCRAPING.md`](BROWSER_SCRAPING.md) covers the two
alternatives: driving your own Chrome through the Chrome DevTools MCP (great for
sampling, can't run unattended) and a Playwright scraper in
[`scrapers/browser/`](scrapers/browser/) (works, but not reliably — read the
warnings).

### LLM

Pick **one** backend. Never mix them.

```
LLM_BACKEND=openrouter   # ~$0.14/1k posts, needs a card
LLM_BACKEND=claude-cli   # your existing Claude Code login, no API key
LLM_BACKEND=codex-cli    # your existing Codex login, no API key
```

The CLI backends use a subscription you already pay for, so they're capped by
your usage limits — fine under roughly 100 posts/day. Over that, use OpenRouter.

### Alerts

Telegram, optionally. Create a bot with [@BotFather](https://t.me/botfather),
message it, then read your chat id from
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

Skip it and browse locally instead:

```bash
uv run python watcher.py web    # http://localhost:8000
```

---

## Commands

| | |
|---|---|
| `watcher sample` | pull real posts, no LLM, no database |
| `watcher test` | run `watch.py` over them, show where each post died |
| `watcher run [--once]` | scrape, parse, match, alert |
| `watcher rematch` | re-apply `matches()` to parsed posts, free |
| `watcher label` / `bench` | build a golden set, compare models on it |
| `watcher doctor` | check your setup before blaming the code |
| `watcher web` | browse what matched |

---

## Keeping it running

It's one process. Pick whichever you already use:

```bash
tmux new -s watcher 'uv run python watcher.py run'          # simplest
*/30 * * * * cd /path/to/repo && uv run python watcher.py run --once   # cron
```

For a laptop, a launchd or systemd user unit is tidier. For a VPS, anything that
restarts on failure. The per-group cooldown is stored in the database, so a
crash-loop can't burn your Apify credit.

---

## When it misbehaves

[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — symptom, cause, fix. Start with
`watcher doctor`.

---

## License

[AGPL-3.0](LICENSE). Use it, change it, run it for yourself — that's the point.
If you offer a modified version to others over a network, the AGPL requires you
to publish your source too.

---

## Notes

- Data stays on your machine. SQLite at `data/watcher.db`, nothing phones home.
- You need to be a member of a private group for any scraper to see it.
- Scraping Facebook is against their terms of service. That risk is yours. Don't
  use your main account with the browser scraper.
- Reposts are detected two ways — identical text, and overlapping photos —
  because in trade groups the same item gets posted daily, forever.
