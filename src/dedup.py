"""Repost detection.

post_url UNIQUE already catches the same post seen twice. This catches the
other thing: agents reposting the *same item* under a new URL, every day,
forever. Without it your phone buzzes about the same condo for a month.

Two independent checks:
  text  — identical text after normalizing unicode/case/whitespace
  image — enough of the same photos as an earlier post
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

IMAGE_EXTENSIONS = (".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".webp")

_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\ufeff]")
# Facebook serves the same photo as ..._n.jpg and ..._o.jpg. Same image.
_FB_VARIANT_RE = re.compile(r"^(\d+_\d+_\d+)_([a-z])(\.[a-z0-9]+)$")


# ── text ─────────────────────────────────────────────────────────────────────

def text_hash(raw_text: Any) -> str | None:
    """Stable hash of normalized text. Conservative: case, unicode form and
    whitespace only, so genuinely different posts never collide."""
    if not isinstance(raw_text, str):
        return None
    norm = unicodedata.normalize("NFKC", raw_text).lower()
    norm = _ZERO_WIDTH_RE.sub("", norm)
    norm = _WHITESPACE_RE.sub(" ", norm).strip()
    if not norm:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


# ── images ───────────────────────────────────────────────────────────────────

def is_image_url(candidate: Any) -> bool:
    if not isinstance(candidate, str):
        return False
    value = candidate.strip()
    if not value.startswith(("http://", "https://")):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if path.endswith(IMAGE_EXTENSIONS):
        return True
    # facebook.com links are posts/pages, not image assets.
    if host.endswith("facebook.com") or host.endswith("fb.com"):
        return False
    if any(t in query for t in ("mime=image/", "format=jpg", "format=jpeg", "format=png", "format=webp")):
        return True
    # Signed fbcdn URLs have no extension but are always images here.
    return host.endswith("fbcdn.net")


def image_signature(image_url: Any) -> str | None:
    """Signature that survives re-signing: drops query params, collapses
    Facebook CDN hosts and filename variants."""
    if not is_image_url(image_url):
        return None
    parsed = urlparse(str(image_url).strip())
    host = (parsed.hostname or "").lower()
    path = unquote((parsed.path or "").strip()).lower()
    if not host or not path:
        return None
    if host.endswith("fbcdn.net"):
        filename = path.rsplit("/", 1)[-1]
        if filename:
            match = _FB_VARIANT_RE.match(filename)
            if match:
                filename = f"{match.group(1)}{match.group(3)}"
            return f"fbcdn.net:{filename}"
    normalized = re.sub(r"/{2,}", "/", path).rstrip("/")
    return f"{host}:{normalized}" if normalized else None


def image_signatures(image_urls: Iterable[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for url in image_urls or []:
        sig = image_signature(url)
        if sig and sig not in seen:
            seen.add(sig)
            out.append(sig)
    return out


def overlap(left: Iterable[str] | None, right: Iterable[str] | None) -> tuple[int, float]:
    """(shared_count, ratio). Ratio divides by the smaller set so a repost that
    dropped a couple of photos still scores high."""
    a = {v for v in (left or []) if v}
    b = {v for v in (right or []) if v}
    if not a or not b:
        return 0, 0.0
    shared = len(a & b)
    return shared, (shared / min(len(a), len(b))) if shared else 0.0


def find_image_duplicate(
    sigs: list[str],
    candidates: list[tuple[str, list[str]]],
    *,
    min_shared: int,
    min_ratio: float,
) -> str | None:
    """First earlier post whose photos overlap enough with `sigs`."""
    if not sigs:
        return None
    for post_url, other in candidates:
        shared, ratio = overlap(sigs, other)
        if shared >= max(1, min_shared) and ratio >= min_ratio:
            return post_url
    return None


# ── post payload helpers ─────────────────────────────────────────────────────

def extract_image_urls(post: dict[str, Any]) -> list[str]:
    """Dig image URLs out of the Apify post payload's nested attachment shapes."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(candidate: Any) -> None:
        if is_image_url(candidate):
            value = str(candidate).strip()
            if value not in seen:
                seen.add(value)
                urls.append(value)

    def walk(attachment: Any) -> None:
        if not isinstance(attachment, dict):
            return
        media = attachment.get("media") if isinstance(attachment.get("media"), dict) else {}
        for key in ("image", "photo_image", "viewer_image"):
            node = media.get(key)
            if isinstance(node, dict):
                add(node.get("src") or node.get("uri"))
        sub = attachment.get("all_subattachments")
        if isinstance(sub, dict):
            for edge in sub.get("edges") or []:
                if isinstance(edge, dict):
                    walk(edge.get("node"))
        for img in attachment.get("images") or []:
            if isinstance(img, dict):
                add(img.get("url") or img.get("src") or img.get("uri"))

    for attachment in post.get("attachments") or []:
        walk(attachment)
    for img in post.get("images") or []:
        if isinstance(img, dict):
            add(img.get("url") or img.get("src") or img.get("uri"))
        else:
            add(img)
    return urls
