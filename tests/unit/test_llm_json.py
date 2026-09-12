"""Malformed model output, repaired without weakening validation (NFR-205).

Models wrap JSON in prose or fences, leave trailing commas, emit smart quotes,
put raw newlines inside strings, add ``//``/``#`` comments, or use single
quotes.  Each of those used to end in "Could not parse JSON from LLM response
for task ...", which the callers then read as a failed task and degraded around.
Each is recovered here, while a genuinely invalid answer still raises the same
task error, and a cut-off answer is still reported as truncated so the fallback
model (or the structural assembly) is reached rather than silently skipped.
"""

from __future__ import annotations

import json
import logging

import pytest
from dreamjob.llm.client import (
    REPAIR_INSTRUCTION,
    LLMClient,
    LLMError,
    LLMResult,
    TruncatedResponse,
    Usage,
    parse_json,
    repair_json,
    salvage_truncated_json,
)

COMPANY = {"company": "Acme", "sector": "software"}

CASES: list[tuple[str, str, object]] = [
    (
        "prose_wrapped_object",
        'Here is the extraction:\n{"company": "Acme", "sector": "software"}\nHope that helps.',
        COMPANY,
    ),
    (
        "prose_wrapped_array",
        'The facts are:\n[{"statement": "Led the data team"}]\nEnd of answer.',
        [{"statement": "Led the data team"}],
    ),
    (
        "fenced_object",
        '```json\n{"company": "Acme", "sector": "software"}\n```',
        COMPANY,
    ),
    ("fenced_array_without_language", "```\n[1, 2, 3]\n```", [1, 2, 3]),
    (
        "trailing_commas",
        '{"company": "Acme", "sizes": [1, 2,],}',
        {"company": "Acme", "sizes": [1, 2]},
    ),
    (
        "smart_quotes",
        "{\u201ccompany\u201d: \u201cAcme\u201d, \u201csector\u201d: \u201csoftware\u201d}",
        COMPANY,
    ),
    (
        "embedded_newlines_and_tabs",
        '{"summary": "line one\nline two\twith a tab", "ok": true}',
        {"summary": "line one\nline two\twith a tab", "ok": True},
    ),
    (
        "line_and_block_comments",
        '{\n  // the employer this extraction is about\n  "company": "Acme", // its name\n'
        '  # the sector\n  "sector": "software" /* trailing block comment */\n}',
        COMPANY,
    ),
    (
        "single_quoted_keys_and_strings",
        "{'company': 'Acme', 'sizes': [1, 2]}",
        {"company": "Acme", "sizes": [1, 2]},
    ),
    (
        "single_quotes_with_a_protected_url",
        "{'source': 'https://example.test/jobs', 'ok': true}",
        {"source": "https://example.test/jobs", "ok": True},
    ),
]


@pytest.mark.parametrize("name,text,expected", CASES, ids=[case[0] for case in CASES])
def test_malformed_answers_are_recovered(name: str, text: str, expected: object) -> None:
    assert parse_json(text, task="extract.company") == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"company": "Acme", "sizes": [1, 2,],}', {"company": "Acme", "sizes": [1, 2]}),
        ("{'company': 'Acme'}", {"company": "Acme"}),
        ('{"a": 1 // note\n}', {"a": 1}),
    ],
)
def test_repair_json_output_parses(text: str, expected: object) -> None:
    assert json.loads(repair_json(text)) == expected


def test_a_curly_quotation_inside_a_straight_quoted_string_survives() -> None:
    """A repair must not corrupt the content it is repairing."""
    text = '{"quote": "il a dit \u201cbonjour\u201d", "n": 1,}'
    assert parse_json(text) == {"quote": "il a dit \u201cbonjour\u201d", "n": 1}


# ---------------------------------------------------------------------------
# Truncated answers: recovered as partial, still reported as truncated
# ---------------------------------------------------------------------------


def test_a_truncated_object_is_recovered_as_partial() -> None:
    with pytest.raises(TruncatedResponse) as raised:
        parse_json('{"company": "Acme", "skills": ["Python", "SQL"', task="extract.company")
    assert raised.value.partial == {"company": "Acme", "skills": ["Python", "SQL"]}
    assert salvage_truncated_json(
        '{"company": "Acme", "skills": ["Python", "SQL"'
    ) == {"company": "Acme", "skills": ["Python", "SQL"]}


def test_a_truncated_array_is_recovered_as_partial() -> None:
    text = '[{"id": 1}, {"id": 2}, {"id": 3'
    with pytest.raises(TruncatedResponse) as raised:
        parse_json(text, task="extract.table")
    assert raised.value.partial == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert salvage_truncated_json(text) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_a_truncated_document_after_prose_is_still_recovered() -> None:
    """A prose brace is not the document; the truncation is still reported."""
    with pytest.raises(TruncatedResponse) as raised:
        parse_json("Here is the table {as requested}: [1, 2", task="extract.table")
    assert raised.value.partial == [1, 2]


def test_a_truncated_value_is_dropped_rather_than_guessed() -> None:
    """The complete pair survives; the half-written one is dropped."""
    out = salvage_truncated_json('{"company": "Acme", "notes": "A Belgian sof')
    assert out == {"company": "Acme"}
    assert "notes" not in out
    # Nothing complete arrived at all, so nothing is invented.
    assert salvage_truncated_json('{"company": "A Belgian sof') is None


# ---------------------------------------------------------------------------
# Genuinely invalid answers keep failing, with a redacted snippet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "this is not JSON at all",
        "{company: Acme, sector: software}",
        "not even {a brace",
    ],
)
def test_genuinely_invalid_input_still_raises_the_task_error(bad: str) -> None:
    with pytest.raises(LLMError) as raised:
        parse_json(bad, task="extract.company")
    assert not isinstance(raised.value, TruncatedResponse)
    assert "Could not parse JSON from LLM response for task 'extract.company'" in str(raised.value)


def test_the_final_failure_logs_a_redacted_snippet(caplog: pytest.LogCaptureFixture) -> None:
    answer = "sorry, I cannot. Reach me at jane.doe@example.test or +32 470 12 34 56"
    client = _StubClient([(answer, False)])
    with (
        caplog.at_level(logging.WARNING, logger="dreamjob.llm.client"),
        pytest.raises(LLMError),
    ):
        client.complete_json("extract.contact", "sys", "user")
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "jane.doe@example.test" not in logged
    assert "[email]" in logged


# ---------------------------------------------------------------------------
# The truncation retry: one smaller ask, then the caller's fallback model
# ---------------------------------------------------------------------------


class _StubClient(LLMClient):
    """Answers ``complete`` from a script, without settings, budget or network."""

    def __init__(self, answers: list[tuple[str, bool]]) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, task: str, system: str, user: str, **kw: object) -> LLMResult:
        self.calls.append({"task": task, "system": system, "user": user, **kw})
        text, truncated = self.answers.pop(0)
        return LLMResult(
            text=text,
            usage=Usage(truncated=truncated),
            model="deepseek-reasoner",
            provider="deepseek",
            raw={},
        )


def test_a_truncated_answer_is_repaired_with_one_smaller_ask() -> None:
    client = _StubClient(
        [
            ('{"company": "Acme", "skills": ["Python"', True),
            ('{"company": "Acme", "skills": ["Python", "SQL"]}', False),
        ]
    )
    result = client.complete_json("extract.company", "sys", "user", max_tokens=8000)

    assert result == {"company": "Acme", "skills": ["Python", "SQL"]}
    assert len(client.calls) == 2
    repair = client.calls[1]
    assert REPAIR_INSTRUCTION in repair["system"]
    # The cut-off answer is data for the repair ask, not instruction text.
    assert ("previous_answer" in (repair.get("untrusted") or {}))


def test_the_repair_ask_is_tried_once_and_then_reported_as_truncated() -> None:
    client = _StubClient(
        [
            ('{"company": "Acme", "skills": ["Python"', True),
            ('{"company": "Acme", "skills": ["Python"', True),
            ('{"company": "Acme"}', False),
        ]
    )
    with pytest.raises(TruncatedResponse) as raised:
        client.complete_json("extract.company", "sys", "user", max_tokens=8000)
    assert len(client.calls) == 2
    assert "8000" in str(raised.value)


def test_a_non_truncated_unparseable_answer_is_not_retried() -> None:
    client = _StubClient([("I cannot answer that.", False)])
    with pytest.raises(LLMError) as raised:
        client.complete_json("extract.company", "sys", "user")
    assert not isinstance(raised.value, TruncatedResponse)
    assert len(client.calls) == 1


def test_a_client_that_cannot_repair_falls_back_to_the_other_model() -> None:
    """``documents/_llm`` is the fallback path the truncation error must reach."""
    from dreamjob.documents._llm import complete_json as document_complete_json

    class _FallbackStub:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def complete_json(self, task: str, system: str, user: str, **kw: object) -> object:
            self.calls.append(dict(kw))
            if kw.get("prefer_strong") is False:
                return {"company": "Acme"}
            raise TruncatedResponse("cut off at its budget and returned incomplete JSON")

    stub = _FallbackStub()
    result = document_complete_json(stub, "extract.company", "sys", "user")

    assert result == {"company": "Acme"}
    assert len(stub.calls) == 2
    assert stub.calls[0].get("prefer_strong") is None
    assert stub.calls[1].get("prefer_strong") is False
