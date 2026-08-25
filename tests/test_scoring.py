from datetime import date, timedelta

from paper_collector.models import Author, Paper
from paper_collector.scoring import citation_score, score_paper, venue_score


def paper(**overrides) -> Paper:
    values = {
        "id": "doi:10.test/1",
        "title": "Retrieval Augmented Generation with Verifiable Grounding",
        "abstract": "We propose a novel retrieval augmented generation framework and improve accuracy by 12%.",
        "authors": [Author("A")],
        "publication_date": "2026-08-21",
        "year": 2026,
        "venue": "Workshop",
    }
    values.update(overrides)
    return Paper(**values)


def test_top_venue_match_is_case_insensitive(config):
    assert venue_score("Thirty-eighth Conference on NeurIPS", config) == 10.0


def test_niche_relevant_work_survives_weak_venue(config):
    result = score_paper(paper(), config)
    assert result.score_breakdown["relevance"] > 5
    assert result.deterministic_score > 4
    assert "RAG" in result.matched_interests


def test_recent_citations_are_age_normalized():
    today = date(2026, 8, 21)
    recent = paper(cited_by_count=10, publication_date=(today - timedelta(days=30)).isoformat())
    old = paper(cited_by_count=10, publication_date=(today - timedelta(days=1000)).isoformat())
    assert citation_score(recent, today) > citation_score(old, today)
