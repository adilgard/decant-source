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


class FilesystemSourceAdapter(SourceAdapter):
    source_system = "filesystem"

    def __init__(self, source_ref: str, root: str | Path,
                 include_hidden: bool = False):
        super().__init__(source_ref)
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"source root is not a directory: {self.root}")
        self.include_hidden = include_hidden
        # Diagnostics from the most recent scan (permission-denied files).
        self.skipped_unreadable: list[str] = []

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
        entries: list[tuple[str, Path, os.stat_result]] = []
        for path in self.root.rglob("*"):
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
        return SourceItem(
            native_id=native_id,
            content=content,
            mime_type=mimetypes.guess_type(path.name)[0],
            size=st.st_size,
            mtime=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            source_acl=self._capture_acl(path, st),
            native_metadata={
                "absolute_path": str(path),
                "root": str(self.root),
                "source_ref": self.source_ref,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "ctime": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
                "platform": os.name,
            },
            cursor=token,
        )

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
