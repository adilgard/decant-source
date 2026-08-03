# Processing Stage B — parse · chunk · embed (Build Prompt 3) — notes

Delivered: processing-side ABCs in `knowledge_hub/interfaces.py` (Parser /
Chunker / Embedder + ParseError / EmbeddingError), implementations
`parsing_docling.py` (Docling), `chunking.py` (section/passage chunker on the
real bge-m3 tokenizer), `embedding_ollama.py` (bge-m3 via Ollama), the flow
`processing.py` (ProcessingService — also the dispatch-queue consumer the
Prompt 2 claim/ack cycle was shipped for), `../migrations/003_document_review.sql`,
and 8 new tests (52 total, all against the real dockerized stack + LIVE
bge-m3 — no mocks). Persistence goes through Prompt 1's `insert_document` /
`insert_chunks` untouched; raw bytes come only from the version-pinned
`raw_uri` (never a re-fetch from the source).

## The three tiers (and where the superparent actually lives)

The schema (v0.2 §3–4) already answers this: **the superparent IS the
`documents` row** — whole-document metadata/provenance roll-up produced by
the Parser — and `chunks` holds the two tiers under it:

| tier        | table/row                   | role                               |
|-------------|-----------------------------|------------------------------------|
| superparent | `documents`                 | metadata/provenance roll-up        |
| parent      | `chunks` level='parent'     | section/procedure; extraction unit |
| child       | `chunks` level='child'      | ~300-token passage; embed/cite unit|

Sections split at the document's **dominant heading level** (ties prefer the
deeper level; capped at h3 so step-level micro-headings don't shred a
procedure), preamble becomes its own section, and any section over 2048
tokens is sub-split on paragraph boundaries. Children are ~300 bge-m3 tokens
with ~15% overlap, split paragraph → sentence via semchunk. 300 is a
retrieval-precision choice, far under bge-m3's ~8k window on purpose.

Token counts are real: the same bge-m3 tokenizer function drives semchunk's
splitting AND `chunks.token_count`, so the persisted counts are the enforced
bounds (asserted in tests). The tokenizer file (`BAAI/bge-m3`, HF
`tokenizers`) ships in the deploy kit and is seeded into the deployment home
(`config.bge_m3_tokenizer_json`) — a deployed box loads it with ZERO egress
(BP28 #20 / BP30); a dev bench without the file falls back to the one-time
hub download. `check_stack.py` exercises the load either way.

Provenance: `char_start/char_end` anchor into the parser's extracted Markdown
(re-derivable from the landed bytes at any time); children nest inside their
parent's span; `locator` carries section index + heading path.

## Contextual retrieval prefix

Every child carries a one-line deterministic blurb — title + heading path +
passage position — in `contextual_prefix`, and `chunking.embedding_text()`
is the single composition rule for what gets embedded (prefix + passage).
The flow embeds through it and the tests re-embed through it and compare
vectors, so prefix-in-the-vector is a verified property, not a convention.
An LLM-written blurb (Anthropic-style contextual retrieval) can swap in
later behind the same column without touching storage or retrieval.

## §8.1a tag-as-claim, wired

The manifest's `data_track`/`doc_type` (source_registry.config, found via
the `source_ref` the adapter stamps into native_metadata; a per-item
native_metadata declaration overrides) is a CLAIM checked per document with
cheap, deterministic shape detection (`detect_data_track`: container format,
else delimiter-row ratio — gross-error-grade by design):

- **agree** → proceed;
- **detector unsure** → the human tag wins, proceed (the pilot SOP-014 run
  actually took this path: numbered SOP steps are comma-heavy enough to make
  the detector abstain — the declared `prose` tag governed, no flag);
- **confident disagreement** → document persisted, flagged to `review_queue`
  (migration 003: `documents.review_status/review_reason` + the view's new
  `document` feeder + `_enqueue_review(kind='document')`), and **chunking is
  withheld** — never silently auto-override the human, never blindly obey a
  contradicted tag. The raw doc stays `landed`; after adjudication,
  `process(force=True)` finishes the job. Redelivery never double-flags.

Non-prose tracks (declared structured, detection agrees) persist the
superparent and return **no chunks** — `no_chunks` status is the router hook
for Prompt 4's structured strategy.

## Idempotency

Chunk identity = sha256 over (tenant, document_id, tier, section, seq,
prefix, text). The documents row is reused per (tenant, raw_document_id), so
re-processing — even `force=True`, which re-parses and re-embeds — inserts
zero new rows through `insert_chunks`' ON CONFLICT. That is what makes the
dispatch queue's at-least-once delivery safe to consume; a poison document
nacks (error recorded on the queue row) and redelivers without wedging the
drain.

## Metadata precedence

Title/author/timestamps prefer the captured `raw_documents.native_metadata`
(irreplaceable, captured generously at acquisition), falling back to the
parse: docx core properties (via python-docx, a Docling dependency — Docling
itself doesn't surface OOXML core props), then first heading, then filename
stem; `source_timestamp` falls back to `captured_at`. Plain-text landings
feed Docling as Markdown (no dedicated plain-text backend; Markdown parses
bare paragraphs faithfully).

## Ollama endpoint

Native on the GPU host, everything else dockerized — so the endpoint is
config (`OLLAMA_HOST` / `settings.ollama_host`), and from WSL or a container
it is the HOST address, not localhost. `OllamaEmbedder` verifies every
vector is exactly `vector(1024)`-shaped and stamps `embedding_version` with
the served model's digest (the honest "which weights made this vector" for
future re-embedding decisions).

## Sample run (pilot stack, tenant `default`, 2026-07-22)

`pilot_sops/SOP-014_Botanical_Extract_Batch_Release.docx` through capture →
dispatch → consume:

```
capture: ok mode=backfill landed=1 replayed=0 dispatched=1
process: processed raw=1 doc=1 superparents=1 parents=6 children=7
tiers: 1 superparent (documents row) / 6 parents / 7 children
  [id 1] seq=0   57 tok  'SOP-014: Botanical Extract Batch Release'  -> 1 child
  [id 3] seq=1   55 tok  '1. Scope'                                  -> 1 child
  [id 5] seq=2   84 tok  '2. Responsibilities'                       -> 1 child
  [id 7] seq=3  287 tok  '3. Release review procedure'               -> 2 children
  [id 10] seq=4  98 tok  '4. Holds and escalation'                   -> 1 child
  [id 12] seq=5  51 tok  '5. Records'                                -> 1 child
sample child [id 8] (parent=7, 186 tok, embedded bge-m3@790764642607):
  prefix : From section "SOP-014: Botanical Extract Batch Release > 3. Release
           review procedure" of the standard operating procedure 'SOP-014:
           Botanical Extract Batch Release' (passage 1 of 2).
  content: ### 3. Release review procedure 1. Verify the batch record is
           complete: every processing step signed and dated, ...
replay: replayed (parents=6, children=7)
```

Title and author came from the docx core properties ('SOP-014: Botanical
Extract Batch Release' / 'Quality Assurance, Diversified Botanics') because
the filesystem adapter's native_metadata carries no title — the documented
fallback chain working as intended.

## Reproducibility bundle

- `docling`, `semchunk`, `tokenizers`, `ollama` pinned in
  `requirements.lock.txt` and now declared as real dependencies of the
  package (`pyproject.toml`).
- **The lock file was rewritten** — it was UTF-16 (a PowerShell 5.1 `>`
  artifact) and contained `-e file:///C:/Users/...` plus unmarked
  Windows-only pins (`pywin32`, `win-precise-time`), any of which could
  break `pip install -r` on the Ubuntu replay. Now UTF-8, no editable line
  (install-ubuntu.sh installs the package editable itself), win-only pins
  behind `; sys_platform == "win32"`.
- `check_stack.py` gained check 5 (`processing`): Docling parse + tokenizer
  download/cache + chunking + one live prefixed-child embedding. All five
  checks green on this machine 2026-07-22.
- Migration 003 applied to the pilot DB and recorded in `schema_migrations`;
  `install-ubuntu.sh` replays it by glob automatically.
