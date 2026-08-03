"""DoclingParser — Docling implementation of Parser (Stage B, prose track).

Input is always the exact landed bytes (fetched via the version-pinned
raw_uri); Docling converts them into a structured document, exported as
Markdown so heading/section structure survives into plain text — that
Markdown IS the chunker's input and the anchor space for chunk char offsets.

Metadata precedence (the capture path captured it generously because it's
irreplaceable): title/author/timestamps come from raw.native_metadata when
present; only what's absent is derived from the parse (docx core properties,
first heading, filename stem). Declarations follow §8.1a: the manifest's
data_track/doc_type tag (merged onto native_metadata by the processing flow)
is a CLAIM — parse() runs cheap deterministic shape detection against it and
records BOTH views on Document.metadata; the flow arbitrates (agree ->
proceed; low-confidence detection -> trust the human tag; confident
disagreement -> review_queue). Detection is deliberately gross-error-grade
(spreadsheet in the SOP folder), not a model — match detector effort to
mismatch cost.

Plain-text landings (.txt / text/plain) are fed to Docling as Markdown: it
has no dedicated plain-text backend, and Markdown is a superset that parses
bare paragraphs faithfully.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Optional

from knowledge_hub.interfaces import ParseError, Parser
from knowledge_hub.models import (
    DATA_TRACKS,
    PROSE_TRACK,
    STRUCTURED_TRACK,
    DocType,
    Document,
    RawDocument,
    data_track_for,
)

logger = logging.getLogger(__name__)

# Extensions/mime types that are structured by construction — no shape
# detection needed, the container format says it all.
_STRUCTURED_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls", ".parquet"}
_STRUCTURED_MIMES = {
    "text/csv", "text/tab-separated-values",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# Filename/heading cues that make a prose document an SOP for doc_type
# purposes (pilot heuristic; the manifest's doc_type tag wins when present).
_SOP_CUE = re.compile(r"\bsop\b|standard operating procedure|work instruction",
                      re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_DELIMS = (",", ";", "\t", "|")


def detect_data_track(mime_type: Optional[str], name: str,
                      text: str) -> tuple[str, bool]:
    """Cheap, deterministic per-document shape detection (§8.1a).

    Returns (track, confident). Confident only on gross signals: a structured
    container format, or extracted text that is overwhelmingly delimiter-rows
    (or overwhelmingly not). Everything ambiguous is (PROSE_TRACK, False) so
    the declared tag stays authoritative.
    """
    ext = PurePosixPath(name).suffix.lower()
    if ext in _STRUCTURED_EXTS or (mime_type or "").lower() in _STRUCTURED_MIMES:
        return STRUCTURED_TRACK, True

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 5:
        # A "row" line carries the same delimiter at least twice (>= 3 cells).
        rows = sum(1 for ln in lines
                   if any(ln.count(d) >= 2 for d in _DELIMS))
        ratio = rows / len(lines)
        if ratio >= 0.8:
            return STRUCTURED_TRACK, True
        if ratio <= 0.2 and len(text.split()) >= 30:
            return PROSE_TRACK, True
    return PROSE_TRACK, False


class DoclingParser(Parser):
    def __init__(self) -> None:
        self._converter = None  # Docling loads models lazily; keep one instance
        # parse() and extract_text() are called on the same bytes back to
        # back; memoize the last few conversions so each document converts
        # exactly once per pass.
        self._memo: dict[str, tuple[Any, str]] = {}

    # ------------------------------------------------------------- Parser --
    def parse(self, raw: RawDocument, content: bytes) -> Document:
        _, markdown = self._convert(raw, content)
        native = raw.native_metadata or {}

        declared_track = native.get("data_track")
        if declared_track is not None and declared_track not in DATA_TRACKS:
            raise ParseError(raw.tenant_id, raw.id,
                             f"declared data_track {declared_track!r} is not "
                             f"one of {DATA_TRACKS}")
        detected_track, confident = detect_data_track(
            raw.mime_type, raw.source_native_id or "", markdown)
        # Effective track: the declared (human) tag when there is one — a
        # confident disagreement is flagged by the flow, never silently
        # overridden here; without a declaration, detection is all we have.
        effective_track = declared_track or detected_track

        declared_type: Optional[DocType] = None
        if native.get("doc_type") is not None:
            try:
                declared_type = DocType(native["doc_type"])
            except ValueError:
                raise ParseError(raw.tenant_id, raw.id,
                                 f"declared doc_type {native['doc_type']!r} "
                                 "is not a DocType") from None

        outline = [{"level": len(m.group(1)), "text": m.group(2)}
                   for m in _HEADING.finditer(markdown)]
        doc_type = self._doc_type(declared_type, effective_track,
                                  raw.source_native_id or "", outline)
        title, author = self._title_author(native, raw, content, outline)

        return Document(
            tenant_id=raw.tenant_id,
            raw_document_id=raw.id,
            doc_type=doc_type,
            title=title,
            author=author,
            source_timestamp=self._source_timestamp(native, raw),
            security_label_id=raw.security_label_id,
            metadata={
                "data_track": effective_track,
                "declared_data_track": declared_track,
                "detected_data_track": detected_track,
                "detection_confident": confident,
                "outline": outline,
                "parser": f"docling {_docling_version()}",
                "source_ref": native.get("source_ref"),
            },
        )

    def extract_text(self, raw: RawDocument, content: bytes) -> str:
        _, markdown = self._convert(raw, content)
        return markdown

    # ---------------------------------------------------------- internals --
    def _convert(self, raw: RawDocument, content: bytes) -> tuple[Any, str]:
        memo = self._memo.get(raw.content_hash)
        if memo is not None:
            return memo

        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling.document_converter import DocumentConverter

        if self._converter is None:
            self._converter = DocumentConverter()
        try:
            stream = DocumentStream(name=self._stream_name(raw),
                                    stream=io.BytesIO(content))
            result = self._converter.convert(stream, raises_on_error=True)
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(raw.tenant_id, raw.id,
                             f"{type(e).__name__}: {e}") from e
        if result.status not in (ConversionStatus.SUCCESS,
                                 ConversionStatus.PARTIAL_SUCCESS):
            raise ParseError(raw.tenant_id, raw.id,
                             f"docling status {result.status}")
        if result.status is ConversionStatus.PARTIAL_SUCCESS:
            logger.warning("docling partial success for raw_document id=%s "
                           "(tenant %r)", raw.id, raw.tenant_id)

        markdown = result.document.export_to_markdown()
        if len(self._memo) >= 4:  # tiny LRU: a pass touches one doc at a time
            self._memo.pop(next(iter(self._memo)))
        self._memo[raw.content_hash] = (result.document, markdown)
        return self._memo[raw.content_hash]

    def _stream_name(self, raw: RawDocument) -> str:
        """Docling detects format from the stream NAME's extension; landed
        bytes only carry source_native_id + mime. Plain text maps to .md."""
        name = PurePosixPath(raw.source_native_id or "document").name
        ext = PurePosixPath(name).suffix.lower()
        if ext in (".txt", "") or (raw.mime_type or "") == "text/plain":
            return PurePosixPath(name).stem + ".md"
        return name

    @staticmethod
    def _doc_type(declared: Optional[DocType], track: str, native_id: str,
                  outline: list[dict]) -> DocType:
        if declared is not None and data_track_for(declared) == track:
            return declared
        # No tag, or the tag's track disagrees with the effective track (the
        # flow's mismatch arbitration handles flagging) — use the track's
        # own default.
        if track == STRUCTURED_TRACK:
            return DocType.tabular
        cues = " ".join([native_id] + [h["text"] for h in outline[:3]])
        return DocType.sop if _SOP_CUE.search(cues) else DocType.prose

    def _title_author(self, native: dict, raw: RawDocument, content: bytes,
                      outline: list[dict]) -> tuple[Optional[str], Optional[str]]:
        title, author = native.get("title"), native.get("author")
        if title and author:
            return title, author
        is_docx = (raw.mime_type == ("application/vnd.openxmlformats-office"
                                     "document.wordprocessingml.document")
                   or (raw.source_native_id or "").lower().endswith(".docx"))
        if is_docx:
            # Docling doesn't surface OOXML core properties; python-docx (a
            # Docling dependency) reads them without a second full parse.
            try:
                import docx
                props = docx.Document(io.BytesIO(content)).core_properties
                title = title or (props.title or None)
                author = author or (props.author or None)
            except Exception:  # corrupt props never fail the parse
                logger.debug("core properties unreadable for raw id=%s", raw.id)
        if not title:
            if outline:
                title = outline[0]["text"]
            elif raw.source_native_id:
                title = PurePosixPath(raw.source_native_id).stem
        return title or None, author or None

    @staticmethod
    def _source_timestamp(native: dict, raw: RawDocument) -> Optional[datetime]:
        mtime = native.get("mtime")
        if mtime:
            try:
                return datetime.fromisoformat(mtime)
            except ValueError:
                pass
        return raw.captured_at


def _docling_version() -> str:
    try:
        from importlib.metadata import version
        return version("docling")
    except Exception:
        return "unknown"
