"""Example watch.py — Bangkok condos. 23 real groups, points scoring, Thai support.

This is the set the template was extracted from: 23 Facebook groups scraped in
production for a Bangkok apartment search, over 100,000 listings across several
months. The prompt, classifier criteria, scoring weights and dedup thresholds
below are all tuned on that data. If you're looking in Bangkok you can use this
as-is — adjust the constants, don't rewrite the prompt.

    cp examples/bangkok_apartments.py watch.py

Setup notes, cost math, and the decisions this preset forces: ../BANGKOK.md

──────────────────────────────────────────────────────────────────────────────
READ THIS BEFORE YOU TURN IT ON

**These groups produce thousands of posts a day.** Together they're one of the
busiest property markets on Facebook. That has three consequences:

1. **Use DeepSeek.** `LLM_MODEL=deepseek/deepseek-v4-flash` in .env. At ~$0.14
   per 1000 posts it's a few dollars a month here. A frontier model on the same
   volume runs to real money for no benefit — this is field extraction, not
   reasoning. Benchmark before you upgrade: `watcher label && watcher bench`.

2. **Turn the classifier on.** `CLASSIFIER_ENABLED=true` in .env. Above ~500
   posts/day the cheap yes/no gate pays for itself, and these groups are full of
   posts that look right to a keyword filter and aren't — agency ads, new-
   development campaigns, people looking rather than offering.

3. **The schedule below is already cost-aware.** It scrapes hard during Bangkok
   office hours and backs off overnight. See `scrape_interval_minutes()`.

**Start broad. Refine later.** Set a generous budget and a wide area list on day
one, let it run, then look at what came through. You'll learn more from a day of
real alerts than from an hour of guessing thresholds. When you want it tighter,
just tell your coding agent — "too many studios", "I only care about Thonglor
now", "add a bonus for high floors" — and have it edit this file. Then:

    uv run python watcher.py rematch

which re-decides your entire history for free, no new LLM calls. Tightening
criteria costs nothing, so there's no reason to over-think them up front.

──────────────────────────────────────────────────────────────────────────────
About the buy/rent split

Bangkok property groups mostly do NOT separate sale from rental — the two
biggest here are literally named "condo for sale AND rent". So the lists below
are leanings, not guarantees. `matches()` does the real filtering on the
extracted `is_rental` field, which is what actually keeps the wrong ones out.

The split that IS reliable is owner-only vs general. Owner groups ban agents, so
posts come from the person who owns the unit — no agent fee, better prices. For
just those: `GROUPS = GROUPS_OWNER`.
──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

# ── what you're doing ────────────────────────────────────────────────────────

LOOKING_FOR = "rent"        # "rent" or "buy"

MAX_RENT_THB = 65_000       # per month
MAX_BUY_THB = 12_000_000    # total
MIN_BEDROOMS = 2            # 1 to sleep in, 1 for an office
MIN_SIZE_SQM = 60

# Alert on anything scoring at least this. Start at 0 (alert on everything that
# passes the hard requirements), watch for a few days, then raise it.
ALERT_THRESHOLD = 0.0


# ── the groups ───────────────────────────────────────────────────────────────
# General / agent-heavy. High volume, most listings come via agents.
# Both sale and rental posts appear in all of these.

GROUPS_GENERAL = [
    "https://www.facebook.com/groups/Bangkokcondoforsaleandrent/",
    "https://www.facebook.com/groups/bangkokhomecondoforsalerent/",
    "https://www.facebook.com/groups/395910312724210/",
    "https://www.facebook.com/groups/1046291836187649/",
    "https://www.facebook.com/groups/273178681735292/",
    "https://www.facebook.com/groups/6521519787867197/",
    "https://www.facebook.com/groups/28729093243342594/",
    "https://www.facebook.com/groups/2157264477895406/",
    "https://www.facebook.com/groups/661207761960248/",
]

# Owner-only. Agents are banned, so posts come from the actual owner.
# Lower volume, better prices, no agent fee.
GROUPS_OWNER = [
    "https://www.facebook.com/groups/owneronly/",
    "https://www.facebook.com/groups/condoowner/",
    "https://www.facebook.com/groups/ownerpropertycenter/",
    "https://www.facebook.com/groups/owneragentpost/",
    "https://www.facebook.com/groups/luxurycondobkk/",
    "https://www.facebook.com/groups/360639508893447/",
    "https://www.facebook.com/groups/412391603132466/",
    "https://www.facebook.com/groups/849933970190019/",
    "https://www.facebook.com/groups/456957028207473/",
    "https://www.facebook.com/groups/299716057099018/",
    "https://www.facebook.com/groups/1148910301908348/",
    "https://www.facebook.com/groups/458098031664389/",
    "https://www.facebook.com/groups/374078016708868/",
    # The only one here whose name commits to sales specifically. Still owner-run.
    "https://www.facebook.com/groups/condosalesbyowner/",
]

GROUPS = GROUPS_GENERAL + GROUPS_OWNER


# ── where you want to live ───────────────────────────────────────────────────
# EXAMPLE VALUES. These are the Sukhumvit sois around Thonglor and Phrom Phong —
# replace them with yours. Sukhumvit sois are odd on the north side, even on the
# south; the number is roughly a distance marker, so neighbouring numbers are
# neighbouring places.

PREMIUM_SOIS = {"39", "49"}                        # 5 points
GOOD_SOIS = {"24", "31", "33", "38", "55"}         # 3 points
OK_SOIS = {"26", "35", "36", "41", "53", "57"}     # 1.5 points

# Lowercase, normalized. The prompt normalizes "thong lor" -> "thonglor" etc.
PREMIUM_AREAS = {"thonglor", "phrom phong"}        # 4 points
GOOD_AREAS = {"ekkamai", "asoke", "ploenchit", "chidlom"}   # 2.5 points
OK_AREAS = {"nana", "phra khanong", "onnut", "langsuan", "ari"}  # 1 point

# Station names that differ from the area name above. Everything else is matched
# against the area sets directly — keeping a second hand-written station list in
# sync with the area lists doesn't work, they drift and then the same place
# scores differently depending on which field the model happened to fill in.
STATION_ALIASES = {
    "thong lo": "thonglor",
    "asok": "asoke",
    "on nut": "onnut",
    "phloen chit": "ploenchit",
    "chit lom": "chidlom",
}

# Hard rejects regardless of anything else.
EXCLUDED_SOIS: set[str] = set()


# ── how often to scrape ──────────────────────────────────────────────────────

def scrape_interval_minutes() -> int:
    """Minutes between scrape cycles, by Bangkok time.

    Apify charges per actor run, and 23 groups is a lot of runs. Bangkok agents
    post during office hours; scraping every 15 minutes at 4am buys nothing.
    This schedule was tuned on the real thing.

    Delete this function to go back to a flat SCRAPE_INTERVAL_MINUTES.
    """
    hour = (datetime.now(timezone.utc) + timedelta(hours=7)).hour  # ICT = UTC+7
    if 7 <= hour < 9:
        return 60      # early listings trickle in
    if 9 <= hour < 18:
        return 15      # office hours, most posts land here
    if 18 <= hour < 21:
        return 45      # evening tail
    if 21 <= hour < 23:
        return 60
    return 150         # overnight, barely anything posts


# ── Gate 0: keyword prefilter ────────────────────────────────────────────────
# Bangkok groups are English/Thai mixed, often in the same post. Include both.
# Kept loose on purpose — anything dropped here is never seen again.
#
# Note: the watcher NFKC-normalizes text before matching, so posts written in
# fancy Unicode ("𝟲𝟱,𝟬𝟬𝟬 𝗧𝗛𝗕") still match plain keywords. Property spam does
# this constantly.

PREFILTER = {
    "include": [
        # English. "br" catches "2BR", which is how most posts write it — it
        # also matches "brand" and "broker", making this gate very permissive.
        # That's the right trade: the gate skips obvious junk, and a
        # wrongly-dropped listing is gone for good.
        "rent", "sale", "sell", "condo", "apartment", "bedroom", "br", "sqm",
        "studio", "bts", "mrt", "sukhumvit",
        # Thai
        "เช่า",         # rent
        "ให้เช่า",       # for rent
        "ขาย",          # sell
        "คอนโด",        # condo
        "อพาร์ตเมนต์",   # apartment
        "บ้าน",         # house
        "ห้องนอน",      # bedroom
        "ห้อง",         # room
        "ตร.ม",         # sqm
        "ตารางเมตร",    # square metres
        "สุขุมวิท",      # sukhumvit
        "ซอย",          # soi
        "เจ้าของ",       # owner
    ],
    "exclude": [
        # English
        "hiring", "job vacancy", "recruit", "we are looking for staff",
        "insurance", "loan approval", "mortgage broker",
        "cleaning service", "moving service", "renovation service",
        # Thai
        "รับสมัครงาน",   # hiring
        "สินเชื่อ",       # loan / credit
        "ประกันภัย",     # insurance
        "บริการทำความสะอาด",  # cleaning service
    ],
}


# ── Gate 1: cheap classifier ─────────────────────────────────────────────────
# Turn this on: CLASSIFIER_ENABLED=true. At this volume it earns its keep.

CLASSIFY_CRITERIA = (
    "a post OFFERING one specific condo, apartment, or house in Bangkok, "
    "for rent or for sale, with details about that particular unit. "
    "NOT someone looking for a place (Thai: หาห้อง, ต้องการเช่า, ใครมีห้อง). "
    "NOT an agency advertising its services or its portfolio. "
    "NOT a new-development sales campaign with no specific unit. "
    "NOT a property-management, cleaning, moving, or loan service."
)


# ── Gate 2: extraction ───────────────────────────────────────────────────────

PROMPT = """You extract Bangkok property listing details from Facebook group posts.

Posts are in English, Thai, or both mixed in a single post. Be conservative: if
a detail is not clearly stated, return null. Never guess, and never fill a field
just to avoid leaving it empty. A null is far more useful than an invention.

TRANSACTION TYPE
- is_rental: true for rental, false for sale.
  Thai rent cues: เช่า, ให้เช่า, ปล่อยเช่า.  Thai sale cues: ขาย, ขายดาวน์.
  A post can offer both — then set is_rental true and also fill sale_price.

PRICES (all in THB)
- price: monthly rent as a number. Ignore deposits, utilities, and agent fees.
  "35k", "35,000", "35000" all mean 35000.
  Thai numerals: หมื่น = 10,000 ("3 หมื่น" = 30000), แสน = 100,000,
  ล้าน = 1,000,000 ("2.5 ล้าน" = 2500000).
- sale_price: total sale price when the post is selling.

SIZE AND LAYOUT
- bedrooms: 0 for a studio (สตูดิโอ). Only from explicit cues: "2BR", "2 bed",
  Thai "2 ห้องนอน". Half steps like 1.5 are real in Bangkok — keep them.
- bathrooms: same rules. Thai ห้องน้ำ. If it's explicitly a studio and no
  bathroom is stated, use 1.
- size_sqm: square metres. Thai ตร.ม. or ตารางเมตร.
- floor: as written. Keep letter suffixes ("7A") and ranges ("11-12").
- property_type: one of condo, apartment, house, townhouse, commercial. null if
  unclear. Thai: คอนโด/คอนโดมิเนียม = condo, อพาร์ตเมนต์/แฟลต = apartment,
  บ้าน/บ้านเดี่ยว = house, ทาวน์เฮ้าส์/ทาวน์โฮม = townhouse,
  อาคารพาณิชย์/ตึกแถว/โฮมออฟฟิศ = commercial.
  If there's no non-condo cue but a clear project name, default to condo.

LOCATION
- area: neighbourhood, lowercase. Thonglor, Ekkamai, Phrom Phong, Asoke, Nana,
  Chidlom, Ploenchit, Langsuan, Sathorn, Silom, Phra Khanong, On Nut, Ari,
  Ratchada, Rama 9. Infer from an unambiguous project name ("Noble Ploenchit"
  -> "ploenchit"). Normalize: "thong lor"/"thong lo" -> "thonglor",
  "phromphong" -> "phrom phong", "on nut" -> "onnut", "ekamai" -> "ekkamai".
  Thai: ทองหล่อ = thonglor, เอกมัย = ekkamai, พร้อมพงษ์ = phrom phong,
  พระโขนง = phra khanong, อ่อนนุช = onnut, อโศก = asoke, สุขุมวิท = sukhumvit.
- soi: the Sukhumvit soi number as a string, but ONLY when the number is
  explicitly tied to Sukhumvit — "Sukhumvit Soi 55", "สุขุมวิท 55", "ซอย 55
  สุขุมวิท". A soi number on any other road ("Wachiratham Soi 55") is NOT a
  Sukhumvit soi. Return null when in doubt.
- project_name: condo or building name, e.g. "Noble Ploenchit", "The Lumpini 24".
- nearest_station: the BTS/MRT/ARL station named, if any.
- walk_minutes: minutes to that station, only when the post states a number.
  If it gives metres, convert at 80 m per minute.
  Thai proximity cues: ใกล้ = near, เดิน = walk, นาที = minutes, เมตร = metres.
  A bare "#BTS" hashtag with no station or access wording is NOT proximity.

WHO IS POSTING
- is_owner: true ONLY if the post explicitly says the poster is the owner.
  English: "by owner", "direct owner", "owner post". Thai: เจ้าของ,
  เจ้าของปล่อยเอง, เจ้าของโพสต์. Do NOT infer this from which group the post
  came from, and do NOT infer it from tone or from an absence of agent wording.
- is_demand_post: true if this person is LOOKING FOR a place rather than
  offering one. English: "looking for", "seeking", "anyone have", "WTR".
  Thai: หาห้อง, หาคอนโด, หาที่พัก, หาเช่า, ต้องการเช่า, ต้องการห้อง,
  ใครมีห้อง, มองหา. When genuinely ambiguous, false.
- contact: phone, Line ID, or WhatsApp as given.

QUALITY SIGNALS — set true only on explicit mention, else null
- is_quiet: quiet, peaceful, not noisy, away from the main road or tollway.
  Thai: เงียบ, เงียบสงบ.
- has_natural_light: bright, sunny, good light, lots of windows, unblocked.
  Thai: แสงธรรมชาติ, สว่าง.
- has_balcony: balcony or terrace. Thai: ระเบียง.
- has_good_kitchen: big kitchen, western kitchen, good for cooking, has a hood
  or exhaust. Thai: ครัวใหญ่, ครัวฝรั่ง.
- has_good_view: unobstructed, city/river/park view, high floor with a view.
  Thai: วิวสวย, วิวเมือง, วิวแม่น้ำ.
- no_flooding: the post explicitly says the place does not flood.
  Thai: ไม่ท่วม, น้ำไม่ท่วม.
- near_convenience_store: 7-11, FamilyMart, Tops, Big C, Lotus within walking
  distance. Thai: เซเว่น, ใกล้เซเว่น.

AMENITIES — true only on explicit mention, else null
- has_parking: Thai ที่จอดรถ. Set parking_spaces if a number is given.
- has_pool: สระว่ายน้ำ
- has_gym: ฟิตเนส, ยิม
- has_oven: เตาอบ
- has_dishwasher: เครื่องล้างจาน
- has_dryer: เครื่องอบผ้า (a washing machine alone is NOT a dryer)
- has_bathtub: อ่างอาบน้ำ
- has_maid_room: ห้องแม่บ้าน
- pets_allowed: สัตว์เลี้ยง, รับสัตว์เลี้ยง
- furnished: fully or partly furnished. Thai: เฟอร์นิเจอร์ครบ, ตกแต่งครบ.

OTHER
- available_from: as written. Thai: เข้าอยู่ได้, ว่างแล้ว.
- summary: one plain sentence. Do not restate price, bedrooms, or size.
"""


class Item(BaseModel):
    # transaction
    is_rental: bool | None = None
    price: float | None = None
    sale_price: float | None = None
    # size and layout
    bedrooms: float | None = None
    bathrooms: float | None = None
    size_sqm: float | None = None
    floor: str | None = None
    property_type: str | None = None
    # location
    area: str | None = None
    soi: str | None = None
    project_name: str | None = None
    nearest_station: str | None = None
    walk_minutes: float | None = None
    # who
    is_owner: bool | None = None
    is_demand_post: bool | None = None
    contact: str | None = None
    # quality
    is_quiet: bool | None = None
    has_natural_light: bool | None = None
    has_balcony: bool | None = None
    has_good_kitchen: bool | None = None
    has_good_view: bool | None = None
    no_flooding: bool | None = None
    near_convenience_store: bool | None = None
    # amenities
    has_parking: bool | None = None
    parking_spaces: float | None = None
    has_pool: bool | None = None
    has_gym: bool | None = None
    has_oven: bool | None = None
    has_dishwasher: bool | None = None
    has_dryer: bool | None = None
    has_bathtub: bool | None = None
    has_maid_room: bool | None = None
    pets_allowed: bool | None = None
    furnished: bool | None = None
    # other
    available_from: str | None = None
    summary: str | None = None


# ── Gate 3: hard requirements, then points ───────────────────────────────────

def hard_requirements(item: Item) -> str | None:
    """Return a rejection reason, or None if the listing qualifies.

    Note the asymmetry: a stated-and-wrong value rejects, a missing value does
    not. Most Bangkok posts omit most fields, and rejecting on absence throws
    away the majority of real listings.
    """
    if item.is_demand_post:
        return "someone looking for a place, not offering one"

    if LOOKING_FOR == "rent":
        if item.is_rental is False:
            return "for sale, you're renting"
        price, ceiling, label = item.price, MAX_RENT_THB, "rent"
    else:
        if item.is_rental is True:
            return "for rent, you're buying"
        price, ceiling, label = item.sale_price, MAX_BUY_THB, "price"

    if price is None:
        return f"no {label} stated"
    if price > ceiling:
        return f"over budget: {price:,.0f} THB"
    # Under ~5k/month in these groups means bait, a roommate share, or a daily rate.
    if LOOKING_FOR == "rent" and price < 5_000:
        return f"implausibly cheap, probably not a real listing: {price:,.0f}"

    if item.bedrooms is not None and item.bedrooms < MIN_BEDROOMS:
        return f"too few bedrooms: {item.bedrooms:g}"
    if item.size_sqm is not None and item.size_sqm < MIN_SIZE_SQM:
        return f"too small: {item.size_sqm:g} sqm"

    if item.soi and item.soi in EXCLUDED_SOIS:
        return f"excluded location: Sukhumvit {item.soi}"

    if not location_score(item):
        return f"outside your areas: {item.area or item.nearest_station or 'no location stated'}"

    return None


def location_score(item: Item) -> float:
    """0-5. A specific soi beats a neighbourhood name, which beats a station."""
    if item.soi:
        if item.soi in PREMIUM_SOIS:
            return 5.0
        if item.soi in GOOD_SOIS:
            return 3.0
        if item.soi in OK_SOIS:
            return 1.5

    area = (item.area or "").lower()
    if area in PREMIUM_AREAS:
        return 4.0
    if area in GOOD_AREAS:
        return 2.5
    if area in OK_AREAS:
        return 1.0

    # No area named — fall back to the station. Plenty of good posts give the
    # station and never name the neighbourhood, and extraction is not
    # deterministic: the same post can yield area="nana" on one run and null on
    # the next. Scoring both fields off the same lists keeps that from changing
    # the verdict.
    station = (item.nearest_station or "").lower()
    if station and (item.walk_minutes is None or item.walk_minutes <= 10):
        for alias, canonical in STATION_ALIASES.items():
            if alias in station:
                station = f"{station} {canonical}"
                break
        if any(a in station for a in PREMIUM_AREAS | GOOD_AREAS | OK_AREAS):
            # Weaker evidence than a stated neighbourhood, so it caps at 1.0.
            return 1.0

    return 0.0


def score(item: Item) -> tuple[float, list[str]]:
    """Total points plus a human-readable breakdown.

    Weights are the interesting part — tune these, not the code. Ask your agent
    for changes in plain language and re-run `watcher rematch`.
    """
    points = location_score(item)
    parts = [f"location {points:g}"]

    def add(value: float, label: str) -> None:
        nonlocal points
        points += value
        parts.append(f"{label} +{value:g}")

    # Quality of life — what makes a place liveable rather than just available.
    if item.is_quiet:
        add(1.0, "quiet")
    if item.has_natural_light:
        add(0.5, "light")
    if item.has_balcony:
        add(0.5, "balcony")
    if item.has_good_kitchen:
        add(0.5, "kitchen")
    if item.has_good_view:
        add(0.5, "view")
    if item.no_flooding:
        add(0.5, "no flooding")
    if item.near_convenience_store:
        add(0.5, "near 7-11")

    # Amenities.
    if item.has_pool:
        add(0.5, "pool")
    if item.has_gym:
        add(0.5, "gym")
    if item.has_oven:
        add(0.5, "oven")
    if item.has_dishwasher:
        add(0.5, "dishwasher")
    if item.has_parking:
        add(0.5, "parking")
    if item.pets_allowed:
        add(0.5, "pets")

    # Size, above the minimum.
    if item.size_sqm:
        if item.size_sqm >= 100:
            add(1.0, "100+ sqm")
        elif item.size_sqm >= 80:
            add(0.5, "80+ sqm")

    # Dealing with the owner directly is worth real money in agent fees.
    if item.is_owner:
        add(1.0, "owner")

    return round(points, 2), parts


def priority(points: float) -> str:
    if points >= 7:
        return "HOT"
    if points >= 4:
        return "good"
    return "maybe"


def matches(item: Item) -> tuple[bool, str]:
    rejection = hard_requirements(item)
    if rejection:
        return False, rejection

    points, parts = score(item)
    if points < ALERT_THRESHOLD:
        return False, f"scored {points:g}, below threshold {ALERT_THRESHOLD:g}"

    price = item.price if LOOKING_FOR == "rent" else item.sale_price
    where = item.area or item.nearest_station or "?"
    if item.soi:
        where = f"Sukhumvit {item.soi}"
    return True, f"[{priority(points)} {points:g}] {price:,.0f} THB · {where} · {', '.join(parts)}"


# ── The alert ────────────────────────────────────────────────────────────────

def notify_text(item: Item, post_url: str, raw_text: str) -> str:
    points, parts = score(item)
    price = item.price if LOOKING_FOR == "rent" else item.sale_price
    suffix = "/mo" if LOOKING_FOR == "rent" else ""

    head = [f"{price:,.0f} THB{suffix}" if price else "no price"]
    if item.bedrooms is not None:
        head.append(f"{item.bedrooms:g}BR")
    if item.bathrooms is not None:
        head.append(f"{item.bathrooms:g}BA")
    if item.size_sqm:
        head.append(f"{item.size_sqm:g}sqm")

    lines = [f"{priority(points)} · {points:g} pts", " · ".join(head)]

    where = [x for x in (item.project_name, item.area) if x]
    if item.soi:
        where.append(f"Sukhumvit {item.soi}")
    if item.floor:
        where.append(f"floor {item.floor}")
    if where:
        lines.append(", ".join(where))

    if item.nearest_station:
        walk = f", {item.walk_minutes:g} min walk" if item.walk_minutes else ""
        lines.append(f"{item.nearest_station}{walk}")

    if item.is_owner:
        lines.append("posted by owner — no agent fee")

    flags = [
        label for value, label in (
            (item.is_quiet, "quiet"), (item.has_natural_light, "good light"),
            (item.has_balcony, "balcony"), (item.has_good_kitchen, "good kitchen"),
            (item.has_good_view, "view"), (item.no_flooding, "no flooding"),
            (item.has_parking, "parking"), (item.has_pool, "pool"),
            (item.has_gym, "gym"), (item.pets_allowed, "pets ok"),
            (item.furnished, "furnished"),
        ) if value
    ]
    if flags:
        lines.append(", ".join(flags))

    if item.available_from:
        lines.append(f"available {item.available_from}")
    if item.summary:
        lines.append(item.summary)
    if item.contact:
        lines.append(f"contact: {item.contact}")

    lines.append(" · ".join(parts))
    lines.append(post_url)
    return "\n".join(lines)
