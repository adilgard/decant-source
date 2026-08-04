"""USLM 1.0 -> readable text + a structural index, in one pass.

USLM (United States Legislative Markup) is the XML the House publishes the
US Code in. Everything in this module is knowledge ABOUT that format, which
is exactly why it lives outside knowledge_hub_pkg: the core pipeline is
corpus-agnostic and must not learn what a `<subsection>` is.

ONE PASS, TWO OUTPUTS, ONE SET OF OFFSETS. The renderer writes Markdown and
records, for every provision and every cross-reference, where it landed in
that Markdown. Producing the text and the offsets separately would be two
walks that can disagree; a fact would then cite a span that says something
else. Here a provision's span is, by construction, the slice of the string
that was written while rendering it.

HEADING DEPTH IS A CHUNKING DECISION. Titles and chapters render as `#`
and `##`, sections as `###`, and everything below a section renders as
inline labelled text. The core chunker splits on the document's dominant
heading level (capped at 3), so this makes the SECTION the parent chunk —
the unit a citation is normally written against. Subsections stay inside
their section rather than being scattered across chunks.

Nothing here imports knowledge_hub. This module is a pure function from
bytes to a small dataclass tree, which is what makes it testable without a
pipeline and replaceable without touching one.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

# Structural levels above a section. Rendered as Markdown headings so the
# chunker can see the document's shape.
BIG_LEVELS = ("title", "subtitle", "chapter", "subchapter", "part",
              "subpart", "division", "subdivision")
SECTION_LEVEL = "section"
# Levels below a section. Rendered inline: a section is one chunk.
SMALL_LEVELS = ("subsection", "paragraph", "subparagraph", "clause",
                "subclause", "item", "subitem", "subsubitem")
PROVISION_LEVELS = BIG_LEVELS + (SECTION_LEVEL,) + SMALL_LEVELS

# Elements whose character data is body text.
TEXT_LEVELS = ("chapeau", "content", "p", "continuation", "text")

# Notes, source credits and editorial apparatus. Rendered (they are part of
# the document a human reads) but never treated as provisions.
NOTE_LEVELS = ("note", "sourceCredit", "editorialNote", "statutoryNote")
# `<notes>` is a WRAPPER, not a note. Rendering it as one emits a blank
# paragraph before every note block it contains.
NOTE_CONTAINERS = ("notes",)

_WS = re.compile(r"\s+")
# A USLM identifier path segment: a level prefix plus its number, e.g.
# 't26', 'stA', 'ch1', 's63', or a bare 'a' for sub-section levels.
_SEG = re.compile(r"^(?P<prefix>[a-z]+)(?P<num>[A-Za-z0-9.\-]+)$")

_SEGMENT_LABELS = {
    "t": "", "st": "subtitle ", "ch": "ch. ", "sch": "subch. ",
    "pt": "pt. ", "spt": "subpt. ", "d": "div. ",
}


class UslmError(ValueError):
    """The bytes are not usable USLM. Raised with what was actually wrong,
    so an operator sees the document's problem rather than a parser trace."""


def _local(tag: str) -> str:
    """Tag name without its namespace. USLM documents are namespaced and
    the namespace URI has changed between published revisions, so matching
    on the local name is the version-tolerant choice."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean(text: Optional[str]) -> str:
    """Collapse XML pretty-printing whitespace. USLM is indented for human
    readability, and that indentation is not part of the law."""
    return _WS.sub(" ", text) if text else ""


@dataclass
class Provision:
    """One numbered unit of law, at any level."""
    identifier: str                 # USLM @identifier, e.g. /us/usc/t26/s1/a
    level: str                      # section, subsection, chapter, ...
    num: str                        # '1', 'a', 'A' — the bare number
    heading: Optional[str]
    citation: str                   # '26 U.S.C. § 1(a)'
    parent: Optional[str]           # parent identifier, None at the root
    char_start: int                 # into the rendered text
    char_end: int
    label_start: int                # the span of just this provision's
    label_end: int                  # heading/label line — a tight citation


@dataclass
class CrossReference:
    """One `<ref>`: this provision pointing at another provision."""
    owner: str                      # identifier of the containing provision
    href: str                       # USLM target path
    char_start: int
    char_end: int
    text: str = ""                  # the rendered link text, filled at the end


@dataclass
class ParsedDocument:
    text: str
    provisions: list[Provision] = field(default_factory=list)
    references: list[CrossReference] = field(default_factory=list)
    title: Optional[str] = None
    doc_number: Optional[str] = None

    def by_identifier(self) -> dict[str, Provision]:
        return {p.identifier: p for p in self.provisions}


class _Writer:
    """Append-only text buffer that reports where each write landed. The
    single source of offset truth — no position is ever computed twice."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.pos = 0

    def write(self, chunk: str) -> int:
        start = self.pos
        self._parts.append(chunk)
        self.pos += len(chunk)
        return start

    def write_prose(self, chunk: str) -> None:
        """Body text, with runs of whitespace collapsed ACROSS writes.

        `_clean` collapses whitespace within one XML text node, but a USLM
        element boundary produces several nodes in a row that each collapse
        to a single space — which is how ' Effective Date:   This section'
        happens. Dropping the leading space when the buffer already ends in
        one fixes it at the only place that can see both sides. Never
        rewrites what is already written, so no recorded offset moves."""
        if not chunk:
            return
        text = self.text()
        if chunk.startswith(" ") and (not text or text[-1] in " \n"):
            chunk = chunk.lstrip(" ")
            if not chunk:
                return
        self.write(chunk)

    def rstrip_to(self, mark: int) -> None:
        """Drop trailing whitespace written since `mark`. Safe for spans:
        a recorded span always ends on a non-space character, so trimming
        the tail can never truncate one."""
        text = self.text()
        trimmed = text[:mark] + text[mark:].rstrip()
        if trimmed != text:
            self._parts[:] = [trimmed]
            self.pos = len(trimmed)

    def ensure_blank_line(self) -> None:
        """Markdown needs a blank line before a heading or the chunker's
        heading regex (anchored per line) will not see it."""
        text = self.text()
        if not text:
            return
        if not text.endswith("\n\n"):
            self.write("\n" if text.endswith("\n") else "\n\n")

    def text(self) -> str:
        return "".join(self._parts)


def citation_for(identifier: str) -> str:
    """A human citation built from the USLM identifier path, deterministically.

    `/us/usc/t26/s1/a/2` -> `26 U.S.C. § 1(a)(2)`. Segments before the
    section carry a level prefix ('t26', 'ch1'); segments after it are bare
    and become the familiar parenthesised trail. An identifier we cannot
    read is returned as-is rather than guessed at — a wrong citation is
    worse than an ugly one.
    """
    if not identifier or not identifier.startswith("/us/usc/"):
        return identifier or ""
    segments = [s for s in identifier[len("/us/usc/"):].split("/") if s]
    if not segments:
        return identifier

    title_num = ""
    parts: list[str] = []
    section_num = ""
    trailing: list[str] = []
    for segment in segments:
        match = _SEG.match(segment)
        if section_num:                       # everything after the section
            trailing.append(segment)
            continue
        if match is None:
            trailing.append(segment)
            continue
        prefix, num = match.group("prefix"), match.group("num")
        if prefix == "t":
            title_num = num
        elif prefix == "s":
            section_num = num
        elif prefix in _SEGMENT_LABELS:
            parts.append(_SEGMENT_LABELS[prefix] + num)
        else:
            parts.append(f"{prefix} {num}")

    stem = f"{title_num} U.S.C." if title_num else "U.S.C."
    if section_num:
        tail = "".join(f"({t})" for t in trailing)
        return f"{stem} § {section_num}{tail}"
    if parts:
        return f"{stem} {parts[-1]}"
    return stem


def parse_uslm(content: bytes) -> ParsedDocument:
    """USLM bytes -> rendered Markdown plus the structural index."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise UslmError(f"not well-formed XML: {e}") from e

    if _local(root.tag) not in ("uscDoc", "lawDoc", "bill", "resolution"):
        raise UslmError(
            f"root element is <{_local(root.tag)}>, which is not a USLM "
            f"document root (expected uscDoc/lawDoc/bill/resolution)")

    body = None
    for candidate in root:
        if _local(candidate.tag) == "main":
            body = candidate
            break
    if body is None:
        raise UslmError("USLM document has no <main> element")

    out = _Writer()
    doc = ParsedDocument(text="")
    _walk(body, out, doc, parent=None, depth=0)
    doc.text = out.text()
    for ref in doc.references:
        ref.text = doc.text[ref.char_start:ref.char_end]
    if not doc.provisions:
        raise UslmError("USLM document contains no numbered provisions")
    return doc


def _walk(elem: ET.Element, out: _Writer, doc: ParsedDocument,
          parent: Optional[str], depth: int) -> None:
    for child in elem:
        tag = _local(child.tag)
        if tag in PROVISION_LEVELS:
            _emit_provision(child, tag, out, doc, parent, depth)
        elif tag in TEXT_LEVELS:
            _emit_body(child, out, doc, parent)
        elif tag in NOTE_LEVELS:
            _emit_note(child, out, doc, parent)
        elif tag in NOTE_CONTAINERS:
            _walk(child, out, doc, parent, depth)   # a wrapper, not content
        elif tag in ("num", "heading"):
            continue          # consumed by the owning provision
        else:
            _walk(child, out, doc, parent, depth)


def _child_text(elem: ET.Element, name: str) -> Optional[str]:
    for child in elem:
        if _local(child.tag) == name:
            return _clean("".join(child.itertext())).strip() or None
    return None


def _emit_provision(elem: ET.Element, level: str, out: _Writer,
                    doc: ParsedDocument, parent: Optional[str],
                    depth: int) -> None:
    identifier = elem.get("identifier") or ""
    num_text = _child_text(elem, "num") or ""
    heading = _child_text(elem, "heading")
    # `<num value="1">§ 1.</num>` — the attribute is the machine-readable
    # number; the element text is decoration that varies by publication.
    num = ""
    for child in elem:
        if _local(child.tag) == "num":
            num = (child.get("value") or "").strip()
            break
    if not num:
        num = num_text.strip("§ .()").strip()

    citation = citation_for(identifier) if identifier else num_text.strip()

    start = out.pos
    if level in BIG_LEVELS or level == SECTION_LEVEL:
        out.ensure_blank_line()
        # Levels ABOVE a section cap at '##' so they never collide with the
        # section's '###'. If a chapter and a section shared a level, the
        # chunker (which splits on the dominant one) would cut a chunk at
        # every chapter heading too, leaving stray heading-only passages
        # competing with the sections in retrieval.
        hashes = "#" * min(2, depth + 1) if level in BIG_LEVELS else "###"
        out.write(f"{hashes} ")
        label_start = out.pos
        line = citation + (f" — {heading}" if heading else "")
        out.write(line)
        label_end = out.pos
        out.write("\n\n")
        next_depth = depth + 1
    else:
        # Below a section: a labelled line, NOT a heading, so the section
        # and its subsections stay one retrievable passage.
        if out.text() and not out.text().endswith("\n"):
            out.write("\n")
        label_start = out.pos
        out.write(f"({num})" if num else "")
        if heading:
            out.write(f" {heading}")
        label_end = out.pos
        # A headed subsection reads as a label over its text; an unheaded
        # one continues on the same line, the way the Code sets it.
        out.write("\n" if heading else " ")
        next_depth = depth

    record = Provision(
        identifier=identifier, level=level, num=num, heading=heading,
        citation=citation, parent=parent, char_start=start, char_end=start,
        label_start=label_start, label_end=label_end)
    doc.provisions.append(record)

    if doc.title is None and level == "title":
        doc.title = heading or citation
        doc.doc_number = num

    _walk(elem, out, doc, parent=identifier or parent, depth=next_depth)
    record.char_end = out.pos


def _emit_body(elem: ET.Element, out: _Writer, doc: ParsedDocument,
               owner: Optional[str]) -> None:
    """Body text, capturing every `<ref>` span as it is written."""
    _inline(elem, out, doc, owner)
    if not out.text().endswith("\n"):
        out.write("\n")


def _emit_note(elem: ET.Element, out: _Writer, doc: ParsedDocument,
               owner: Optional[str]) -> None:
    """Editorial apparatus: rendered because a reader wants it, never
    indexed as a provision. Buffered and trimmed rather than streamed —
    note bodies are the whitespace-heaviest part of a USLM file and would
    otherwise leave ragged runs of spaces in the middle of the text."""
    out.ensure_blank_line()
    heading = _child_text(elem, "heading")
    if heading:
        out.write(f"{heading}: ")
    mark = out.pos
    _inline(elem, out, doc, owner, skip=("heading",))
    out.rstrip_to(mark)
    if out.pos == mark and not heading:
        return                      # an empty note is not worth a paragraph
    out.write("\n")


def _inline(elem: ET.Element, out: _Writer, doc: ParsedDocument,
            owner: Optional[str], skip: tuple[str, ...] = ()) -> None:
    """Render mixed content, recording cross-reference spans.

    A `<ref>`'s span is captured around the recursive render of its own
    children, so the recorded offsets bound exactly the link text that
    reaches the reader — which is what a citation should point at."""
    out.write_prose(_clean(elem.text))
    for child in elem:
        tag = _local(child.tag)
        if tag in skip:
            pass
        elif tag == "ref":
            start = out.pos
            _inline(child, out, doc, owner)
            href = child.get("href") or ""
            if href:
                doc.references.append(CrossReference(
                    owner=owner or "", href=href,
                    char_start=start, char_end=out.pos))
        elif tag in PROVISION_LEVELS or tag in NOTE_LEVELS:
            # A nested provision inside body text: hand it back to the main
            # walk so it is indexed, not flattened into prose.
            _walk(elem, out, doc, owner, depth=3)
            return
        else:
            _inline(child, out, doc, owner)
        out.write_prose(_clean(child.tail))
