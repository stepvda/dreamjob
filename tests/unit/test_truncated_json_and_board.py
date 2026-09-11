"""Recovering a truncated model answer, and not losing a write to a unique key.

Two failures kept company profiles empty, and neither announced itself:
the answer was cut off mid-JSON so the parse raised, and the write that would
have saved it collided with the unique ATS board key and rolled back. Both are
pinned here.
"""

from __future__ import annotations

from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.llm.client import salvage_truncated_json


class TestSalvageTruncatedJson:
    def test_the_fields_that_arrived_are_kept(self) -> None:
        """The real shape: a complete summary, cut off later."""
        truncated = (
            '{"business_summary": {"text": "A Belgian software company."}, '
            '"sector_codes": ["62.010", "62.020"], '
            '"values_culture": [{"cue": "Know'
        )
        out = salvage_truncated_json(truncated)
        assert out["business_summary"] == {"text": "A Belgian software company."}
        assert out["sector_codes"] == ["62.010", "62.020"]
        # The half-written field is not guessed at.
        assert "values_culture" not in out

    def test_a_complete_document_is_untouched(self) -> None:
        assert salvage_truncated_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}

    def test_a_comma_inside_a_string_is_not_a_separator(self) -> None:
        out = salvage_truncated_json('{"a": "x, y { z", "b": 2, "c": "unfin')
        assert out == {"a": "x, y { z", "b": 2}

    def test_nothing_usable_returns_none(self) -> None:
        assert salvage_truncated_json("no json here") is None
        assert salvage_truncated_json("") is None
        assert salvage_truncated_json("{") is None

    def test_it_never_invents_a_value(self) -> None:
        """A truncated first value leaves nothing to recover, not a guess."""
        assert salvage_truncated_json('{"business_summary": "A Belgian sof') is None


class TestBoardCollision:
    def _company(self, name: str, *, vendor=None, slug=None) -> str:
        return insert_row(
            "company",
            {
                "normalised_name": name.lower(),
                "name": name,
                "ats_vendor": vendor,
                "ats_slug": slug,
                "collected_at": utcnow(),
            },
        )

    def test_the_holder_is_found(self) -> None:
        holder = self._company("Board holder", vendor="recruitee", slug="acme")
        assert kb_repo.company_id_for_board("recruitee", "acme") == holder

    def test_the_holder_excludes_the_company_asking(self) -> None:
        """A company reflecting on its own board is not a collision."""
        mine = self._company("Mine", vendor="lever", slug="mine")
        assert kb_repo.company_id_for_board("lever", "mine", exclude=mine) is None

    def test_two_companies_cannot_claim_one_board(self) -> None:
        a = self._company("First", vendor="greenhouse", slug="shared")
        b = self._company("Second")
        assert kb_repo.company_id_for_board("greenhouse", "shared", exclude=b) == a

    def test_no_board_means_no_holder(self) -> None:
        assert kb_repo.company_id_for_board(None, "acme") is None
        assert kb_repo.company_id_for_board("recruitee", None) is None
