"""The ONE place an ollama.Client is constructed.

Every Ollama consumer used to write the same line for itself —
`client or ollama.Client(host=host or settings.ollama_host)` — in seven
modules. That duplication was not just repetition: it meant the library's
default HTTP budget was inherited seven times over, and the library's default
is `httpx.Timeout(None)`. Unbounded on connect AND on read.

Found the hard way on 2026-08-03. A full test run wedged for 45 minutes with
two ESTABLISHED connections to 127.0.0.1:11434, zero bytes moving and zero CPU
on either side — a read that would never return and never give up. `khctl` had
bounded every `psycopg.connect` after the same class of hang (commit fe30871,
`connect_timeout=10` everywhere) and every `urllib.request.urlopen` already
passes a timeout; the inference seam was the one that got missed.

Two budgets, because the failure modes are unrelated (see config.py for the
values and the reasoning). A single number would fix the hang but would also
make a dead connect wait out the whole generation budget.

Consumers keep their `client` injection parameter — tests pass fakes through
it, and that seam is untouched. What they no longer do is build a real one
themselves.
"""
from __future__ import annotations

from typing import Optional

import httpx
import ollama

from knowledge_hub.config import settings


def ollama_timeout() -> httpx.Timeout:
    """The HTTP budget every Ollama call runs under. Read from settings on each
    call, not captured at import: `reload_settings()` refreshes the singleton
    in place when the launcher chdirs into a deployment home, and a value
    frozen at import time would ignore that deployment's tuning."""
    return httpx.Timeout(
        connect=settings.ollama_connect_timeout_s,
        read=settings.ollama_read_timeout_s,
        write=settings.ollama_connect_timeout_s,
        pool=settings.ollama_connect_timeout_s)


def make_ollama_client(host: Optional[str] = None) -> ollama.Client:
    """An Ollama client with a BOUNDED HTTP budget.

    `host` follows the codebase's constructor convention: None = the pilot
    default from settings. Ollama runs natively on the GPU host while the rest
    of the stack is dockerized, so from inside a container or WSL that host is
    NOT localhost — which is exactly why the parameter exists.
    """
    return ollama.Client(host=host or settings.ollama_host,
                         timeout=ollama_timeout())
