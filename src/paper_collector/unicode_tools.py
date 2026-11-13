"""Keep valid mathematical Unicode intact; reject unrecoverable surrogate text."""

from __future__ import annotations

import re
from typing import Any

_PAIR = re.compile(r"[\ud800-\udbff][\udc00-\udfff]")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


class InvalidUnicodeText(UnicodeError):
    pass


def utf8_text(text: str) -> str:
    # Do not use NFC/NFKC or errors='ignore'/'replace': bold mathematical
    # variables and other distinct symbols must not be silently changed.
    text = _PAIR.sub(
        lambda pair: chr(0x10000 + ((ord(pair[0][0]) - 0xD800) << 10) + ord(pair[0][1]) - 0xDC00),
        text,
    )
    invalid = _SURROGATE.search(text)
    if invalid:
        raise InvalidUnicodeText(
            f"存在无法恢复的 Unicode 字形 U+{ord(invalid[0]):04X}（字符位置 {invalid.start()}）"
        )
    return text


def utf8_value(value: Any) -> Any:
    if isinstance(value, str):
        return utf8_text(value)
    if isinstance(value, list):
        return [utf8_value(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = utf8_text(key) if isinstance(key, str) else key
            if normalized in result:
                raise InvalidUnicodeText("Unicode 修复后出现重复字段，拒绝覆盖数据")
            result[normalized] = utf8_value(item)
        return result
    return value
