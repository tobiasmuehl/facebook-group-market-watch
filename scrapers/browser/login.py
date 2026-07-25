#!/usr/bin/env python3
"""Log into Facebook by hand, once, and save the session.

    uv run python scrapers/browser/login.py --session secrets/fb_session.json

Opens a real browser window. You log in yourself — solve whatever checkpoint or
captcha Facebook shows you. Press Enter here when you're on your feed, and the
session is written to disk for scrape.py to reuse.

Read BROWSER_SCRAPING.md first. Use a burner account.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Facebook login, saves a Playwright session")
    parser.add_argument("--session", default="secrets/fb_session.json",
                        help="where to write the session (default: secrets/fb_session.json)")
    parser.add_argument("--start-url", default="https://www.facebook.com/")
    args = parser.parse_args(argv)

    session_path = Path(args.session)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("Facebook manual login")
    print(f"session file: {session_path}")
    print("-" * 70)
    print("1. Log in in the browser window that opens.")
    print("2. Solve any checkpoint or captcha yourself.")
    print("3. When you can see your normal feed, come back here and press Enter.")
    print("=" * 70)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        # Reuse an existing session if there is one, so a refresh doesn't mean
        # logging in from scratch.
        context = (
            browser.new_context(storage_state=str(session_path))
            if session_path.exists()
            else browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
        )
        page = context.new_page()
        page.goto(args.start_url, wait_until="domcontentloaded")

        input("\nPress Enter once you're logged in and looking at your feed... ")

        current = page.url
        if "login" in current or "checkpoint" in current:
            print(f"\nStill on {current}")
            print("That's not a logged-in session. Nothing was saved.")
            browser.close()
            return 1

        context.storage_state(path=str(session_path))
        print(f"\nSaved session to {session_path}")
        print("Sessions go stale. When scrape.py starts landing on a login page, run this again.")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
