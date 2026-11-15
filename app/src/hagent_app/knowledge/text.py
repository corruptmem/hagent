"""Text extraction and passage splitting (design section 13.4).

The first version accepts PDF files with a text layer, Markdown, plain text
and HTML. Extraction gives a list of sections. A **section** is a part of a
document under one heading or on one page. Splitting cuts each section into
**passages**: pieces of text that the search index holds, at most
``MAX_PASSAGE_CHARS`` long. A passage keeps the heading and the page of its
section, so that a search result can name them.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Literal

MAX_PASSAGE_CHARS = 1500

type MediaType = Literal["application/pdf", "text/markdown", "text/plain", "text/html"]

_EXTENSIONS: dict[str, MediaType] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
}
_BY_MEDIA: dict[str, MediaType] = {media: media for media in _EXTENSIONS.values()}
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_NAME_BAD = re.compile(r"[^A-Za-z0-9_\-./ ]+")


class ExtractError(ValueError):
    """The app cannot extract text from the upload."""


@dataclass(frozen=True, slots=True)
class Section:
    """A part of a text under one heading or on one page."""

    heading: str | None
    page: int | None
    text: str


@dataclass(frozen=True, slots=True)
class Passage:
    """One piece of text for the search index."""

    ordinal: int
    heading: str | None
    page: int | None
    text: str


@dataclass(frozen=True, slots=True)
class Extracted:
    """The text of an upload."""

    media_type: MediaType
    sections: list[Section]
    pages: int | None

    @property
    def text(self) -> str:
        """The full text, with headings."""
        return join_sections(self.sections)


def join_sections(sections: list[Section]) -> str:
    """The sections as one text."""
    parts = []
    for section in sections:
        if section.heading:
            parts.append(f"## {section.heading}\n\n{section.text}".strip())
        else:
            parts.append(section.text.strip())
    return "\n\n".join(p for p in parts if p)


def media_type_of(name: str, content_type: str | None) -> MediaType:
    """The media type from the content type or the file extension."""
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base in _BY_MEDIA:
            return _BY_MEDIA[base]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in _EXTENSIONS:
        return _EXTENSIONS[suffix]
    msg = "accepted documents are PDF, Markdown, plain text and HTML"
    raise ExtractError(msg)


def safe_name(name: str) -> str:
    """A store path from an upload name: unsafe characters become ``_``."""
    parts = []
    for part in name.replace("\\", "/").split("/"):
        clean = _NAME_BAD.sub("_", part).strip()
        if clean and clean not in {".", ".."}:
            parts.append(clean[:120])
    if not parts:
        msg = "the document needs a name"
        raise ExtractError(msg)
    return "/".join(parts)[:255]


def decode(data: bytes) -> str:
    """Text from bytes: UTF-8, else Latin-1."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def extract(data: bytes, media_type: MediaType) -> Extracted:
    """Extract the sections of an upload. Raise ``ExtractError``.

    PDF extraction is slow. Call this function in a worker thread.
    """
    if media_type == "application/pdf":
        return _extract_pdf(data)
    text = decode(data)
    if media_type == "text/html":
        sections = _HtmlSections.parse(text)
    elif media_type == "text/markdown":
        sections = markdown_sections(text)
    else:
        sections = [Section(None, None, text.strip())]
    sections = [s for s in sections if s.text.strip() or s.heading]
    if not any(s.text.strip() for s in sections):
        msg = "the document holds no text"
        raise ExtractError(msg)
    return Extracted(media_type, sections, None)


def _extract_pdf(data: bytes) -> Extracted:
    from pypdf import PdfReader  # noqa: PLC0415 - slow import, used rarely
    from pypdf.errors import PdfReadError  # noqa: PLC0415

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            msg = "the PDF is encrypted"
            raise ExtractError(msg)
        sections = []
        for number, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                sections.append(Section(None, number, text))
        pages = len(reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError) as err:
        if isinstance(err, ExtractError):
            raise
        msg = f"the PDF cannot be read: {err}"
        raise ExtractError(msg) from err
    if not sections:
        msg = "the PDF has no text layer"
        raise ExtractError(msg)
    return Extracted("application/pdf", sections, pages)


def markdown_sections(text: str) -> list[Section]:
    """Split Markdown at ATX headings. Headings inside code fences stay text."""
    sections: list[Section] = []
    heading: str | None = None
    lines: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        match = None if fenced else _HEADING.match(line)
        if match:
            if heading is not None or "".join(lines).strip():
                sections.append(Section(heading, None, "\n".join(lines).strip()))
            heading = match.group(2).strip()
            lines = []
        else:
            lines.append(line)
    if heading is not None or "".join(lines).strip():
        sections.append(Section(heading, None, "\n".join(lines).strip()))
    return sections


class _HtmlSections(HTMLParser):
    """Collect HTML text. Headings ``h1`` to ``h6`` start a new section."""

    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "table", "section", "article", "pre", "ul"}
    )
    _SKIP = frozenset({"script", "style", "head", "template", "noscript"})
    _HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[Section] = []
        self._heading: str | None = None
        self._buffer: list[str] = []
        self._heading_buffer: list[str] | None = None
        self._skip = 0

    @classmethod
    def parse(cls, text: str) -> list[Section]:
        parser = cls()
        parser.feed(text)
        parser.close()
        parser._flush()
        return parser.sections

    def _flush(self) -> None:
        body = re.sub(r"[ \t]+", " ", "".join(self._buffer))
        body = re.sub(r"\n\s*\n\s*", "\n\n", body).strip()
        if body or self._heading:
            self.sections.append(Section(self._heading, None, body))
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._HEADINGS:
            self._flush()
            self._heading_buffer = []
        elif tag in self._BLOCK:
            self._buffer.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._HEADINGS and self._heading_buffer is not None:
            self._heading = " ".join("".join(self._heading_buffer).split()) or None
            self._heading_buffer = None
        elif tag in self._BLOCK:
            self._buffer.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._heading_buffer is not None:
            self._heading_buffer.append(data)
        else:
            self._buffer.append(data)


def split(sections: list[Section], max_chars: int = MAX_PASSAGE_CHARS) -> list[Passage]:
    """Split sections into passages at paragraphs. Long paragraphs split at words."""
    passages: list[Passage] = []
    for section in sections:
        chunks: list[str] = []
        current = ""
        for paragraph in re.split(r"\n\s*\n", section.text):
            paragraph = paragraph.strip()  # noqa: PLW2901
            if not paragraph:
                continue
            for piece in _pieces(paragraph, max_chars):
                if current and len(current) + 2 + len(piece) > max_chars:
                    chunks.append(current)
                    current = piece
                else:
                    current = f"{current}\n\n{piece}" if current else piece
        if current:
            chunks.append(current)
        if not chunks and section.heading:
            chunks.append("")
        for chunk in chunks:
            text = f"{section.heading}\n\n{chunk}".strip() if section.heading else chunk
            passages.append(Passage(len(passages), section.heading, section.page, text))
    return passages


def _pieces(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for word in paragraph.split():
        while len(word) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(word[:max_chars])
            word = word[max_chars:]  # noqa: PLW2901
        if current and len(current) + 1 + len(word) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        pieces.append(current)
    return pieces
