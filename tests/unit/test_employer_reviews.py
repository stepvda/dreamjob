"""Employer-review signals from a company's own pages (FR-265, FR-384).

The consuming path existed but was unfed.  These tests pin the schema.org
extraction and that a stored page produces an ``employer_review_summary`` row
on the permitted source, not a prohibited one.
"""

from __future__ import annotations

from dreamjob.config import get_settings
from dreamjob.db.repositories import companies as company_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.pipeline import employer_reviews

HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme",
 "aggregateRating":{"@type":"AggregateRating","ratingValue":"4.2",
                    "reviewCount":"128","bestRating":"5"}}
</script>
</head><body>Careers at Acme</body></html>
"""


def test_extract_ratings_reads_schema_org() -> None:
    ratings = employer_reviews.extract_ratings(HTML)
    assert ratings
    assert ratings[0]["rating"] == 4.2
    assert ratings[0]["review_count"] == 128
    assert ratings[0]["scale"] == 5.0


def test_extract_ratings_ignores_malformed_blocks() -> None:
    assert employer_reviews.extract_ratings("<script type='application/ld+json'>{bad") == []
    assert employer_reviews.extract_ratings(None) == []


def test_a_stored_page_produces_a_review_summary(monkeypatch) -> None:
    root = get_settings().abs_data_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "review-page.html").write_text(HTML, encoding="utf-8")

    monkeypatch.setattr(
        company_repo,
        "crawled_pages",
        lambda company_id, limit=80: [
            {"storage_path": "review-page.html", "url": "https://acme.test/about"}
        ],
    )
    captured: dict = {}
    monkeypatch.setattr(opp_repo, "record_employer_review", lambda data: captured.update(data))

    result = employer_reviews.build_for_company("company-1")
    assert result["rating"] == 4.2
    assert captured["source"] == "company_site"
    assert captured["rating"] == 4.2
    # A self-published rating is a weak signal, and says so.
    assert captured["confidence"] < 0.5


def test_a_company_with_no_published_rating_records_nothing(monkeypatch) -> None:
    monkeypatch.setattr(company_repo, "crawled_pages", lambda company_id, limit=80: [])
    called = []
    monkeypatch.setattr(opp_repo, "record_employer_review", lambda data: called.append(data))
    result = employer_reviews.build_for_company("company-2")
    assert result["rating"] is None
    assert called == []
