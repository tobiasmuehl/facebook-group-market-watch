"""Telegram alerts. Message text comes from watch.notify_text().

ponytail: stdlib urllib, no python-telegram-bot. It's one POST.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/sendMessage"


def enabled() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip())


def send(text: str) -> bool:
    """Returns True if delivered. Never raises — a dead bot token must not stop
    the watcher; the post is still stored and shows up in `watcher web`."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4096],  # Telegram hard limit
        "disable_web_page_preview": False,
    }).encode()
    request = urllib.request.Request(
        API.format(token=token), data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read()).get("ok", False)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        print(f"  telegram failed: HTTP {exc.code} {body}")
    except Exception as exc:
        print(f"  telegram failed: {exc}")
    return False


def send_test() -> bool:
    return send("Facebook Group Market Watch is connected. You'll get alerts here.")
