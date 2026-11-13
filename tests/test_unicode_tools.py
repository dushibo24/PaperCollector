import pytest

from paper_collector.unicode_tools import InvalidUnicodeText, utf8_text, utf8_value


@pytest.mark.parametrize(
    "text,expected",
    [
        ("中文 𝑥 𝐗 α 𝛼 👩‍💻", "中文 𝑥 𝐗 α 𝛼 👩‍💻"),
        ("\\ud835\\udc65", "\\ud835\\udc65"),  # literal escapes are not decoded twice
        ("\ud835\udc65", "𝑥"),
        ("\ud83d\ude00", "😀"),
    ],
)
def test_utf8_repair_is_lossless_and_does_not_fold_mathematical_symbols(text, expected):
    result = utf8_text(text)
    assert result == expected
    result.encode("utf-8")


@pytest.mark.parametrize(
    "prefix,codepoint,suffix", [("", 0xD835, ""), ("", 0xDC65, ""), ("matrix ", 0xD835, " (DDG)")]
)
def test_unpaired_surrogate_is_rejected_instead_of_deleted_or_replaced(prefix, codepoint, suffix):
    with pytest.raises(InvalidUnicodeText) as error:
        utf8_text(prefix + chr(codepoint) + suffix)
    assert "Unicode" in str(error.value)
    str(error.value).encode("utf-8")


def test_unicode_tree_repairs_values_and_keys_without_changing_numbers():
    assert utf8_value({"\ud835\udc65": [{"value": "\ud83d\ude00", "score": 10.5}]}) == {
        "𝑥": [{"value": "😀", "score": 10.5}]
    }


def test_unicode_tree_repair_cannot_overwrite_colliding_keys():
    with pytest.raises(InvalidUnicodeText, match="重复字段"):
        utf8_value({"𝑥": 1, "\ud835\udc65": 2})
