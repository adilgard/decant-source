"""SpanGrounder — deterministic evidence verification (no LLM, ever).

Extraction's determinism guardrail on the provenance side: a fact's
`evidence_span` is only trusted after this module re-finds it in the source
text the fact claims to come from. P3 guarantees chunk char offsets slice
back into the extracted text, so a verified span becomes real document
char_start/char_end provenance on the staged fact.

Two-tier matching, both reproducible:
  1. EXACT — the evidence quote occurs verbatim in the source.
  2. FUZZY — whitespace-collapsed, case-folded matching with an offset map
     back into the original text (models love to reflow whitespace and
     normalize case when 'quoting verbatim'; that is not a lie about the
     document, so it still passes).

Then the span must actually CONTAIN the fact's components (subject surface,
object surface or literal), checked with the same exact-then-fuzzy rule.

Failure is a FLAG, never a rejection: legitimate paraphrase exists ("thirty
minutes" vs "30 minutes"), so the flow lowers confidence and routes the fact
to review instead of dropping a possibly-true assertion. The distinct
failure modes (span_missing vs components_missing) are persisted per fact —
benchmark signal, not just a boolean.
"""
from __future__ import annotations

from typing import Optional, Sequence

from knowledge_hub.interfaces import Grounder, GroundingResult


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Whitespace-collapsed, case-folded copy of `text` plus a map from each
    normalized index back to its original index. 1:1 per character (lower()
    on single chars), so spans map back exactly."""
    out: list[str] = []
    idx_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if out and not prev_space:
                out.append(" ")
                idx_map.append(i)
            prev_space = True
        else:
            low = ch.lower()
            out.append(low if len(low) == 1 else ch)
            idx_map.append(i)
            prev_space = False
    return "".join(out), idx_map


def find_span(needle: str, haystack: str) -> Optional[tuple[int, int]]:
    """(start, end) of needle in haystack: exact, then fuzzy (whitespace-
    collapsed, case-folded) with the offsets mapped back into the original
    text. Shared by the grounder and the flow's mention-offset anchoring."""
    pos = haystack.find(needle)
    if pos >= 0:
        return pos, pos + len(needle)
    norm_hay, idx_map = _normalize_with_map(haystack)
    norm_needle, _ = _normalize_with_map(needle)
    norm_needle = norm_needle.strip()
    if not norm_needle:
        return None
    pos = norm_hay.find(norm_needle)
    if pos < 0:
        return None
    start = idx_map[pos]
    end = idx_map[pos + len(norm_needle) - 1] + 1
    return start, end


class SpanGrounder(Grounder):
    def ground(self, evidence: str, components: Sequence[str],
               source_text: str, base_offset: int = 0) -> GroundingResult:
        evidence = (evidence or "").strip()
        if not evidence:
            return GroundingResult(status="span_missing",
                                   note="empty evidence span")

        span = find_span(evidence, source_text)
        if span is None:
            return GroundingResult(status="span_missing",
                                   note="evidence not found in source text")
        start, end = span
        span_text = source_text[start:end]

        missing = [c for c in components
                   if c and not self._contains(span_text, c)]
        if missing:
            return GroundingResult(
                status="components_missing",
                char_start=base_offset + start, char_end=base_offset + end,
                note="span lacks: " + "; ".join(repr(m) for m in missing))
        return GroundingResult(status="pass",
                               char_start=base_offset + start,
                               char_end=base_offset + end)

    # -------------------------------------------------------------- helpers --
    @staticmethod
    def _contains(span_text: str, component: str) -> bool:
        return find_span(component, span_text) is not None
