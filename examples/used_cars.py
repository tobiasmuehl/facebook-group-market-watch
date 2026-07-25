"""Example watch.py — used cars in local buy/sell groups.

Shows: matching against a set of acceptable models, sanity-checking prices to
catch scams, and refusing posts that are missing the one field you can't work
without (mileage).

Copy over watch.py and edit.
"""

from __future__ import annotations

from pydantic import BaseModel

GROUPS = [
    "https://www.facebook.com/groups/yourcityCarsForSale/",
    "https://www.facebook.com/groups/yourregionAutoTrader/",
]

PREFILTER = {
    "include": ["for sale", "selling", "km", "miles", "owner", "toyota", "honda", "mazda"],
    "exclude": ["wanted", "looking for", "wtb", "car loan", "insurance quote", "car wash"],
}

CLASSIFY_CRITERIA = (
    "a post SELLING one specific used car, with details about that car. "
    "Not a dealership advertising inventory, not someone looking to buy, "
    "not parts or accessories, not a finance or insurance offer."
)

PROMPT = """You extract used-car listing details from Facebook posts.

Be conservative. If a detail is not clearly stated, return null. Never guess.

- make / model: as written, but normalized to lowercase ("Toyota Corolla" -> make
  "toyota", model "corolla"). Drop trim levels from model; put them in `trim`.
- year: four-digit model year. "2018 plate" means 2018.
- price: asking price as a number. "$12.5k" -> 12500. If the post says OBO or
  negotiable, still return the number stated.
- mileage_km: convert miles to km (1 mile = 1.609 km) and say so in `notes`.
- transmission: "auto" or "manual" only.
- accident_free: true only if the post explicitly claims no accidents.
- service_history: true only if the post explicitly mentions service records.
- owner_count: number of previous owners if stated.
- is_dealer: true if the post reads as a dealership or trader rather than a
  private seller — multiple cars mentioned, showroom address, finance offers.
- notes: one sentence on condition and anything unusual.
"""


class Item(BaseModel):
    make: str | None = None
    model: str | None = None
    trim: str | None = None
    year: int | None = None
    price: float | None = None
    mileage_km: float | None = None
    transmission: str | None = None
    accident_free: bool | None = None
    service_history: bool | None = None
    owner_count: int | None = None
    is_dealer: bool | None = None
    contact: str | None = None
    notes: str | None = None


WANT = {
    # model -> (min_year, max_price)
    "corolla": (2016, 15_000),
    "civic": (2016, 16_000),
    "mazda3": (2017, 15_000),
    "yaris": (2017, 12_000),
}


def matches(item: Item) -> tuple[bool, str]:
    if item.is_dealer:
        return False, "dealer, not a private sale"

    if not item.model:
        return False, "no model identified"
    key = item.model.lower().replace(" ", "")
    if key not in WANT:
        return False, f"not a model I want: {item.model}"
    min_year, max_price = WANT[key]

    if item.year is None:
        return False, "no year stated"
    if item.year < min_year:
        return False, f"too old: {item.year} (want {min_year}+)"

    if item.price is None:
        return False, "no price stated"
    if item.price > max_price:
        return False, f"over budget: {item.price:,.0f} > {max_price:,.0f}"
    # A price far under market for the year is a scam or a wreck, every time.
    if item.price < max_price * 0.25:
        return False, f"implausibly cheap, likely a scam: {item.price:,.0f}"

    # Mileage is the field that decides whether a car is worth a message.
    # No mileage, no alert — don't relax this, you'll drown in vague posts.
    if item.mileage_km is None:
        return False, "no mileage stated"
    if item.mileage_km > 150_000:
        return False, f"too many km: {item.mileage_km:,.0f}"

    return True, f"{item.year} {item.model} · {item.price:,.0f} · {item.mileage_km:,.0f}km"


def notify_text(item: Item, post_url: str, raw_text: str) -> str:
    head = f"{item.year or '?'} {item.make or ''} {item.model or ''} {item.trim or ''}".strip()
    lines = [head, f"{item.price:,.0f}" if item.price else "no price"]
    if item.mileage_km:
        lines.append(f"{item.mileage_km:,.0f} km")
    flags = []
    if item.accident_free:
        flags.append("accident free")
    if item.service_history:
        flags.append("has service history")
    if item.owner_count:
        flags.append(f"{item.owner_count} owner(s)")
    if item.transmission:
        flags.append(item.transmission)
    if flags:
        lines.append(" · ".join(flags))
    if item.notes:
        lines.append(item.notes)
    if item.contact:
        lines.append(f"contact: {item.contact}")
    lines.append(post_url)
    return "\n".join(lines)
