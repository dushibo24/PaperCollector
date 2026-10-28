import json
from dataclasses import replace

from paper_collector.config import Interest
from paper_collector.models import Author, Paper
from paper_collector.official_sources import (
    ElsevierAdapter,
    IEEEAdapter,
    JMLRAdapter,
    OfficialSources,
    OpenReviewAdapter,
    PMLRAdapter,
    PVLDBAdapter,
    _paper_from_meta,
    _redact_url,
)
from paper_collector.pipeline import Pipeline


def _paper(title: str, source: str = "test") -> Paper:
    return Paper(
        id=f"title:{title.casefold().replace(' ', '-')}",
        title=title,
        abstract="A retrieval augmented generation agent uses a large language model.",
        authors=[],
        publication_date="2026-08-01",
        year=2026,
        source=source,
    )


def test_scholarly_meta_parser_preserves_authors_abstract_and_pdf():
    page = """
    <meta name="citation_title" content="An Agent Paper">
    <meta name="citation_author" content="Ada Lovelace">
    <meta name="citation_author" content="Alan Turing">
    <meta name="citation_publication_date" content="2026/08/20">
    <meta name="citation_conference_title" content="ICML">
    <meta name="citation_doi" content="10.1/example">
    <meta name="citation_pdf_url" content="https://example.test/paper.pdf">
    <div id="abstract"><p>A tool-using <b>LLM agent</b>.</p></div>
    """

    paper = _paper_from_meta(
        page,
        source="fixture",
        fallback_venue="Unknown",
        fallback_year=2025,
        landing_url="https://example.test/paper",
    )

    assert paper is not None
    assert paper.id == "doi:10.1/example"
    assert [author.name for author in paper.authors] == ["Ada Lovelace", "Alan Turing"]
    assert paper.abstract == "A tool-using LLM agent ."
    assert paper.publication_date == "2026-08-20"
    assert paper.pdf_url == "https://example.test/paper.pdf"


def test_api_keys_are_redacted_from_error_urls():
    redacted = _redact_url("https://example.test/search?apikey=private-value&query=agent")

    assert "private-value" not in redacted
    assert "query=agent" in redacted


def test_openreview_v2_content_values_are_unwrapped(config):
    calls = []

    def request_json(url: str, **_kwargs):
        calls.append(url)
        return {
            "notes": [
                {
                    "id": "note-1",
                    "pdate": 1787529600000,
                    "content": {
                        "title": {"value": "A New LLM Agent Architecture"},
                        "abstract": {"value": "An agent learning method."},
                        "authors": {"value": ["Ada", "Alan"]},
                        "venue": {"value": "ICLR 2026"},
                    },
                }
            ]
        }

    adapter = OpenReviewAdapter(config, request_json=request_json)
    adapter.years = [2026]
    papers = adapter.fetch()

    assert len(calls) == 2  # ICLR plus the rolling TMLR journal.
    assert len(papers) == 1
    assert papers[0].title == "A New LLM Agent Architecture"
    assert [author.name for author in papers[0].authors] == ["Ada", "Alan"]
    assert papers[0].pdf_url == "https://openreview.net/pdf?id=note-1"


def test_openreview_uses_authenticated_client_when_credentials_are_configured(config):
    client_kwargs = {}
    requested_venues = []

    class FakeNote:
        def to_json(self):
            return {
                "id": "note-1",
                "pdate": 1787529600000,
                "content": {
                    "title": {"value": "An Authenticated OpenReview Paper"},
                    "abstract": {"value": "An agent learning method."},
                    "authors": {"value": ["Ada"]},
                    "venue": {"value": "ICLR 2026"},
                },
            }

    class FakeClient:
        def get_all_notes(self, *, content):
            requested_venues.append(content["venueid"])
            return [FakeNote()]

    def client_factory(**kwargs):
        client_kwargs.update(kwargs)
        return FakeClient()

    authenticated = replace(
        config,
        openreview_username="researcher@example.com",
        openreview_password="test-password",
    )
    adapter = OpenReviewAdapter(
        authenticated,
        request_json=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("anonymous request should not be used")
        ),
        client_factory=client_factory,
    )
    adapter.years = [2026]

    papers = adapter.fetch()

    assert client_kwargs == {
        "baseurl": "https://api2.openreview.net",
        "username": "researcher@example.com",
        "password": "test-password",
    }
    assert requested_venues == ["ICLR.cc/2026/Conference", "TMLR"]
    assert papers[0].title == "An Authenticated OpenReview Paper"


def test_openreview_access_token_takes_precedence_over_password(config):
    client_kwargs = {}

    class FakeClient:
        def get_all_notes(self, *, content):
            return []

    adapter = OpenReviewAdapter(
        replace(
            config,
            openreview_username="researcher@example.com",
            openreview_password="test-password",
            openreview_access_token="test-token",
        ),
        client_factory=lambda **kwargs: client_kwargs.update(kwargs) or FakeClient(),
    )
    adapter.years = [2026]

    adapter.fetch()

    assert client_kwargs == {"baseurl": "https://api2.openreview.net", "token": "test-token"}


def test_pmlr_index_prefilters_then_reads_detail_metadata(config):
    root = """
    <li><a href="v999"><b>Volume 999</b></a> Proceedings of ICML 2026</li>
    <li><a href="v100"><b>Volume 100</b></a> Proceedings of ICML 2020</li>
    """
    volume = """
    <div class="paper"><p class="title">Retrieval Augmented Generation Agent</p>
    <a href="https://proceedings.mlr.press/v999/example.html">abs</a>
    <a href="https://proceedings.mlr.press/v999/example.pdf">Download PDF</a></div>
    <div class="paper"><p class="title">Unrelated Geometry</p>
    <a href="https://proceedings.mlr.press/v999/nope.html">abs</a></div>
    """
    detail = """
    <meta name="citation_title" content="Retrieval Augmented Generation Agent">
    <meta name="citation_author" content="Ada Lovelace">
    <meta name="citation_conference_title" content="ICML">
    <meta name="citation_publication_date" content="2026/07/01">
    <meta name="citation_pdf_url" content="https://proceedings.mlr.press/v999/example.pdf">
    <div id="abstract">A RAG agent with verifiable tool use.</div>
    """
    requested = []

    def request_text(url: str, **_kwargs):
        requested.append(url)
        return {
            "https://proceedings.mlr.press/": root,
            "https://proceedings.mlr.press/v999/": volume,
            "https://proceedings.mlr.press/v999/example.html": detail,
        }[url]

    adapter = PMLRAdapter(config, request_text=request_text)
    adapter.years = [2026]
    papers = adapter.fetch()

    assert len(papers) == 1
    assert papers[0].abstract == "A RAG agent with verifiable tool use."
    assert "nope.html" not in " ".join(requested)


def test_jmlr_rss_and_pvldb_next_data_are_parsed(config):
    feed = """
    <rss><channel><item>
      <title>Large Language Model Agents</title>
      <link>https://jmlr.org/papers/v27/agent.html</link>
      <pdf>https://jmlr.org/papers/volume27/agent.pdf</pdf>
      <pubDate>2026</pubDate><author>Ada Lovelace, Alan Turing</author>
      <description>An agent learning architecture.</description>
    </item></channel></rss>
    """
    jmlr = JMLRAdapter(config, request_text=lambda _url, **_kwargs: feed)
    jmlr.years = [2026]
    jmlr_papers = jmlr.fetch()

    payload = {
        "props": {
            "pageProps": {
                "groupedIssues": {
                    "1": [
                        {
                            "title": "LLM Agents for Databases",
                            "authors": "Ada Lovelace, Alan Turing",
                            "pdf": "https://vldb.org/pvldb/vol19/p1.pdf",
                        }
                    ]
                }
            }
        }
    }
    page = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    pvldb = PVLDBAdapter(config, request_text=lambda _url, **_kwargs: page)
    pvldb.years = [2026]
    pvldb_papers = pvldb.fetch()

    assert jmlr_papers[0].pdf_url.endswith("agent.pdf")
    assert jmlr_papers[0].venue_type == "journal"
    assert pvldb_papers[0].venue == "Proceedings of the VLDB Endowment"
    assert pvldb_papers[0].pdf_url.endswith("p1.pdf")


def test_official_sources_fetch_each_adapter_once_and_match_locally(config):
    class FakeAdapter:
        name = "fixture"

        def __init__(self):
            self.calls = 0

        def fetch(self):
            self.calls += 1
            return [_paper("Retrieval Augmented Generation with an LLM Agent")]

    adapter = FakeAdapter()
    source = OfficialSources(config, adapters=[adapter])

    assert source.discover(config.interests[0])
    assert source.discover(config.interests[1])
    assert adapter.calls == 1


def test_ieee_is_silently_skipped_without_key(config):
    called = False

    def request_json(_url: str, **_kwargs):
        nonlocal called
        called = True
        return {}

    adapter = IEEEAdapter(replace(config, ieee_xplore_api_key=None), request_json=request_json)

    assert adapter.fetch() == []
    assert called is False


def test_ieee_and_elsevier_official_api_payloads_are_parsed(config):
    ieee_payload = {
        "articles": [
            {
                "article_title": "LLM Agents for Network Management",
                "abstract": "A large language model agent manages a network.",
                "publication_year": 2026,
                "publication_date": "2026-07-01",
                "publication_title": "IEEE/ACM Transactions on Networking",
                "doi": "10.1/ieee",
                "html_url": "https://ieeexplore.ieee.org/document/1",
                "authors": {"authors": [{"full_name": "Ada Lovelace"}]},
            }
        ]
    }
    ieee = IEEEAdapter(
        replace(config, ieee_xplore_api_key="test-key"),
        request_json=lambda _url, **_kwargs: ieee_payload,
    )
    ieee.years = [2026]

    seen_headers = {}
    elsevier_payload = {
        "search-results": {
            "entry": [
                {
                    "dc:title": "Large Language Models for Network Operations",
                    "dc:creator": "Alan Turing",
                    "prism:coverDate": "2026-08-01",
                    "prism:publicationName": "Computer Networks",
                    "prism:doi": "10.1/elsevier",
                    "citedby-count": "4",
                    "link": [{"@ref": "scopus", "@href": "https://example.test/scopus"}],
                }
            ]
        }
    }

    def request_elsevier(_url: str, **kwargs):
        seen_headers.update(kwargs["headers"])
        return elsevier_payload

    elsevier = ElsevierAdapter(
        replace(config, elsevier_api_key="test-key"), request_json=request_elsevier
    )
    elsevier.years = [2026]

    assert ieee.fetch()[0].venue == "IEEE/ACM Transactions on Networking"
    assert elsevier.fetch()[0].cited_by_count == 4
    assert seen_headers == {"X-ELS-APIKey": "test-key"}


def test_pipeline_prefers_official_metadata_but_keeps_openalex_author_ids(config):
    title = "Retrieval Augmented Generation with an LLM Agent"
    aggregate = _paper(title, "openalex")
    aggregate.abstract = "Short abstract."
    aggregate.authors = [Author(name="Ada Lovelace", openalex_id="A123")]
    aggregate.landing_url = "https://openalex.org/work"
    official = _paper(title, "official:pmlr")
    official.id = "title:official-id"
    official.abstract = (
        "A much longer official abstract about retrieval augmented generation agents."
    )
    official.authors = [Author(name="Ada Lovelace")]
    official.doi = "10.1/official"
    official.venue = "ICML"
    official.pdf_url = "https://example.test/official.pdf"
    official.landing_url = "https://example.test/official"

    class FakeSource:
        def __init__(self, paper):
            self.paper = paper

        def discover(self, interest: Interest):
            paper = self.paper
            paper.matched_interests = [interest.name]
            return [paper]

    pipeline = Pipeline(config)
    pipeline.openalex = FakeSource(aggregate)
    pipeline.arxiv = FakeSource(aggregate)
    pipeline.official = FakeSource(official)
    try:
        papers = pipeline.discover()
    finally:
        pipeline.close()

    assert len(papers) == 1
    merged = papers[0]
    assert merged.source == "official:pmlr"
    assert merged.abstract == official.abstract
    assert merged.doi == "10.1/official"
    assert merged.venue == "ICML"
    assert merged.pdf_url == "https://example.test/official.pdf"
    assert merged.authors[0].openalex_id == "A123"
