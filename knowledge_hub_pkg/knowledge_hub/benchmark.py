"""Benchmark runner (Build Prompt 6) — methodology §7.2: one call = one
config against one gold set, everything recorded; enforcement structural.

    runner = BenchmarkRunner(store)
    run = runner.run(tenant, axis="c_embedder",
                     config={"embedder": "bge-m3", "label": "bge-m3 incumbent"},
                     gold_set_version="dryrun-0.1",
                     pin_profile_name="pins-2026-07-v1")

What "structural" means here:
  * config keys are validated against the axis's knob schema — an off-axis
    knob is refused BEFORE anything runs (one-axis-at-a-time is not a
    convention, it's a check).
  * the pin profile must exist; its snapshot is denormalized onto the run.
    A profile entry pinning an explicit model "digest" is verified against
    the served model and refused on mismatch.
  * gold sets must be ACTIVE (drafts are for review); the set's content hash
    and the corpus hash (retrieval) are verified, not trusted.
  * the run row is written status='running' BEFORE execution — a crash
    leaves a visible error row, never a phantom run.
  * an identical (axis, config, gold set, pin profile) 'ok' run is refused
    (force=True overrides, recording a distinct run).
  * aggregates are computed FROM the per-item outcomes that get persisted,
    so recompute-from-items equality holds by construction and is asserted
    by the dry-run.

Only the c_embedder evaluator ships in this phase (the dry-run's vehicle;
exact brute-force cosine per §4 of the methodology — Axis A owns index
noise). a_index / b_er / d_extraction evaluators arrive with the campaign;
asking for them is a clean refusal, not an error row.
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import random
import subprocess
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Optional

from knowledge_hub.config import settings
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.goldsets import GoldSetStore
from knowledge_hub.models import BENCHMARK_AXES, BenchmarkRun, GoldSet, GoldSetItem

RUNNER_VERSION = "0.1.0"
KS = (3, 5, 10, 20)
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 42


class BenchmarkError(Exception):
    pass


AXIS_GOLD_KIND = {
    "a_index": "retrieval",
    "b_er": "er",
    "c_embedder": "retrieval",
    "d_extraction": "extraction",
}

# The knob schema per axis: `required` must be present, anything outside
# `allowed` is an off-axis variation and is refused.
AXIS_KNOBS: dict[str, dict[str, set[str]]] = {
    "a_index": {"required": {"index"},
                "allowed": {"index", "params", "label", "notes"}},
    "b_er": {"required": {"scorer"},
             "allowed": {"scorer", "params", "label", "notes"}},
    "c_embedder": {"required": {"embedder"},
                   "allowed": {"embedder", "mode", "dim", "chunk_tokens",
                               "overlap_pct", "contextual_prefix", "reranker",
                               "prompt_style", "prompt_style_detail",
                               "engine", "fusion_method", "rrf_k",
                               "sparse_weight", "label", "notes"}},
    "d_extraction": {"required": {"model", "contract"},
                     "allowed": {"model", "contract", "params", "label",
                                 "notes"}},
}

# Per-model retrieval prompt styles — OFFICIAL formats sourced from the model
# cards (Build Prompt 7; round 1 ran every model bare, which is only correct
# for bge-m3). Asymmetric: query-side and document-side differ. The runner
# denormalizes the chosen style's EXACT strings into the recorded config
# (prompt_style_detail), so a bad-looking result can always be checked
# against what was actually sent — was it the model, or a bad prefix?
PROMPT_STYLES: dict[str, dict[str, str]] = {
    "none": {
        # bge-m3 is prefix-free by design (BAAI/bge-m3 model card).
        "source": "bge-m3 model card: no query/document prefix",
    },
    "nomic-search": {
        "query_prefix": "search_query: ",
        "document_prefix": "search_document: ",
        "source": "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5 "
                  "(task prefixes required; quality degrades without)",
    },
    "qwen3-instruct": {
        # Documented template; instruction on QUERIES only, documents plain.
        "query_template": ("Instruct: Given a web search query, retrieve "
                           "relevant passages that answer the query\n"
                           "Query: {query}"),
        "source": "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B + "
                  "https://github.com/QwenLM/Qwen3-Embedding "
                  "(Instruct/Query template; +1-5% vs no instruction)",
    },
    "arctic-query": {
        "query_prefix": "Represent this sentence for searching relevant "
                        "passages: ",
        "source": "https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v1.5 "
                  "(query prefix; documents plain)",
    },
    "mxbai-query": {
        "query_prefix": "Represent this sentence for searching relevant "
                        "passages: ",
        "source": "https://huggingface.co/mixedbread-ai/mxbai-embed-large-v1 "
                  "(retrieval query prompt; documents plain)",
    },
}


def code_hash() -> str:
    """sha256 over the sorted package sources — code identity without git
    (the infra folder is not a repo; see methodology §7.1 note)."""
    pkg_dir = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(pkg_dir.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def package_version() -> str:
    try:
        return metadata.version("knowledge_hub")
    except metadata.PackageNotFoundError:
        return "unknown"


def hardware_fingerprint(store: PostgresFactStore, tenant_id: str) -> dict[str, Any]:
    """Best-effort, never fatal: every piece degrades to 'unknown'."""
    fp: dict[str, Any] = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": None, "ram_gb": None,
        "gpus": "unknown",
        "postgres": "unknown", "extensions": {},
    }
    try:
        import psutil
        fp["cpu_count"] = psutil.cpu_count(logical=True)
        fp["ram_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            fp["gpus"] = [line.strip() for line in out.stdout.strip().splitlines()]
    except Exception:
        pass
    try:
        with store.transaction(tenant_id) as conn:
            fp["postgres"] = conn.execute("SHOW server_version").fetchone()[
                "server_version"]
            for row in conn.execute(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname IN ('vector','age','pg_trgm')"):
                fp["extensions"][row["extname"]] = row["extversion"]
    except Exception:
        pass
    return fp


def resolve_model_digests(models: list[str],
                          host: Optional[str] = None) -> dict[str, str]:
    import ollama
    client = ollama.Client(host=host or settings.ollama_host)
    digests: dict[str, str] = {}
    served = {}
    try:
        for entry in client.list().models:
            name = getattr(entry, "model", "") or ""
            served[name.split(":")[0]] = (getattr(entry, "digest", "") or "")[:12]
            served[name] = (getattr(entry, "digest", "") or "")[:12]
    except Exception:
        pass
    for m in models:
        digests[m] = served.get(m, "unknown")
    return digests


# ---------------------------------------------------------------------------
# Retrieval evaluator (axes C now; A reuses it with index modes next phase)
# ---------------------------------------------------------------------------
def detect_embedding_dim(model: str, host: Optional[str] = None) -> int:
    """Probe the served model for its native dimension — challenger embedders
    differ (nomic 768, bge-m3 1024, …) and configs shouldn't hardcode it."""
    import ollama
    client = ollama.Client(host=host or settings.ollama_host)
    try:
        return len(client.embed(model=model, input=["dimension probe"]).embeddings[0])
    except Exception as e:
        raise BenchmarkError(
            f"could not probe embedding dim for {model!r}: {e}") from e


_FLAG_MODELS: dict[str, Any] = {}   # module cache: loading costs ~20-60s


class FlagEmbeddingEncoder:
    """bge-m3 via FlagEmbedding (BAAI's own library) — the only path to the
    model's LEARNED SPARSE (lexical weights); Ollama's embed API is dense-only
    (verified: EmbedResponse has no lexical field). Also usable dense-only as
    an engine diagnostic, since its fp16 HF weights are not byte-identical to
    Ollama's GGUF quantization."""

    HF_NAMES = {"bge-m3": "BAAI/bge-m3"}

    def __init__(self, model: str, want_sparse: bool):
        hf_name = self.HF_NAMES.get(model)
        if hf_name is None:
            raise BenchmarkError(
                f"engine=flagembedding only supports {sorted(self.HF_NAMES)} "
                f"(got {model!r})")
        if hf_name not in _FLAG_MODELS:
            from FlagEmbedding import BGEM3FlagModel
            # Pin to ONE device: with multiple GPUs visible FlagEmbedding
            # spawns a multiprocessing encode pool, which deadlocks under
            # Windows spawn (observed) and is pure overhead at our batch
            # sizes anyway.
            _FLAG_MODELS[hf_name] = BGEM3FlagModel(
                hf_name, use_fp16=True, devices="cuda:0")
        self._model = _FLAG_MODELS[hf_name]
        self._want_sparse = want_sparse
        self.model = model

    def encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict]]:
        out = self._model.encode(list(texts), return_dense=True,
                                 return_sparse=self._want_sparse)
        dense = [list(map(float, v)) for v in out["dense_vecs"]]
        sparse = out.get("lexical_weights") if self._want_sparse else None
        return dense, (list(sparse) if sparse is not None else [{}] * len(texts))

    def lexical_score(self, q_weights: dict, d_weights: dict) -> float:
        return float(self._model.compute_lexical_matching_score(
            q_weights, d_weights))


class RetrievalEvaluator:
    """Exact brute-force retrieval over the gold set's pinned corpus.

    The corpus CONTENT is re-embedded in memory with the config's embedder +
    engine on every run — never read from the stored (incumbent) vectors.
    Stored chunk rows are only the source of content + the integrity hash.

    mode=dense: cosine over dense vectors. mode=hybrid: bge-m3's dense AND
    its own learned sparse (FlagEmbedding), fused per ranked list — RRF by
    default (one constant, no tuning), weighted fusion behind the knob.

    Per-query latency (embed single query + score + fuse) is measured for
    EVERY run this evaluator executes — the methodology's p95 <= 300ms gate
    is adjudicated from these numbers, so both contenders are timed the same
    way on the same hardware.
    """

    def __init__(self, store: PostgresFactStore, config: dict[str, Any]):
        self._store = store
        self._config = config
        self._mode = config.get("mode", "dense")
        self._engine = config.get("engine", "ollama")
        model = config["embedder"]
        if self._engine == "flagembedding":
            self._flag = FlagEmbeddingEncoder(
                model, want_sparse=(self._mode == "hybrid"))
            self._embedder = None
        else:
            from knowledge_hub.embedding_ollama import OllamaEmbedder
            dim = config.get("dim") or detect_embedding_dim(model)
            self._embedder = OllamaEmbedder(model=model, dim=dim)
            self._flag = None
        style = config.get("prompt_style", "none")
        if style not in PROMPT_STYLES:
            raise BenchmarkError(
                f"unknown prompt_style {style!r} — known: "
                f"{sorted(PROMPT_STYLES)}")
        self._style = PROMPT_STYLES[style]

    def _encode(self, texts: list[str]) -> tuple[list[list[float]], list[dict]]:
        if self._flag is not None:
            return self._flag.encode(texts)
        return self._embedder.embed(texts), [{}] * len(texts)

    def _format_query(self, query: str) -> str:
        template = self._style.get("query_template")
        if template:
            return template.format(query=query)
        return self._style.get("query_prefix", "") + query

    def _format_document(self, text: str) -> str:
        return self._style.get("document_prefix", "") + text

    @property
    def models(self) -> list[str]:
        return [self._config["embedder"]]

    def evaluate(self, tenant_id: str, gold: GoldSet,
                 items: list[GoldSetItem]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        import numpy as np

        corpus_ids = gold.spec.get("corpus_chunk_ids") or []
        if not corpus_ids:
            raise BenchmarkError("retrieval gold set has no corpus_chunk_ids in spec")
        with self._store.transaction(tenant_id) as conn:
            rows = conn.execute(
                """SELECT id, content_hash, content
                   FROM chunks WHERE tenant_id=%s AND id = ANY(%s)
                   ORDER BY id""", (tenant_id, corpus_ids)).fetchall()
        if len(rows) != len(corpus_ids):
            raise BenchmarkError(
                f"corpus mismatch: spec names {len(corpus_ids)} chunks, "
                f"{len(rows)} found for tenant {tenant_id}")
        by_id = {r["id"]: r for r in rows}
        got_hash = hashlib.sha256("".join(
            by_id[cid]["content_hash"] for cid in corpus_ids).encode()).hexdigest()
        want_hash = gold.spec.get("corpus_hash")
        if want_hash and got_hash != want_hash:
            raise BenchmarkError("corpus content hash mismatch — the corpus "
                                 "changed since the gold set was registered")

        doc_texts = [self._format_document(by_id[cid]["content"])
                     for cid in corpus_ids]
        dense_docs, sparse_docs = self._encode(doc_texts)
        matrix = np.array(dense_docs, dtype=np.float64)
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)

        # Per-query loop, timed one query at a time: embed + score (+ sparse
        # + fuse) is the serving-shaped unit the p95 gate adjudicates.
        outcomes: list[dict[str, Any]] = []
        for it in items:
            t0 = time.perf_counter()
            dense_q, sparse_q = self._encode(
                [self._format_query(it.item["query"])])
            qv = np.array(dense_q[0], dtype=np.float64)
            qv /= np.linalg.norm(qv)
            dense_scores = matrix @ qv
            if self._mode == "hybrid":
                sparse_scores = np.array(
                    [self._flag.lexical_score(sparse_q[0], dw)
                     for dw in sparse_docs])
                order = self._fuse(dense_scores, sparse_scores)
            else:
                order = np.argsort(-dense_scores)
            latency_ms = (time.perf_counter() - t0) * 1000

            ranked_ids = [corpus_ids[i] for i in order]
            relevant = set(it.item["relevant_chunk_ids"])
            ranks = [ranked_ids.index(cid) + 1 for cid in relevant
                     if cid in by_id]
            first = min(ranks) if ranks else None
            outcome: dict[str, Any] = {
                "ranked_chunk_ids": ranked_ids[:max(KS)],
                "relevant_ranks": sorted(ranks),
                "first_relevant_rank": first,
                "latency_ms": round(latency_ms, 2),
            }
            for k in KS:
                top = set(ranked_ids[:k])
                outcome[f"hit_any_at_{k}"] = bool(relevant & top)
                outcome[f"hit_all_at_{k}"] = relevant <= top
            outcomes.append(outcome)

        metrics = self.aggregate(outcomes)
        metrics["embedding_dim"] = len(dense_docs[0])   # resolved, not assumed
        return outcomes, metrics

    def _fuse(self, dense_scores, sparse_scores):
        """Fuse the two ranked lists. RRF (default): rank-based, one constant,
        nothing to tune. Weighted: max-normalized score blend — a dial-in-
        later knob, deliberately not tuned in the round that introduces it."""
        import numpy as np
        if self._config.get("fusion_method", "rrf") == "rrf":
            k = self._config.get("rrf_k", 60)
            dense_rank = np.empty_like(dense_scores, dtype=np.int64)
            dense_rank[np.argsort(-dense_scores)] = np.arange(1, len(dense_scores) + 1)
            sparse_rank = np.empty_like(sparse_scores, dtype=np.int64)
            sparse_rank[np.argsort(-sparse_scores)] = np.arange(1, len(sparse_scores) + 1)
            fused = 1.0 / (k + dense_rank) + 1.0 / (k + sparse_rank)
        else:
            w = self._config.get("sparse_weight", 0.3)
            smax = sparse_scores.max()
            sparse_norm = sparse_scores / smax if smax > 0 else sparse_scores
            fused = (1.0 - w) * dense_scores + w * sparse_norm
        return np.argsort(-fused)

    @staticmethod
    def aggregate(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregates FROM per-item outcomes — the same function the dry-run
        uses to prove recompute-from-items equality."""
        n = len(outcomes)
        metrics: dict[str, Any] = {"n_queries": n}
        for k in KS:
            metrics[f"recall_at_{k}_any"] = sum(
                o[f"hit_any_at_{k}"] for o in outcomes) / n
            metrics[f"recall_at_{k}_all"] = sum(
                o[f"hit_all_at_{k}"] for o in outcomes) / n
        metrics["mrr"] = sum(
            (1.0 / o["first_relevant_rank"]) if o["first_relevant_rank"] else 0.0
            for o in outcomes) / n
        ndcg = []
        for o in outcomes:
            rel = set(o["relevant_ranks"])
            dcg = sum(1.0 / math.log2(r + 1) for r in o["relevant_ranks"] if r <= 10)
            ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(rel), 10)))
            ndcg.append(dcg / ideal if ideal else 0.0)
        metrics["ndcg_at_10"] = sum(ndcg) / n

        # Bootstrap CI on the headline (§6.2: resample gold items, 1000x).
        rng = random.Random(BOOTSTRAP_SEED)
        hits = [1.0 if o["hit_any_at_10"] else 0.0 for o in outcomes]
        samples = sorted(
            sum(hits[rng.randrange(n)] for _ in range(n)) / n
            for _ in range(BOOTSTRAP_RESAMPLES))
        lo = samples[int(0.025 * BOOTSTRAP_RESAMPLES)]
        hi = samples[min(int(0.975 * BOOTSTRAP_RESAMPLES),
                         BOOTSTRAP_RESAMPLES - 1)]
        metrics["recall_at_10_any_ci95"] = [lo, hi]
        metrics["bootstrap"] = {"resamples": BOOTSTRAP_RESAMPLES,
                                "seed": BOOTSTRAP_SEED}

        # Per-query latency percentiles (present since Build Prompt 8; older
        # runs' outcomes lack latency_ms and simply don't get these keys —
        # aggregate() must recompute cleanly from either vintage).
        lats = sorted(o["latency_ms"] for o in outcomes if "latency_ms" in o)
        if len(lats) == len(outcomes):
            def pct(p: float) -> float:
                return lats[min(int(p * len(lats)), len(lats) - 1)]
            metrics["latency_ms_p50"] = pct(0.50)
            metrics["latency_ms_p95"] = pct(0.95)
            metrics["latency_ms_p99"] = pct(0.99)

        metrics["headline_name"] = "recall_at_10_any"
        metrics["headline_value"] = metrics["recall_at_10_any"]
        return metrics


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------
class BenchmarkRunner:
    def __init__(self, store: PostgresFactStore):
        self._store = store
        self._gold = GoldSetStore(store)

    def run(self, tenant_id: str, *, axis: str, config: dict[str, Any],
            gold_set_version: str, pin_profile_name: str,
            notes: Optional[str] = None, force: bool = False) -> BenchmarkRun:
        # --- structural refusals (nothing recorded yet) --------------------
        if axis not in BENCHMARK_AXES:
            raise BenchmarkError(f"axis must be one of {BENCHMARK_AXES}")
        knobs = AXIS_KNOBS[axis]
        missing = knobs["required"] - config.keys()
        if missing:
            raise BenchmarkError(f"{axis} config missing required knobs: {missing}")
        offaxis = config.keys() - knobs["allowed"]
        if offaxis:
            raise BenchmarkError(
                f"config keys {sorted(offaxis)} are not {axis} knobs — one "
                "axis at a time; everything else is pinned by the profile")
        if axis == "c_embedder":
            # Denormalize the prompt style's EXACT strings into the recorded
            # config: the prompt is provenance (was it the model, or a bad
            # prefix?). Callers may not supply their own detail.
            style = config.get("prompt_style", "none")
            if style not in PROMPT_STYLES:
                raise BenchmarkError(
                    f"unknown prompt_style {style!r} — known: "
                    f"{sorted(PROMPT_STYLES)}")
            supplied = config.get("prompt_style_detail")
            if supplied is not None and supplied != PROMPT_STYLES[style]:
                raise BenchmarkError(
                    "prompt_style_detail is derived from the registry, "
                    "not caller-supplied")
            config = {**config, "prompt_style": style,
                      "prompt_style_detail": PROMPT_STYLES[style]}
            # Retrieval mode + fusion (Build Prompt 8). Same denormalization
            # discipline: hybrid runs record their engine + fusion params.
            mode = config.setdefault("mode", "dense")
            engine = config.setdefault("engine", "ollama")
            if mode not in ("dense", "hybrid"):
                raise BenchmarkError(f"mode must be dense|hybrid, got {mode!r}")
            if engine not in ("ollama", "flagembedding"):
                raise BenchmarkError(
                    f"engine must be ollama|flagembedding, got {engine!r}")
            if mode == "hybrid":
                if engine != "flagembedding":
                    raise BenchmarkError(
                        "hybrid needs engine='flagembedding': Ollama's embed "
                        "API returns dense vectors only (no bge-m3 lexical "
                        "weights) — the sparse side must come from "
                        "FlagEmbedding, and that source is provenance")
                fusion = config.setdefault("fusion_method", "rrf")
                if fusion == "rrf":
                    config.setdefault("rrf_k", 60)
                elif fusion == "weighted":
                    config.setdefault("sparse_weight", 0.3)
                else:
                    raise BenchmarkError(
                        f"fusion_method must be rrf|weighted, got {fusion!r}")
            else:
                hybrid_only = {"fusion_method", "rrf_k",
                               "sparse_weight"} & config.keys()
                if hybrid_only:
                    raise BenchmarkError(
                        f"{sorted(hybrid_only)} are hybrid-only knobs; this "
                        "config is mode=dense")

        with self._store.transaction(tenant_id) as conn:
            prof = conn.execute("SELECT * FROM pin_profiles WHERE name=%s",
                                (pin_profile_name,)).fetchone()
        if prof is None:
            raise BenchmarkError(f"unknown pin profile {pin_profile_name!r}")
        pin_profile = prof["profile"]

        gold, items = self._gold.get(tenant_id, AXIS_GOLD_KIND[axis],
                                     gold_set_version)
        if gold.status != "active":
            raise BenchmarkError(
                f"gold set {gold.kind}/{gold.version} is {gold.status!r} — "
                "runs need an ACTIVE (reviewed) set")

        if not force:
            with self._store.transaction(tenant_id) as conn:
                dup = conn.execute(
                    """SELECT id FROM benchmark_runs
                       WHERE tenant_id=%s AND axis=%s AND gold_set_id=%s
                         AND pin_profile_name=%s AND config=%s AND status='ok'""",
                    (tenant_id, axis, gold.id, pin_profile_name,
                     json.dumps(config, sort_keys=True))).fetchone()
            if dup:
                raise BenchmarkError(
                    f"identical run already recorded (run {dup['id']}) — "
                    "pass force=True to record another")

        if axis != "c_embedder":
            raise BenchmarkError(
                f"the {axis} evaluator arrives with the campaign phase; "
                "only c_embedder runs in the recording phase")
        evaluator = RetrievalEvaluator(self._store, config)

        # --- provenance, then the running row -------------------------------
        pinned_models = [v.get("embedder") or v.get("model")
                         for v in pin_profile.values() if isinstance(v, dict)]
        digests = resolve_model_digests(
            sorted({m for m in evaluator.models + pinned_models if m}))
        for axis_key, pinned in pin_profile.items():
            want = isinstance(pinned, dict) and pinned.get("digest")
            name = isinstance(pinned, dict) and (pinned.get("embedder") or
                                                 pinned.get("model"))
            if want and name and digests.get(name) not in (want, "unknown"):
                raise BenchmarkError(
                    f"pin profile pins {name}@{want} but served digest is "
                    f"{digests.get(name)} ({axis_key})")

        provenance = dict(
            model_digests=json.dumps(digests),
            package_version=package_version(),
            code_hash=code_hash(),
            runner_version=RUNNER_VERSION,
            hardware=json.dumps(hardware_fingerprint(self._store, tenant_id)),
        )
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                """INSERT INTO benchmark_runs
                     (tenant_id, axis, config, pin_profile_name, pin_profile,
                      gold_set_id, gold_set_hash, advisory, model_digests,
                      package_version, code_hash, runner_version, hardware,
                      notes, status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running')
                   RETURNING id, started_at""",
                (tenant_id, axis, json.dumps(config, sort_keys=True),
                 pin_profile_name, json.dumps(pin_profile), gold.id,
                 gold.content_hash, not gold.floors_met,
                 provenance["model_digests"], provenance["package_version"],
                 provenance["code_hash"], provenance["runner_version"],
                 provenance["hardware"], notes)).fetchone()
        run_id = row["id"]

        # --- execute; ok and error both land on the same row ---------------
        t0 = time.monotonic()
        try:
            outcomes, metrics = evaluator.evaluate(tenant_id, gold, items)
            wall_ms = int((time.monotonic() - t0) * 1000)
            with self._store.transaction(tenant_id) as conn:
                for it, outcome in zip(items, outcomes):
                    conn.execute(
                        """INSERT INTO benchmark_run_items
                             (run_id, gold_set_item_id, outcome)
                           VALUES (%s,%s,%s)""",
                        (run_id, it.id, json.dumps(outcome)))
                final = conn.execute(
                    """UPDATE benchmark_runs
                       SET metrics=%s, wall_ms=%s, status='ok', finished_at=now()
                       WHERE id=%s RETURNING *""",
                    (json.dumps(metrics), wall_ms, run_id)).fetchone()
            return BenchmarkRun(**final)
        except Exception as e:
            wall_ms = int((time.monotonic() - t0) * 1000)
            with self._store.transaction(tenant_id) as conn:
                conn.execute(
                    """UPDATE benchmark_runs
                       SET status='error', error=%s, wall_ms=%s, finished_at=now()
                       WHERE id=%s""",
                    (f"{type(e).__name__}: {e}", wall_ms, run_id))
            raise BenchmarkError(
                f"run {run_id} failed and was recorded as error: {e}") from e
