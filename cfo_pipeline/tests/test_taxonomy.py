"""Niche taxonomy — hierarchy integrity + rollup."""

from __future__ import annotations

from cfo_pipeline import taxonomy


def test_every_niche_maps_to_exactly_one_parent():
    for niche in taxonomy.ALL_NICHES:
        parent = taxonomy.parent_of(niche)
        assert parent in taxonomy.PARENTS


def test_parent_children_covers_all_niches_without_dupes():
    flat = [n for kids in taxonomy.PARENT_CHILDREN.values() for n in kids]
    assert sorted(flat) == sorted(taxonomy.ALL_NICHES)
    assert len(flat) == len(set(flat))  # no niche under two parents


def test_niche_enum_matches_the_map():
    assert {n.value for n in taxonomy.Niche} == set(taxonomy.ALL_NICHES)


def test_rollup_examples():
    assert taxonomy.parent_of("b2b_saas") == "software_saas"
    assert taxonomy.parent_of("ai_ml") == "software_saas"
    assert taxonomy.parent_of("dental") == "healthcare"
    assert taxonomy.parent_of("restaurant") == "hospitality_food"
    assert taxonomy.parent_of("trucking_freight") == "logistics_transport"


def test_unknown_and_none_fall_back():
    assert taxonomy.parent_of(None) == "unknown"
    assert taxonomy.parent_of("") == "unknown"
    assert taxonomy.parent_of("not_a_real_niche") == "unknown"
    assert taxonomy.parent_of("unknown") == "unknown"


def test_broad_coverage():
    # Sanity: the taxonomy is genuinely broad (per the product ask).
    assert len(taxonomy.ALL_NICHES) >= 60
    assert len(taxonomy.PARENTS) >= 15
