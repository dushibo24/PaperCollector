from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


class FontRepairError(RuntimeError):
    pass


SUPPORTED_VERSIONS = {"pdf2zh": "1.9.11", "pymupdf": "1.25.2"}
LEGACY_SUBSETTING = """
if not skip_subset_fonts:
    doc_zh.subset_fonts(fallback=True)
    doc_en.subset_fonts(fallback=True)
"""
NATIVE_SUBSETTING = LEGACY_SUBSETTING.replace("fallback=True", "fallback=False")


def repair_font_source(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FontRepairError("pdf2zh 源码无法解析，拒绝修改") from exc
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "translate_stream"
    ]
    if len(functions) != 1:
        raise FontRepairError("未找到唯一的 translate_stream，拒绝修改")
    original = ast.parse(LEGACY_SUBSETTING).body[0]
    repaired = ast.parse(NATIVE_SUBSETTING).body[0]
    candidates = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.If) and ast.dump(node.test) == ast.dump(original.test)
    ]
    if len(candidates) != 1:
        raise FontRepairError("字体子集化调用位置不唯一，拒绝修改")
    candidate = candidates[0]
    if ast.dump(candidate) == ast.dump(repaired):
        return source
    if ast.dump(candidate) != ast.dump(original):
        raise FontRepairError("字体子集化调用与已验证源码不匹配，拒绝修改")
    lines = source.splitlines(keepends=True)
    for statement in reversed(candidate.body):
        argument = statement.value.keywords[0].value
        line = lines[argument.lineno - 1]
        lines[argument.lineno - 1] = (
            line[: argument.col_offset] + "False" + line[argument.end_col_offset :]
        )
    result = "".join(lines)
    compile(result, "pdf2zh/high_level.py", "exec")
    return result


def repair_file(path: Path, *, check: bool = False) -> dict:
    path = path.resolve()
    original = path.read_text(encoding="utf-8")
    repaired = repair_font_source(original)
    result = {
        "path": str(path),
        "status": "already_repaired" if repaired == original else "needs_repair",
        "sha256": hashlib.sha256(repaired.encode()).hexdigest(),
    }
    if repaired == original or check:
        return result
    backup = path.with_name(path.name + ".papercollector-original")
    if not backup.exists():
        with backup.open("x", encoding="utf-8") as handle:
            handle.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".font-repair-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), stat.S_IMODE(path.stat().st_mode))
            handle.write(repaired)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_text(encoding="utf-8") != original:
            raise FontRepairError("修复期间源码发生变化，拒绝覆盖")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {**result, "status": "repaired", "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description="让锁定 pdf2zh 使用 MuPDF 原生字体子集化")
    parser.add_argument("--check", action="store_true", help="仅检查，不修改依赖")
    args = parser.parse_args()
    try:
        versions = {name: importlib.metadata.version(name) for name in SUPPORTED_VERSIONS}
        if versions != SUPPORTED_VERSIONS:
            raise FontRepairError(f"仅支持已验证依赖 {SUPPORTED_VERSIONS}，当前为 {versions}")
        distribution = importlib.metadata.distribution("pdf2zh")
        result = repair_file(
            Path(distribution.locate_file("pdf2zh/high_level.py")), check=args.check
        )
    except (FontRepairError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        print(f"字体修复失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps({**versions, "backend": "mupdf-native", **result}, ensure_ascii=False))
    return int(result["status"] == "needs_repair")


if __name__ == "__main__":
    raise SystemExit(main())
