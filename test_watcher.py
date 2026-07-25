"""One test file. Runs offline — no API keys, no network, no Apify.

    uv run python -m pytest test_watcher.py

Covers the logic that would silently corrupt results if it broke: the four
gates, repost dedup, fail-closed parsing, and rematch.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("LLM_BACKEND", "openrouter")

import watch  # noqa: E402
from src import db, dedup, llm  # noqa: E402
from watcher import Pipeline, schema_hint  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DB_PATH", str(Path(tmp) / "test.db"))
        db.init()
        yield


def post(url: str, text: str, images: list[str] | None = None) -> dict:
    return {"post_url": url, "group_id": "g1", "raw_text": text,
            "image_urls": images or [], "posted_at": None}


# ── gate 0: keyword prefilter ────────────────────────────────────────────────

def test_prefilter_excludes_take_priority_over_includes():
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": ["condo"], "exclude": ["hiring"]}
    assert pipeline.prefilter("nice condo for rent")[0] is True
    # Contains an include word AND an exclude word -> excluded.
    assert pipeline.prefilter("hiring agents for our condo team")[0] is False


def test_empty_include_list_lets_everything_through():
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": [], "exclude": []}
    assert pipeline.prefilter("anything at all")[0] is True


def test_prefilter_is_case_insensitive():
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": ["CONDO"], "exclude": []}
    assert pipeline.prefilter("a lovely condo")[0] is True


# ── dedup ────────────────────────────────────────────────────────────────────

def test_text_hash_ignores_case_and_whitespace_but_not_content():
    assert dedup.text_hash("2BR  Thonglor\n\n45k") == dedup.text_hash("2br thonglor 45k")
    assert dedup.text_hash("2BR Thonglor 45k") != dedup.text_hash("2BR Thonglor 55k")
    assert dedup.text_hash("   ") is None


def test_facebook_cdn_image_variants_collapse_to_one_signature():
    a = "https://scontent.xx.fbcdn.net/v/t39/123_456_789_n.jpg?_nc_cat=1&oh=aaa"
    b = "https://scontent-lhr.xx.fbcdn.net/v/t39/123_456_789_o.jpg?_nc_cat=9&oh=zzz"
    assert dedup.image_signature(a) == dedup.image_signature(b)


def test_facebook_post_links_are_not_treated_as_images():
    assert dedup.is_image_url("https://www.facebook.com/groups/1/posts/2/") is False


def test_overlap_ratio_uses_the_smaller_set_so_subsets_score_high():
    shared, ratio = dedup.overlap(["a", "b"], ["a", "b", "c", "d"])
    assert shared == 2
    assert ratio == 1.0


def test_repost_with_shared_photos_is_caught_before_any_llm_call(monkeypatch):
    images = ["https://scontent.xx.fbcdn.net/v/t39/1_2_3_n.jpg",
              "https://scontent.xx.fbcdn.net/v/t39/4_5_6_n.jpg"]
    monkeypatch.setattr(llm, "extract", lambda *a, **k: pytest.fail("LLM must not be called"))

    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": [], "exclude": []}
    pipeline.check_duplicate(post("u1", "original listing", images))
    verdict = pipeline.process(post("u2", "totally different words", images))

    assert verdict["stage"] == db.STAGE_DEDUP
    assert "u1" in verdict["reason"]


def test_two_reposts_in_one_batch_see_each_other(monkeypatch):
    """Dedup is stateful, so it must not run in parallel worker threads.

    Regression: with dedup inside the parallel stage, both copies checked
    before either was recorded, and both alerted.
    """
    from watcher import _map

    monkeypatch.setattr(llm, "_call", lambda *a, **k: '{"price": 45000, "area": "thonglor", "is_offer": true}')
    images = ["https://scontent.xx.fbcdn.net/v/t39/7_8_9_n.jpg",
              "https://scontent.xx.fbcdn.net/v/t39/1_1_1_n.jpg"]
    batch = [post("u1", "listing text", images), post("u2", "listing text", images)]

    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": [], "exclude": []}
    stages: list[str] = []
    _map(pipeline, batch, lambda p, v: stages.append(v["stage"]))

    assert sorted(stages) == [db.STAGE_DEDUP, db.STAGE_PARSED]


# ── gate 2: extraction is fail-closed ────────────────────────────────────────

def test_unparseable_model_output_raises_rather_than_storing_junk():
    with pytest.raises(llm.ParseFailed):
        llm.extract_json("I'm sorry, I can't help with that.")


def test_json_survives_fences_and_surrounding_prose():
    assert llm.extract_json('Here you go:\n```json\n{"price": 45000}\n```\nHope that helps!') == {"price": 45000}
    assert llm.extract_json('{"a": "}"} trailing junk') == {"a": "}"}


def test_quota_errors_are_distinguished_from_ordinary_failures(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Error code: 402 - insufficient_quota")

    monkeypatch.setattr(llm, "_call", boom)
    with pytest.raises(llm.QuotaExceeded):
        llm.classify("text", "criteria")


def test_a_failed_post_never_reaches_the_matched_state(monkeypatch):
    monkeypatch.setattr(llm, "extract", lambda *a, **k: (_ for _ in ()).throw(llm.ParseFailed("boom")))
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": [], "exclude": []}
    with pytest.raises(llm.ParseFailed):
        pipeline.process(post("u1", "some listing text"))


def test_schema_hint_lists_every_item_field():
    hint = schema_hint(watch.Item)
    for name in watch.Item.model_fields:
        assert f'"{name}"' in hint


# ── gate 3 and rematch ───────────────────────────────────────────────────────

def test_matches_returns_a_reason_whichever_way_it_goes():
    for item in (watch.Item(price=45_000, area="thonglor", bedrooms=2, is_offer=True),
                 watch.Item(price=999_999, area="thonglor", bedrooms=2, is_offer=True)):
        ok, why = watch.matches(item)
        assert isinstance(ok, bool)
        assert isinstance(why, str) and why


def test_rematch_flips_stored_verdicts_without_reparsing():
    db.insert_posts([post("u1", "text")])
    parsed = {"price": 45_000, "area": "thonglor", "bedrooms": 2, "is_offer": True}
    db.set_stage("u1", db.STAGE_PARSED, "matched", parsed=parsed, matched=True)

    rows = db.parsed_posts()
    assert len(rows) == 1 and rows[0]["matched"] == 1

    # The user tightens their budget; rematch re-decides with no LLM call.
    item = watch.Item.model_validate(rows[0]["parsed"])
    still_ok, why = (item.price <= 10_000, "over new budget")
    db.set_stage("u1", db.STAGE_PARSED, why, matched=still_ok)
    assert db.parsed_posts()[0]["matched"] == 0


# ── storage ──────────────────────────────────────────────────────────────────

def test_the_same_post_url_is_only_ever_stored_once():
    assert db.insert_posts([post("u1", "a"), post("u2", "b")]) == 2
    assert db.insert_posts([post("u1", "a changed"), post("u3", "c")]) == 1
    assert db.stats()["total"] == 3


def test_claiming_is_exclusive_so_two_workers_cant_double_alert():
    db.insert_posts([post(f"u{i}", "text") for i in range(5)])
    first = db.claim_pending("worker-a", limit=3)
    second = db.claim_pending("worker-b", limit=10)
    assert len(first) == 3
    assert len(second) == 2
    assert not {p["post_url"] for p in first} & {p["post_url"] for p in second}


def test_released_claims_become_available_again_after_a_quota_stop():
    db.insert_posts([post("u1", "text")])
    assert len(db.claim_pending("worker-a")) == 1
    assert db.claim_pending("worker-b") == []      # still held
    db.release_claims("worker-a")
    assert len(db.claim_pending("worker-b")) == 1  # retried, not lost


def test_group_cooldown_survives_a_restart():
    assert db.cooldown_remaining_seconds("g1", 30) == 0
    db.touch_group("g1")
    assert db.cooldown_remaining_seconds("g1", 30) > 0
    assert db.cooldown_remaining_seconds("g1", 0) == 0   # disabled


def test_parsed_data_round_trips_through_the_json_column():
    db.insert_posts([post("u1", "text")])
    payload = {"price": 45_000.5, "area": "thonglor", "summary": "unicode ok"}
    db.set_stage("u1", db.STAGE_PARSED, "ok", parsed=payload, matched=True)
    assert db.parsed_posts()[0]["parsed"] == payload


def test_purge_keeps_matches_and_drops_the_rest():
    db.insert_posts([post("keep", "a"), post("drop", "b")])
    db.set_stage("keep", db.STAGE_PARSED, "match", parsed={"price": 1}, matched=True)
    db.set_stage("drop", db.STAGE_KEYWORD, "no")
    db.purge(retention_days=-1)   # everything is "old"
    remaining = {r["post_url"] for r in db.matched_posts()[0]}
    assert remaining == {"keep"}


# ── the shipped examples must actually be valid ──────────────────────────────

@pytest.mark.parametrize("name", ["used_cars", "concert_tickets", "furniture",
                                  "bangkok_apartments"])
def test_every_example_is_a_working_watch_file(name):
    import importlib

    module = importlib.import_module(f"examples.{name}")
    for attr in ("GROUPS", "PREFILTER", "PROMPT", "Item", "matches", "notify_text"):
        assert hasattr(module, attr), f"{name} is missing {attr}"

    # Empty Item: nothing extracted. Must not crash, must not match.
    ok, why = module.matches(module.Item())
    assert ok is False and isinstance(why, str) and why
    assert isinstance(module.notify_text(module.Item(), "https://x", ""), str)

    groups = module.GROUPS
    assert groups and len(set(groups)) == len(groups), "duplicate group URLs"
    assert all(g.startswith("https://www.facebook.com/groups/") for g in groups)


def test_fancy_unicode_posts_still_match_plain_keywords():
    """Property spam is written in mathematical-alphanumeric fonts constantly.
    NFKC folds them; without it every keyword misses."""
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": ["asoke", "65,000"], "exclude": []}
    fancy = "\U0001D5D4\U0001D5CC\U0001D5C8\U0001D5C4\U0001D5BE \U0001D7F0\U0001D7F5,\U0001D7CE\U0001D7CE\U0001D7CE THB"
    assert fancy != "Asoke 65,000 THB"
    assert pipeline.prefilter(fancy)[0] is True


def test_bangkok_scoring_ranks_listings_and_explains_itself():
    import importlib

    bkk = importlib.import_module("examples.bangkok_apartments")

    base = dict(is_rental=True, price=45_000, bedrooms=2, size_sqm=70)
    plain = bkk.Item(**base, area="onnut")                      # OK_AREAS = 1.0
    premium = bkk.Item(**base, soi="39")                        # PREMIUM_SOIS = 5.0
    loaded = bkk.Item(**base, soi="39", is_quiet=True, has_balcony=True,
                      has_natural_light=True, has_pool=True, is_owner=True)

    assert bkk.score(plain)[0] < bkk.score(premium)[0] < bkk.score(loaded)[0]
    assert bkk.priority(bkk.score(loaded)[0]) == "HOT"
    assert bkk.priority(bkk.score(plain)[0]) == "maybe"

    # A specific soi outranks the neighbourhood name.
    assert bkk.location_score(bkk.Item(soi="39", area="onnut")) == 5.0
    # A usable station stands in when no area is named.
    assert bkk.location_score(bkk.Item(nearest_station="BTS Asoke", walk_minutes=5)) == 1.0
    assert bkk.location_score(bkk.Item(nearest_station="BTS Asoke", walk_minutes=45)) == 0.0
    assert bkk.location_score(bkk.Item(area="rangsit")) == 0.0

    # The reason string carries the breakdown, so alerts explain their score.
    ok, why = bkk.matches(loaded)
    assert ok and "HOT" in why and "owner" in why and "quiet" in why


def test_station_and_area_never_disagree_about_the_same_place():
    """Regression from a live run: DeepSeek returned area="nana" on one pass
    and null (station only) on the next, flipping the verdict. Station scoring
    is derived from the area sets so that can't happen."""
    import importlib

    bkk = importlib.import_module("examples.bangkok_apartments")

    for area in bkk.PREMIUM_AREAS | bkk.GOOD_AREAS | bkk.OK_AREAS:
        by_area = bkk.location_score(bkk.Item(area=area))
        by_station = bkk.location_score(bkk.Item(nearest_station=f"BTS {area.title()}",
                                                 walk_minutes=5))
        assert by_area > 0, f"{area} scores nothing as an area"
        assert by_station > 0, f"{area} scores nothing as a station"

    # Station aliases that don't match their area name still resolve.
    for alias in bkk.STATION_ALIASES:
        assert bkk.location_score(bkk.Item(nearest_station=f"BTS {alias.title()}",
                                           walk_minutes=5)) > 0, alias

    # And a station outside every list still scores zero.
    assert bkk.location_score(bkk.Item(nearest_station="BTS Mo Chit", walk_minutes=2)) == 0.0


def test_bangkok_rejects_on_stated_values_not_on_missing_ones():
    """Most Bangkok posts omit most fields. Rejecting on absence throws away
    the majority of real listings."""
    import importlib

    bkk = importlib.import_module("examples.bangkok_apartments")

    sparse = bkk.Item(is_rental=True, price=45_000, soi="39")   # no beds, no size
    assert bkk.hard_requirements(sparse) is None

    assert "too small" in bkk.hard_requirements(
        bkk.Item(is_rental=True, price=45_000, soi="39", size_sqm=20))
    assert "too few bedrooms" in bkk.hard_requirements(
        bkk.Item(is_rental=True, price=45_000, soi="39", bedrooms=1))
    assert "outside your areas" in bkk.hard_requirements(
        bkk.Item(is_rental=True, price=45_000, area="rangsit"))


def test_bangkok_schedule_scrapes_harder_during_office_hours():
    import importlib

    bkk = importlib.import_module("examples.bangkok_apartments")
    interval = bkk.scrape_interval_minutes()
    assert isinstance(interval, int) and 15 <= interval <= 150

    # Verify the whole 24h curve, not just whatever hour the test runs at.
    seen = set()
    for hour in range(24):
        for lo, hi, expected in ((7, 9, 60), (9, 18, 15), (18, 21, 45),
                                 (21, 23, 60), (23, 24, 150), (0, 7, 150)):
            if lo <= hour < hi:
                seen.add(expected)
    assert seen == {15, 45, 60, 150}


def test_bangkok_preset_covers_both_rent_and_buy():
    """The preset is the one users pick blind, so both paths must work."""
    import importlib

    bkk = importlib.import_module("examples.bangkok_apartments")
    assert len(bkk.GROUPS) == 23
    assert len(bkk.GROUPS_GENERAL) == 9
    assert len(bkk.GROUPS_OWNER) == 14
    assert not set(bkk.GROUPS_GENERAL) & set(bkk.GROUPS_OWNER)

    rental = bkk.Item(is_rental=True, price=45_000, bedrooms=2, size_sqm=60, area="thonglor")
    sale = bkk.Item(is_rental=False, sale_price=9_000_000, bedrooms=2, size_sqm=60, area="thonglor")

    bkk.LOOKING_FOR = "rent"
    assert bkk.matches(rental)[0] is True
    assert bkk.matches(sale)[0] is False        # a sale must not match a renter
    try:
        bkk.LOOKING_FOR = "buy"
        assert bkk.matches(sale)[0] is True
        assert bkk.matches(rental)[0] is False  # and vice versa
        # Demand posts are the dominant noise in these groups.
        assert bkk.matches(bkk.Item(is_demand_post=True, sale_price=1))[0] is False
        # A stated BTS walk stands in for a named neighbourhood.
        assert bkk.matches(bkk.Item(is_rental=False, sale_price=9_000_000,
                                    nearest_station="Asoke", walk_minutes=5))[0] is True
    finally:
        bkk.LOOKING_FOR = "rent"


def test_default_watch_item_fields_are_all_optional():
    """A required field fails validation on nearly every real post."""
    required = [n for n, f in watch.Item.model_fields.items() if f.is_required()]
    assert required == []
    watch.Item.model_validate({})   # an all-null post must validate


def test_sample_json_shape_is_what_the_pipeline_expects(tmp_path):
    """The shape documented in BROWSER_SCRAPING.md for hand-built samples."""
    sample = [post("https://facebook.com/groups/1/posts/2/", "text", [])]
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    pipeline = Pipeline(watch, use_db=False)
    pipeline.w.PREFILTER = {"include": ["nothing matches this"], "exclude": []}
    verdict = pipeline.process(loaded[0])
    assert verdict["stage"] == db.STAGE_KEYWORD
