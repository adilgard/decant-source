# Knowledge Hub — Postgres 16 with pgvector + Apache AGE + pg_trgm
# We build from the official postgres:16 image (Debian-based) and compile the two
# extensions that don't ship with it. pg_trgm is already included with Postgres.
#
# Pinned versions (change deliberately, then rebuild):
#   pgvector  v0.8.0
#   Apache AGE release/PG16/1.5.0
FROM postgres:16

# Build tooling needed to compile the extensions from source.
# ca-certificates is critical: without it, `git clone` over HTTPS fails with SSL errors.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      git \
      postgresql-server-dev-16 \
      libreadline-dev \
      zlib1g-dev \
      flex \
      bison \
    && rm -rf /var/lib/apt/lists/*

# --- pgvector: dense vector column type + HNSW index (our embeddings live here) ---
# OPTFLAGS="" disables CPU-specific optimizations so the image runs on any hardware.
RUN git clone --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector \
    && make OPTFLAGS="" && make install \
    && rm -rf /tmp/pgvector

# --- Apache AGE: graph database extension (the graph projection layer) ---
# NOTE: this is a BRANCH (release/PG16/1.5.0), not a tag — a common gotcha.
RUN git clone --branch release/PG16/1.5.0 --depth 1 https://github.com/apache/age.git /tmp/age \
    && cd /tmp/age \
    && make && make install \
    && rm -rf /tmp/age
