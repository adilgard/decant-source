"""UslmParser — a US Code (USLM XML) plugin for decant.Source.

Implements BOTH core contracts against one internal parse:

    Parser      bytes -> a Document, and bytes -> readable text
    FactParser  a document -> deterministic assertions with spans

Both roles on one object is the point, not a convenience. A fact's
character offsets have to index the same string the chunker cut, and the
only way to guarantee that is for one parse to produce both. Two objects,
even agreeing ones, are two things that can drift.

WHAT THIS KNOWS THAT CORE MUST NOT. Everything here is domain knowledge:
that `<subsection>` nests inside `<section>`, that `<ref href>` is a
cross-reference, that `/us/usc/t26/s1/a` is cited `26 U.S.C. § 1(a)`. None
of it belongs in knowledge_hub_pkg, which serves every corpus and should
not have opinions about any of them. That is the whole reason this package
exists separately, is installed separately, and is reached only by a source
config value naming it.

WHAT IT DELIBERATELY DOES NOT DO:
  * No LLM. Not as a fallback, not for the hard cases. Every assertion is a
    fact about the XML tree, which is what makes them reproducible.
  * No interpretation. It emits structure, headings and cross-references —
    what the markup literally says. What a provision MEANS is not something
    a parser can know, and pretending otherwise is how a deterministic
    pipeline quietly stops being one.
  * No ontology decisions. It emits the vocabulary in
    `ontology/tax-statute-0.1.json`; whether the active ontology permits
    those terms is core's call, and core will quarantine anything it does
    not recognise. This plugin is a producer, never an authority.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, ClassVar, Optional

from knowledge_hub.interfaces import FactParser, ParsedFact, ParseError, Parser
from knowledge_hub.models import DocType, Document, RawDocument

from ds_parser_uslm.uslm import ParsedDocument, UslmError, parse_uslm

logger = logging.getLogger(__name__)

# The vocabulary this plugin emits. Mirrors ontology/tax-statute-0.1.json —
# kept here as constants so the emission sites are greppable and the test
# can assert the two agree.
PROVISION = "Provision"
PART_OF = "part_of"
REFERENCES = "references"
HAS_HEADING = "has_heading"

EMITTED_ENTITY_TYPES = frozenset({PROVISION})
EMITTED_PREDICATES = frozenset({PART_OF, REFERENCES, HAS_HEADING})

# The key the resolver blocks on. A USLM identifier is a globally unique,
# stable path for a provision, which makes it a genuine deterministic key
# (the resolver's T0 tier) rather than a name that merely looks unique.
IDENTIFIER_KEY = "uslm_identifier"


class UslmParser(Parser, FactParser):
    name: ClassVar[str] = "uslm"
    # Code version, and part of the idempotency ledger key
    # (extraction_runs.ux_extraction_unit: tenant + unit_hash + extractor +
    # extractor_version + ontology_version). BUMP IT WHENEVER OUTPUT CHANGES,
    # or re-extraction of already-extracted content replays as a no-op and the
    # change never reaches the documents that need it.
    #
    # 0.1.1 (2026-08-04): citation_for now reads en-dashed section numbers
    # (`s1400Z–2`, `s300gg–44`), which USLM uses throughout and the previous
    # ASCII-only pattern dropped — rendering 90 identifiers as a bare
    # '26 U.S.C.' / '42 U.S.C.' and collapsing unrelated provisions onto one
    # surface. Facts staged under 0.1.0 that cite such a target carry the
    # collapsed object citation and must be re-extracted to be corrected.
    #
    # 0.1.2 (2026-08-05): quoted amendment text (`<quotedContent>` /
    # `<quotedText>`) renders but is never indexed — under 0.1.1 its markup
    # became 12,408 identifier-less Provisions (17.5% of the corpus count),
    # whose ''-identifier entry in by_identifier() handed ownerless refs a
    # junk subject keyed on the empty string: ~128 keyless mentions through
    # the resolver's fuzzy path (Job 8, D4). Identifier-less numbered
    # elements outside quotes are likewise rendered, never indexed. The
    # RENDERED TEXT IS BYTE-IDENTICAL to 0.1.1 for the whole Title 26 corpus;
    # what changed is only what gets asserted about it. References inside
    # quoted content no longer emit edges (the amending act's citations are
    # not this provision's).
    version = "0.1.2"

    def __init__(self, memo_size: int = 4) -> None:
        # parse() and extract_text() are called back to back on the same
        # bytes, and parse_facts() follows on a later pass. Memoise so one
        # document is walked once per pass — the same tactic the core
        # Docling parser uses, for the same reason.
        self._memo: dict[str, ParsedDocument] = {}
        self._memo_size = memo_size

    # -------------------------------------------------------------- Parser --
    def parse(self, raw: RawDocument, content: bytes) -> Document:
        parsed = self._parsed(raw, content)
        # None when every numbered element in the file is past law (a wholly
        # repealed chapter). The document still lands and is still readable;
        # it just has no current provision to name itself after.
        root = parsed.provisions[0] if parsed.provisions else None
        return Document(
            tenant_id=raw.tenant_id,
            raw_document_id=raw.id,
            # Statutes are prose: they chunk and embed like any other prose
            # so the corpus stays retrievable. Only the FACT producer is
            # unusual, and that is a separate axis by design.
            doc_type=DocType.prose,
            title=(raw.native_metadata or {}).get("title")
                  or parsed.title
                  or (root.citation if root else _repealed_title(raw)),
            source_timestamp=None,
            security_label_id=raw.security_label_id,
            metadata={
                "data_track": "prose",
                "declared_data_track": (raw.native_metadata or {}).get(
                    "data_track"),
                "detected_data_track": "prose",
                # Not a guess: the root element was verified to be a USLM
                # document root before anything else ran.
                "detection_confident": True,
                "parser": f"uslm {self.version}",
                "source_ref": (raw.native_metadata or {}).get("source_ref"),
                "uslm_identifier": root.identifier if root else None,
                "uslm_root_citation": root.citation if root else None,
                "provisions": len(parsed.provisions),
                "cross_references": len(parsed.references),
                # Rendered but not indexed as law. On the document so a run is
                # answerable without re-parsing: "why does this chapter have
                # fewer provisions than it has numbered elements" has an answer
                # sitting next to the count.
                "skipped_by_status": parsed.skipped_by_status,
                "duplicate_identifiers": parsed.duplicate_identifiers,
                "quoted_elements": parsed.quoted_elements,
            },
        )

    def extract_text(self, raw: RawDocument, content: bytes) -> str:
        return self._parsed(raw, content).text

    # ---------------------------------------------------------- FactParser --
    def parse_facts(self, document: Document, text: str,
                    content: bytes) -> list[ParsedFact]:
        parsed = self._parse_bytes(content)
        index = parsed.by_identifier()
        facts: list[ParsedFact] = []

        for provision in parsed.provisions:
            if not provision.identifier:
                continue        # unnumbered wrapper; nothing to assert about
            subject = _entity(provision.citation, provision.identifier)
            label = (provision.label_start, provision.label_end)

            # STRUCTURE. 'this provision sits inside that one' — the spine a
            # statute is navigated by, and the thing prose retrieval alone
            # cannot answer.
            parent = index.get(provision.parent or "")
            if parent is not None and parent.identifier:
                facts.append(ParsedFact(
                    **subject,
                    predicate=PART_OF,
                    object_text=parent.citation,
                    object_type=PROVISION,
                    object_keys={IDENTIFIER_KEY: parent.identifier},
                    locator={"identifier": provision.identifier,
                             "level": provision.level},
                    **_span(text, label)))

            # HEADING. A literal-valued attribute, so a heading is queryable
            # without a full-text search that might match the body instead.
            if provision.heading:
                facts.append(ParsedFact(
                    **subject,
                    predicate=HAS_HEADING,
                    object_literal=provision.heading,
                    locator={"identifier": provision.identifier},
                    **_span(text, label)))

        # CROSS-REFERENCES. Each `<ref>` becomes an edge whose span is the
        # link text itself, so a citation of this fact lands on the exact
        # words that make the reference.
        for ref in parsed.references:
            owner = index.get(ref.owner)
            target_identifier = _normalise_href(ref.href)
            if owner is None or not target_identifier:
                continue
            target = index.get(target_identifier)
            target_citation = (target.citation if target is not None
                               else _citation_for_href(target_identifier))
            if not target_citation or target_identifier == owner.identifier:
                continue        # a self-reference asserts nothing
            facts.append(ParsedFact(
                **_entity(owner.citation, owner.identifier),
                predicate=REFERENCES,
                object_text=target_citation,
                object_type=PROVISION,
                object_keys={IDENTIFIER_KEY: target_identifier},
                locator={"identifier": owner.identifier,
                         "href": ref.href},
                **_span(text, (ref.char_start, ref.char_end))))

        logger.info("uslm: %d provision(s), %d cross-reference(s) -> %d fact(s)",
                    len(parsed.provisions), len(parsed.references), len(facts))
        return facts

    # ----------------------------------------------------------- internals --
    def _parsed(self, raw: RawDocument, content: bytes) -> ParsedDocument:
        try:
            return self._parse_bytes(content)
        except UslmError as e:
            # Core's Parser contract: a document that cannot be read raises
            # ParseError, which the queue consumer nacks so the item
            # redelivers instead of vanishing.
            raise ParseError(raw.tenant_id, raw.id, str(e)) from e

    def _parse_bytes(self, content: bytes) -> ParsedDocument:
        key = hashlib.sha256(content).hexdigest()
        cached = self._memo.get(key)
        if cached is None:
            cached = parse_uslm(content)
            if len(self._memo) >= self._memo_size:
                self._memo.pop(next(iter(self._memo)))
            self._memo[key] = cached
        return cached


def _repealed_title(raw: RawDocument) -> str:
    """A name for a file whose every numbered element is past law.

    Title 26 has 21 of these. They have no current provision to be named
    after, and calling them all the same thing would make a document list
    unreadable — so the source's own identifier for the file carries the
    name, which is the only thing left that distinguishes them.
    """
    native = (raw.native_metadata or {}).get("absolute_path") \
        or raw.source_native_id or ""
    stem = str(native).replace("\\", "/").rsplit("/", 1)[-1]
    return f"{stem or 'document'} — repealed, no current provisions"


def _entity(citation: str, identifier: str) -> dict[str, Any]:
    return {"subject_text": citation, "subject_type": PROVISION,
            "subject_keys": {IDENTIFIER_KEY: identifier}}


def _span(text: str, span: tuple[int, int]) -> dict[str, Any]:
    """Declare the computed span and the text it points at.

    The offsets come from this plugin's own render, and core verifies them
    by slicing the text it rebuilt from the persisted chunks. Deliberately
    NOT re-located here if the two disagree: a second search in the plugin
    would paper over a real divergence between what was rendered and what
    was chunked. Let it surface as core's 'span_mismatch' and land in
    review, where somebody can see it.
    """
    start, end = span
    if end <= start or start < 0:
        return {}
    return {"char_start": start, "char_end": end,
            "span_text": text[start:end]}


def _normalise_href(href: str) -> Optional[str]:
    """A `<ref href>` to the provision path it names. USLM hrefs are
    usually already identifier paths; a fragment or a query is trimmed, and
    anything that is not a US Code path is skipped rather than guessed."""
    if not href:
        return None
    target = href.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return target if target.startswith("/us/usc/") else None


def _citation_for_href(identifier: str) -> str:
    from ds_parser_uslm.uslm import citation_for
    return citation_for(identifier)
