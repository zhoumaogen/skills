"""Cross-platform core logic for mirroring a Skill across AI coding tools.

Only depends on the Python standard library. Imported by the CLI wrapper
(sync_skills.py) and exercised directly by unit tests.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import uuid
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Mapping, Optional, Sequence

sys.dont_write_bytecode = True


class ExitCode(IntEnum):
    SUCCESS = 0
    PARTIAL_FAILURE = 1
    INVALID_INPUT = 2
    USER_INPUT_REQUIRED = 3


@dataclass(frozen=True)
class ManifestEntry:
    kind: str          # "file" or "directory"
    size: int
    sha256: str


@dataclass(frozen=True)
class Operation:
    action: str        # "create", "replace", or "delete"
    relative_path: str
    kind: str


@dataclass(frozen=True)
class TargetPlan:
    skills_root: Path
    destination: Path
    operations: tuple  # tuple[Operation, ...]


@dataclass(frozen=True)
class TargetResult:
    destination: Path
    status: str        # "success", "skipped", or "failed"
    message: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _is_link_like(path: Path) -> bool:
    """True for symlinks, junctions and other reparse points."""
    try:
        st = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(st, "st_file_attributes", 0)
    return path.is_symlink() or bool(reparse_flag and attributes & reparse_flag)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_replace(src: Path, dst: Path) -> None:
    """Replace *dst* with *src*, atomically when the platform allows."""
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    # Fallback: some platforms cannot atomically swap directories.
    if dst.exists() or dst.is_symlink():
        _remove_tree(dst)
    os.replace(src, dst)


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


# --------------------------------------------------------------------------- #
# Source validation & directory discovery
# --------------------------------------------------------------------------- #

def validate_source(source: Path) -> tuple:
    """Return (resolved_path, skill_name) or raise ValueError."""
    resolved = source.expanduser().resolve(strict=True)
    skill_md = resolved / "SKILL.md"
    if not resolved.is_dir() or not skill_md.is_file() or _is_link_like(skill_md):
        raise ValueError("source must contain a regular SKILL.md")
    if resolved.name in {"", ".", ".."}:
        raise ValueError("invalid source skill name")
    return resolved, resolved.name


def _skills_root_from_explicit(path: Path) -> Path:
    raw = path.expanduser()
    candidate = raw if raw.name.casefold() == "skills" else raw / "skills"
    if not candidate.exists() or _is_link_like(candidate):
        raise ValueError(f"unsafe or missing skills directory: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or _is_link_like(resolved):
        raise ValueError(f"unsafe or missing skills directory: {resolved}")
    return resolved


def discover_skills_roots(
    source: Path,
    user_home: Optional[Path],
    explicit_targets: Sequence[Path],
    env: Mapping[str, str],
) -> tuple:
    """Find candidate ``skills`` roots under the user home and explicit targets.

    Only checks the home itself (``<home>/skills``) and direct one-level
    children (``<home>/<tool>/skills``). Never recurses deeper.
    """
    source, _ = validate_source(source)

    homes: list = []
    raw_homes: list = [user_home, env.get("HOME"), env.get("USERPROFILE")]
    try:
        raw_homes.append(Path.home())
    except (OSError, RuntimeError):
        pass
    # Infer home from source layout <home>/<tool>/skills/<skill-name>.
    if source.parent.name.casefold() == "skills" and len(source.parents) >= 3:
        raw_homes.append(source.parent.parent.parent)

    for raw_home in raw_homes:
        if raw_home is None:
            continue
        try:
            home = Path(raw_home).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if home.is_dir() and not _is_link_like(home):
            homes.append(home)

    roots: list = []
    for home in homes:
        direct = home / "skills"
        if direct.is_dir() and not _is_link_like(direct):
            roots.append(direct.resolve())
        try:
            children = sorted(home.iterdir(), key=lambda item: item.name.casefold())
        except PermissionError as error:
            raise ValueError(
                f"permission denied while scanning user home: {home}; "
                "run the scan with permission to list that directory"
            ) from error
        for child in children:
            if not child.is_dir() or _is_link_like(child):
                continue
            candidate = child / "skills"
            if candidate.is_dir() and not _is_link_like(candidate):
                roots.append(candidate.resolve())

    roots.extend(_skills_root_from_explicit(Path(item)) for item in explicit_targets)

    unique: dict = {}
    for root in roots:
        unique.setdefault(_path_key(root), root)
    return tuple(unique[key] for key in sorted(unique))


# --------------------------------------------------------------------------- #
# Manifest, diff planning & fingerprint
# --------------------------------------------------------------------------- #

def build_manifest(root: Path) -> dict:
    """Map every regular entry under *root* to a deterministic ManifestEntry."""
    if not root.exists():
        return {}
    if not root.is_dir() or _is_link_like(root):
        raise ValueError(f"unsafe manifest root: {root}")
    manifest: dict = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if _is_link_like(path):
            raise ValueError(f"links and reparse points are unsupported: {path}")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            manifest[relative] = ManifestEntry("directory", 0, "")
        elif path.is_file():
            manifest[relative] = ManifestEntry("file", path.stat().st_size, _file_digest(path))
        else:
            raise ValueError(f"special files are unsupported: {path}")
    return manifest


def plan_target(source: Path, skills_root: Path) -> TargetPlan:
    """Compute create/replace/delete operations to mirror *source* into *skills_root*."""
    source, skill_name = validate_source(source)
    skills_root = _skills_root_from_explicit(skills_root)
    destination = skills_root / skill_name
    source_manifest = build_manifest(source)
    target_manifest = build_manifest(destination)
    operations = []
    for relative in sorted(set(source_manifest) | set(target_manifest)):
        src = source_manifest.get(relative)
        tgt = target_manifest.get(relative)
        if tgt is None:
            operations.append(Operation("create", relative, src.kind))
        elif src is None:
            operations.append(Operation("delete", relative, tgt.kind))
        elif src != tgt:
            operations.append(Operation("replace", relative, src.kind))
    return TargetPlan(skills_root, destination, tuple(operations))


def plan_fingerprint(source_manifest: Mapping[str, ManifestEntry], plans: Sequence[TargetPlan]) -> str:
    """Stable SHA-256 over the source manifest and every target plan.

    Changing the source files, adding or removing a target, or changing any
    operation set produces a different fingerprint.
    """
    digest = hashlib.sha256()
    for relative in sorted(source_manifest):
        entry = source_manifest[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(entry.kind.encode("utf-8"))
        digest.update(str(entry.size).encode("ascii"))
        digest.update(b"\x00")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    for plan in sorted(plans, key=lambda p: _path_key(p.destination)):
        digest.update(_path_key(plan.destination).encode("utf-8"))
        digest.update(b"\n")
        for op in plan.operations:
            digest.update(op.action.encode("utf-8"))
            digest.update(b":")
            digest.update(op.relative_path.encode("utf-8"))
            digest.update(b":")
            digest.update(op.kind.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Approval selection & transactional execution
# --------------------------------------------------------------------------- #

def select_approved_plans(
    plans: Sequence[TargetPlan],
    approved_targets: Sequence[Path],
) -> tuple:
    """Keep only plans whose destination matches an approved target path."""
    approved_keys = {_path_key(Path(t)) for t in approved_targets}
    return tuple(p for p in plans if _path_key(p.destination) in approved_keys)


def execute_plan(source: Path, plan: TargetPlan) -> TargetResult:
    """Mirror *source* into the plan destination via staging, backup and restore.

    Deletions are strictly confined to the target Skill directory. On swap
    failure the previous target is restored from backup.
    """
    source, skill_name = validate_source(source)
    if _path_key(source) == _path_key(plan.destination):
        return TargetResult(plan.destination, "skipped", "source directory")
    if not plan.operations:
        return TargetResult(plan.destination, "skipped", "already mirrored")

    token = uuid.uuid4().hex
    staging = plan.skills_root / f".{skill_name}.sync-staging-{token}"
    backup = plan.skills_root / f".{skill_name}.sync-backup-{token}"
    moved_existing = False
    try:
        shutil.copytree(source, staging, copy_function=shutil.copy2)
        if build_manifest(staging) != build_manifest(source):
            raise OSError("staging manifest differs from source")
        if plan.destination.exists():
            _atomic_replace(plan.destination, backup)
            moved_existing = True
        _atomic_replace(staging, plan.destination)
        if backup.exists():
            _remove_tree(backup)
        return TargetResult(plan.destination, "success", "mirrored")
    except Exception as error:
        if moved_existing and backup.exists() and not plan.destination.exists():
            try:
                _atomic_replace(backup, plan.destination)
            except Exception as restore_error:
                return TargetResult(
                    plan.destination, "failed",
                    f"{error}; restore failed: {restore_error}; backup: {backup}",
                )
        if staging.exists():
            try:
                _remove_tree(staging)
            except Exception as cleanup_error:
                return TargetResult(
                    plan.destination, "failed",
                    f"{error}; staging cleanup failed: {cleanup_error}; staging: {staging}",
                )
        return TargetResult(plan.destination, "failed", str(error))
