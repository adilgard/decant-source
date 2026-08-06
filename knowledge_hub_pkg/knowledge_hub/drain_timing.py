"""Read-only timing probes for the resolution drain.

Measurement instrumentation for attributing drain wall-clock to its actual
sinks (the job-9 finding: ~3 min of decisions inside a ~287-min drain).
OFF unless the KH_DRAIN_TIMING environment variable is set — an unset
variable makes every probe a None-check and nothing else, so a normal run
pays nothing and writes nothing.

    KH_DRAIN_TIMING=1                 log one summary line per sweep
    KH_DRAIN_TIMING=C:\\path\\drain.jsonl   same, plus append one JSON
                                      object per sweep to that file

OBSERVE ONLY. Nothing in this module (or its call sites) may change what
the pipeline does: no query it runs, no row it writes, no decision it
makes. The one deliberate exception is the periodic EXPLAIN ANALYZE of
promote_pending's selection (sweep 1, then every 25th), which re-executes
that read-only SELECT to capture its plan — it doubles the cost of that
one query on those sweeps and is labeled in the output, so the budget
stays reconcilable.

Not thread-safe by design: one drain loop per process is the shipped
shape (JobRunner), and a second concurrent sweep would interleave into
one collector. Fine for measurement, which is all this is for.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_ENV = "KH_DRAIN_TIMING"

# The active sweep's accumulator, or None when disabled / between sweeps.
_active: Optional[dict[str, Any]] = None
_sweep_seq = 0

# EXPLAIN the selection on the first sweep and every Nth after — often
# enough to see the plan drift as facts grows, rare enough not to distort.
EXPLAIN_EVERY = 25


def _configured() -> Optional[str]:
    return os.environ.get(_ENV) or None


def enabled() -> bool:
    return _configured() is not None


def sweep_begin(tenant_id: str) -> None:
    """Open a per-sweep accumulator. No-op unless KH_DRAIN_TIMING is set."""
    global _active, _sweep_seq
    if not enabled():
        _active = None
        return
    _sweep_seq += 1
    _active = {"sweep": _sweep_seq, "tenant": tenant_id,
               "t_begin": time.monotonic()}


def active() -> bool:
    return _active is not None


def t0() -> Optional[float]:
    """Start a lap. Returns None (and lap() no-ops) when disabled, so the
    hot-loop pattern is two cheap calls, not a context manager."""
    return time.monotonic() if _active is not None else None


def lap(key: str, started: Optional[float], n: int = 1) -> None:
    """Accumulate wall-time since t0() and a call count under `key`."""
    if started is None or _active is None:
        return
    _active[f"{key}_s"] = _active.get(f"{key}_s", 0.0) \
        + (time.monotonic() - started)
    _active[f"{key}_n"] = _active.get(f"{key}_n", 0) + n


def count(key: str, n: int = 1) -> None:
    if _active is not None:
        _active[key] = _active.get(key, 0) + n


@contextmanager
def timed(key: str) -> Iterator[None]:
    """Context-manager lap for once-or-few-per-sweep sections. Do not use
    inside per-row loops — use t0()/lap() there."""
    started = t0()
    try:
        yield
    finally:
        lap(key, started)


def should_explain() -> bool:
    """True on the sweeps whose selection plan gets captured."""
    return (_active is not None
            and (_active["sweep"] == 1
                 or _active["sweep"] % EXPLAIN_EVERY == 0))


def explain_captured(label: str, plan_lines: list[str]) -> None:
    if _active is None:
        return
    logger.info("DRAIN_TIMING_EXPLAIN sweep=%d %s\n%s",
                _active["sweep"], label, "\n".join(plan_lines))
    _active.setdefault("explains", []).append(label)


def sweep_end(**final_counts: Any) -> None:
    """Close the accumulator: total the sweep, log it, optionally append
    JSONL. Rounds seconds to ms precision for readability."""
    global _active
    if _active is None:
        return
    rec = _active
    _active = None
    rec.update(final_counts)
    rec["sweep_wall_s"] = time.monotonic() - rec.pop("t_begin")
    out = {k: (round(v, 4) if isinstance(v, float) else v)
           for k, v in rec.items()}
    logger.info("DRAIN_TIMING %s", json.dumps(out, sort_keys=True))
    target = _configured()
    if target and target != "1":
        try:
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(out, sort_keys=True) + "\n")
        except OSError as e:
            # Measurement must never take the drain down with it.
            logger.warning("DRAIN_TIMING file append failed (%s): %s",
                           target, e)
