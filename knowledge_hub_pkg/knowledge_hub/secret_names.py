"""The ONE definition of a secret-shaped filename, plus the tracked files that
deliberately wear the shape and carry no values.

Two readers, one definition:

  * `deploy_kit.assert_no_secrets` — the kit gate. Scans a FINISHED kit and
    fails the build if any file matches. Ignores COMMIT_ALLOWLIST entirely: a
    kit is an allowlist bundle, none of those paths is ever staged into one,
    and a guard on what leaves the bench should not learn repo exceptions.
  * `hooks/check_staged_secrets.py` — the pre-commit gate. Scans STAGED files
    and aborts the commit on a match. This one needs COMMIT_ALLOWLIST, because
    it sees the whole repo rather than a curated bundle.

This module exists so those two can never drift. It was extracted from
deploy_kit for a second reason: the hook has to run from a FRESH CLONE, before
anyone has made a venv or pip-installed the package. Importing deploy_kit costs
638 modules and an editable install; this file imports `re` and nothing else,
so the hook can load it straight off disk by path. Keep it that way — a single
non-stdlib import here silently disarms the commit guard on new clones.

Matching is by BARE FILENAME, not path (deploy_kit passes `p.name`), which is
why COMMIT_ALLOWLIST below has to be path-based to stay precise.
"""

from __future__ import annotations

import re

# Anything matching this anywhere in a finished kit fails the build — the
# second net behind the allowlist. Kits carry NO secrets, NO engagement
# artifacts, NO usage logs. Anything matching it in a STAGED file aborts the
# commit, unless its path is in COMMIT_ALLOWLIST below.
#
# `.secrets.local*` (d.s Stage 2): the local-posture credential file. It lives
# in the infra dir beside .env, which is exactly where the bundle stage reads
# from, so it is one glob away from riding to another machine — and it holds
# BOTH source credentials and the console principal registry. That would be the
# single worst thing this build could cause, so it is caught here as well as by
# the make-kit posture gate and .gitignore. Three nets, because the ones that
# matter get more than one: the posture gate stops the ordinary mistake, this
# stops it on a deliberately hardened build too.
#
# NOTE this guard is posture-BLIND on purpose. Everything else about kit
# ceremony became conditional in Stage 2; this did not. It is a safety check on
# what leaves the bench, not product ceremony, and a kit built in either posture
# must never carry a credential.
FORBIDDEN_NAMES = re.compile(
    r"^(\.env.*|\.secrets\.local.*|deploy_plan\.json|probe_report\.json|"
    r"\.apply_progress\.json|s3config\.json|.*usage.*\.jsonl|"
    r".*\.(bak|key|pem))$")

# Tracked-on-purpose files whose NAMES match FORBIDDEN_NAMES but whose CONTENTS
# carry no live credential. Exact repo-relative POSIX paths, never patterns:
# the guard's whole value is that adding an exception is a deliberate, reviewed
# line in a diff, not a glob that quietly widens later.
#
# There is deliberately NO "already tracked in HEAD" escape hatch. That would be
# less maintenance and strictly worse — if a real secret ever did reach history,
# an is-it-tracked test would bless it forever, and every later commit touching
# it would pass in silence. A path not listed here fails, tracked or not.
COMMIT_ALLOWLIST = frozenset({
    # The two placeholder companions. .gitignore already makes exactly this
    # claim about them ("carries no values", "no real values") — this is the
    # same claim, enforced at commit time.
    ".env.example",
    ".secrets.local.example.json",
    # The local-posture SeaweedFS identity. Its key pair is named
    # local_dev_only_* precisely so this entry needs no defending: the values
    # are published in .env.example and config.py as well, the gateway only
    # listens on 127.0.0.1, and a real box never runs this file at all
    # (deploy_apply.render_s3config mints a pair and writes it on site; the
    # kit refuses to carry one). If this file ever holds a value that is NOT
    # a local_dev_only_* placeholder, delete this line rather than edit it.
    "seaweedfs/s3config.json",
})

# OUT OF SCOPE here, on purpose: content-based secret scanning. This guard is
# NAME-based, which is the contract the kit gate already enforces — a scanner
# that reads file bodies for key-shaped strings is a separate, larger build with
# a different false-positive profile. Also out of scope: making `--no-verify`
# impossible (it is git's intended escape hatch; the goal is to stop the
# accident, not the deliberate act), and any CI-side scan, which is a
# GitHub-side control rather than a hook.


def offending_paths(paths):
    """The subset of `paths` this guard refuses, in input order.

    `paths` are repo-relative POSIX strings. A path is refused when its bare
    filename matches FORBIDDEN_NAMES and the path itself is not allowlisted.
    """
    return [p for p in paths
            if p not in COMMIT_ALLOWLIST
            and FORBIDDEN_NAMES.match(p.rsplit("/", 1)[-1])]
