import pytest

from paper_collector import sources
from paper_collector.models import Paper
from paper_collector.sources import (
    SemanticScholarEnricher,
    _abstract_from_inverted,
    _stable_id,
)


def test_openalex_inverted_abstract_is_reconstructed():
    assert _abstract_from_inverted({"world": [1], "hello": [0, 2]}) == "hello world hello"


def test_title_fallback_id_is_stable():
    assert _stable_id("Hello, World!") == _stable_id("hello world")


def test_doi_is_canonicalized():
    assert _stable_id("ignored", "https://doi.org/10.1/ABC") == "doi:10.1/abc"


def test_semantic_scholar_requests_are_limited_to_one_per_second(monkeypatch):
    clock = iter([10.0, 10.25])
    sleeps: list[float] = []
    requests: list[str] = []

    monkeypatch.setattr(sources.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sources.time, "sleep", sleeps.append)

    def fake_request(url: str, **_kwargs):
        requests.append(url)
        return {"citationCount": 0, "influentialCitationCount": 0}

    monkeypatch.setattr(sources, "_request_json", fake_request)
    enricher = SemanticScholarEnricher("test-key")

    for suffix in ("first", "second"):
        enricher.enrich(
            Paper(
                id=f"doi:10.1/{suffix}",
                doi=f"10.1/{suffix}",
                title=suffix,
                abstract="",
                authors=[],
                publication_date="2026-01-01",
                year=2026,
            )
        )

    assert len(requests) == 2
    assert sleeps == [pytest.approx(0.75)]


def test_semantic_scholar_stops_when_daily_quota_is_exhausted(monkeypatch):
    requests: list[str] = []
    monkeypatch.setattr(
        sources,
        "_request_json",
        lambda url, **_kwargs: requests.append(url) or {"citationCount": 0},
    )
    enricher = SemanticScholarEnricher("test-key", daily_request_limit=1)

    for suffix in ("first", "second"):
        enricher.enrich(
            Paper(
                id=f"doi:10.1/{suffix}",
                doi=f"10.1/{suffix}",
                title=suffix,
                abstract="",
                authors=[],
                publication_date="2026-01-01",
                year=2026,
            )
        )

    assert len(requests) == 1
