"""Example watch.py — concert and event tickets in fan resale groups.

Shows: a time-sensitive watch where speed matters more than precision, scalper
detection, and matching against a list of things you want rather than a numeric
range.

Copy over watch.py and edit.

Note on speed: tickets go in minutes. Set SCRAPE_INTERVAL_MINUTES=10 and
GROUP_COOLDOWN_MINUTES=10 in .env, and expect a bigger Apify bill for it.
"""

from __future__ import annotations

from pydantic import BaseModel

GROUPS = [
    "https://www.facebook.com/groups/yourcityTicketExchange/",
    "https://www.facebook.com/groups/yourvenueFanResale/",
]

PREFILTER = {
    # Deliberately loose — a missed ticket post can't be recovered, and this
    # gate is free. Let the classifier do the real work.
    "include": ["ticket", "tickets", "seat", "seats", "pair", "GA", "standing"],
    "exclude": ["looking for", "wtb", "want to buy", "iso ", "raffle", "giveaway"],
}

CLASSIFY_CRITERIA = (
    "a post SELLING tickets to a specific event on a specific date. "
    "Not someone looking to buy, not a general ticket-broker advertisement, "
    "not a discussion about an event."
)

PROMPT = """You extract ticket resale details from Facebook posts.

Be conservative. If a detail is not clearly stated, return null.

- artist: the performer or event name, lowercase.
- event_date: as written in the post, e.g. "March 14" or "Sat 3/14". Do not
  convert or guess a year.
- venue: venue name if stated.
- quantity: number of tickets available.
- price_each: price per ticket as a number. If the post gives a total for
  multiple tickets, divide and note it in `notes`.
- face_value: the printed face value per ticket, if the post states it.
- section: section, block, or "GA"/"standing"/"floor" as written.
- will_split: true if they will sell fewer than the full quantity.
- is_scalper: true if this reads as a bulk reseller — many events mentioned,
  multiple listings in one post, "DM for my inventory", pricing well above face.
- notes: one sentence on anything unusual (transfer method, restrictions).
"""


class Item(BaseModel):
    artist: str | None = None
    event_date: str | None = None
    venue: str | None = None
    quantity: int | None = None
    price_each: float | None = None
    face_value: float | None = None
    section: str | None = None
    will_split: bool | None = None
    is_scalper: bool | None = None
    contact: str | None = None
    notes: str | None = None


WANT_ARTISTS = {"radiohead", "phoebe bridgers", "jamie xx", "caribou"}
MAX_OVER_FACE = 1.3   # refuse to pay more than 30% over face
MAX_PRICE_EACH = 180  # hard ceiling when face value is unknown
NEED = 2              # you want a pair


def matches(item: Item) -> tuple[bool, str]:
    if item.is_scalper:
        return False, "bulk reseller"

    if not item.artist:
        return False, "no artist identified"
    artist = item.artist.lower()
    if not any(want in artist for want in WANT_ARTISTS):
        return False, f"not an artist I'm watching: {item.artist}"

    if item.price_each is None:
        return False, "no price stated"

    if item.face_value:
        ceiling = item.face_value * MAX_OVER_FACE
        if item.price_each > ceiling:
            return False, f"{item.price_each:.0f} is over {MAX_OVER_FACE:g}x face ({item.face_value:.0f})"
    elif item.price_each > MAX_PRICE_EACH:
        return False, f"{item.price_each:.0f} over my ceiling of {MAX_PRICE_EACH}"

    # A single ticket is fine only if they'd have split a pair anyway.
    if item.quantity is not None and item.quantity < NEED and not item.will_split:
        return False, f"only {item.quantity}, need {NEED}"

    return True, f"{item.artist} · {item.price_each:.0f} each · {item.section or 'section ?'}"


def notify_text(item: Item, post_url: str, raw_text: str) -> str:
    lines = [f"{item.artist or 'tickets'} — {item.event_date or 'date ?'}"]
    bits = []
    if item.quantity:
        bits.append(f"{item.quantity}x")
    if item.price_each:
        bits.append(f"{item.price_each:.0f} each")
    if item.face_value:
        bits.append(f"(face {item.face_value:.0f})")
    if item.section:
        bits.append(item.section)
    if bits:
        lines.append(" · ".join(bits))
    if item.venue:
        lines.append(item.venue)
    if item.notes:
        lines.append(item.notes)
    if item.contact:
        lines.append(f"contact: {item.contact}")
    lines.append(post_url)
    return "\n".join(lines)
