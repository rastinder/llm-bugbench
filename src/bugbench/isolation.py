"""Enforcing test isolation inside the grading process.

The threat is specific: the grader must execute model-authored code to know whether a fix
works, and that code runs in the same process as the hidden tests. So the model does not
need to break out of anything -- it only has to call ``open()``.

Two independent mechanisms close that, and they are layered because each has a hole the
other does not:

  * ``sys.addaudithook`` denies the test path precisely. Bypassable in principle: ``ctypes``
    can issue a raw ``open(2)`` without emitting an audit event, and a subprocess runs a
    fresh interpreter with no hook installed.
  * A guest uid makes the test tree unreadable at the kernel level, so those bypasses get
    EACCES anyway. This is the actual boundary; the hook is for fast, legible errors.

Installed before the tests run, never after: an audit hook cannot be removed once added,
which is what makes it safe to rely on.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class IsolationReport:
    """What the isolation layer actually achieved, for the run record."""

    protected_paths: list[str]
    audit_hook_installed: bool = False
    guest_uid: int | None = None
    readonly_roots: list[str] = None  # type: ignore[assignment]

    def as_dict(self) -> dict:
        return {
            "protected_paths": self.protected_paths,
            "audit_hook_installed": self.audit_hook_installed,
            "guest_uid": self.guest_uid,
            "readonly_roots": self.readonly_roots or [],
        }


def install_read_denial(protected: list[Path]) -> IsolationReport:
    """Deny reads of ``protected`` for the rest of this interpreter's life.

    Paths are resolved and compared by *containment*, not equality. Protecting a directory
    has to protect everything beneath it -- an exact-match check on the directory path
    alone leaves every file inside readable, which is the whole set of secrets. Both sides
    are resolved first, because an audit hook comparing raw strings is trivially defeated
    by ``./`` or a symlink.
    """
    roots: list[str] = []
    for p in protected:
        try:
            roots.append(str(Path(p).resolve()))
        except OSError:
            roots.append(str(p))
    # Longest first, so the deepest matching root wins and a file inside two protected
    # trees is attributed to the more specific one.
    roots.sort(key=len, reverse=True)

    def _hook(event: str, args) -> None:
        if event != "open":
            return
        target = args[0]
        if isinstance(target, int):
            return                       # a bare fd: nothing to resolve, nothing to leak
        try:
            path = str(Path(os.fsdecode(target)).resolve())
        except (OSError, ValueError):
            return
        for root in roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                raise PermissionError(
                    f"access denied: {path} is part of the grading material"
                )

    sys.addaudithook(_hook)
    return IsolationReport(
        protected_paths=list(roots),
        audit_hook_installed=True,
        guest_uid=os.getuid(),
    )


def drop_privileges(uid: int = 10002, gid: int = 10002) -> bool:
    """Permanently become an unprivileged user.

    Returns False when it could not be done, which the caller must treat as a hard failure
    rather than a warning: running model code as root would make the audit hook the only
    thing standing between the model and the test tree, which is precisely the hole the
    uid exists to close.
    """
    if os.getuid() != 0:
        return os.getuid() == uid
    try:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    except OSError:
        return False
    return os.getuid() == uid and os.geteuid() == uid


def seal_test_tree(test_root: Path, owner_uid: int = 0) -> dict:
    """Make a test tree unreadable to anyone but its owner.

    Applied to the directory as well as the files: a readable parent would let the model
    list and read the contents regardless of the files' own modes.
    """
    test_root = Path(test_root)
    if not test_root.exists():
        return {"applied": False, "reason": "no test tree present"}

    changed = 0
    for path in [test_root, *test_root.rglob("*")]:
        try:
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(path, owner_uid, owner_uid)
            changed += 1
        except OSError:
            continue
    return {"applied": True, "paths_sealed": changed, "root": str(test_root)}


def assert_unreadable(path: Path) -> None:
    """Prove the isolation works by trying to read the thing it protects.

    A security control that is never tested is an assumption. This raises if the path IS
    readable, so the gate fails loudly rather than scoring a run whose isolation silently
    did not apply.
    """
    try:
        Path(path).read_bytes()
    except (PermissionError, FileNotFoundError):
        return
    raise AssertionError(
        f"ISOLATION FAILED: {path} is readable by uid {os.getuid()}"
    )
