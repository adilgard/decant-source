# Extraction — the fact producer (Build Prompt 4) — notes

Delivered: extraction-side ABCs in `knowledge_hub/interfaces.py`
(OntologyBinding / ExtractionStrategy / Grounder + ExtractionError and the
candidate/result/digest models), implementations `ontology.py` (binding
generated from ontology_versions data), `extraction_llm.py` (qwen3.6 joint
pass), `extraction_structured.py` (deterministic SoR column map),
`grounding.py` (SpanGrounder), the flow `extraction.py` (ExtractionService —
also the extraction_queue consumer), `../migrations/004_extraction.sql`
(applied to the pilot DB, tracked in schema_migrations, picked up by
install-ubuntu.sh's glob), and 10 new tests (62 total, real stack + LIVE
qwen3.6, no mocks). check_stack.py gained check 6 (extraction).

**Read this first:** Prompts 1–3 were deterministic infrastructure; this
stage's output is a judgment. Green tests mean the MACHINERY works — staging,
routing, quarantine, grounding, envelopes, idempotency. They say nothing
about whether qwen3.6 extracted the *right* facts; that is the extraction
benchmark's question (Axes A/B/C), and everything this stage persists —
grounding results, quarantine reasons with raw model output, per-unit token/
wall numbers — exists to give that benchmark signal. §"What the pilot run
shows about quality" below is observation, not something that was tuned away.

## The parser_supplied seam (added after BP4; corpus-agnostic build)

Routing used to be one line: `if document.data_track == PROSE_TRACK` picked
the LLM strategy, else the structured one. That conflated two questions —
what SHAPE the content is (which drives parsing and chunking) and WHO
produces its facts. They are now separate, and both are source config:

| key | selects | absent means |
|---|---|---|
| `parser` | a registered `Parser` (bytes -> text) | the injected default (Docling) |
| `extraction_strategy` | `llm` \| `structured_map` \| `parser_supplied` | the old data_track branch, exactly |
| `fact_parser` | a `FactParser` plugin | n/a (required by `parser_supplied`) |

`plugins.py` holds the registries and the config resolution. A component is
named either by a registered short name or by `package.module:Attribute`,
resolved with importlib at selection time — which is why core never imports
a plugin: the module name arrives as DATA, from a database row. A reference
pointing back into `knowledge_hub` is REFUSED (`BoundaryViolation`), because
a plugin inside the corpus-agnostic package is the violation the seam exists
to prevent.

**The ontology gate is why the plugin contract stops one step short of
`ExtractionResult`.** Vocabulary validation has always lived inside the
strategy — `_finalize` does none, and the database does none either
(`facts.predicate` is bare TEXT; only `ontology_version` has an FK). So a
plugin allowed to return a finished result would sit downstream of the only
allowlist check that exists. Instead a plugin returns `ParsedFact`, and
`extraction_parser_supplied.py` is the only thing that can turn one into a
candidate. It applies the same three checks the LLM path applies, and
quarantines rejects into the same review queue with the plugin's raw output
attached. A parser gets no more trust than a model.

**Spans are verified, not trusted.** A plugin declares character offsets and
names the text there; `Grounder.verify_span` slices and compares
(`declared_span` on success, `span_mismatch` on failure, which takes the
usual confidence penalty and review flag). This is the reverse of the quote
path and strictly better where it applies: searching for a quote returns the
FIRST occurrence, which for a phrase that recurs is usually the wrong one.
Whitespace and case differences are tolerated — right characters, different
rendering. Offsets with no text named are treated as unverified, not as a
free pass.

Offsets anchor into the document's **extracted text** (what `extract_text`
returned and what chunks were cut from), never into the raw bytes; byte
offsets would not line up with any chunk. Extraction holds no Parser, so it
rebuilds that text from the persisted parent chunks at their original
offsets (`document_text_from_chunks`) rather than re-parsing and risking two
parses that disagree. Each verified span is then anchored to the parent
chunk containing it, so retrieval's `facts_citing` enrichment surfaces
plugin facts exactly like model facts; a span straddling two parents stays
document-anchored rather than being assigned to an arbitrary one.

Provenance names the producer: `extractor = "parser_supplied:<plugin>"` and
`extractor_version = <plugin version>`. Because the idempotency ledger keys
on both, shipping a new plugin version makes the same document fresh work
instead of replaying the old plugin's verdict.

Console: `GET /v1/components` reports what this build has registered, and
the `set_extraction_setup` write op MERGES the three keys into a source's
config. It is deliberately not `edit_scope`, which replaces the config
wholesale — a three-field form driving that would silently drop
`data_track`, `structured_map` and the folder root.

No migration was needed: `pending_facts.grounding` is bare TEXT, and the
quarantine reasons reused here already exist in `chk_quarantine_reason`.

## Where facts go (and don't)

Extraction stages into `pending_facts` via `stage_pending`, never into
`facts`: pre-resolution refs are `mention:<entity_mentions.id>`, rewritten
from extraction-local keys as mentions persist. The resolver promotes later
(`Pipeline.promote_pending`). `facts.subject_entity_id` being NOT NULL makes
this structural, not conventional. The envelope rides every staged fact:
ontology_version, extractor + extractor_version (the served model digest —
see "extractor_version" below), confidence, security_label inherited from the
source document, and the v0.2 size soft-alarm (now computed at staging too,
same rule as `write_facts`).

## Routing

`document.data_track` picks the strategy: prose/SOP/comms → `llm_joint` over
the parent chunks, sequentially within a document (the digest accumulates),
parallel across documents (SKIP LOCKED claims — add workers, no
coordination). The `no_chunks` documents P3 emits (SoR/tabular) →
`structured_map` over the exact landed bytes (version-pinned raw_uri).
Review-held documents (§8.1a) are not extracted — a human owns them.
Everything is tenant-scoped.

## The queue (outbox, repeated)

Migration 004 adds `extraction_queue`, shape-identical to `dispatch_queue`;
`PostgresDispatcher` grew a `table` argument (allowlisted) so one
implementation serves both stages. ProcessingService now takes an optional
`extraction_dispatcher` and enqueues after every successful pass (processed /
replayed / no_chunks — never review). ExtractionService.consume is the
claim → extract → ack loop; poison (e.g. a raw doc with no document row yet)
nacks with the error recorded and redelivers by lease.

## Ontology binding is DATA

`PostgresOntologyBinding` derives everything from one ontology_versions row:
the output JSON schema, the vocabulary checks, the prompt's vocabulary block
(with per-type/per-predicate examples), and the predicate alias map. New
ontology row → new binding, zero code change. Migration 004 enriched
baseline-0.1's definition with `examples` and `predicate_aliases` — both are
ontology data, so the real ontology replaces them wholesale.

Aliases handle the surface-variant normalization the smoke test flagged:
`"owned by" → owns` with `swap: true` (the owner becomes the subject).
Normalization happens ONLY through this data map after case/whitespace
canonicalization; anything else is a genuine unknown → quarantine.

## Constrain structure, validate meaning (§8.2c/f/g)

- The Ollama `format=` schema makes malformed JSON impossible at decode time.
- Predicates and entity types are **deliberately NOT enum-constrained** in
  that schema: an off-ontology attempt must survive to the quarantine —
  what the model keeps trying to say and can't is the signal that grows the
  ontology. The pilot run proved the point immediately: `retained_for`
  showed up 3× (SOP-014 §5 Records), which is a real modeling gap
  (retention periods), exactly what the quarantine exists to surface.
- Meaning is validated by deterministic code (Pydantic shape → key
  discipline → vocabulary via the binding → self-loop guard), never by an
  LLM. Repair is capped at one round-trip; won't-validate-after-one → the
  raw output is quarantined (`validation_failure`), never dropped.

## Where qwen3.6's structured output needed coaxing (benchmark input!)

The first pilot pass (prompt contract p1) produced **0 staged facts and 24
quarantines** on SOP-014 — the machinery caught everything, and the reasons
were instructive:

1. **Objectless facts.** With `object`/`object_literal` merely optional in
   the schema, constrained decoding let qwen3.6 omit them, and it did so
   for ~80% of facts (`"object": null`, no literal). Fix (contract p2):
   both keys are **required but nullable** — the decoder must emit them, so
   the model must choose explicitly — plus a prompt rule "if you cannot
   fill either, do not emit the fact". This took SOP-014 from 0 → 26 staged
   facts. This is a structural fix, not content tuning.
2. **Names as keys.** The model referenced entities it never declared
   (`"subject": "SOP-006"`). p2 adds an explicit key-discipline rule;
   remaining offenders still quarantine (`validation_failure`).
3. **Self-referential facts.** One `certificate part_of certificate`
   surfaced; now deterministically quarantined (subject == object).
4. `think:false` + `format=` works cleanly on qwen3.6 (Ollama 0.6.2) — no
   think-block leakage, temp 0, ~1–6s per parent on this GPU. Zero repair
   retries were needed across the whole pilot run and test suite; the
   repair path is exercised only synthetically so far.

**extractor_version = `qwen3.6@<digest>/p<N>`** — the served-weights digest
plus the prompt-contract revision. Same weights under a changed contract are
a different extractor: the idempotency ledger then re-extracts instead of
replaying stale runs, and the benchmark can compare contracts honestly.
(The pilot DB currently holds both p1 runs — all-quarantine — and p2 runs;
that history is itself benchmark data.)

## Coreference: the digest works

The carried-forward digest (`e<mention_id>` keys, most-referenced entities
sticky, top 15 in the prompt) resolved in-pass on both the test doc (a
pronoun subject in a later parent landed on the parent-1 mention) and
SOP-014 (later parents emitted facts about `e1`/`e7`/`e8` — digest keys —
without re-declaring them; p2 runs staged 0 new mentions for 5 of 6 parents
because everything resolved against p1's). The digest is rebuilt from the DB
on entry, so partial replays and process restarts keep their context.
Pronouns never become mentions (prompt rule + asserted in tests).

## Grounding: deterministic, flag-don't-reject

`SpanGrounder`: evidence must exist in the parent text (exact, then
whitespace/case-normalized with offsets mapped back), and must contain the
fact's components (subject/object surface or literal, same matching).
Verified spans become real document char offsets (parent.char_start + local —
the P3 guarantee that chunk offsets slice back into the extracted text is
what makes this trustworthy). Failure lowers confidence ×0.5 and flags
needs_review — never rejects, because legitimate paraphrase exists. The
failure MODE persists per fact (`pending_facts.grounding`: pass /
span_missing / components_missing; SoR facts are `construction`).

## Quarantine + review feeders (migration 004)

`quarantined_extractions` keeps reason (unbound_entity_type /
unbound_predicate / validation_failure), detail, and the RAW model output —
the flywheel's labels. review_queue gained two feeders: `quarantine` (open
items) and `pending_fact` (grounding-flagged facts), same pattern as 003's
document feeder. `_enqueue_review` knows both kinds.

## Observability (the benchmark's inputs)

`extraction_runs`, one row per unit: strategy, extractor_version,
ontology_version, prompt/output token counts, wall-clock, facts/mentions
staged, quarantined, grounding flags, repairs, status. Doubles as the
**idempotency ledger**: unique on (tenant, unit_hash, extractor,
extractor_version, ontology_version) where status='ok'; unit_hash is the
parent's content_hash (prose) or the raw doc's (structured). Re-extraction
replays; a concurrent duplicate trips the index inside the same transaction
as stage_pending, so at-least-once delivery can never double-stage.

## SoR: structured_map

Deterministic, no LLM, grounded by construction, locator = the cell
({"row": n, "col": name}). The column→predicate map is MANIFEST data
(`structured_map` in source_registry.config or per-item native_metadata) —
a system of record's meaning is declared by its owner, not guessed by a
model. Mapped-but-unbound predicates quarantine ONCE per document (the
mapping is wrong, not the rows). No mapping at all → row mentions only
(entity observations without invented predicates). Source-native keys
(`key_columns`) ride mentions' extracted_keys; prose mentions get
email/domain/tax_id by deterministic regex over the surface form only.
Match-normalization stays the resolver's job.

## What the pilot run shows about quality (for the benchmark, not fixed here)

p2 on SOP-014: 26 staged facts across 6 parents (0 from the title parent),
19 clean-grounded, 7 grounding-flagged, 3 quarantined. Real signal visible
in the staged rows:

- `part_of` is the model's dumping ground ("certificate part_of 'seven
  years'", "certificate part_of QA archive") — retention/location need
  their own predicates in the real ontology.
- `participated_in` gets whole clauses as literals ("assembles the batch
  record and submits it for review") — role/duty facts don't fit the
  baseline vocabulary; oversized-literal facts like these are what the
  size soft-alarm and the resolver's review will catch.
- Directionality is shaky even with examples in the prompt (the very first
  smoke test emitted `SOP owns QA Team`). The alias swap fixes the marked
  variants; bare `owns` misdirection is a model-quality axis to measure.

None of this was prompt-tuned away — it is the honest baseline the model
benchmark starts from.

## Sample run (pilot stack, tenant `default`, SOP-014, 2026-07-22)

```
extract: extracted raw=1 doc=1 units=6 facts=26 mentions=8(+16 from p1) quarantined=3 grounding_flags=7
sample staged tuples (chunk 5 = '2. Responsibilities'):
  [5] QA reviewer (Person, mention:6) --participated_in-->
      'performs the release assessment described here and owns the release decision'
      conf=1.00 grounding=pass chars=582..675
  [6] QA reviewer (Person, mention:6) --owns--> 'the release decision'
      conf=1.00 grounding=pass chars=582..675
      evidence: The QA reviewer performs the release assessment described here and owns the release decision.
sample quarantined item:
  reason=unbound_predicate detail='retained_for' chunk=12 status=open  -> review_queue kind='quarantine'
  raw: {"subject": "n1", "predicate": "retained_for", "object": null, ... "evidence": "...quality records retained for seven years..."}
sample grounding flag:
  [1] SOP-014 (Document, mention:1) --part_of--> Building A facility (Location, mention:2)
      conf=0.50 grounding=components_missing chars=267..348  [needs_review -> review_queue kind='pending_fact']
observability (extraction_runs, p2):
  chunk=3  qwen3.6@07d35212591f/p2 tok=970/246  wall=1922ms facts=3  quar=0 flags=1
  chunk=7  qwen3.6@07d35212591f/p2 tok=1181/892 wall=5592ms facts=5  quar=2 flags=4
  chunk=12 qwen3.6@07d35212591f/p2 tok=963/1016 wall=6250ms facts=11 quar=0 flags=2
replay: re-extract -> replayed (units=6, replayed=6); pending_facts/mentions/runs unchanged
```

## Reproducibility

No new dependencies (ollama/pydantic already pinned); package version
0.4.0. Migration 004 applied to the pilot DB + recorded in
schema_migrations; install-ubuntu.sh replays it by glob. check_stack.py
check 6 exercises binding-from-DB (fails loudly if 004's ontology data is
missing), one live schema-constrained qwen3.6 call, and the grounder — the
extraction-specific pieces most likely to differ on the Ubuntu boxes.

## The extractor is backend-dependent (2026-07-28, Strix Halo spike, BP43)

> **Reference-manual flag:** the reference manual does not exist yet. When it
> is built, this section is REQUIRED CONTENT for Part III (extraction) and
> Part V (backend selection). Until then, this section is the canonical
> statement. Spike record:
> `.Handoff Docs/strix-halo-inference-spike-2026-07-28.md`.

There is no single universal extraction model. The model is a function of
the deployment path, and any prose that states "the extractor is
qwen3.6:27b-bf16" as a flat fact is wrong by omission:

- **NVIDIA appliance path (CUDA):** dense `qwen3.6:27b-bf16`. Unchanged.
- **AMD Strix Halo path (ROCm, Ollama-bundled `rocm_v7_2`):** MoE
  `qwen3.6:35b-a3b-q4_K_M`. A dense 27B is bandwidth-bound on Strix Halo's
  ~256 GB/s unified memory: ~16 GB of weights read per token puts the
  ceiling near 15 tok/s, and 12.7 tok/s was measured. The MoE activates
  ~3B parameters per token and measured 58.8 tok/s. Throughput figures are
  single samples, but the ceiling is arithmetic, not sampling noise:
  **dense models of any real size are bandwidth-limited on Strix Halo
  regardless of backend, so MoE is the model class for this hardware.**
  Vulkan works and is the documented fallback; ROCm won on prefill and on
  the visible memory ceiling. Caveat: the prefill advantage was measured on
  a one-sentence prompt; real-document prefill is untested.

**The fact envelope is already correct. Do not "fix" it.** `extractor` and
`extractor_version` are per-fact PROVENANCE: they record whichever model
actually produced each fact. A fact extracted on the AMD box genuinely was
produced by the MoE q4 model, and its envelope should say so. Backend
dependence changes the prose above, never the envelope.

**No quality-equivalence claim.** The spike measured throughput, GPU
engagement, and JSON well-formedness only. It never measured extraction
accuracy against ground truth. Nothing here says the AMD path extracts as
well as the NVIDIA path; that is Axis D's question, and it is gated on the
open decision below.

### Contract requirements the spike surfaced (model-independent)

1. **Reification is a CONTRACT MANDATE, not a model preference.** The MoE
   flattened event qualifiers (`Diversified Botanics / acquisition date /
   March 2024`) where the dense model reified them (`acquisition of Verdant
   Labs by Diversified Botanics / occurred in / March 2024`). The flat form
   is lossy: it does not bind the qualifier to a specific event, so a second
   same-subject event collides. The prompt contract must specify reification
   explicitly rather than assume the model does it.
2. **Completeness is CONTRACT-sensitive, not quantization-sensitive.** A
   naive "extract every triple" prompt returned 3 of ~7 available facts; an
   explicit contract (decompose fully, qualifiers become their own triples,
   no vague subjects) returned all 7 on the same model at the same quant.
   Do not misread a low completeness number as evidence against
   quantization before checking the contract.
3. **`"think": false` is required for qwen3.6** (a hybrid reasoning model)
   or thinking tokens leak into the response body. Already the p2 contract's
   setting; now re-evidenced on the MoE + ROCm path.
4. **Entity canonicalization and coreference are downstream (or contract)
   work.** Subjects come back as descriptive noun phrases (`the extraction
   facility operated by Verdant Labs`), not stable identifiers. This matches
   the existing boundary (extraction CAPTURES, the resolver CANONICALIZES,
   §8.2h) but must be stated in the contract, not assumed.

### Architecture across paths: DECIDED 2026-07-28, Option A (same architecture)

Posed as open by BP43; decided the same day (BP44). Canonical decision
record: progress doc §8.26d. Short form:

- **DECIDED: Option A, SAME architecture on both paths.** The MoE in bf16
  on NVIDIA (`qwen3.6:35b-a3b-bf16`, 71 GB, registry-verified 2026-07-28)
  and the same MoE at q4 on AMD (`qwen3.6:35b-a3b-q4_K_M`). The only
  cross-path variable is precision, so "is the AMD path as good as the
  NVIDIA path" becomes a one-variable question Axis D can actually answer.
- Option B (dense bf16 on NVIDIA, MoE q4 on AMD) was rejected: the paths
  would differ on both precision and architecture, and no clean Axis-D
  equivalence claim would ever be possible.

**Consequences for this contract:** the NVIDIA appliance model changes from
the proven dense `qwen3.6:27b-bf16` (still what the signed 0.26.x kits
carry) to the MoE bf16, executed at a future kit build. The p2 prompt
contract is proven on the dense model only; the MoE bf16 needs its OWN
contract validation when that box is built, and the reification mandate
(item 1 above) is exactly the behavior to re-verify, since the spike showed
the MoE architecture flattens qualifiers when the contract does not forbid
it. Full cross-path accuracy benchmarking is deferred; **quality
equivalence remains UNVALIDATED. Option A makes it a clean one-variable
measurement; it does not itself prove parity.**
