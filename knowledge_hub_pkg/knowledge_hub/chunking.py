"""SectionChunker — the prose-track Chunker (Stage B).

Three tiers. The superparent is the documents row itself (whole-document
metadata/provenance roll-up — the Parser's output); this module produces the
two chunk tiers under it:

  parent  = a section/procedure — the extraction unit and LLM context window.
            Sections come from the Markdown headings the parser preserved:
            the split level is the document's dominant heading level (capped
            at 3 so step-level micro-headings don't shred a procedure), with
            any preamble before the first heading as its own section, and
            oversized sections sub-split on paragraph boundaries.
  child   = a ~300-token passage — the embed/cite unit. Split on semantic
            boundaries (paragraph -> sentence -> word, via semchunk) with
            ~15% token overlap so a fact straddling a cut survives in at
            least one passage. 300 stays far under bge-m3's window on
            purpose: retrieval precision beats context stuffing here.

Every child carries a one-line contextual-retrieval prefix (deterministic:
title + section path + position) in `contextual_prefix`; `embedding_text()`
is THE composition rule for what actually gets embedded — the flow and the
tests both use it, so prefix-in-the-vector is a checkable property, not a
convention.

Provenance: chunk char_start/char_end anchor into the parser's extracted
Markdown (children nest inside their parent's span). Linkage: parent ids
don't exist before insert, so the returned list is ordered parent-first with
each parent immediately followed by its children (the Chunker ABC contract);
locator carries the section index + heading path for human-readable
provenance. content_hash is deterministic over (tenant, document, tier,
position, prefix, text) so re-chunking the same document replays as a no-op
through insert_chunks' ON CONFLICT.

Token counts use the real bge-m3 tokenizer (HuggingFace `tokenizers`) —
size control against the embedder's actual vocabulary, and the SAME counter
drives both semchunk's splitting and Chunk.token_count, so the persisted
counts are the enforced bounds. The tokenizer file ships in the deploy kit
and is seeded into the deployment home (config.bge_m3_tokenizer_json), so a
deployed box loads it with ZERO egress (BP28 #20); a dev bench without the
file falls back to the one-time hub download.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import semchunk

from knowledge_hub.interfaces import Chunker
from knowledge_hub.models import (
    PROSE_TRACK,
    Chunk,
    ChunkLevel,
    DocType,
    Document,
)

BGE_M3_TOKENIZER = "BAAI/bge-m3"
CHILD_TOKENS = 300          # ~retrieval-precision sweet spot; << bge-m3 window
CHILD_OVERLAP = 0.15        # fraction of CHILD_TOKENS shared between neighbors
MAX_PARENT_TOKENS = 2048    # soft cap: an extraction unit that fits one call
MAX_SPLIT_LEVEL = 3         # never split deeper than heading level 3

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

_DOC_TYPE_LABELS = {
    DocType.sop: "standard operating procedure",
    DocType.prose: "document",
    DocType.communication: "communication",
    DocType.tabular: "table",
    DocType.sor: "system-of-record extract",
    DocType.form: "form",
}

_tokenizer = None


def _bge_m3_token_counter() -> Callable[[str], int]:
    """Token counter over the real bge-m3 vocabulary (no special tokens —
    counts are of the TEXT; semchunk assumes a custom counter is already
    net of specials). Loads the kit-seeded local file when present —
    offline by construction on a deployed box (BP28 #20); the hub
    download is the dev-bench fallback only."""
    global _tokenizer
    if _tokenizer is None:
        from pathlib import Path

        from tokenizers import Tokenizer

        from knowledge_hub.config import settings
        local = Path(settings.bge_m3_tokenizer_json)
        if local.is_file():
            _tokenizer = Tokenizer.from_file(str(local))
        else:
            _tokenizer = Tokenizer.from_pretrained(BGE_M3_TOKENIZER)
    return lambda text: len(_tokenizer.encode(text, add_special_tokens=False).ids)


def embedding_text(chunk: Chunk) -> str:
    """The exact text a child is embedded as: contextual prefix + passage.
    Single source of truth — used by the processing flow AND asserted by the
    tests, so the prefix provably reaches the vector."""
    if chunk.contextual_prefix:
        return f"{chunk.contextual_prefix}\n\n{chunk.content}"
    return chunk.content


@dataclass
class _Section:
    heading: Optional[str]          # None for preamble-only sections
    heading_path: list[str] = field(default_factory=list)
    start: int = 0                  # char offsets into the extracted text
    end: int = 0
    part: Optional[int] = None      # set when an oversized section was split


class SectionChunker(Chunker):
    def __init__(
        self,
        token_counter: Optional[Callable[[str], int]] = None,
        child_tokens: int = CHILD_TOKENS,
        child_overlap: float = CHILD_OVERLAP,
        max_parent_tokens: int = MAX_PARENT_TOKENS,
    ):
        self._count = token_counter or _bge_m3_token_counter()
        self.child_tokens = child_tokens
        self.child_overlap = child_overlap
        self.max_parent_tokens = max_parent_tokens
        self._child_split = semchunk.chunkerify(self._count, chunk_size=child_tokens)
        self._parent_split = semchunk.chunkerify(self._count,
                                                 chunk_size=max_parent_tokens)

    # ------------------------------------------------------------ Chunker --
    def chunk(self, document: Document, text: str) -> list[Chunk]:
        if document.data_track != PROSE_TRACK:
            return []  # structured tracks produce facts, not chunks (router)
        if document.id is None:
            raise ValueError("document must be persisted before chunking "
                             "(content hashes and linkage need document.id)")
        if not text.strip():
            return []

        chunks: list[Chunk] = []
        for section_ix, section in enumerate(self._sections(text)):
            parent_content = text[section.start:section.end]
            locator = {
                "section": section_ix,
                "heading": section.heading,
                "heading_path": " > ".join(section.heading_path) or None,
            }
            if section.part is not None:
                locator["part"] = section.part
            parent = Chunk(
                tenant_id=document.tenant_id,
                document_id=document.id,
                level=ChunkLevel.parent,
                seq=section_ix,
                content=parent_content,
                content_hash=self._hash(document, ChunkLevel.parent,
                                        section_ix, 0, None, parent_content),
                token_count=self._count(parent_content),
                char_start=section.start,
                char_end=section.end,
                locator=locator,
            )
            chunks.append(parent)
            chunks.extend(self._children(document, section, section_ix,
                                         parent_content))
        return chunks

    # ---------------------------------------------------------- internals --
    def _children(self, document: Document, section: _Section,
                  section_ix: int, parent_content: str) -> list[Chunk]:
        passages, spans = self._child_split(
            parent_content, overlap=self.child_overlap, offsets=True)
        total = len(passages)
        children: list[Chunk] = []
        for child_ix, (passage, (rel_start, rel_end)) in enumerate(
                zip(passages, spans)):
            prefix = self._prefix(document, section, child_ix, total)
            children.append(Chunk(
                tenant_id=document.tenant_id,
                document_id=document.id,
                level=ChunkLevel.child,
                seq=child_ix,
                content=passage,
                contextual_prefix=prefix,
                content_hash=self._hash(document, ChunkLevel.child,
                                        section_ix, child_ix, prefix, passage),
                token_count=self._count(passage),
                char_start=section.start + rel_start,
                char_end=section.start + rel_end,
                locator={
                    "section": section_ix,
                    "heading": section.heading,
                    "heading_path": " > ".join(section.heading_path) or None,
                },
            ))
        return children

    def _sections(self, text: str) -> list[_Section]:
        """Split the extracted Markdown into section spans at the document's
        dominant heading level (capped at MAX_SPLIT_LEVEL), then sub-split
        anything larger than max_parent_tokens on paragraph boundaries."""
        headings: list[tuple[int, int, str]] = []  # (offset, level, text)
        offset = 0
        for line in text.splitlines(keepends=True):
            m = _HEADING_LINE.match(line.rstrip("\r\n"))
            if m:
                headings.append((offset, len(m.group(1)), m.group(2)))
            offset += len(line)

        levels = [lv for _, lv, _ in headings]
        # Dominant heading level = the document's structural rhythm; ties
        # break toward the DEEPER level (finer sections beat one mega-section,
        # e.g. a lone title heading + one body heading).
        split_level = (min(max(sorted(set(levels), reverse=True),
                               key=levels.count), MAX_SPLIT_LEVEL)
                       if levels else 0)
        split_at = [h for h in headings if h[1] <= split_level]
        if not split_at:  # headings absent, or all deeper than the cap
            return self._subsplit(_Section(heading=None, start=0,
                                           end=len(text)), text)

        sections: list[_Section] = []
        path: list[tuple[int, str]] = []  # open ancestor headings (level, text)
        current: Optional[_Section] = None
        if text[:split_at[0][0]].strip():  # preamble before the first section
            current = _Section(heading=None, start=0)

        for h_start, level, h_text in headings:
            if level <= split_level:
                if current is not None:
                    current.end = h_start
                    sections.append(current)
                current = _Section(
                    heading=h_text,
                    heading_path=[t for lv, t in path if lv < level] + [h_text],
                    start=h_start,
                )
            path = [(lv, t) for lv, t in path if lv < level] + [(level, h_text)]
        current.end = len(text)
        sections.append(current)

        return [part
                for section in sections if text[section.start:section.end].strip()
                for part in self._subsplit(section, text)]

    def _subsplit(self, section: _Section, text: str) -> list[_Section]:
        """Keep sections within the extraction-unit budget: oversized ones
        split on paragraph boundaries (semchunk, no overlap — parents tile)."""
        parts, spans = self._parent_split(text[section.start:section.end],
                                          offsets=True)
        if len(parts) <= 1:
            return [section]
        return [
            _Section(heading=section.heading,
                     heading_path=section.heading_path,
                     start=section.start + s,
                     end=section.start + e,
                     part=ix)
            for ix, (s, e) in enumerate(spans)
        ]

    def _prefix(self, document: Document, section: _Section,
                child_ix: int, total: int) -> str:
        """One-line contextual-retrieval blurb situating the passage in the
        document (deterministic; an LLM-written blurb can swap in later
        behind the same column)."""
        label = _DOC_TYPE_LABELS.get(document.doc_type, "document")
        where = (f'section "{" > ".join(section.heading_path)}"'
                 if section.heading else "the opening")
        title = document.title or "an untitled document"
        return (f"From {where} of the {label} '{title}' "
                f"(passage {child_ix + 1} of {total}).")

    @staticmethod
    def _hash(document: Document, level: ChunkLevel, section_ix: int,
              seq: int, prefix: Optional[str], content: str) -> str:
        """Deterministic identity for idempotent replay: same tenant +
        document row + tier + position + prefix + text => same hash => the
        chunks unique index makes re-insertion a no-op. Includes document_id
        (reused across re-processing runs) so identical boilerplate in two
        documents still gets its own rows."""
        basis = "\x1f".join([
            document.tenant_id, str(document.id), level.value,
            str(section_ix), str(seq), prefix or "", content,
        ])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()
