"""Example watch.py — secondhand furniture and free giveaways.

Shows: a watch where the interesting signal is qualitative (condition, brand,
"free"), and where distance matters more than price.

Copy over watch.py and edit.
"""

from __future__ import annotations

from pydantic import BaseModel

GROUPS = [
    "https://www.facebook.com/groups/yourcityFreecycle/",
    "https://www.facebook.com/groups/yourneighbourhoodBuyNothing/",
    "https://www.facebook.com/groups/yourcitySecondhandFurniture/",
]

PREFILTER = {
    "include": ["free", "giving away", "sofa", "couch", "table", "chair", "desk",
                "shelf", "bookcase", "wardrobe", "dresser"],
    "exclude": ["looking for", "iso ", "wtb", "delivery service", "removals",
                "cleaning service", "assembly service"],
}

CLASSIFY_CRITERIA = (
    "a post OFFERING a specific piece of furniture, either for sale or free. "
    "Not someone looking for furniture, not a shop advertisement, "
    "not a removals or assembly service."
)

PROMPT = """You extract secondhand furniture listings from Facebook posts.

Be conservative. If a detail is not clearly stated, return null.

- category: one of sofa, table, chair, desk, bed, storage, lighting, other.
- brand: brand name if stated, lowercase. IKEA product names count as brand
  "ikea" with the product name in `model`.
- price: number. Use 0 when the post says free, giving away, or similar.
- condition: one of new, like_new, good, worn, damaged. Judge from the wording:
  "barely used" is like_new, "some scratches" is worn, "needs repair" is damaged.
  Use null if the post says nothing about condition.
- dimensions_cm: as written, e.g. "180x90x75". null if not stated.
- material: wood, metal, glass, fabric, leather, plastic, mixed.
- pickup_area: neighbourhood or district for collection, lowercase.
- needs_van: true if the item plainly cannot fit in a car — sofas, wardrobes,
  dining tables. Infer this from the category and size, it is rarely stated.
- collection_deadline: as written, if the post says it must go by some time.
- notes: one sentence, including any defects mentioned.
"""


class Item(BaseModel):
    category: str | None = None
    brand: str | None = None
    model: str | None = None
    price: float | None = None
    condition: str | None = None
    dimensions_cm: str | None = None
    material: str | None = None
    pickup_area: str | None = None
    needs_van: bool | None = None
    collection_deadline: str | None = None
    contact: str | None = None
    notes: str | None = None


WANT_CATEGORIES = {"sofa", "desk", "storage"}
NEARBY = {"northside", "riverfield", "old town", "harbour"}
MAX_PRICE = 200
OK_CONDITION = {"new", "like_new", "good"}


def matches(item: Item) -> tuple[bool, str]:
    if item.category not in WANT_CATEGORIES:
        return False, f"not a category I want: {item.category}"

    if item.condition and item.condition not in OK_CONDITION:
        return False, f"condition: {item.condition}"

    if item.price is None:
        return False, "no price stated"
    if item.price > MAX_PRICE:
        return False, f"over budget: {item.price:.0f}"

    # Collection is the real constraint with furniture — a great free sofa two
    # hours away is not a great free sofa.
    if item.pickup_area and item.pickup_area.lower() not in NEARBY:
        return False, f"too far: {item.pickup_area}"

    if item.needs_van and item.price == 0:
        # Still alert — free is free — but say so up front in the reason.
        return True, "FREE but you'll need a van"

    label = "FREE" if item.price == 0 else f"{item.price:.0f}"
    return True, f"{label} {item.category} in {item.pickup_area or 'unknown area'}"


def notify_text(item: Item, post_url: str, raw_text: str) -> str:
    price = "FREE" if item.price == 0 else (f"{item.price:.0f}" if item.price else "price ?")
    head = " ".join(x for x in [item.brand, item.model, item.category] if x) or "furniture"
    lines = [f"{head} — {price}"]

    bits = [x for x in [item.condition, item.material, item.dimensions_cm] if x]
    if bits:
        lines.append(" · ".join(bits))
    if item.pickup_area:
        lines.append(f"pickup: {item.pickup_area}")
    if item.needs_van:
        lines.append("won't fit in a car")
    if item.collection_deadline:
        lines.append(f"must go by {item.collection_deadline}")
    if item.notes:
        lines.append(item.notes)
    if item.contact:
        lines.append(f"contact: {item.contact}")
    lines.append(post_url)
    return "\n".join(lines)
