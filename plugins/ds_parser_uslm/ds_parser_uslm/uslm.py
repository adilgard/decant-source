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

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

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

# Statuses that make an element PAST law rather than current law. Same
# treatment as editorial apparatus: rendered, because a reader searching the
# corpus wants to find that § 4521 was repealed in 1962, but never a
# Provision, because it is not one now.
#
# This is also the fix for a real collision in the published data. Title 26
# carries TWO <chapter identifier="/us/usc/t26/stD/ch38">: a repealed chapter
# 38 and the current ENVIRONMENTAL TAXES chapter 38 (OLRC footnotes the first
# with "A new chapter 38 (§ 4611 et seq.) follows"). Both render the citation
# '26 U.S.C. ch. 38' and carry the same uslm_identifier, so nothing downstream
# can tell them apart and the two silently become one entity. Dropping the
# repealed one from the provision set removes the collision at its source.
SKIPPED_STATUSES = frozenset({"repealed"})

# Notes, source credits and editorial apparatus. Rendered (they are part of
# the document a human reads) but never treated as provisions.
NOTE_LEVELS = ("note", "sourceCredit", "editorialNote", "statutoryNote")
# `<notes>` is a WRAPPER, not a note. Rendering it as one emits a blank
# paragraph before every note block it contains.
NOTE_CONTAINERS = ("notes",)

# Verbatim quotation of ANOTHER document — a note quoting the text of the
# amending Public Law: `<quotedContent origin="/us/pl/...">`. The quote
# contains `<paragraph>`/`<subparagraph>` markup with num and heading but no
# identifier, because quoted PL text has none. Rendered (the reader wants the
# quoted language) but NOTHING inside is ever indexed: not a Provision, not a
# CrossReference. Treating quoted markup as law put 12,408 identifier-less
# provisions (17.5% of the count) into the first full-title parse, and their
# shared ""-identifier entry in by_identifier() gave ownerless refs a junk
# subject — the source of the ~128 keyless mentions the fuzzy path had to
# separate one by one (Job 8, D4). `quotedText` is the schema's inline
# sibling, handled the same way for the same reason.
QUOTED_LEVELS = ("quotedContent", "quotedText")

_WS = re.compile(r"\s+")
# A USLM identifier path segment: a level prefix plus its number, e.g.
# 't26', 'stA', 'ch1', 's63', or a bare 'a' for sub-section levels.
#
# THE NUMBER CLASS INCLUDES THE EN DASH (U+2013), not just the ASCII hyphen.
# OLRC punctuates compound section numbers typographically — `s300gg–44`,
# `s1320a–1`, `s1400Z–2` — and it appears 261 times in Title 26's identifiers
# alone. An ASCII-only class silently failed every one of them: the segment
# fell through to the parenthesised trail and the citation came out as bare
# '26 U.S.C.', which is not merely ugly. Every en-dashed section in a title
# rendered as the SAME string, so §§ 1400Z-1 and 1400Z-2 and all 89 of their
# descendants became one surface, and 88 cross-reference targets across 14
# other titles collapsed per-title. Found on the first full-title run
# (2026-08-04) by core's one-surface-one-key guard, which is the only reason
# it surfaced as quarantine rather than as a silent mass merge.
_SEG = re.compile(r"^(?P<prefix>[a-z]+)(?P<num>[A-Za-z0-9.–\-]+)$")

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
    # Rendered but deliberately not provisions, counted so a run can say so
    # rather than quietly returning fewer facts than the markup implies.
    skipped_by_status: int = 0
    duplicate_identifiers: list[str] = field(default_factory=list)
    # Numbered elements inside quoted amendment text, rendered and refused as
    # provisions. Counted separately from skipped_by_status because past law
    # and quoted-other-document are different answers to "where did the
    # markup's count go".
    quoted_elements: int = 0
    # Membership set maintained as provisions are appended. A full title runs
    # ~59,000 of them, so the duplicate check has to be O(1) per element —
    # rebuilding by_identifier() to ask "have I seen this?" would make the
    # parse quadratic.
    _claimed: set[str] = field(default_factory=set)

    def claim(self, identifier: str) -> bool:
        """Record an identifier, returning False if something already holds it."""
        if identifier in self._claimed:
            return False
        self._claimed.add(identifier)
        return True

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
    if _WS.search(identifier):
        # TWO identifiers in one attribute, space separated — USLM does this for
        # repealed ranges (`/us/usc/t26/s4531 /us/usc/t26/s4532`), 13 times in
        # Title 26. Parsed as one path it yields a citation built from the LAST
        # title and section in the string with the first spliced into the trail:
        # '26 U.S.C. § 4532(s4531 )'. Plausible-looking and simply false, which
        # is the worst kind. Which of the two the element means is not knowable
        # here, so neither is guessed at.
        return identifier
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
        # THE WHOLE PATH, not parts[-1]. Structural labels repeat everywhere —
        # Title 26 alone has 46 parts numbered II and 36 subchapters lettered
        # B — so the last label names a KIND of place, not a place. On the
        # first full-title run (2026-08-04) that collided 25 of the corpus's
        # 129 structural citations, quarantined the part_of facts of 11 files,
        # and left 46 entities all named '26 U.S.C. pt. II'. The Bluebook cites
        # these the same way for the same reason: 'pt. II' means nothing
        # without the subchapter and chapter it sits in.
        return f"{stem} {', '.join(parts)}"
    # A TITLE-ONLY CITATION IS NEVER AN ANSWER when the identifier named
    # something inside the title. '26 U.S.C.' does not identify a provision, so
    # returning it makes every unreadable identifier in a title render
    # identically — and identical surfaces are how unrelated provisions become
    # one entity. The identifier is ugly as a citation and perfectly unique as
    # one, which is the right trade: the docstring's own rule is that a wrong
    # citation is worse than an ugly one, and a colliding one is worse still.
    # The en-dash bug above is why this exists; the guard is here so the NEXT
    # unhandled punctuation degrades to unique instead of to a mass merge.
    if len(segments) > 1:
        return identifier
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
    if not doc.provisions and not doc.skipped_by_status \
            and not doc.quoted_elements:
        raise UslmError("USLM document contains no numbered provisions")
    if not doc.provisions:
        # Every numbered element in this file is PAST law — Title 26's
        # repealed chapter 38 is exactly this. Not a parse failure: the markup
        # was read correctly and the answer is "nothing here is current". It
        # must not raise, because ParseError nacks the queue item and the file
        # would redeliver forever. It lands as retrievable text with no facts,
        # which is the truthful outcome.
        logger.info("uslm: %d element(s) skipped as past law, leaving no "
                    "current provisions — the document renders as text and "
                    "produces no facts", doc.skipped_by_status)
    return doc


def _walk(elem: ET.Element, out: _Writer, doc: ParsedDocument,
          parent: Optional[str], depth: int, quoted: bool = False) -> None:
    """`quoted` means "inside a QUOTED_LEVELS element": everything renders
    exactly as it always did, and nothing is indexed. The flag only ever
    turns ON — a quote cannot contain non-quoted law."""
    for child in elem:
        tag = _local(child.tag)
        if tag in PROVISION_LEVELS:
            _emit_provision(child, tag, out, doc, parent, depth, quoted)
        elif tag in TEXT_LEVELS:
            _emit_body(child, out, doc, parent, quoted)
        elif tag in NOTE_LEVELS:
            _emit_note(child, out, doc, parent, quoted)
        elif tag in NOTE_CONTAINERS:
            _walk(child, out, doc, parent, depth, quoted)  # a wrapper, not content
        elif tag in QUOTED_LEVELS:
            _walk(child, out, doc, parent, depth, quoted=True)
        elif tag in ("num", "heading"):
            continue          # consumed by the owning provision
        else:
            _walk(child, out, doc, parent, depth, quoted)


def _child_text(elem: ET.Element, name: str) -> Optional[str]:
    for child in elem:
        if _local(child.tag) == name:
            return _clean("".join(child.itertext())).strip() or None
    return None


def _emit_provision(elem: ET.Element, level: str, out: _Writer,
                    doc: ParsedDocument, parent: Optional[str],
                    depth: int, quoted: bool = False) -> None:
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

    # PAST LAW, or a SECOND CLAIM ON ONE IDENTIFIER. Either way the element is
    # already rendered above — a reader still finds it — but it does not become
    # a Provision, and it does not become anyone's parent: children re-parent to
    # the nearest ancestor that IS current, so the structural spine stays whole.
    status = (elem.get("status") or "").strip().lower()
    demoted: Optional[str] = None
    if quoted:
        # Quoted amendment text. Not past law, not a defect in the file —
        # just another document's words, so it gets its own counter and does
        # not touch skipped_by_status.
        demoted = "quoted"
        doc.quoted_elements += 1
    elif status in SKIPPED_STATUSES:
        demoted = status
        doc.skipped_by_status += 1
    elif not identifier:
        # A real current-law provision always carries an identifier (proven
        # across all 287 Title 26 files: every identifier-less element was
        # quoted PL text). If one ever shows up outside a quote it renders
        # as text — because a Provision with identifier '' would land in
        # by_identifier() under '', where ownerless refs would find it and
        # emit facts keyed on the empty string. That is exactly the D4
        # keyless-mention leak, refused here at its second door.
        demoted = "no_identifier"
        logger.warning(
            "uslm: a <%s> outside quoted content has no identifier; rendered "
            "as text, not indexed as a provision (num=%r heading=%r)",
            level, num, heading)
    elif not doc.claim(identifier):
        # A USLM identifier is supposed to name exactly one thing, and almost
        # always does — but not always, so this refuses to guess. The FIRST
        # occurrence keeps the identifier; later ones render and are recorded
        # here. Keeping the last (what a dict build does by default) would be
        # just as arbitrary and completely silent.
        demoted = "duplicate_identifier"
        doc.duplicate_identifiers.append(identifier)
        logger.warning(
            "uslm: identifier %s appears more than once in this document; "
            "keeping the first occurrence as the provision and rendering the "
            "rest as text", identifier)

    if demoted is not None:
        _walk(elem, out, doc, parent=parent, depth=next_depth, quoted=quoted)
        return

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
               owner: Optional[str], quoted: bool = False) -> None:
    """Body text, capturing every `<ref>` span as it is written."""
    _inline(elem, out, doc, owner, quoted=quoted)
    if not out.text().endswith("\n"):
        out.write("\n")


def _emit_note(elem: ET.Element, out: _Writer, doc: ParsedDocument,
               owner: Optional[str], quoted: bool = False) -> None:
    """Editorial apparatus: rendered because a reader wants it, never
    indexed as a provision. Buffered and trimmed rather than streamed —
    note bodies are the whitespace-heaviest part of a USLM file and would
    otherwise leave ragged runs of spaces in the middle of the text."""
    out.ensure_blank_line()
    heading = _child_text(elem, "heading")
    if heading:
        out.write(f"{heading}: ")
    mark = out.pos
    _inline(elem, out, doc, owner, skip=("heading",), quoted=quoted)
    out.rstrip_to(mark)
    if out.pos == mark and not heading:
        return                      # an empty note is not worth a paragraph
    out.write("\n")


def _inline(elem: ET.Element, out: _Writer, doc: ParsedDocument,
            owner: Optional[str], skip: tuple[str, ...] = (),
            quoted: bool = False) -> None:
    """Render mixed content, recording cross-reference spans.

    A `<ref>`'s span is captured around the recursive render of its own
    children, so the recorded offsets bound exactly the link text that
    reaches the reader — which is what a citation should point at.

    Inside quoted content (`quoted=True`) everything still renders — the
    link text is part of the quote — but no CrossReference is recorded: a
    `<ref>` in quoted PL text is the AMENDING ACT citing something, not this
    provision citing it, and indexing it would assert an edge the quoting
    document never states."""
    out.write_prose(_clean(elem.text))
    for child in elem:
        tag = _local(child.tag)
        if tag in skip:
            pass
        elif tag == "ref":
            start = out.pos
            _inline(child, out, doc, owner, quoted=quoted)
            href = child.get("href") or ""
            if href and not quoted:
                doc.references.append(CrossReference(
                    owner=owner or "", href=href,
                    char_start=start, char_end=out.pos))
        elif tag in QUOTED_LEVELS:
            _inline(child, out, doc, owner, quoted=True)
        elif tag in PROVISION_LEVELS or tag in NOTE_LEVELS:
            # A nested provision inside body text: hand it back to the main
            # walk so it is indexed, not flattened into prose. Under a quote
            # the flag rides along and the walk renders without indexing.
            _walk(elem, out, doc, owner, depth=3, quoted=quoted)
            return
        else:
            _inline(child, out, doc, owner, quoted=quoted)
        out.write_prose(_clean(child.tail))
