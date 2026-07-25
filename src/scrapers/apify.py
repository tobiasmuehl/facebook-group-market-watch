"""Apify scraper — the default backend.

Everything version-fragile lives in ACTOR and build_input() below. When Apify
actors change their input schema or get deprecated (they do), that is the only
place you edit.

Known ceiling: for shared/reposted wrapper posts this actor returns the wrapper
text with empty attachments, so those posts extract poorly. Not fixable here.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from src import db
from src.dedup import extract_image_urls

# ── the fragile part ─────────────────────────────────────────────────────────

ACTOR = "curious_coder/facebook-post-scraper"


def build_input(group_id: str, limit: int) -> dict[str, Any]:
    """Map our request onto this actor's input schema."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "count": limit,
        "cursor": "",
        "maxDelay": 10,
        "minDelay": 1,
        "proxy": {"useApifyProxy": True, "apifyProxyGroups": []},
        "scrapeGroupPosts.groupUrl": f"https://www.facebook.com/groups/{group_id}/",
        "scrapeUntil": since,
        "sortType": "new_posts",
    }


# Other actors that scrape FB groups, if this one dies or gets too expensive:
#   apify/facebook-groups-scraper
#   easyapi/facebook-group-posts-scraper
# Swap ACTOR and rewrite build_input() to match the new actor's input schema,
# then map its output in normalize() below. Do not add fallback chains.

# ─────────────────────────────────────────────────────────────────────────────


def group_id_from_url(url: str) -> str | None:
    match = re.search(r"/groups/([^/?#]+)", url)
    return match.group(1) if match else None


def normalize(post: dict[str, Any], group_id: str) -> dict[str, Any] | None:
    """Actor payload -> our row shape."""
    url = post.get("url") or post.get("postUrl") or post.get("id") or ""
    if not url:
        return None
    posted_at = None
    created = post.get("createdAt") or post.get("time")
    if created:
        try:
            # UTC. Naive local timestamps caused off-by-hours bugs before.
            posted_at = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            pass
    return {
        "post_url": str(url),
        "group_id": group_id,
        "raw_text": post.get("text") or "",
        "image_urls": extract_image_urls(post),
        "posted_at": posted_at,
    }


def fetch_group(client: Any, group_id: str, limit: int) -> list[dict[str, Any]]:
    """One actor run. Blocks until it finishes."""
    run = client.actor(ACTOR).start(run_input=build_input(group_id, limit))
    run_client = client.run(run["id"])

    while True:
        info = run_client.get()
        status = info.get("status")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(2)

    if status != "SUCCEEDED":
        raise RuntimeError(f"apify run {status} for group {group_id}")
    return list(client.dataset(info["defaultDatasetId"]).iterate_items())


def scrape(groups: list[str], *, respect_cooldown: bool = True, limit: int | None = None) -> dict[str, int]:
    """Scrape every group and store new posts. Returns a summary."""
    from apify_client import ApifyClient

    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is not set (see .env)")

    client = ApifyClient(token)
    per_cycle = limit or int(os.getenv("POSTS_PER_CYCLE", "10"))
    deep = int(os.getenv("DEEP_FETCH_POSTS", "50"))
    cooldown_minutes = int(os.getenv("GROUP_COOLDOWN_MINUTES", "30")) if respect_cooldown else 0

    summary = {"fetched": 0, "new": 0, "skipped": 0, "errors": 0}

    for url in groups:
        group_id = group_id_from_url(url) or url
        remaining = db.cooldown_remaining_seconds(group_id, cooldown_minutes)
        if remaining > 0:
            print(f"  {group_id}: cooldown, {remaining // 60}m {remaining % 60}s left")
            summary["skipped"] += 1
            continue

        try:
            posts = fetch_group(client, group_id, per_cycle)
            db.touch_group(group_id)
            summary["fetched"] += len(posts)

            rows = [r for r in (normalize(p, group_id) for p in posts) if r]
            unseen = [r for r in rows if not db.post_exists(r["post_url"])]

            # If literally everything is new, we are fetching too shallow and
            # posts are slipping past between cycles. Go deeper, once.
            if unseen and len(unseen) == len(rows) >= per_cycle:
                print(f"  {group_id}: all {len(rows)} new — fetching {deep} deep")
                deep_posts = fetch_group(client, group_id, deep)
                rows = [r for r in (normalize(p, group_id) for p in deep_posts) if r]
                unseen = [r for r in rows if not db.post_exists(r["post_url"])]
                summary["fetched"] += len(deep_posts)

            inserted = db.insert_posts(unseen)
            summary["new"] += inserted
            print(f"  {group_id}: {len(rows)} fetched, {inserted} new")

        except Exception as exc:
            print(f"  {group_id}: ERROR {exc}")
            summary["errors"] += 1

    return summary


def sample(groups: list[str], per_group: int = 25) -> list[dict[str, Any]]:
    """Raw posts for calibration. No DB writes, no LLM, no cooldown.

    This is what your coding agent reads before writing watch.py.
    """
    from apify_client import ApifyClient

    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("APIFY_API_TOKEN is not set (see .env)")

    client = ApifyClient(token)
    out: list[dict[str, Any]] = []
    for url in groups:
        group_id = group_id_from_url(url) or url
        try:
            posts = fetch_group(client, group_id, per_group)
            out.extend(r for r in (normalize(p, group_id) for p in posts) if r)
            print(f"  {group_id}: {len(posts)} posts")
        except Exception as exc:
            print(f"  {group_id}: ERROR {exc}")
    return out
