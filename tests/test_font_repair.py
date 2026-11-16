import ast
import hashlib
from pathlib import Path
from types import CodeType, FunctionType, SimpleNamespace

import pytest

from paper_collector import font_repair


@pytest.fixture
def legacy_source():
    return (
        "def translate_stream(doc_zh, doc_en, skip_subset_fonts=False):\n"
        "    if not skip_subset_fonts:\n"
        "        doc_zh.subset_fonts(fallback=True)\n"
        "        doc_en.subset_fonts(fallback=True)\n"
        "    return doc_zh, doc_en\n"
    )


def translator(source):
    compiled = compile(source, "font-regression", "exec")
    code = next(code for code in compiled.co_consts if isinstance(code, CodeType))
    return FunctionType(code, {}, argdefs=(False,))


class Document:
    def __init__(self):
        self.calls = []

    def subset_fonts(self, *, fallback):
        self.calls.append(fallback)
        if fallback:
            error = ValueError("bad 'value'")
            raise error


def test_original_legacy_backend_reproduces_production_failure(legacy_source):
    with pytest.raises(ValueError, match="bad 'value'"):
        translator(legacy_source)(Document(), Document())


def test_repair_subsets_both_outputs_with_native_backend(legacy_source):
    chinese, bilingual = Document(), Document()
    translator(font_repair.repair_font_source(legacy_source))(chinese, bilingual)
    assert chinese.calls == [False]
    assert bilingual.calls == [False]


def test_explicit_emergency_skip_remains_available(legacy_source):
    chinese, bilingual = Document(), Document()
    translator(font_repair.repair_font_source(legacy_source))(
        chinese, bilingual, skip_subset_fonts=True
    )
    assert chinese.calls == bilingual.calls == []


def test_repair_only_changes_two_backend_arguments_and_is_idempotent(legacy_source):
    source = "unrelated_before = 123\n" + legacy_source + "unrelated_after = 456\n"
    repaired = font_repair.repair_font_source(source)
    assert repaired == source.replace("fallback=True", "fallback=False")
    assert font_repair.repair_font_source(repaired) == repaired
    assert len(ast.parse(repaired).body) == len(ast.parse(source).body)


@pytest.mark.parametrize(
    "change",
    [
        lambda source: source.replace("translate_stream", "another_function"),
        lambda source: source.replace("doc_en.subset_fonts", "doc_en.another_method"),
        lambda source: source.replace("fallback=True", "fallback=unknown"),
        lambda source: source.replace("fallback=True", "fallback=False", 1),
        lambda source: source + source,
        lambda _source: "def translate_stream(:",
    ],
)
def test_unknown_source_is_not_modified(legacy_source, tmp_path, change):
    path = tmp_path / "high_level.py"
    source = change(legacy_source)
    path.write_text(source)
    with pytest.raises(font_repair.FontRepairError):
        font_repair.repair_file(path)
    assert path.read_text() == source
    assert sorted(item.name for item in tmp_path.iterdir()) == ["high_level.py"]


def test_file_repair_supports_check_backup_permissions_and_repeat(legacy_source, tmp_path):
    path = tmp_path / "high_level.py"
    path.write_text(legacy_source)
    path.chmod(0o640)
    assert font_repair.repair_file(path, check=True)["status"] == "needs_repair"
    assert path.read_text() == legacy_source
    result = font_repair.repair_file(path)
    assert result["status"] == "repaired"
    assert Path(result["backup"]).read_text() == legacy_source
    assert path.stat().st_mode & 0o777 == 0o640
    assert font_repair.repair_file(path)["status"] == "already_repaired"
    assert font_repair.repair_file(path, check=True)["status"] == "already_repaired"


def test_failed_atomic_replacement_keeps_original(legacy_source, tmp_path, monkeypatch):
    path = tmp_path / "high_level.py"
    path.write_text(legacy_source)

    def fail_replace(*_args):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        font_repair.repair_file(path)
    assert path.read_text() == legacy_source
    assert not list(tmp_path.glob(".font-repair-*"))


def test_cli_refuses_unverified_dependency_versions(monkeypatch, capsys):
    monkeypatch.setattr(font_repair.importlib.metadata, "version", lambda _name: "9.9.9")
    monkeypatch.setattr(font_repair.sys, "argv", ["font_repair"])
    assert font_repair.main() == 1
    assert "9.9.9" in capsys.readouterr().err


def test_cli_check_and_repair_use_the_selected_environment(
    legacy_source, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "high_level.py"
    path.write_text(legacy_source)
    monkeypatch.setattr(
        font_repair.importlib.metadata, "version", font_repair.SUPPORTED_VERSIONS.__getitem__
    )
    monkeypatch.setattr(
        font_repair.importlib.metadata,
        "distribution",
        lambda _name: SimpleNamespace(locate_file=lambda _relative: path),
    )
    monkeypatch.setattr(font_repair.sys, "argv", ["font_repair", "--check"])
    assert font_repair.main() == 1
    assert '"status": "needs_repair"' in capsys.readouterr().out
    assert path.read_text() == legacy_source
    monkeypatch.setattr(font_repair.sys, "argv", ["font_repair"])
    assert font_repair.main() == 0
    assert '"status": "repaired"' in capsys.readouterr().out
    monkeypatch.setattr(font_repair.sys, "argv", ["font_repair", "--check"])
    assert font_repair.main() == 0
    assert '"status": "already_repaired"' in capsys.readouterr().out


def make_test_font(path):
    builder_module = pytest.importorskip("fontTools.fontBuilder")
    pen_module = pytest.importorskip("fontTools.pens.ttGlyphPen")
    characters = "Math: Ax = α ≤ ∑; 0123"
    glyph_order = [".notdef"] + [f"glyph_{index}" for index in range(512)]
    builder = builder_module.FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(
        {
            ord(character): glyph_order[index + 1]
            for index, character in enumerate(dict.fromkeys(characters))
        }
    )
    glyphs = {}
    metrics = {}
    for index, name in enumerate(glyph_order):
        pen = pen_module.TTGlyphPen(None)
        pen.moveTo((40, 0))
        pen.lineTo((400 + index % 100, 0))
        pen.lineTo((400 + index % 100, 700))
        pen.lineTo((40, 700))
        pen.closePath()
        glyphs[name] = pen.glyph()
        metrics[name] = (600 + index % 100, 40)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "PaperCollectorRegression", "styleName": "Regular"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)
    return characters


def font_regression_document(path, variant):
    pymupdf = pytest.importorskip("pymupdf")
    characters = make_test_font(path)
    document = pymupdf.open()
    page = document.new_page()
    writer = pymupdf.TextWriter(page.rect)
    writer.append((50, 80), characters, font=pymupdf.Font(fontfile=str(path)), fontsize=12)
    writer.write_text(page)
    font_xref = page.get_fonts()[0][0]
    descendant = document.xref_get_key(font_xref, "DescendantFonts")[1]
    descendant_xref = int(descendant[1:-1].replace("0 R", ""))
    widths = document.xref_get_key(descendant_xref, "W")
    if variant != "indirect_widths":
        document.xref_set_key(descendant_xref, "W", document.xref_object(int(widths[1].split()[0])))
    if variant == "missing_widths":
        document.xref_set_key(descendant_xref, "W", "null")
    default_width = {"real_width": "1000.5", "zero_width": "0", "missing_widths": "1000.5"}.get(
        variant, "null"
    )
    document.xref_set_key(descendant_xref, "DW", default_width)
    writer = pymupdf.TextWriter(page.rect)
    writer.append((50, 120), "中文字体与数学符号", font=pymupdf.Font("cjk"), fontsize=12)
    writer.write_text(page)
    output = document.tobytes(garbage=4, deflate=True)
    document.close()
    return pymupdf.open(stream=output, filetype="pdf")


def render_signature(document):
    pymupdf = pytest.importorskip("pymupdf")
    return [
        (
            page.get_text(),
            hashlib.sha256(page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).samples).hexdigest(),
        )
        for page in document
    ]


def test_real_legacy_backend_reproduces_font_width_error(tmp_path, legacy_source):
    with (
        font_regression_document(tmp_path / "regression.ttf", "missing_default") as document,
        pytest.raises(ValueError, match="bad 'value'"),
    ):
        translator(legacy_source)(document, document)


@pytest.mark.parametrize(
    "variant", ["missing_default", "indirect_widths", "real_width", "zero_width", "missing_widths"]
)
def test_native_font_subsetting_preserves_chinese_math_text_and_pixels(
    legacy_source, tmp_path, variant
):
    pymupdf = pytest.importorskip("pymupdf")
    with font_regression_document(tmp_path / "regression.ttf", variant) as chinese:
        before = render_signature(chinese)
        original = chinese.tobytes(garbage=4, deflate=True)
        with pymupdf.open(stream=original, filetype="pdf") as bilingual:
            translator(font_repair.repair_font_source(legacy_source))(chinese, bilingual)
            for document in (chinese, bilingual):
                output = document.tobytes(garbage=4, deflate=True)
                with pymupdf.open(stream=output, filetype="pdf") as reopened:
                    assert render_signature(reopened) == before
                    assert "中文字体与数学符号" in reopened[0].get_text()
                    assert "α ≤ ∑" in reopened[0].get_text()
                    assert len(output) < len(original) / 2
