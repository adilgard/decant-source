"""OllamaEmbedder — bge-m3 via Ollama, the pilot Embedder.

Ollama runs NATIVELY on the GPU host (Windows here, systemd on the Ubuntu
boxes) while everything else is dockerized, so the endpoint is configurable:
settings.ollama_host / OLLAMA_HOST. From inside WSL or a container that host
is NOT localhost — pass the host machine's address.

Shape is verified, not assumed: every returned vector must have exactly
`dim` floats (schema vector(1024)); anything else raises EmbeddingError
rather than letting a mis-pulled model quantly poison the index.

embedding_version is the served model's digest (12 hex chars, resolved once
from the Ollama registry at first use) — the honest "which exact weights
made this vector" stamp that re-embedding decisions need. Falls back to
"unknown" if the digest can't be resolved; embedding still proceeds.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import ollama

from knowledge_hub.config import settings
from knowledge_hub.interfaces import Embedder, EmbeddingError
from knowledge_hub.ollama_client import make_ollama_client

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 32


class OllamaEmbedder(Embedder):
    def __init__(
        self,
        host: Optional[str] = None,
        model: Optional[str] = None,
        dim: Optional[int] = None,
        batch_size: int = DEFAULT_BATCH,
        client: Optional[ollama.Client] = None,
    ):
        self.model = model or settings.embedding_model
        self.dim = dim or settings.embedding_dim
        self._batch_size = batch_size
        self._client = client or make_ollama_client(host)
        self._version: Optional[str] = None

    @property
    def version(self) -> str:
        """Digest of the served model (lazily resolved, cached)."""
        if self._version is None:
            self._version = self._resolve_version()
        return self._version

    # ----------------------------------------------------------- Embedder --
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start:start + self._batch_size])
            try:
                response = self._client.embed(model=self.model, input=batch)
            except Exception as e:
                raise EmbeddingError(
                    f"ollama embed failed for batch at offset {start} "
                    f"(model {self.model!r}): {type(e).__name__}: {e}") from e
            got = list(response.embeddings)
            if len(got) != len(batch):
                raise EmbeddingError(
                    f"ollama returned {len(got)} vectors for {len(batch)} "
                    f"texts (model {self.model!r})")
            for ix, vec in enumerate(got):
                if len(vec) != self.dim:
                    raise EmbeddingError(
                        f"vector {start + ix} has dim {len(vec)}, schema "
                        f"expects vector({self.dim}) (model {self.model!r})")
            vectors.extend([list(map(float, vec)) for vec in got])
        return vectors

    # ---------------------------------------------------------- internals --
    def _resolve_version(self) -> str:
        try:
            for entry in self._client.list().models:
                name = getattr(entry, "model", "") or ""
                if name == self.model or name.startswith(f"{self.model}:"):
                    digest = getattr(entry, "digest", "") or ""
                    if digest:
                        return digest[:12]
        except Exception as e:
            logger.warning("could not resolve %r model digest: %s",
                           self.model, e)
        return "unknown"
