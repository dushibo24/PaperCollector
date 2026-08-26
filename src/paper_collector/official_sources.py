from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, ClassVar, Protocol

from paper_collector.config import AppConfig, Interest
from paper_collector.models import Author, Paper
from paper_collector.sources import SourceError, _stable_id

LOGGER = logging.getLogger(__name__)

RequestText = Callable[..., str]
RequestJSON = Callable[..., Any]


def _redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, "***" if key.casefold() in {"apikey", "api_key", "key"} else value)
        for key, value in query
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(redacted),
            parsed.fragment,
        )
    )


def _request_text(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "PaperCollector/0.1 (official proceedings collector)",
            "Accept": "text/html, application/xml, text/xml;q=0.9, */*;q=0.8",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise SourceError(f"请求失败 {_redact_url(url)}: {exc}") from exc


def _request_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30) -> Any:
    text = _request_text(
        url, headers={"Accept": "application/json", **(headers or {})}, timeout=timeout
    )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"JSON 解析失败 {_redact_url(url)}: {exc}") from exc


def _clean_html(value: str) -> str:
    value = re.sub(r"<(script|style)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def _year_date(value: str | None, fallback_year: int) -> tuple[str, int]:
    value = (value or "").strip()
    match = re.search(r"(?:19|20)\d{2}", value)
    year = int(match.group()) if match else fallback_year
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value, year
    if re.fullmatch(r"\d{4}/\d{2}/\d{2}", value):
        return value.replace("/", "-"), year
    return f"{year}-01-01", year


def _content_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _normalize_doi(value: Any) -> str | None:
    if not value:
        return None
    doi = str(value).strip()
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.IGNORECASE)
    return doi or None


def _matches_interest(paper: Paper, interest: Interest) -> bool:
    title = paper.title.casefold()
    haystack = f"{paper.title} {paper.abstract}".casefold()
    if any(item.casefold() in haystack for item in interest.negative_keywords if len(item) >= 4):
        return False
    phrases = [item.casefold().strip() for item in interest.keywords if item.strip()]
    if any(phrase in haystack for phrase in phrases):
        return True
    tokens = {
        token
        for phrase in phrases
        for token in re.findall(r"[a-z0-9][a-z0-9+.-]{2,}", phrase)
        if token
        not in {
            "and",
            "for",
            "from",
            "with",
            "based",
            "model",
            "models",
            "learning",
            "system",
            "systems",
        }
    }
    title_hits = sum(token in title for token in tokens)
    all_hits = sum(token in haystack for token in tokens)
    return title_hits >= 2 or all_hits >= 3


def _matches_any(paper: Paper, interests: list[Interest]) -> bool:
    return any(_matches_interest(paper, interest) for interest in interests)


class _ScholarlyMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self._abstract_depth = 0
        self._abstract_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "meta":
            key = (values.get("name") or values.get("property") or "").casefold()
            if key.startswith("citation_") or key in {"description", "og:description"}:
                self.meta.setdefault(key, []).append(values.get("content", ""))
        classes = set(values.get("class", "").casefold().split())
        if values.get("id", "").casefold() == "abstract" or "paper-abstract" in classes:
            self._abstract_depth = 1
        elif self._abstract_depth:
            self._abstract_depth += 1

    def handle_endtag(self, _tag: str) -> None:
        if self._abstract_depth:
            self._abstract_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._abstract_depth:
            self._abstract_chunks.append(data)

    @property
    def abstract(self) -> str:
        captured = " ".join(" ".join(self._abstract_chunks).split())
        if captured:
            return captured
        for key in ("citation_abstract", "og:description", "description"):
            if self.meta.get(key):
                return self.meta[key][0].strip()
        return ""


def _paper_from_meta(
    page: str,
    *,
    source: str,
    fallback_venue: str,
    fallback_year: int,
    landing_url: str,
    fallback_title: str = "",
    fallback_pdf: str | None = None,
) -> Paper | None:
    parser = _ScholarlyMetaParser()
    parser.feed(page)
    meta = parser.meta
    title = (meta.get("citation_title") or [fallback_title])[0].strip()
    if not title:
        return None
    doi = _normalize_doi((meta.get("citation_doi") or [None])[0])
    publication_date, year = _year_date(
        (meta.get("citation_publication_date") or [None])[0], fallback_year
    )
    raw_pdf_url = (meta.get("citation_pdf_url") or [fallback_pdf])[0]
    pdf_url = urllib.parse.urljoin(landing_url, raw_pdf_url) if raw_pdf_url else None
    venue = (
        meta.get("citation_conference_title")
        or meta.get("citation_journal_title")
        or [fallback_venue]
    )[0]
    return Paper(
        id=_stable_id(title, doi),
        title=title,
        abstract=parser.abstract,
        authors=[Author(name=name) for name in meta.get("citation_author", [])],
        publication_date=publication_date,
        year=year,
        venue=venue or fallback_venue,
        venue_type="journal" if "journal" in " ".join(meta).casefold() else "conference",
        doi=doi,
        landing_url=landing_url,
        pdf_url=pdf_url,
        source=f"official:{source}",
    )


class OfficialAdapter(Protocol):
    name: str

    def fetch(self) -> list[Paper]: ...


class _Adapter:
    name = "base"

    def __init__(
        self,
        config: AppConfig,
        request_text: RequestText = _request_text,
        request_json: RequestJSON = _request_json,
    ) -> None:
        self.config = config
        self.request_text = request_text
        self.request_json = request_json
        current_year = datetime.now(UTC).year
        self.years = list(range(current_year, current_year - config.official_lookback_years, -1))

    def optional_text(self, url: str, **kwargs: Any) -> str | None:
        try:
            return self.request_text(url, **kwargs)
        except SourceError as exc:
            LOGGER.info("官网入口暂不可用，跳过 %s: %s", url, exc)
            return None

    def candidate(self, title: str, year: int, venue: str, **kwargs: Any) -> Paper:
        return Paper(
            id=_stable_id(title),
            title=title,
            abstract=kwargs.get("abstract", ""),
            authors=kwargs.get("authors", []),
            publication_date=kwargs.get("publication_date", f"{year}-01-01"),
            year=year,
            venue=venue,
            venue_type=kwargs.get("venue_type", "conference"),
            doi=kwargs.get("doi"),
            landing_url=kwargs.get("landing_url"),
            pdf_url=kwargs.get("pdf_url"),
            source=f"official:{self.name}",
        )


class OpenReviewAdapter(_Adapter):
    name = "openreview"
    base_url = "https://api2.openreview.net/notes"

    def fetch(self) -> list[Paper]:
        venue_ids = [f"ICLR.cc/{year}/Conference" for year in self.years]
        venue_ids.append("TMLR")
        result: dict[str, Paper] = {}
        page_size = 1000
        for venue_id in venue_ids:
            for offset in range(0, 5000, page_size):
                query = urllib.parse.urlencode(
                    {"content.venueid": venue_id, "limit": page_size, "offset": offset}
                )
                try:
                    payload = self.request_json(f"{self.base_url}?{query}")
                except SourceError as exc:
                    LOGGER.info("OpenReview %s 暂不可用：%s", venue_id, exc)
                    break
                notes = payload.get("notes", [])
                for note in notes:
                    content = note.get("content") or {}
                    title = str(_content_value(content.get("title")) or "").strip()
                    if not title:
                        continue
                    abstract = str(_content_value(content.get("abstract")) or "").strip()
                    raw_authors = _content_value(content.get("authors")) or []
                    if isinstance(raw_authors, str):
                        raw_authors = [raw_authors]
                    venue = str(
                        _content_value(content.get("venue"))
                        or (
                            "Transactions on Machine Learning Research"
                            if venue_id == "TMLR"
                            else venue_id
                        )
                    )
                    stamp = note.get("pdate") or note.get("cdate")
                    stamp_date = ""
                    if isinstance(stamp, (float, int)):
                        stamp_date = datetime.fromtimestamp(stamp / 1000, UTC).date().isoformat()
                    publication_date, year = _year_date(stamp_date, self.years[0])
                    note_id = note.get("id")
                    paper = Paper(
                        id=_stable_id(title),
                        title=title,
                        abstract=abstract,
                        authors=[Author(name=str(author)) for author in raw_authors],
                        publication_date=publication_date,
                        year=year,
                        venue=venue,
                        venue_type="journal" if venue_id == "TMLR" else "conference",
                        landing_url=(
                            f"https://openreview.net/forum?id={note_id}" if note_id else None
                        ),
                        pdf_url=f"https://openreview.net/pdf?id={note_id}" if note_id else None,
                        source=f"official:{self.name}",
                    )
                    result[paper.id] = paper
                if len(notes) < page_size:
                    break
        return list(result.values())


class _PMLRIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, str]] = []
        self._depth = 0
        self._row: dict[str, str] | None = None
        self._title_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "div" and "paper" in (values.get("class") or "").split():
            self._depth = 1
            self._row = {}
            return
        if self._depth:
            self._depth += 1
            if tag == "p" and "title" in (values.get("class") or "").split():
                self._title_depth = self._depth
            if tag == "a":
                href = values.get("href") or ""
                if href.endswith(".html"):
                    self._row["landing"] = href
                elif href.endswith(".pdf"):
                    self._row["pdf"] = href

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        if self._title_depth == self._depth and tag == "p":
            self._title_depth = 0
        self._depth -= 1
        if self._depth == 0 and self._row:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._row is not None and self._title_depth:
            self._row["title"] = self._row.get("title", "") + data


class PMLRAdapter(_Adapter):
    name = "pmlr"
    root = "https://proceedings.mlr.press/"
    venue_pattern = re.compile(r"\b(ICML|AISTATS|UAI|CoRL)\b", re.IGNORECASE)

    def fetch(self) -> list[Paper]:
        root = self.request_text(self.root)
        volumes = re.findall(
            r'<a\s+href=["\']?(v\d+)["\']?[^>]*>.*?</a>\s*([^<\n]+)',
            root,
            re.IGNORECASE | re.DOTALL,
        )
        papers: list[Paper] = []
        for volume, label in volumes:
            label = _clean_html(label)
            year_match = re.search(r"(?:19|20)\d{2}", label)
            if not year_match or int(year_match.group()) not in self.years:
                continue
            venue_match = self.venue_pattern.search(label)
            if not venue_match:
                continue
            year = int(year_match.group())
            venue = venue_match.group().upper()
            page = self.optional_text(urllib.parse.urljoin(self.root, f"{volume}/"))
            if not page:
                continue
            parser = _PMLRIndexParser()
            parser.feed(page)
            details_used = 0
            for row in parser.rows:
                title = " ".join(row.get("title", "").split())
                landing = urllib.parse.urljoin(self.root, row.get("landing", ""))
                candidate = self.candidate(
                    title,
                    year,
                    venue,
                    landing_url=landing,
                    pdf_url=urllib.parse.urljoin(self.root, row.get("pdf", "")),
                )
                if not title or not _matches_any(candidate, self.config.interests):
                    continue
                if details_used >= self.config.official_per_source_limit:
                    continue
                details_used += 1
                detail = self.optional_text(landing)
                paper = (
                    _paper_from_meta(
                        detail,
                        source=self.name,
                        fallback_venue=venue,
                        fallback_year=year,
                        landing_url=landing,
                        fallback_title=title,
                        fallback_pdf=candidate.pdf_url,
                    )
                    if detail
                    else candidate
                )
                if paper:
                    papers.append(paper)
        return papers


class _TitleLinkParser(HTMLParser):
    def __init__(self, href_pattern: str, parent_class: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.pattern = re.compile(href_pattern)
        self.parent_class = parent_class
        self.links: list[tuple[str, str]] = []
        self._capture_href: str | None = None
        self._chunks: list[str] = []
        self._parent_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if self.parent_class and self.parent_class in classes:
            self._parent_depth = 1
        elif self._parent_depth:
            self._parent_depth += 1
        if (
            tag == "a"
            and self.pattern.search(values.get("href") or "")
            and (not self.parent_class or self._parent_depth)
        ):
            self._capture_href = values.get("href")
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_href:
            title = " ".join(" ".join(self._chunks).split())
            if title:
                self.links.append((self._capture_href, title))
            self._capture_href = None
        if self._parent_depth:
            self._parent_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._capture_href:
            self._chunks.append(data)


class ACLAdapter(_Adapter):
    name = "acl"
    events = ("acl", "emnlp", "naacl", "coling")

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        seen: set[str] = set()
        for year in self.years:
            for event in self.events:
                page_url = f"https://aclanthology.org/events/{event}-{year}/"
                page = self.optional_text(page_url)
                if not page:
                    continue
                parser = _TitleLinkParser(rf"/{year}\.[^/]+\.\d+/$")
                parser.feed(page)
                details_used = 0
                for href, title in parser.links:
                    landing = urllib.parse.urljoin(page_url, href)
                    candidate = self.candidate(title, year, event.upper(), landing_url=landing)
                    if candidate.id in seen or not _matches_any(candidate, self.config.interests):
                        continue
                    if details_used >= self.config.official_per_source_limit:
                        continue
                    details_used += 1
                    detail = self.optional_text(landing)
                    if not detail:
                        continue
                    paper = _paper_from_meta(
                        detail,
                        source=self.name,
                        fallback_venue=event.upper(),
                        fallback_year=year,
                        landing_url=landing,
                        fallback_title=title,
                        fallback_pdf=landing.rstrip("/") + ".pdf",
                    )
                    if paper:
                        papers.append(paper)
                        seen.add(paper.id)
        tacl_url = "https://aclanthology.org/venues/tacl/"
        tacl_page = self.optional_text(tacl_url)
        if tacl_page:
            parser = _TitleLinkParser(
                rf"/({'|'.join(str(year) for year in self.years)})\.tacl-1\.\d+/$"
            )
            parser.feed(tacl_page)
            details_used = 0
            for href, title in parser.links:
                year_match = re.search(r"/(\d{4})\.", href)
                year = int(year_match.group(1)) if year_match else self.years[0]
                landing = urllib.parse.urljoin(tacl_url, href)
                candidate = self.candidate(title, year, "TACL", landing_url=landing)
                if candidate.id in seen or not _matches_any(candidate, self.config.interests):
                    continue
                if details_used >= self.config.official_per_source_limit:
                    continue
                details_used += 1
                detail = self.optional_text(landing)
                if not detail:
                    continue
                paper = _paper_from_meta(
                    detail,
                    source=self.name,
                    fallback_venue="Transactions of the Association for Computational Linguistics",
                    fallback_year=year,
                    landing_url=landing,
                    fallback_title=title,
                    fallback_pdf=landing.rstrip("/") + ".pdf",
                )
                if paper:
                    papers.append(paper)
                    seen.add(paper.id)
        return papers


class CVFAdapter(_Adapter):
    name = "cvf"
    conferences = ("CVPR", "ICCV", "WACV")

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        for year in self.years:
            for conference in self.conferences:
                page_url = f"https://openaccess.thecvf.com/{conference}{year}?day=all"
                page = self.optional_text(page_url)
                if not page:
                    continue
                parser = _TitleLinkParser(rf"/content/{conference}{year}/html/.*\.html$")
                parser.feed(page)
                details_used = 0
                for href, title in parser.links:
                    landing = urllib.parse.urljoin(page_url, href)
                    candidate = self.candidate(title, year, conference, landing_url=landing)
                    if not _matches_any(candidate, self.config.interests):
                        continue
                    if details_used >= self.config.official_per_source_limit:
                        continue
                    details_used += 1
                    detail = self.optional_text(landing)
                    if not detail:
                        continue
                    paper = _paper_from_meta(
                        detail,
                        source=self.name,
                        fallback_venue=conference,
                        fallback_year=year,
                        landing_url=landing,
                        fallback_title=title,
                    )
                    if paper:
                        papers.append(paper)
        return papers


class NeurIPSAdapter(_Adapter):
    name = "neurips"

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        for year in self.years:
            page_url = f"https://proceedings.neurips.cc/paper_files/paper/{year}"
            page = self.optional_text(page_url)
            if not page:
                continue
            parser = _TitleLinkParser(rf"/paper_files/paper/{year}/hash/.*-Abstract-.*\.html$")
            parser.feed(page)
            details_used = 0
            for href, title in parser.links:
                landing = urllib.parse.urljoin(page_url, href)
                candidate = self.candidate(title, year, "NeurIPS", landing_url=landing)
                if not _matches_any(candidate, self.config.interests):
                    continue
                if details_used >= self.config.official_per_source_limit:
                    continue
                details_used += 1
                detail = self.optional_text(landing)
                if not detail:
                    continue
                paper = _paper_from_meta(
                    detail,
                    source=self.name,
                    fallback_venue="NeurIPS",
                    fallback_year=year,
                    landing_url=landing,
                    fallback_title=title,
                )
                if paper:
                    papers.append(paper)
        return papers


def _authors_from_text(value: str) -> list[Author]:
    value = re.sub(r"\([^)]*(?:University|Institute|Laborator|Corporation|Inc\.)[^)]*\)", "", value)
    parts = re.split(r",|\band\b|;", value)
    return [Author(name=part.strip()) for part in parts if 1 < len(part.strip()) < 100]


class USENIXAdapter(_Adapter):
    name = "usenix"
    conferences: ClassVar[dict[str, str]] = {
        "usenixsecurity": "USENIX Security",
        "nsdi": "NSDI",
        "osdi": "OSDI",
        "atc": "USENIX ATC",
    }

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        for year in self.years:
            short_year = str(year)[-2:]
            for slug, venue in self.conferences.items():
                page_url = (
                    f"https://www.usenix.org/conference/{slug}{short_year}/technical-sessions"
                )
                page = self.optional_text(page_url)
                if not page:
                    continue
                details_used = 0
                for article in re.findall(
                    r"<article\b.*?</article>", page, re.IGNORECASE | re.DOTALL
                ):
                    title_match = re.search(
                        r'<h2[^>]*>\s*<a\s+href=["\']([^"\']+/presentation/[^"\']+)["\'][^>]*>(.*?)</a>',
                        article,
                        re.IGNORECASE | re.DOTALL,
                    )
                    if not title_match:
                        continue
                    landing = urllib.parse.urljoin(page_url, title_match.group(1))
                    title = _clean_html(title_match.group(2))
                    abstract_match = re.search(
                        r"field-name-field-paper-description-long[^>]*>(.*?)</div>\s*<div",
                        article,
                        re.IGNORECASE | re.DOTALL,
                    )
                    authors_match = re.search(
                        r"field-name-field-paper-people-text[^>]*>.*?<p>(.*?)</p>",
                        article,
                        re.IGNORECASE | re.DOTALL,
                    )
                    candidate = self.candidate(
                        title,
                        year,
                        venue,
                        abstract=_clean_html(abstract_match.group(1)) if abstract_match else "",
                        authors=_authors_from_text(_clean_html(authors_match.group(1)))
                        if authors_match
                        else [],
                        landing_url=landing,
                    )
                    if not _matches_any(candidate, self.config.interests):
                        continue
                    if details_used >= self.config.official_per_source_limit:
                        continue
                    details_used += 1
                    detail = self.optional_text(landing)
                    if detail:
                        pdf_match = re.search(
                            r'<a[^>]+class=["\'][^"\']*pdf-button[^"\']*["\'][^>]+href=["\']([^"\']+\.pdf)',
                            detail,
                            re.IGNORECASE,
                        ) or re.search(
                            r'href=["\']([^"\']+\.pdf)["\'][^>]*>\s*Paper\s*</a>',
                            detail,
                            re.IGNORECASE,
                        )
                        if pdf_match:
                            candidate.pdf_url = urllib.parse.urljoin(landing, pdf_match.group(1))
                    papers.append(candidate)
        return papers


class NDSSAdapter(_Adapter):
    name = "ndss"

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        for year in self.years:
            page_url = f"https://www.ndss-symposium.org/ndss{year}/accepted-papers/"
            page = self.optional_text(page_url)
            if not page:
                continue
            parser = _TitleLinkParser(r"/ndss-paper/[^/]+/$", parent_class="pt-cv-title")
            parser.feed(page)
            details_used = 0
            for href, title in parser.links:
                landing = urllib.parse.urljoin(page_url, href)
                candidate = self.candidate(title, year, "NDSS", landing_url=landing)
                if not _matches_any(candidate, self.config.interests):
                    continue
                if details_used >= self.config.official_per_source_limit:
                    continue
                details_used += 1
                detail = self.optional_text(landing)
                if detail:
                    content_match = re.search(
                        r'<div\s+class=["\']entry-content["\']>(.*?)</article>',
                        detail,
                        re.IGNORECASE | re.DOTALL,
                    )
                    paragraphs = (
                        re.findall(
                            r"<p[^>]*>(.*?)</p>", content_match.group(1), re.IGNORECASE | re.DOTALL
                        )
                        if content_match
                        else []
                    )
                    cleaned = [_clean_html(item) for item in paragraphs if _clean_html(item)]
                    if cleaned:
                        candidate.authors = _authors_from_text(cleaned[0])
                    if len(cleaned) > 1:
                        candidate.abstract = max(cleaned[1:], key=len)
                    pdf_match = re.search(
                        r'class=["\'][^"\']*pdf-button[^"\']*["\'][^>]*href=["\']([^"\']+\.pdf)',
                        detail,
                        re.IGNORECASE,
                    )
                    if pdf_match:
                        candidate.pdf_url = urllib.parse.urljoin(landing, pdf_match.group(1))
                papers.append(candidate)
        return papers


class JMLRAdapter(_Adapter):
    name = "jmlr"
    feed_url = "https://www.jmlr.org/jmlr.xml"

    def fetch(self) -> list[Paper]:
        feed = self.request_text(self.feed_url)
        try:
            root = ET.fromstring(feed)
        except ET.ParseError as exc:
            raise SourceError(f"JMLR RSS 解析失败：{exc}") from exc
        papers: list[Paper] = []
        minimum_year = min(self.years)
        for item in root.findall(".//item"):
            title = " ".join((item.findtext("title") or "").split())
            publication_date, year = _year_date(item.findtext("pubDate"), self.years[0])
            if not title or year < minimum_year:
                continue
            author_text = item.findtext("author") or ""
            link = item.findtext("link")
            pdf = item.findtext("pdf")
            if not pdf:
                pdf_node = next(
                    (child for child in item if child.tag.rsplit("}", 1)[-1] == "pdf"), None
                )
                pdf = pdf_node.text if pdf_node is not None else None
            papers.append(
                self.candidate(
                    title,
                    year,
                    "Journal of Machine Learning Research",
                    abstract=_clean_html(item.findtext("description") or ""),
                    authors=_authors_from_text(author_text),
                    publication_date=publication_date,
                    landing_url=link,
                    pdf_url=pdf,
                    venue_type="journal",
                )
            )
        return papers


class PVLDBAdapter(_Adapter):
    name = "pvldb"

    def fetch(self) -> list[Paper]:
        papers: list[Paper] = []
        for year in self.years:
            volume = year - 2007
            page_url = f"https://www.vldb.org/pvldb/volumes/{volume}"
            page = self.optional_text(page_url)
            if not page:
                continue
            match = re.search(
                r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                page,
                re.IGNORECASE | re.DOTALL,
            )
            if not match:
                LOGGER.info("PVLDB volume %s 未找到 __NEXT_DATA__", volume)
                continue
            try:
                payload = json.loads(html.unescape(match.group(1)))
                groups = payload["props"]["pageProps"]["groupedIssues"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                LOGGER.info("PVLDB volume %s 数据结构无法解析：%s", volume, exc)
                continue
            for rows in groups.values():
                for row in rows:
                    title = str(row.get("title") or "").strip()
                    if not title or title.casefold() == "front matter":
                        continue
                    pdf = row.get("pdf")
                    papers.append(
                        self.candidate(
                            title,
                            year,
                            "Proceedings of the VLDB Endowment",
                            authors=_authors_from_text(str(row.get("authors") or "")),
                            landing_url=pdf,
                            pdf_url=pdf,
                            venue_type="journal",
                        )
                    )
        return papers


def _query_phrases(config: AppConfig, per_interest: int = 2) -> list[str]:
    phrases: list[str] = []
    for interest in config.interests:
        phrases.extend(interest.keywords[:per_interest])
    return list(dict.fromkeys(phrases))


class IEEEAdapter(_Adapter):
    name = "ieee"
    endpoint = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
    target_venues: ClassVar[tuple[str, ...]] = (
        "transactions on networking",
        "dependable and secure computing",
        "information forensics and security",
        "network and service management",
        "pattern analysis and machine intelligence",
        "neural networks and learning systems",
        "knowledge and data engineering",
        "software engineering",
        "infocom",
        "robotics and automation",
        "intelligent robots and systems",
    )

    def fetch(self) -> list[Paper]:
        if not self.config.ieee_xplore_api_key:
            return []
        query = " OR ".join(f'"{phrase}"' for phrase in _query_phrases(self.config))
        params = urllib.parse.urlencode(
            {
                "apikey": self.config.ieee_xplore_api_key,
                "format": "json",
                "max_records": min(200, self.config.official_per_source_limit * 5),
                "start_record": 1,
                "sort_order": "desc",
                "sort_field": "publication_year",
                "querytext": query,
            }
        )
        payload = self.request_json(f"{self.endpoint}?{params}")
        papers: list[Paper] = []
        for row in payload.get("articles", []):
            title = _clean_html(str(row.get("title") or row.get("article_title") or ""))
            if not title:
                continue
            doi = _normalize_doi(row.get("doi"))
            publication_date, year = _year_date(
                row.get("publication_date"), int(row.get("publication_year") or self.years[0])
            )
            venue = str(row.get("publication_title") or "IEEE Xplore")
            if year < min(self.years) or not any(
                target in venue.casefold() for target in self.target_venues
            ):
                continue
            raw_authors = (row.get("authors") or {}).get("authors") or []
            venue_type = (
                "conference"
                if any(word in venue.casefold() for word in ("conference", "symposium", "infocom"))
                else "journal"
            )
            papers.append(
                Paper(
                    id=_stable_id(title, doi),
                    title=title,
                    abstract=_clean_html(str(row.get("abstract") or "")),
                    authors=[Author(name=item.get("full_name", "Unknown")) for item in raw_authors],
                    publication_date=publication_date,
                    year=year,
                    venue=venue,
                    venue_type=venue_type,
                    doi=doi,
                    landing_url=row.get("html_url") or row.get("abstract_url"),
                    pdf_url=row.get("pdf_url"),
                    source=f"official:{self.name}",
                )
            )
        return papers


class ElsevierAdapter(_Adapter):
    name = "elsevier"
    endpoint = "https://api.elsevier.com/content/search/scopus"

    def fetch(self) -> list[Paper]:
        if not self.config.elsevier_api_key:
            return []
        terms = " OR ".join(f'TITLE-ABS-KEY("{item}")' for item in _query_phrases(self.config))
        params = urllib.parse.urlencode(
            {
                "query": f'({terms}) AND SRCTITLE("Computer Networks")',
                "count": min(200, self.config.official_per_source_limit * 5),
                "sort": "-coverDate",
            }
        )
        payload = self.request_json(
            f"{self.endpoint}?{params}", headers={"X-ELS-APIKey": self.config.elsevier_api_key}
        )
        papers: list[Paper] = []
        for row in (payload.get("search-results") or {}).get("entry", []):
            title = _clean_html(str(row.get("dc:title") or ""))
            if not title or row.get("error"):
                continue
            doi = _normalize_doi(row.get("prism:doi"))
            publication_date, year = _year_date(row.get("prism:coverDate"), self.years[0])
            if year < min(self.years):
                continue
            links = {
                item.get("@ref"): item.get("@href")
                for item in row.get("link", [])
                if item.get("@ref")
            }
            papers.append(
                Paper(
                    id=_stable_id(title, doi),
                    title=title,
                    abstract="",
                    authors=[Author(name=str(row.get("dc:creator") or "Unknown"))],
                    publication_date=publication_date,
                    year=year,
                    venue=str(row.get("prism:publicationName") or "Computer Networks"),
                    venue_type="journal",
                    doi=doi,
                    landing_url=links.get("scopus") or links.get("self"),
                    pdf_url=None,
                    cited_by_count=int(row.get("citedby-count") or 0),
                    source=f"official:{self.name}",
                )
            )
        return papers


ADAPTERS: dict[str, type[_Adapter]] = {
    "openreview": OpenReviewAdapter,
    "pmlr": PMLRAdapter,
    "acl": ACLAdapter,
    "cvf": CVFAdapter,
    "neurips": NeurIPSAdapter,
    "usenix": USENIXAdapter,
    "ndss": NDSSAdapter,
    "jmlr": JMLRAdapter,
    "pvldb": PVLDBAdapter,
    "ieee": IEEEAdapter,
    "elsevier": ElsevierAdapter,
}


class OfficialSources:
    """Fan out to official proceedings once, then match every interest locally."""

    def __init__(
        self,
        config: AppConfig,
        *,
        adapters: list[OfficialAdapter] | None = None,
    ) -> None:
        self.config = config
        self.adapters: list[OfficialAdapter] = adapters or [
            ADAPTERS[name](config) for name in config.official_sources
        ]
        self._cache: dict[str, list[Paper]] | None = None

    def _load(self) -> dict[str, list[Paper]]:
        if self._cache is not None:
            return self._cache
        self._cache = {}
        for adapter in self.adapters:
            try:
                self._cache[adapter.name] = adapter.fetch()
            except (SourceError, AttributeError, KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("官方数据源 %s 抓取失败：%s", adapter.name, exc)
                self._cache[adapter.name] = []
        return self._cache

    def discover(self, interest: Interest) -> list[Paper]:
        found: list[Paper] = []
        for papers in self._load().values():
            matched = [paper for paper in papers if _matches_interest(paper, interest)]
            matched.sort(
                key=lambda paper: (paper.publication_date, bool(paper.abstract)), reverse=True
            )
            for paper in matched[: self.config.official_per_source_limit]:
                paper.matched_interests = sorted(set(paper.matched_interests + [interest.name]))
                found.append(paper)
        return found
