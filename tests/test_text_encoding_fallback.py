"""#24: text-like parsers must tolerate legacy encodings.

_parse_text / _parse_markdown / _parse_html opened files with a hardcoded
encoding='utf-8'. A GBK/GB18030-encoded file (the default output encoding
of legacy Chinese enterprise tooling) raised UnicodeDecodeError and the
whole document failed ingestion.
"""
import pytest

from core.ingestion import parser as parser_mod

CN_TEXT = "温度传感器技术指标\n工作温度范围：-40℃ 至 125℃\n" * 20


def _write(tmp_path, name, text, encoding):
    f = tmp_path / name
    f.write_bytes(text.encode(encoding))
    return f


def test_gbk_text_parses(tmp_path):
    f = _write(tmp_path, "note.txt", CN_TEXT, "gbk")
    doc = parser_mod.DocumentParser()._parse_text(f)
    assert doc.pages
    assert "温度传感器" in "".join(p.raw_text for p in doc.pages)


def test_gb18030_text_parses(tmp_path):
    f = _write(tmp_path, "spec.txt", CN_TEXT, "gb18030")
    doc = parser_mod.DocumentParser()._parse_text(f)
    assert doc.pages
    assert "温度传感器" in "".join(p.raw_text for p in doc.pages)


def test_utf8_text_unchanged(tmp_path):
    f = _write(tmp_path, "note.txt", CN_TEXT, "utf-8")
    doc = parser_mod.DocumentParser()._parse_text(f)
    assert doc.pages
    assert "温度传感器" in "".join(p.raw_text for p in doc.pages)


def test_utf8_bom_stripped(tmp_path):
    f = _write(tmp_path, "note.txt", CN_TEXT, "utf-8-sig")
    doc = parser_mod.DocumentParser()._parse_text(f)
    assert doc.pages
    text = "".join(p.raw_text for p in doc.pages)
    assert "温度传感器" in text
    assert "\ufeff" not in text


def test_gbk_markdown_parses(tmp_path):
    f = _write(tmp_path, "doc.md", "# 产品规格\n\n" + CN_TEXT, "gbk")
    doc = parser_mod.DocumentParser()._parse_markdown(f)
    assert doc.pages
    assert "产品规格" in "".join(p.raw_text for p in doc.pages)


@pytest.mark.skipif(not parser_mod.HAS_BS4, reason="bs4 not installed")
def test_gbk_html_parses(tmp_path):
    html = "<html><body><h1>数据手册</h1><p>" + CN_TEXT + "</p></body></html>"
    f = _write(tmp_path, "sheet.html", html, "gbk")
    doc = parser_mod.DocumentParser()._parse_html(f)
    assert doc.pages
    assert "数据手册" in "".join(p.raw_text for p in doc.pages)
