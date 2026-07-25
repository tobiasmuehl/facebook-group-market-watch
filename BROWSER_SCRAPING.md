# Scraping without Apify

**Use Apify.** It's the default for a reason: it works unattended, it doesn't
get your account banned, and it costs less than your time. Everything below is
what to do if you can't or won't.

There are two non-Apify options and they are not equivalent.

| | Chrome DevTools MCP | Playwright (`scrapers/browser/`) |
|---|---|---|
| What it drives | **your** Chrome, already logged in | a separate headless browser + saved cookies |
| Detection risk | very low — it is genuinely you | moderate to high |
| Runs unattended | no | sort of |
| Setup | none | login, session file, breakage |
| Good for | a first look before you pay | a watcher you can't pay for |

Neither replaces Apify. Apify is a two-minute setup that runs unattended on any
machine; both options below need your own logged-in browser and your own
attention. Use them to see this working before you commit, then switch.

---

## Option 1 — Chrome DevTools MCP (for a first look only)

If your coding agent has the Chrome DevTools MCP server, it can drive your real
Chrome, with your real Facebook login, from your own home IP. Nothing looks
automated because nothing is being faked.

This is the best way to get posts for calibration **before you've paid for
anything**. Your agent opens each group, reads the feed, and writes
`data/sample.json`. Then you write `watch.py`, run `watcher test`, and only sign
up for Apify once you can see it finding the things you want.

Two hard limits:

- **Local laptop only.** From a VPS or datacenter IP, in headless mode,
  Facebook checkpoints you almost immediately. This is not a thing you can
  tune around.
- **Not a daemon.** It's tied to an active agent session, so it can't be your
  24/7 watcher. Use it to sample and calibrate; use Apify to run.

`data/sample.json` is a JSON list of objects with these keys:

```json
[
  {
    "post_url": "https://www.facebook.com/groups/123/posts/456/",
    "group_id": "123",
    "raw_text": "the full post text",
    "image_urls": ["https://scontent.xx.fbcdn.net/..."],
    "posted_at": null
  }
]
```

Anything that produces that shape works — `watcher test` doesn't care where it
came from.

---

## Option 2 — Playwright (`scrapers/browser/`)

```bash
uv sync --extra browser
uv run playwright install chromium

uv run python scrapers/browser/login.py --session secrets/fb_session.json
uv run python scrapers/browser/scrape.py --session secrets/fb_session.json
uv run python watcher.py run --once --no-scrape
```

`login.py` opens a real window and waits while you log in by hand, including
any checkpoint or captcha. `scrape.py` reuses that session, scrolls each group's
chronological feed, and writes posts into the same database Apify writes to.

### Read this before you rely on it

This is a trimmed-down version of a scraper that ran in production for months.
It worked, and it was never *reliable*. Specifically:

**You will hit checkpoints.** Facebook throws identity checks at automated
sessions. The checkpoint URL, the page layout, and the button labels all vary —
they're localized, they render late, and broad selectors click the wrong thing.
There is no selector that handles every variant. When it happens, run with
`--headful` and solve it yourself.

**Checkpoints escalate to captcha.** Sometimes clicking the right button just
gets you a captcha. At that point automation is over until a human intervenes.

**Behaviour is not deterministic.** The same code, same account, same group
works on Monday and gets checkpointed on Tuesday. Account age, cookie freshness,
and IP reputation all matter, and none of them are under your control.

**Sessions go stale.** When `scrape.py` starts landing on a login page, rerun
`login.py`. Expect this every couple of weeks.

**Use a burner account.** Not your real one. Facebook bans for automation, and
losing your actual account over an apartment search is a bad trade. Be aware
that automated scraping is against Facebook's terms of service; the risk is
yours.

**Don't run it from a VPS.** Datacenter IPs get checkpointed immediately. This
runs on your laptop or it doesn't run.

**Don't add a proxy pool.** The predecessor to this code had one, with per-
account health scoring and rotation across dozens of residential proxies. It
made things *worse* — more checkpoints, not fewer, and a lot more code to
debug. One account from one honest IP is the configuration that worked.

### When it breaks

The DOM extraction in `scrape.py` keys on two things: `[role="article"]` for post
containers, and `/groups/<id>/posts/` links for permalinks. Both have been stable
for years. Facebook's class names are generated garbage that changes weekly —
if you find yourself reaching for one, you're on the wrong path.

Run `--headful` and watch what actually happens. Nine times out of ten it's a
stale session or a checkpoint, not a selector.
