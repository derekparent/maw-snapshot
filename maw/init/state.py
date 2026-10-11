"""`.maw/state.json` reader/writer for /maw:init — contract §4 (Option A).

State file records per-installed-file SHA-256 + template-of-origin so that
re-init can detect drift (target file SHA != installed SHA) and decide
between preserve-as-sibling vs overwrite vs skip-current per the §3
behavior matrix.

Contract §7 invariant: state.json is the LAST write in a successful run.
Its presence indicates earlier writes completed; its absence after a
.maw/config.yaml means a prior init was interrupted. Re-run handles both
cases via write-if-different at the per-file layer.

Stdlib-only; JSON instead of YAML keeps drift-detection inspection cheap.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Bump when the state.json schema changes incompatibly. Readers may
# upgrade older versions or refuse + suggest --force.
SCHEMA_VERSION = 1


class StateError(Exception):
    """state.json malformed / schema-version unknown."""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text(encoding="utf-8"))


@dataclass
class FileEntry:
    template: str           # template-relative path (e.g., "hooks/budget-guard.py")
    installed_sha256: str
    installed_at: str


@dataclass
class StateFile:
    schema_version: int = SCHEMA_VERSION
    installed_at: str = ""
    substrate_version: str = ""
    # The plugin COMMIT the substrate was transplanted from. substrate_version is
    # the manifest label, which has not moved since before the doctrine it is
    # supposed to date — every install stamps the same string, so no comparison
    # against it can ever be false (#201). These two move with the plugin.
    substrate_sha: str = ""
    substrate_committed_at: str = ""
    profile: str = ""
    files: dict[str, FileEntry] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "installed_at": self.installed_at,
            "substrate_version": self.substrate_version,
            "substrate_sha": self.substrate_sha,
            "substrate_committed_at": self.substrate_committed_at,
            "profile": self.profile,
            "files": {p: asdict(e) for p, e in sorted(self.files.items())},
        }
        return json.dumps(payload, indent=2, sort_keys=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "StateFile":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateError(f"state.json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError("state.json must be a JSON object")
        sv = data.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise StateError(
                f"state.json schema_version={sv!r} not supported "
                f"(this binary speaks {SCHEMA_VERSION})"
            )
        files_raw = data.get("files") or {}
        if not isinstance(files_raw, dict):
            raise StateError("state.json `files` must be an object")
        files: dict[str, FileEntry] = {}
        for path, entry in files_raw.items():
            if not isinstance(entry, dict):
                raise StateError(f"state.json files[{path!r}] is not an object")
            try:
                files[path] = FileEntry(
                    template=str(entry["template"]),
                    installed_sha256=str(entry["installed_sha256"]),
                    installed_at=str(entry["installed_at"]),
                )
            except KeyError as exc:
                raise StateError(
                    f"state.json files[{path!r}] missing field {exc!s}"
                ) from exc
        return cls(
            schema_version=SCHEMA_VERSION,
            installed_at=str(data.get("installed_at", "")),
            substrate_version=str(data.get("substrate_version", "")),
            # Absent in every state.json written before #201 — an unstamped
            # substrate is exactly what drift_warning must flag, so "" is data,
            # not an error.
            substrate_sha=str(data.get("substrate_sha", "")),
            substrate_committed_at=str(data.get("substrate_committed_at", "")),
            profile=str(data.get("profile", "")),
            files=files,
        )


def state_path(repo_root: Path) -> Path:
    return repo_root / ".maw" / "state.json"


def read_state(repo_root: Path) -> StateFile | None:
    path = state_path(repo_root)
    if not path.is_file():
        return None
    return StateFile.from_json(path.read_text(encoding="utf-8"))


def write_state(repo_root: Path, state: StateFile) -> None:
    """Atomic write of state.json, refusing a symlink at or below the repo."""
    write_repo_bytes(repo_root, state_path(repo_root), state.to_json().encode("utf-8"))


def write_atomic_text(path: Path, content: str) -> None:
    """write tmpfile → fsync → rename. Contract §7 per-file atomicity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


class SymlinkRefusal(Exception):
    """A repo-local write would go through a symlink; nothing was written."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} (or a directory above it) is a symlink")
        self.path = path


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _is_link_at(name: str, dir_fd: int) -> bool:
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
    except FileNotFoundError:
        return False


def _open_dirs_nofollow(repo_root: Path, path: Path, parts: tuple[str, ...]) -> int:
    """Open (creating as needed) each directory below repo_root without
    following a symlink. The root itself is followed: only what lies below it
    is the repo's, and on macOS a temp root is itself reached via /var."""
    fd = os.open(repo_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o777, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR) and _is_link_at(part, fd):
                        raise SymlinkRefusal(path) from exc
                    raise
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR) and _is_link_at(part, fd):
                    raise SymlinkRefusal(path) from exc
                raise
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_repo_bytes(
    repo_root: Path,
    path: Path,
    data: bytes,
    *,
    mode: int = 0o600,
    times_ns: tuple[int, int] | None = None,
) -> None:
    """Atomically write a repo-local file, never through a symlink.

    The symlink check and the write are one step: every directory below the
    root is opened O_NOFOLLOW and the file is placed relative to that fd, so a
    link swapped in after any earlier check cannot redirect the write. A new
    file is placed with link(2), which fails on any existing name (a link
    included) instead of replacing it; an existing file is checked with lstat
    on the same fd right before rename(2). Raises SymlinkRefusal.
    """
    root = repo_root.absolute()
    rel = path.absolute().relative_to(root)
    if not rel.parts:
        raise ValueError(f"{path} is the repo root, not a file in it")
    *dirs, name = rel.parts
    dfd = _open_dirs_nofollow(root, path, tuple(dirs))
    tmp = f".{name}.tmp.{secrets.token_hex(6)}"
    try:
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=dfd)
        with os.fdopen(tfd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(), mode)
            if times_ns is not None:
                os.utime(f.fileno(), ns=times_ns)
            os.fsync(f.fileno())
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
            return
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this filesystem: fall through to the rename.
            pass
        if _is_link_at(name, dfd):
            raise SymlinkRefusal(path)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    finally:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        os.close(dfd)


def write_repo_text(repo_root: Path, path: Path, content: str, *, mode: int = 0o600) -> None:
    write_repo_bytes(repo_root, path, content.encode("utf-8"), mode=mode)


def read_repo_bytes_nofollow(path: Path) -> tuple[bytes, os.stat_result]:
    """Read a file, refusing (SymlinkRefusal) if the file itself is a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SymlinkRefusal(path) from exc
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read(), os.fstat(f.fileno())


__all__ = [
    "SCHEMA_VERSION",
    "StateError",
    "StateFile",
    "FileEntry",
    "sha256_text",
    "sha256_file",
    "state_path",
    "read_state",
    "write_state",
    "write_atomic_text",
    "write_repo_bytes",
    "write_repo_text",
    "read_repo_bytes_nofollow",
    "SymlinkRefusal",
    "utcnow_iso",
]
