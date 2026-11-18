"""Small, dependency-free document ingestion helpers.

Markdown is split on ATX headings while retaining 1-based line bounds. HTML is
converted to the same passage shape with block-level, not byte-level, lines.
"""
from __future__ import annotations

import re
from hashlib import sha256
from html.parser import HTMLParser
from typing import Iterable, Literal

from app.rag.models import DocumentMetadata, Passage

SourceFormat = Literal["markdown", "html", "text"]
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_BLANK = re.compile(r"\n{3,}")


def content_sha256(content: bytes | str) -> str:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return sha256(data).hexdigest()


def _passage(metadata: DocumentMetadata, title: str, section: str, lines: list[str],
             line_start: int, line_end: int) -> Passage:
    text = _BLANK.sub("\n\n", "\n".join(lines).strip())
    return Passage(
        document_id=metadata.document_id,
        version=metadata.version,
        title=title or metadata.title,
        section=section,
        text=text,
        line_start=max(1, line_start),
        line_end=max(line_start, line_end),
        metadata=metadata,
    )


def parse_markdown(text: str, metadata: DocumentMetadata) -> tuple[Passage, ...]:
    """Split Markdown on level 2+ ATX headings and keep source line bounds."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = metadata.title
    passages: list[Passage] = []
    current_section: str | None = None
    current_start = 1
    body: list[str] = []

    def flush(last_line: int) -> None:
        nonlocal body
        if current_section is None:
            body = []
            return
        if any(line.strip() for line in body):
            # Do not extend a citation over trailing blank lines.
            while last_line >= current_start and not lines[last_line - 1].strip():
                last_line -= 1
            passages.append(_passage(metadata, title, current_section, body, current_start, last_line))
        body = []

    for number, line in enumerate(lines, 1):
        match = _HEADING.match(line)
        if not match:
            if current_section is not None:
                body.append(line)
            continue
        level, label = len(match.group(1)), match.group(2).strip()
        if level == 1 and not passages and current_section is None:
            title = label
            continue
        if level >= 2:
            flush(number - 1)
            current_section = label
            current_start = number
            body = []

    flush(len(lines))
    if not passages and any(line.strip() for line in lines):
        content = [line for line in lines if line.strip() and not _HEADING.match(line)]
        if content:
            passages.append(_passage(metadata, title, title, content, 1, len(lines)))
    return tuple(passages)


class _HTMLCollector(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._heading: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self._heading = tag
        elif self._skip_depth == 0 and tag in {"p", "li", "pre", "blockquote", "td", "th"}:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "blockquote", "td", "th"}:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buffer.append(data)

    def _flush(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.blocks.append((self._heading or "p", text))
        self._buffer = []
        self._heading = None

    def close(self) -> None:
        super().close()
        self._flush()


def parse_html(text: str, metadata: DocumentMetadata) -> tuple[Passage, ...]:
    """Extract headings and text blocks from HTML in source order."""
    parser = _HTMLCollector()
    parser.feed(text)
    parser.close()
    title = metadata.title
    passages: list[Passage] = []
    section = metadata.title
    body: list[str] = []
    start = 1

    def flush(end: int) -> None:
        nonlocal body
        if any(body):
            passages.append(_passage(metadata, title, section, body, start, end))
        body = []

    block = 0
    for tag, value in parser.blocks:
        block += 1
        if tag == "h1" and len(passages) == 0 and not body:
            title = value
            continue
        if tag in {"h2", "h3"}:
            flush(block - 1)
            section = value
            start = block
            continue
        if tag == "h1":
            continue
        body.append(value)
    flush(block)
    return tuple(passages)


def parse_text(text: str, metadata: DocumentMetadata) -> tuple[Passage, ...]:
    """Split plain text into blank-line paragraphs."""
    paragraphs: list[tuple[str, int, int]] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body: list[str] = []
    start = 1
    for number, line in enumerate(lines, 1):
        if line.strip():
            if not body:
                start = number
            body.append(line)
        elif body:
            paragraphs.append(("\n".join(body), start, number - 1))
            body = []
    if body:
        paragraphs.append(("\n".join(body), start, len(lines)))
    return tuple(
        _passage(metadata, metadata.title, metadata.title, [content], start, end)
        for content, start, end in paragraphs
        if content.strip()
    )


def chunk_document(text: str, metadata: DocumentMetadata, *, format: SourceFormat = "markdown") -> tuple[Passage, ...]:
    if format == "markdown":
        return parse_markdown(text, metadata)
    if format == "html":
        return parse_html(text, metadata)
    if format == "text":
        return parse_text(text, metadata)
    raise ValueError(f"Unsupported source format: {format}")
