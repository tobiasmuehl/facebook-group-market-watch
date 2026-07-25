#!/usr/bin/env python3
"""Scrape Facebook groups with a real browser instead of Apify.

    uv run python scrapers/browser/scrape.py --session secrets/fb_session.json
    uv run python scrapers/browser/scrape.py --session ... --sample   # write data/sample.json

This is the fallback. It is not as reliable as Apify and it never will be —
read BROWSER_SCRAPING.md before you depend on it. One account, one browser, all
your groups, from your own IP. No proxy pool: proxies made things worse, not
better, in every version of this that came before.

Writes into the same database as the Apify path, so `watcher run --no-scrape`
picks the posts up and processes them normally.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from src import db  # noqa: E402
from src.scrapers.apify import group_id_from_url  # noqa: E402

# Pulls posts out of the group feed. Facebook's class names are generated and
# change constantly; role="article" and the /groups/<id>/posts/ permalink shape
# have been stable for years. Don't get cleverer than this.
EXTRACT_JS = r"""
() => {
  const posts = [];
  const seen = new Set();

  for (const article of document.querySelectorAll('[role="article"]')) {
    const links = Array.from(article.querySelectorAll('a[href]'));
    const permalink = links.find((a) => /\/groups\/[^/]+\/(posts|permalink)\//.test(a.href || ""));
    if (!permalink) continue;

    const url = (permalink.href || "").split("?")[0];
    if (!url || seen.has(url)) continue;
    seen.add(url);

    // The longest dir="auto" block is the post body; the rest is chrome.
    let text = "";
    for (const el of article.querySelectorAll('div[data-ad-preview="message"], div[dir="auto"]')) {
      const candidate = (el.innerText || "").trim();
      if (candidate.length > text.length) text = candidate;
    }
    if (!text) text = (article.innerText || "").trim();
    text = text.replace(/\n(?:Like|Comment|Share)\b[\s\S]*$/i, "").trim();

    const images = [];
    for (const img of article.querySelectorAll("img[src^='http']")) {
      const box = img.getBoundingClientRect();
      // Anything small is an avatar or a reaction icon, not listing media.
      if (box.width < 80 || box.height < 80) continue;
      if (!(img.src || "").includes("fbcdn")) continue;
      if (!images.includes(img.src)) images.push(img.src);
    }

    posts.push({ post_url: url, raw_text: text, image_urls: images });
  }
  return posts;
}
"""

# Truncated posts hide exactly the details you want. Click every "See more".
EXPAND_JS = r"""
() => {
  let clicked = 0;
  const labels = ["see more", "voir plus", "mehr anzeigen", "ver mas", "ver mais", "altro"];
  for (const el of document.querySelectorAll('[role="button"], span, div')) {
    const text = (el.innerText || el.textContent || "").trim().toLowerCase();
    if (text.length > 20) continue;
    if (!labels.some((l) => text === l || text === l + "...")) continue;
    try { el.click(); clicked++; } catch (e) { /* detached node */ }
  }
  return clicked;
}
"""


def scrape_group(page: Any, group_url: str, max_scrolls: int, wait_ms: int) -> list[dict[str, Any]]:
    group_id = group_id_from_url(group_url) or group_url
    # ?sorting_setting=CHRONOLOGICAL gives newest first; the default feed is
    # ranked and will bury new posts under whatever Facebook thinks you like.
    page.goto(f"https://www.facebook.com/groups/{group_id}/?sorting_setting=CHRONOLOGICAL",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)

    if "login" in page.url or "checkpoint" in page.url:
        raise RuntimeError(f"not logged in (landed on {page.url}) — rerun login.py")

    collected: dict[str, dict[str, Any]] = {}
    last_height = 0

    for scroll in range(max_scrolls):
        try:
            page.evaluate(EXPAND_JS)
        except Exception:
            pass  # expansion is best-effort; truncated text still parses

        for post in page.evaluate(EXTRACT_JS):
            post["group_id"] = group_id
            post["posted_at"] = None
            collected.setdefault(post["post_url"], post)

        page.mouse.wheel(0, random.randint(1500, 2500))
        page.wait_for_timeout(wait_ms + random.randint(0, 800))

        height = page.evaluate("document.body.scrollHeight")
        if height == last_height and scroll > 2:
            break  # feed stopped growing
        last_height = height

    return list(collected.values())


def main(argv: list[str] | None = None) -> int:
    import json

    import watch

    parser = argparse.ArgumentParser(description="Browser-based Facebook group scraper")
    parser.add_argument("--session", default="secrets/fb_session.json")
    parser.add_argument("--max-scrolls", type=int, default=8)
    parser.add_argument("--wait-ms", type=int, default=1_500)
    parser.add_argument("--headful", action="store_true",
                        help="show the browser — do this when something breaks")
    parser.add_argument("--sample", action="store_true",
                        help="write data/sample.json instead of the database")
    args = parser.parse_args(argv)

    session_path = Path(args.session)
    if not session_path.exists():
        print(f"No session at {session_path}. Run:")
        print(f"  uv run python scrapers/browser/login.py --session {session_path}")
        return 1

    db.init()
    all_posts: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headful)
        context = browser.new_context(storage_state=str(session_path),
                                      locale="en-US", viewport={"width": 1280, "height": 900})
        page = context.new_page()

        for group_url in watch.GROUPS:
            try:
                posts = scrape_group(page, group_url, args.max_scrolls, args.wait_ms)
                all_posts.extend(posts)
                print(f"  {group_url}: {len(posts)} posts")
            except Exception as exc:
                print(f"  {group_url}: ERROR {exc}")
            # Don't hammer. A human doesn't open six groups in six seconds.
            time.sleep(random.uniform(5, 15))

        # Save the refreshed session so the cookies stay warm.
        context.storage_state(path=str(session_path))
        browser.close()

    if args.sample:
        out = Path("data/sample.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_posts, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote {len(all_posts)} posts to {out}")
    else:
        inserted = db.insert_posts(all_posts)
        print(f"\n{len(all_posts)} scraped, {inserted} new")
        print("Now run: uv run python watcher.py run --once --no-scrape")
    return 0


if __name__ == "__main__":
    sys.exit(main())
