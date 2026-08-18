"""Refuse to commit a staged file whose NAME is secret-shaped.

decant-source is a PUBLIC repo. `git add -f` past .gitignore, or a `git add -A`
in a directory .gitignore does not cover, puts a credential into world-readable
history — and history is fixed by rotating the credential, not by reverting the
commit. .gitignore stops the ordinary case; this stops the forced one.

ONE DEFINITION. The forbidden set and its allowlist come from
knowledge_hub/secret_names.py, the same module the kit gate
(deploy_kit.assert_no_secrets) reads. This file deliberately loads it BY PATH
rather than by import, so the hook works in a fresh clone that has no venv and
no `pip install -e` yet — the machine most likely to make this mistake.

FAIL-CLOSED. A match aborts the commit. So does any failure to run the check
at all (module missing, git unreadable): a secret guard that cannot answer must
not answer "fine".

NOT a content scanner — see the out-of-scope note in secret_names.py.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# ANSI only when the stream is a terminal; git hook output is piped in some GUIs.
_TTY = sys.stderr.isatty()
_RED = "\033[31m" if _TTY else ""
_BOLD = "\033[1m" if _TTY else ""
_OFF = "\033[0m" if _TTY else ""


def _die(message: str) -> "NoReturn":  # noqa: F821
    """Report and abort. Never raises on the way out.

    Message TEXT here is kept ASCII on purpose: a Windows console is cp1252
    and turns an em dash into a replacement char, which makes a security
    message look corrupted at the worst possible moment. Staged FILENAMES are
    not under our control (they arrive surrogate-escaped), so the write is
    defensive too: a UnicodeEncodeError here would abort the commit with a
    traceback instead of the reason.
    """
    text = (f"\n{_RED}{_BOLD}pre-commit: commit refused{_OFF}"
            f"\n{message}\n")
    encoding = sys.stderr.encoding or "utf-8"
    data = text.encode(encoding, "backslashreplace")
    stream = getattr(sys.stderr, "buffer", None)
    if stream is None:
        sys.stderr.write(data.decode(encoding, "replace"))
    else:
        stream.write(data)
        stream.flush()
    raise SystemExit(1)


def _git(*args: str) -> bytes:
    try:
        done = subprocess.run(("git",) + args, capture_output=True, check=False)
    except OSError as exc:
        _die(f"  could not run git ({exc}). The secret guard cannot verify this\n"
             f"  commit, so it is refusing it rather than guessing.")
    if done.returncode != 0:
        _die(f"  `git {' '.join(args)}` failed:\n"
             f"  {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def _load_guard(repo_root: Path):
    """Load secret_names.py off disk — no package install, no venv required."""
    module_path = repo_root / "knowledge_hub_pkg" / "knowledge_hub" / "secret_names.py"
    if not module_path.is_file():
        _die(f"  the shared definition is missing:\n"
             f"    {module_path}\n"
             f"  Without it there is one definition of a secret and it is gone, so\n"
             f"  this commit is refused. Restore the file, or pass --no-verify if\n"
             f"  you are deliberately committing its deletion.")
    import importlib.util
    spec = importlib.util.spec_from_file_location("_ds_secret_names", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    repo_root = Path(_git("rev-parse", "--show-toplevel").decode("utf-8").strip())
    guard = _load_guard(repo_root)

    # -z because a filename with a space or a non-ASCII byte is otherwise
    # returned quoted, and a quoted name would silently miss the match.
    # ACMR: added, copied, modified, renamed — the paths this commit will
    # actually write. Deletions (D) are how a secret LEAVES, never how it lands.
    raw = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    staged = [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]

    offenders = guard.offending_paths(staged)
    if not offenders:
        return 0

    listed = "\n".join(f"    {p}" for p in offenders)
    _die(
        f"  These staged file(s) have secret-shaped names:\n\n"
        f"{listed}\n\n"
        f"  This repo is PUBLIC. A credential committed here is world-readable\n"
        f"  history. Fixing it means ROTATING the credential, not reverting the\n"
        f"  commit. That is why this stops you before the commit, not after.\n\n"
        f"  To proceed:\n"
        f"    unstage it   git restore --staged <file>\n"
        f"    or, if the file genuinely carries no values and should be tracked,\n"
        f"    add its exact path to COMMIT_ALLOWLIST in\n"
        f"      knowledge_hub_pkg/knowledge_hub/secret_names.py\n"
        f"    or, if you truly mean it   git commit --no-verify\n"
    )


if __name__ == "__main__":
    sys.exit(main())
