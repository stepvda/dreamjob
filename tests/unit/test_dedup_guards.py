"""FR-184 de-duplication guards: a merge must be earned, not assumed.

These tests exist because the previous settings were *plausible* and wrong, and
wrongness in a de-duplicator is invisible: every discarded opening looks like a
successful merge, the run reports `done`, and the corpus is quietly 18%
smaller.  Measurements on real boards and the resulting decisions are in
docs/Data_Gathering_Plan.md (section 3 step 9, section 5 C4 and N6):

* the fuzzy threshold was 0.86, which merged 18% of genuinely distinct openings
  on real boards; 0.92 retains 95.5%;
* "Data Engineer" in Brussels and "Data Engineer" in Ghent merged at 0.873 on
  the boards the plan measured - two stated, different places now veto a merge
  outright, so no agreement on title, employer and date can outvote them, and
  no later re-weighting of those three can bring the merge back;
* a differing number was noise: "Support Engineer (Level 6)" and "(Level 7)"
  scored a perfect 1.000, and "Magazijnier 100234" and "Magazijnier 100567"
  shared a deterministic key;
* an unknown location scored 0.6, which carried a pair over the old threshold
  on its own: "Data Engineer" in Brussels against "Data Engineer" with no
  stated place scored 0.900 and merged.

The centrepiece is ``test_a_board_of_227_openings_stays_227_rows``: a board of
227 reference-numbered postings was written as **six** rows, one per role name
(retention 2.6%, measured against this module before the change), because the
deterministic key threw every reference away as if it were a postcode.  Six
rows out of 227 was reported as a clean collection.
"""

from __future__ import annotations

import pytest
from dreamjob.pipeline import dedup

# ---------------------------------------------------------------------------
# A board that the previous rules destroyed
# ---------------------------------------------------------------------------

EMPLOYER = "Zephyr Logistics NV"
POSTED = "2026-09-01"

# Six role families, each opening carrying its own reference number the way a
# staffing-heavy board writes them.  The formats differ per family so that the
# bracketed reference is exercised too; in every one of them the reference is
# the only thing that tells two openings apart.
_FAMILIES: tuple[tuple[str, str, int], ...] = (
    ("Magazijnier", "{role} {ref}", 40),
    ("Chauffeur CE", "{role} ({ref})", 40),
    ("Heftruckchauffeur", "{role} - {ref}", 38),
    ("Dispatcher", "{role} | {ref}", 37),
    ("Onderhoudstechnicus", "{role} {ref}", 36),
    ("Ploegverantwoordelijke", "{role} ({ref})", 36),
)


def board_of_227() -> list[dict]:
    """227 distinct openings at one employer, one per reference number."""
    rows: list[dict] = []
    for family, (role, fmt, count) in enumerate(_FAMILIES):
        for index in range(count):
            reference = 100_000 + 1_000 * family + index
            rows.append(
                {
                    "title": fmt.format(role=role, ref=reference),
                    "company_name_raw": EMPLOYER,
                    "location": None,  # this feed states the employer, not the city
                    "posted_at": POSTED,
                }
            )
    return rows


def collect(rows: list[dict]) -> list[dict]:
    """The writer's de-duplication over one board, in arrival order.

    Mirrors ``KnowledgeBaseWriter._write_vacancy``: assign the deterministic
    key, look that key up, and only on a miss compare fuzzily against the
    employer's most recent rows - ``knowledge.vacancy_candidates`` returns 50.
    Kept here rather than driven through the database so that a failure names
    the de-duplication rule that changed and nothing else.
    """
    kept: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        record = dict(row)
        key = dedup.assign_dedup_key(record)
        if key in seen:
            continue
        if dedup.best_vacancy_match(record, kept[-50:])[0] is not None:
            continue
        kept.append(record)
        seen[key] = record
    return kept


def test_a_board_of_227_openings_stays_227_rows() -> None:
    """The regression: 227 postings used to be written as 6 rows.

    ``normalise_title`` stripped every 4-6 digit group as a postcode, so all 40
    "Magazijnier 1000xx" openings normalised to "magazijnier", keyed
    identically, and merged on the deterministic path - before any threshold,
    any score, or any log line could show it happening.  A bracketed reference
    ("Chauffeur CE (101000)") was discarded twice over, by the bracket rule and
    by the postcode rule.
    """
    rows = board_of_227()
    assert len(rows) == 227

    keys = {
        dedup.vacancy_dedup_key(r["title"], r["company_name_raw"], r["location"], r["posted_at"])
        for r in rows
    }
    assert len(keys) == 227, "each reference is a different opening (this was 6)"

    kept = collect(rows)
    retention = len(kept) / len(rows)
    assert retention >= 0.95, f"retention {retention:.1%}, measured at 2.6% before the fix"
    assert len(kept) == 227


def test_re_collecting_that_board_adds_nothing() -> None:
    """The fix must not turn re-collection into duplication (FR-184)."""
    rows = board_of_227()
    assert len(collect(rows + rows)) == 227


# ---------------------------------------------------------------------------
# N6 guard 1 - two stated places are two openings
# ---------------------------------------------------------------------------


def test_two_cities_are_never_one_opening() -> None:
    base = {"title": "Data Engineer", "company_name_raw": "Acme NV", "posted_at": POSTED}
    brussels, ghent = dict(base, location="Brussels"), dict(base, location="Ghent")

    assert dedup.locations_conflict("Brussels", "Ghent")
    # A veto, not a low score.  Title, employer and date agree perfectly here,
    # which is worth 0.75 before the location is even read; this pair merged at
    # 0.873 on the boards the plan measured, and a veto cannot be out-weighted
    # by a later re-weighting of the other three signals.
    assert dedup.vacancy_similarity(brussels, ghent) == 0.0
    assert not dedup.is_duplicate_vacancy(brussels, ghent)


def test_the_same_place_said_twice_is_not_a_conflict() -> None:
    """One side adding the country or the postcode is not a second location."""
    base = {"title": "Data Engineer", "company_name_raw": "Acme NV", "posted_at": POSTED}
    assert not dedup.locations_conflict("Gent, Belgium", "9000 Gent")
    assert dedup.is_duplicate_vacancy(
        dict(base, location="Gent, Belgium"), dict(base, location="9000 Gent")
    )
    # An unstated location claims nothing, so it cannot conflict with anything.
    assert not dedup.locations_conflict(None, "Gent")
    assert not dedup.locations_conflict("", "Gent")


# ---------------------------------------------------------------------------
# N6 guard 2 - a differing number is a level or a reference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Support Engineer 6", "Support Engineer 7"),
        ("Support Engineer (Level 6)", "Support Engineer (Level 7)"),  # scored 1.000
        ("Verpleegkundige (shift 2)", "Verpleegkundige (shift 3)"),    # scored 1.000
        ("Magazijnier 100234", "Magazijnier 100567"),                  # one shared key
        ("Technicien - 25871", "Technicien - 25872"),                  # one shared key
        ("Account Manager VII", "Account Manager VIII"),   # roman levels past v
    ],
)
def test_a_differing_number_is_two_openings(left: str, right: str) -> None:
    base = {"company_name_raw": "Acme NV", "location": "Gent", "posted_at": POSTED}
    a, b = dict(base, title=left), dict(base, title=right)

    assert dedup.normalise_title(left) != dedup.normalise_title(right), "the key must see it"
    assert not dedup.is_duplicate_vacancy(a, b)


def test_a_number_that_states_a_workload_or_a_place_is_still_noise() -> None:
    """The numeric guard must not split a posting on "(80%)" or a postcode."""
    base = {"company_name_raw": "Acme NV", "location": "Gent", "posted_at": POSTED}
    plain = dict(base, title="Verpleegkundige")
    for variant in ("Verpleegkundige (80%)", "Verpleegkundige (4/5)", "Verpleegkundige (m/v)",
                    "Verpleegkundige 0,8 FTE", "Verpleegkundige 9000 Gent"):
        assert dedup.is_duplicate_vacancy(dict(base, title=variant), plain), variant


# ---------------------------------------------------------------------------
# N6 guard 3 - an unknown location decides nothing
# ---------------------------------------------------------------------------


def test_an_unknown_location_cannot_carry_a_merge() -> None:
    """0.45 title + 0.25 employer + 0.05 recency + 0.25 x 0.5 = 0.875 < 0.92."""
    base = {"title": "Data Engineer", "company_name_raw": "Acme NV", "posted_at": POSTED}
    stated, unstated = dict(base, location="Brussels"), dict(base, location=None)

    assert dedup.location_similarity(None, "Brussels") == dedup.UNKNOWN_LOCATION_SIMILARITY
    score = dedup.vacancy_similarity(stated, unstated)
    assert score == pytest.approx(0.875), "everything else about this pair agrees perfectly"
    assert dedup.VACANCY_MATCH_THRESHOLD == 0.92
    assert score < dedup.VACANCY_MATCH_THRESHOLD, "this used to score 0.90 against 0.86"
    assert not dedup.is_duplicate_vacancy(stated, unstated)


# ---------------------------------------------------------------------------
# What must keep merging
# ---------------------------------------------------------------------------


def test_a_repost_across_a_week_boundary_still_merges() -> None:
    """The deterministic key changes every week, so the fuzzy path still has work."""
    base = {"title": "Data Engineer", "company_name_raw": "Acme NV", "location": "Gent"}
    first = dict(base, posted_at="2026-09-01")
    repost = dict(base, posted_at="2026-09-09")

    assert dedup.assign_dedup_key(dict(first)) != dedup.assign_dedup_key(dict(repost))
    assert dedup.is_duplicate_vacancy(first, repost)


def test_a_board_that_appends_the_city_still_merges() -> None:
    base = {"company_name_raw": "Acme NV", "location": "Gent", "posted_at": POSTED}
    assert dedup.is_duplicate_vacancy(
        dict(base, title="Data Engineer"), dict(base, title="Data Engineer - Gent")
    )
