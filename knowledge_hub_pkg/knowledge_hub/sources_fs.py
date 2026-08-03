"""FilesystemSourceAdapter — the pilot SourceAdapter: a local folder (which is
also how an SFTP/SMB share arrives when mounted, e.g. sshfs). No OAuth, no
credentials — prepare() stays the ABC's no-op. A native SFTP adapter (paramiko)
slots behind the same ABC later: build connect kwargs on an OutboundRequest,
have SecretsProvider.inject_credential fill username/password, connect.

Cursor design: files are scanned in (mtime, path) order and each SourceItem
carries the token

    f"{mtime_ns:020d}:{relative_posix_path}"

Zero-padding makes string order equal scan order, so both resume flavors are
one comparison: backfill(resume_after=t) and incremental(cursor=t) yield items
with token strictly greater than t. A later re-modification bumps a file's
mtime, which re-yields it past any old cursor — changed bytes then land as a
new version (via Pipeline._next_version), identical bytes are a hash no-op.

Metadata is captured generously at acquisition (it's irreplaceable): stat
times/size/mode plus owner/group where the platform exposes them. The POSIX
permission bits + ownership are normalized into a posix.v1 SourceAcl (the
faithful stat payload rides in SourceAcl.raw); everything else goes to
native_metadata. Files that vanish mid-scan are skipped; files we cannot READ
are skipped and counted in `skipped_unreadable` (the source's ACL said no —
one forbidden file must not wedge the whole pull at its cursor forever).
"""
from __future__ import annotations

import fnmatch
import logging
import mimetypes
import os
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from knowledge_hub.interfaces import (
    AclGrant,
    SourceAcl,
    SourceAdapter,
    SourceItem,
)

logger = logging.getLogger(__name__)

# The console folder-ingest starter set (d.s Stage 2): what DoclingParser's
# prose track handles today. Structured containers (.csv/.xlsx/...) keep
# arriving via CLI-registered sources, which pass extensions=None (no
# filter) — existing behavior unchanged.
ELIGIBLE_EXTENSIONS = frozenset({".md", ".txt", ".pdf", ".docx"})


def _glob_match(rel_posix: str, patterns: list[str]) -> bool:
    """Case-insensitive match of a RELATIVE posix path against any pattern.
    fnmatchcase over casefolded inputs, deliberately: fnmatch.fnmatch would
    route through os.path.normcase, whose behavior differs per platform —
    this rule is identical on the dev bench and the deployed box."""
    folded = rel_posix.casefold()
    return any(fnmatch.fnmatchcase(folded, p.casefold()) for p in patterns)


class FilesystemSourceAdapter(SourceAdapter):
    source_system = "filesystem"

    def __init__(self, source_ref: str, root: str | Path,
                 include_hidden: bool = False,
                 recurse: bool = True,
                 include: Optional[list[str]] = None,
                 exclude: Optional[list[str]] = None,
                 extensions: Optional[frozenset[str] | set[str]] = None,
                 extra_metadata: Optional[dict[str, Any]] = None):
        """d.s Stage 2 options, all defaulting to the pilot behavior:
        `recurse` off limits the scan to the root itself; `include`/`exclude`
        are glob patterns over the RELATIVE posix path (exclude wins);
        `extensions` is the eligible-suffix allowlist — a non-matching file
        is SKIPPED AND COUNTED, never fatal (an unparseable format must not
        wedge a folder run); `extra_metadata` is merged into every item's
        native_metadata (the folder-job path stamps its fixed
        ontology_version_override through this)."""
        super().__init__(source_ref)
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"source root is not a directory: {self.root}")
        self.include_hidden = include_hidden
        self.recurse = recurse
        self.include = list(include) if include else None
        self.exclude = list(exclude) if exclude else None
        self.extensions = frozenset(e.lower() for e in extensions) \
            if extensions else None
        self.extra_metadata = dict(extra_metadata) if extra_metadata else None
        # Diagnostics from the most recent scan (permission-denied files).
        self.skipped_unreadable: list[str] = []
        # Stage 2 counters, reset per scan: ineligible suffix (logged,
        # never fatal) and operator-glob exclusions.
        self.skipped_unknown: list[str] = []
        self.excluded_by_glob: int = 0

    # ------------------------------------------------------------ iterators --
    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        return self._scan(after=resume_after)

    def incremental(self, tenant_id: str,
                    cursor: Optional[str]) -> Iterator[SourceItem]:
        return self._scan(after=cursor)

    # ------------------------------------------------------------- internals --
    def _scan(self, after: Optional[str]) -> Iterator[SourceItem]:
        self.skipped_unreadable = []
        self.skipped_unknown = []
        self.excluded_by_glob = 0
        entries: list[tuple[str, Path, os.stat_result]] = []
        walker = self.root.rglob("*") if self.recurse else self.root.glob("*")
        for path in walker:
            try:
                if not self.include_hidden and any(
                        part.startswith(".") for part in
                        path.relative_to(self.root).parts):
                    continue
                st = path.stat()
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
            except OSError:
                continue  # vanished or unstatable mid-scan; next run re-sees it
            native_id = path.relative_to(self.root).as_posix()
            # Operator scope first (their stated intent), eligibility second
            # (our parsing reality, counted so the run summary can say what
            # was left behind — silent truncation reads as "covered it").
            if self.include is not None and \
                    not _glob_match(native_id, self.include):
                self.excluded_by_glob += 1
                continue
            if self.exclude is not None and \
                    _glob_match(native_id, self.exclude):
                self.excluded_by_glob += 1
                continue
            if self.extensions is not None and \
                    path.suffix.lower() not in self.extensions:
                self.skipped_unknown.append(native_id)
                logger.info("source %s: %r has no eligible extension "
                            "(%s), skipping", self.source_ref, native_id,
                            ", ".join(sorted(self.extensions)))
                continue
            entries.append((self._token(st.st_mtime_ns, native_id), path, st))

        for token, path, st in sorted(entries, key=lambda e: e[0]):
            if after is not None and token <= after:
                continue
            try:
                content = path.read_bytes()
            except OSError as e:
                native_id = path.relative_to(self.root).as_posix()
                self.skipped_unreadable.append(native_id)
                logger.warning("source %s: cannot read %r (%s), skipping",
                               self.source_ref, native_id, type(e).__name__)
                continue
            yield self._item(token, path, st, content)

    @staticmethod
    def _token(mtime_ns: int, native_id: str) -> str:
        return f"{mtime_ns:020d}:{native_id}"

    def _item(self, token: str, path: Path, st: os.stat_result,
              content: bytes) -> SourceItem:
        native_id = path.relative_to(self.root).as_posix()
        metadata = {
            "absolute_path": str(path),
            "root": str(self.root),
            "source_ref": self.source_ref,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "ctime": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
            "platform": os.name,
        }
        if self.extra_metadata:
            # Job-fixed stamps (e.g. ontology_version_override) ride the raw
            # row's native_metadata — durable provenance of operator intent.
            metadata.update(self.extra_metadata)
        return SourceItem(
            native_id=native_id,
            content=content,
            mime_type=mimetypes.guess_type(path.name)[0],
            size=st.st_size,
            mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            source_acl=self._capture_acl(path, st),
            native_metadata=metadata,
            cursor=token,
        )

    def stats(self) -> dict[str, Any]:
        """Post-run skip accounting for the run summary (paths relative to
        the root — safe to log, no content)."""
        return {
            "skipped_unknown": len(self.skipped_unknown),
            "skipped_unknown_files": self.skipped_unknown[:50],
            "skipped_unreadable": len(self.skipped_unreadable),
            "excluded_by_glob": self.excluded_by_glob,
        }

    @staticmethod
    def _capture_acl(path: Path, st: os.stat_result) -> SourceAcl:
        """Best-effort filesystem ACL normalized to posix.v1: the mode triads
        become owner/group/anyone grants (read/write bits only — execute
        carries no read/write meaning here and stays in raw's mode). POSIX
        gives mode/uid/gid (+resolved owner/group names); Windows exposes
        only the read-only bit through stat — recorded as such, not padded
        out. The faithful stat payload is preserved in raw."""
        mode = stat_mod.S_IMODE(st.st_mode)
        raw: dict[str, Any] = {
            "mode": oct(mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "platform": os.name,
        }
        owner = group = None
        try:
            owner, group = path.owner(), path.group()
            raw["owner"], raw["group"] = owner, group
        except (KeyError, NotImplementedError, OSError):
            pass  # Windows / unresolvable ids: uid/gid stay as captured

        grants: list[AclGrant] = []
        for ptype, pid, display, r_bit, w_bit in (
            ("user", owner or str(st.st_uid), owner,
             stat_mod.S_IRUSR, stat_mod.S_IWUSR),
            ("group", group or str(st.st_gid), group,
             stat_mod.S_IRGRP, stat_mod.S_IWGRP),
            ("anyone", None, None, stat_mod.S_IROTH, stat_mod.S_IWOTH),
        ):
            roles = [role for bit, role in ((r_bit, "read"), (w_bit, "write"))
                     if mode & bit]
            if roles:
                grants.append(AclGrant(principal_type=ptype, principal_id=pid,
                                       display=display, roles=roles))
        return SourceAcl(model="posix.v1", owner=owner or str(st.st_uid),
                         grants=grants, raw=raw)
