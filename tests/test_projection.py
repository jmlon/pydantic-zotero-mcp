"""Projection tests: the token budget is won or lost here (PRD 6, 9.2)."""

from __future__ import annotations

import json

from tests.fake_zotero import make_attachment, make_collection, make_item, make_note
from zotero_mcp import projection

#: Rough chars-per-token for English + JSON punctuation. Approximate on purpose: the
#: point is to catch an order-of-magnitude regression, not to be a tokenizer.
CHARS_PER_TOKEN = 4
TOKEN_BUDGET_25_ITEMS = 4_000


def test_summary_drops_api_noise():
    raw = make_item("AAAA1111", doi="10.1234/abc")
    summary = projection.item_summary(raw)
    dumped = summary.model_dump()

    assert "links" not in dumped
    assert "library" not in dumped
    assert "meta" not in dumped
    # Empty source fields must not survive as nulls.
    assert "callNumber" not in dumped
    assert all(value is not None for value in dumped.values())


def test_summary_collapses_creators_and_parses_year():
    raw = make_item("AAAA1111", date="2017-06-12")
    summary = projection.item_summary(raw)
    assert summary.creators == "Lovelace"
    assert summary.year == 2017
    assert isinstance(summary.creators, str)


def test_year_falls_back_to_regex_on_free_text_dates():
    raw = make_item("AAAA1111", date="Spring 2019")
    raw["meta"]["parsedDate"] = None
    assert projection.item_summary(raw).year == 2019


def test_year_is_none_when_undated():
    raw = make_item("AAAA1111", date="n.d.")
    raw["meta"]["parsedDate"] = None
    assert projection.item_summary(raw).year is None


def test_creator_collapse_without_zotero_summary():
    def summary_for(creators):
        raw = make_item("AAAA1111", creators=creators)
        raw["meta"]["creatorSummary"] = None
        return projection.item_summary(raw).creators

    one = [{"creatorType": "author", "firstName": "A", "lastName": "Smith"}]
    two = one + [{"creatorType": "author", "firstName": "B", "lastName": "Jones"}]
    three = two + [{"creatorType": "author", "firstName": "C", "lastName": "Wu"}]

    assert summary_for(one) == "A Smith"
    assert summary_for(two) == "A Smith and B Jones"
    assert summary_for(three) == "A Smith et al."


def test_doi_normalization_and_extraction():
    assert projection.normalize_doi("https://doi.org/10.1/ABC") == "10.1/abc"
    assert projection.normalize_doi("doi:10.1/abc") == "10.1/abc"
    assert projection.normalize_doi(None) is None

    from_extra = make_item("AAAA1111", doi=None, extra="DOI: 10.5555/xyz")
    assert projection.item_summary(from_extra).doi == "10.5555/xyz"


def test_collection_keys_become_names():
    raw = make_item("AAAA1111", collections=["COLL0001"])
    summary = projection.item_summary(raw, collection_names={"COLL0001": "NLP"})
    assert summary.collections == ["NLP"]


def test_has_fulltext_false_when_no_children():
    raw = make_item("AAAA1111", num_children=0)
    assert projection.item_summary(raw).has_fulltext is False


def test_has_fulltext_unknown_when_children_exist_but_unexamined():
    raw = make_item("AAAA1111", num_children=2)
    assert projection.item_summary(raw).has_fulltext is None


def test_has_fulltext_definitive_once_children_known():
    raw = make_item("AAAA1111", num_children=1)
    children = [projection.child_summary(make_attachment("ATCH1111"))]
    assert projection.item_detail(raw, children=children).has_fulltext is True

    linked = [
        projection.child_summary(
            make_attachment("ATCH2222", content_type="", link_mode="linked_url")
        )
    ]
    assert projection.item_detail(raw, children=linked).has_fulltext is False


def test_notes_are_stripped_to_plain_text():
    note = projection.note(
        make_note("NOTE1111", text="<p>Hello <b>world</b>&amp;co</p>")
    )
    assert "<" not in note.text
    assert "Hello" in note.text and "world" in note.text and "&co" in note.text


def test_collection_tree_nests_and_promotes_orphans():
    raw = [
        make_collection("COLL0001", "NLP"),
        make_collection("COLL0003", "Transformers", parent="COLL0001"),
        make_collection("COLL0009", "Orphan", parent="MISSING1"),
    ]
    nodes, total = projection.collection_tree(raw)
    assert total == 3
    by_name = {n.name: n for n in nodes}
    # Orphan is surfaced, not silently dropped.
    assert set(by_name) == {"NLP", "Orphan"}
    assert [c.name for c in by_name["NLP"].children] == ["Transformers"]


def test_twenty_five_item_page_stays_within_token_budget():
    raws = [
        make_item(
            f"KEY{index:05d}",
            title="A Reasonably Long Research Paper Title About Something",
            tags=["alpha", "beta", "gamma"],
            collections=["COLL0001"],
            doi=f"10.1234/example.{index}",
        )
        for index in range(25)
    ]
    summaries = [
        projection.item_summary(r, collection_names={"COLL0001": "NLP"}) for r in raws
    ]
    payload = json.dumps([s.model_dump(mode="json") for s in summaries])

    estimated_tokens = len(payload) / CHARS_PER_TOKEN
    assert estimated_tokens < TOKEN_BUDGET_25_ITEMS, (
        f"25 summaries ~= {estimated_tokens:.0f} tokens, over the "
        f"{TOKEN_BUDGET_25_ITEMS} budget"
    )

    raw_payload = json.dumps(raws)
    assert len(payload) < len(raw_payload) / 2, "projection should at least halve size"
