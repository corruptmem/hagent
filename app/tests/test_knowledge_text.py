"""Text extraction and passage splitting (design section 13.4)."""

from __future__ import annotations

import pytest
from hagent_app.knowledge.index import fts_query
from hagent_app.knowledge.text import (
    ExtractError,
    Section,
    extract,
    markdown_sections,
    media_type_of,
    safe_name,
    split,
)
from helpers.pdf import make_pdf


def test_pdf_text_layer_gives_one_section_per_page() -> None:
    data = make_pdf(["Boiler manual\nReset the boiler.", None, "Error codes\nE1 low"])
    result = extract(data, "application/pdf")
    assert result.pages == 3
    assert [s.page for s in result.sections] == [1, 3]
    assert "Reset the boiler." in result.sections[0].text
    assert "E1 low" in result.text


def test_pdf_without_text_layer_fails() -> None:
    with pytest.raises(ExtractError, match="no text layer"):
        extract(make_pdf([None, None]), "application/pdf")


def test_broken_pdf_fails() -> None:
    with pytest.raises(ExtractError):
        extract(b"%PDF-1.4 not a pdf", "application/pdf")


def test_markdown_splits_at_headings_but_not_in_code_fences() -> None:
    text = "intro\n\n# Boiler\nfacts\n```\n# not a heading\n```\n## Pump\nmore"
    sections = markdown_sections(text)
    assert [s.heading for s in sections] == [None, "Boiler", "Pump"]
    assert "# not a heading" in sections[1].text


def test_html_drops_scripts_and_splits_at_headings() -> None:
    html = (
        "<html><head><title>x</title><style>p{}</style></head><body>"
        "<p>Intro &amp; more</p><script>alert(1)</script>"
        "<h2>Radiators</h2><p>Bleed them</p><ul><li>yearly</li></ul></body></html>"
    )
    result = extract(html.encode(), "text/html")
    assert [s.heading for s in result.sections] == [None, "Radiators"]
    assert result.sections[0].text == "Intro & more"
    assert "alert" not in result.text
    assert "yearly" in result.sections[1].text


def test_plain_text_and_empty_upload() -> None:
    assert extract(b"hello\n\nworld", "text/plain").sections[0].text == (
        "hello\n\nworld"
    )
    with pytest.raises(ExtractError, match="no text"):
        extract(b"   \n", "text/plain")


def test_split_keeps_heading_and_page_and_limits_length() -> None:
    long_paragraph = " ".join(["word"] * 1000)
    passages = split([Section("Pump", 4, f"short\n\n{long_paragraph}")], max_chars=500)
    assert len(passages) > 2
    assert all(len(p.text) <= 500 + len("Pump\n\n") for p in passages)
    assert all(p.heading == "Pump" and p.page == 4 for p in passages)
    assert [p.ordinal for p in passages] == list(range(len(passages)))


def test_media_types_and_names() -> None:
    assert media_type_of("a.PDF", None) == "application/pdf"
    assert media_type_of("x", "text/markdown; charset=utf-8") == "text/markdown"
    assert media_type_of("x.htm", "application/octet-stream") == "text/html"
    with pytest.raises(ExtractError):
        media_type_of("x.docx", None)
    assert safe_name("Manual (2019).pdf") == "Manual _2019_.pdf"
    assert safe_name("../a/./b.md") == "a/b.md"


def test_fts_query_quotes_every_word() -> None:
    assert fts_query('boiler AND "reset" -x:*') == '"boiler" OR "and" OR "reset" OR "x"'
    assert fts_query("!!!") is None
