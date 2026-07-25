"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  THIS IS THE ONLY FILE YOU EDIT.                                             ║
║                                                                              ║
║  Everything else is plumbing that never needs touching. If your coding agent ║
║  is setting this up for you, it rewrites this file and leaves the rest alone.║
║                                                                              ║
║  What's here now is an EXAMPLE (apartment rentals). Replace it with whatever ║
║  you're actually hunting — see examples/ for cars, tickets, furniture.       ║
╚══════════════════════════════════════════════════════════════════════════════╝

A post travels through four gates. Each one that drops a post records why, so
`watcher test` can show you exactly where things died.

  gate 0  PREFILTER          free       obvious noise
  gate 1  CLASSIFY_CRITERIA  ~$0.03/1k  semantic noise keywords can't catch (opt-in)
  gate 2  PROMPT + Item      ~$0.14/1k  pull the fields out
  gate 3  matches()          free       your actual criteria

Gate 2 and gate 3 are deliberately separate: extract generously, filter
narrowly. Changing your mind about price or size then costs nothing —
`watcher rematch` re-runs gate 3 over posts you already paid to parse.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Which groups to watch ────────────────────────────────────────────────────
# Any Facebook group URL. Numeric IDs and vanity names both work.
# You must be a member of private groups for a scraper to see them.

GROUPS = [
    "https://www.facebook.com/groups/Bangkokcondoforsaleandrent/",
    "https://www.facebook.com/groups/condoowner/",
]


# ── Gate 0: keyword prefilter (free) ─────────────────────────────────────────
# Case-insensitive substring match on the raw post text.
#   include — post must contain at least one. Empty list = let everything through.
#   exclude — post is dropped if it contains any. Checked first.
#
# Keep `include` loose. This gate is a cost saver, not your real filter — a post
# dropped here is never seen again. If you're not sure, leave include empty and
# let the LLM decide.

PREFILTER = {
    "include": ["rent", "for rent", "condo", "apartment", "bedroom", "bhk"],
    "exclude": ["job vacancy", "hiring", "insurance", "loan approval"],
}


# ── Gate 1: cheap LLM classifier (opt-in, CLASSIFIER_ENABLED in .env) ────────
# One sentence describing what "relevant" means. Answers yes/no, nothing else.
# Worth turning on when you're seeing >500 posts/day or your extraction model is
# expensive. It catches things keywords can't — someone *looking for* a rental
# reads almost identically to someone *offering* one.

CLASSIFY_CRITERIA = (
    "a post OFFERING a specific apartment or condo for rent, with an actual unit. "
    "Not someone looking for a place, not a general agency advertisement, "
    "not a property-management service pitch."
)


# ── Gate 2: extraction ───────────────────────────────────────────────────────
# Instructions for the model. Be specific about the *hard* cases — that's where
# the accuracy is won. Write this AFTER reading real posts from your groups
# (`watcher sample`), not from imagination.

PROMPT = """You extract apartment rental details from Facebook group posts.

Posts may be in any language, or mixed languages. Be conservative: if a detail
is not clearly stated, return null. Never infer a value to fill a field.

- price: monthly rent as a number, in the local currency of the post. Ignore
  deposits, utilities, and agent fees. "35k" means 35000.
- bedrooms: 0 for a studio. Only from explicit cues ("2BR", "2 bed", "studio").
- size_sqm: square meters only. Convert from sq ft if that is what's given.
- area: neighbourhood or district name, lowercase.
- is_offer: true if someone is offering a place, false if they are looking for
  one. When genuinely ambiguous, false.
- is_owner: true ONLY if the post explicitly says the poster is the owner.
  Do not infer this from the group name or from the tone of the post.
- available_from: as written, e.g. "1 March" or "immediately". null if absent.
- contact: phone, Line ID, or whatever contact handle is given.
- summary: one plain sentence. Do not restate price, bedrooms, or size.
"""


class Item(BaseModel):
    """The fields you want out of each post.

    Every field MUST be optional with a default. Models return null for
    anything a post doesn't mention, and a required field turns that into a
    validation error on nearly every post.
    """

    price: float | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    size_sqm: float | None = None
    area: str | None = None
    is_offer: bool | None = None
    is_owner: bool | None = None
    pets_allowed: bool | None = None
    furnished: bool | None = None
    available_from: str | None = None
    contact: str | None = None
    summary: str | None = Field(None, description="one sentence")


# ── Gate 3: your actual criteria (free, re-runnable) ─────────────────────────

def matches(item: Item) -> tuple[bool, str]:
    """Return (alert_me, why). The reason is stored either way, so a post that
    didn't match still tells you what it was rejected for.

    Plain Python. Anything you can express, you can filter on.
    """
    if item.is_offer is False:
        return False, "someone looking, not offering"

    if item.price is None:
        return False, "no price stated"
    if item.price > 60_000:
        return False, f"too expensive: {item.price:,.0f}"
    if item.price < 5_000:
        return False, f"suspiciously cheap, probably not a real listing: {item.price:,.0f}"

    if item.bedrooms is not None and item.bedrooms < 1:
        return False, "studio"

    # Only mention size when the post actually stated it.
    if item.size_sqm is not None and item.size_sqm < 35:
        return False, f"too small: {item.size_sqm} sqm"

    wanted = {"thonglor", "ekkamai", "phrom phong"}
    if item.area and item.area.lower() not in wanted:
        return False, f"wrong area: {item.area}"

    return True, f"{item.price:,.0f} in {item.area or 'unknown area'}"


# ── The alert ────────────────────────────────────────────────────────────────

def notify_text(item: Item, post_url: str, raw_text: str) -> str:
    """The Telegram message. Plain text; keep it skimmable on a phone."""
    lines = []
    price = f"{item.price:,.0f}" if item.price else "price not stated"
    beds = f"{item.bedrooms:g}BR" if item.bedrooms is not None else "?BR"
    size = f"{item.size_sqm:g}sqm" if item.size_sqm else ""
    lines.append(" · ".join(x for x in [price, beds, size, item.area] if x))

    if item.is_owner:
        lines.append("posted by owner")
    if item.summary:
        lines.append(item.summary)

    extras = []
    if item.furnished:
        extras.append("furnished")
    if item.pets_allowed:
        extras.append("pets ok")
    if item.available_from:
        extras.append(f"from {item.available_from}")
    if extras:
        lines.append(", ".join(extras))

    if item.contact:
        lines.append(f"contact: {item.contact}")
    lines.append(post_url)
    return "\n".join(lines)
