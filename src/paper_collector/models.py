from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Author:
    name: str
    openalex_id: str | None = None
    h_index: int | None = None
    cited_by_count: int | None = None
    notable_work_count: int | None = None


@dataclass(slots=True)
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[Author]
    publication_date: str
    year: int
    venue: str = ""
    venue_type: str = ""
    doi: str | None = None
    landing_url: str | None = None
    pdf_url: str | None = None
    cited_by_count: int = 0
    influential_citation_count: int = 0
    source: str = ""
    concepts: list[str] = field(default_factory=list)
    matched_interests: list[str] = field(default_factory=list)
    deterministic_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Paper:
        value = dict(value)
        value["authors"] = [Author(**item) for item in value.get("authors", [])]
        return cls(**value)


@dataclass(slots=True)
class Review:
    paper_id: str
    recommend: bool
    confidence: float
    overall_score: float
    relevance_score: float
    content_quality_score: float
    novelty_score: float
    impact_score: float
    timeliness_score: float
    evidence_quality_score: float
    verdict: str
    rejection_reason: str
    title_zh: str
    hook: str
    summary: str
    contributions: list[str]
    evidence: list[str]
    limitations: list[str]
    audience: str
    why_now: str
    tags: list[str]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Review:
        return cls(**value)
