from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime

from paper_collector.config import AppConfig, Interest
from paper_collector.models import Paper

NOVELTY_TERMS = {
    "first",
    "novel",
    "new framework",
    "new benchmark",
    "we introduce",
    "we propose",
    "state-of-the-art",
    "outperform",
    "significant improvement",
    "surprisingly",
}


def _contains(text: str, term: str) -> bool:
    return term.casefold() in text


def interest_relevance(paper: Paper, interest: Interest) -> float:
    text = f"{paper.title} {paper.abstract} {' '.join(paper.concepts)}".casefold()
    title = paper.title.casefold()
    hits = sum(1 for keyword in interest.keywords if _contains(text, keyword.casefold()))
    title_hits = sum(1 for keyword in interest.keywords if _contains(title, keyword.casefold()))
    negatives = sum(
        1 for keyword in interest.negative_keywords if _contains(text, keyword.casefold())
    )
    if not interest.keywords:
        return 0.0
    coverage = hits / len(interest.keywords)
    score = 10.0 * min(1.0, coverage * 1.8 + title_hits * 0.18) - negatives * 2.5
    return max(0.0, min(10.0, score)) * interest.weight


def venue_score(venue: str, config: AppConfig) -> float:
    normalized = venue.casefold().strip()
    if not normalized:
        return 0.0
    if any(item == normalized or item in normalized for item in config.top_venues):
        return 10.0
    if any(item == normalized or item in normalized for item in config.strong_venues):
        return 7.0
    return 2.5


def citation_score(paper: Paper, today: date | None = None) -> float:
    today = today or datetime.now(UTC).date()
    try:
        published = date.fromisoformat(paper.publication_date)
    except (TypeError, ValueError):
        published = date(paper.year or today.year, 1, 1)
    age_months = max(0.0, (today - published).days / 30.44)
    annualized = paper.cited_by_count * 12.0 / max(3.0, age_months)
    return min(10.0, math.log1p(annualized) * 2.2)


def freshness_score(paper: Paper, today: date | None = None) -> float:
    today = today or datetime.now(UTC).date()
    try:
        published = date.fromisoformat(paper.publication_date)
    except (TypeError, ValueError):
        return 2.0
    days = max(0, (today - published).days)
    return max(0.5, 10.0 * math.exp(-days / 500.0))


def author_score(paper: Paper) -> float:
    if not paper.authors:
        return 0.0
    values: list[float] = []
    for author in paper.authors:
        h_component = min(6.0, math.log1p(author.h_index or 0) * 1.45)
        cited_component = min(4.0, math.log1p(author.cited_by_count or 0) * 0.38)
        values.append(h_component + cited_component)
    return max(values, default=0.0)


def abstract_signal(paper: Paper) -> float:
    abstract = paper.abstract.casefold()
    length_score = min(5.0, len(abstract.split()) / 35.0)
    novelty = sum(1 for term in NOVELTY_TERMS if term in abstract)
    numeric_evidence = min(2.0, len(re.findall(r"\b\d+(?:\.\d+)?%", abstract)) * 0.7)
    return min(10.0, length_score + min(3.0, novelty * 0.7) + numeric_evidence)


def score_paper(paper: Paper, config: AppConfig) -> Paper:
    relevance_by_interest = {
        interest.name: interest_relevance(paper, interest) for interest in config.interests
    }
    matches = sorted(relevance_by_interest, key=relevance_by_interest.get, reverse=True)
    best_interest_name = matches[0]
    best_interest = next(item for item in config.interests if item.name == best_interest_name)
    relevance = relevance_by_interest[best_interest_name]
    venue = venue_score(paper.venue, config)
    citations = citation_score(paper)
    freshness = freshness_score(paper)
    authors = author_score(paper)
    content = abstract_signal(paper)

    if best_interest.scope == "niche":
        total = (
            0.40 * relevance
            + 0.22 * content
            + 0.10 * venue
            + 0.08 * citations
            + 0.08 * authors
            + 0.12 * freshness
        )
    else:
        total = (
            0.34 * relevance
            + 0.12 * content
            + 0.19 * venue
            + 0.15 * citations
            + 0.10 * authors
            + 0.10 * freshness
        )
        if venue < 7.0 and citations < 5.0 and authors < 5.0:
            total -= 1.2
    paper.matched_interests = [name for name in matches if relevance_by_interest[name] >= 2.0]
    paper.score_breakdown = {
        "relevance": round(relevance, 3),
        "venue": round(venue, 3),
        "citations": round(citations, 3),
        "freshness": round(freshness, 3),
        "author": round(authors, 3),
        "abstract_signal": round(content, 3),
    }
    paper.deterministic_score = round(max(0.0, min(10.0, total)), 3)
    return paper
