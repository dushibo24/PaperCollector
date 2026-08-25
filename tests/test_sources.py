from paper_collector.sources import _abstract_from_inverted, _stable_id


def test_openalex_inverted_abstract_is_reconstructed():
    assert _abstract_from_inverted({"world": [1], "hello": [0, 2]}) == "hello world hello"


def test_title_fallback_id_is_stable():
    assert _stable_id("Hello, World!") == _stable_id("hello world")


def test_doi_is_canonicalized():
    assert _stable_id("ignored", "https://doi.org/10.1/ABC") == "doi:10.1/abc"
