"""Restricted, stdlib-only delivery of the checked-in frontend build."""
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Optional
from urllib.parse import unquote


@dataclass(frozen=True)
class StaticResponse:
    status: int
    body: bytes
    content_type: str


_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
_NOT_FOUND = StaticResponse(404, b"Asset not found. Reopen the UI after updating.", "text/plain; charset=utf-8")
_NO_ENTRY = StaticResponse(
    503,
    b"Frontend build unavailable. Update the tool checkout, then restart and reopen the UI. "
    b"Contributors: follow ui/README.md and run just --justfile ui/justfile build.",
    "text/plain; charset=utf-8",
)


def static_response(build_dir: Path, raw_path: str) -> Optional[StaticResponse]:
    """Serve only the entry and canonical bundled assets; never fall back to source.

    Vite output names need no escaping. Reject encoded aliases (including encoded
    separators and repeated encoding), dot segments and symlinks before reading.
    The build is trusted code shipped with the tool, never a writable workspace.
    """
    path = raw_path.split("?", 1)[0]
    if path != "/" and not path.startswith("/assets/"):
        return None
    entry = path == "/"
    failure = _NO_ENTRY if entry else _NOT_FOUND
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        return _NOT_FOUND
    if decoded != path or "%" in path:
        return _NOT_FOUND
    relative = "index.html" if entry else path[1:]
    parts = relative.split("/")
    if any(not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part) for part in parts):
        return _NOT_FOUND
    content_type = "text/html; charset=utf-8" if entry else _TYPES.get(Path(relative).suffix)
    if not content_type:
        return _NOT_FOUND
    try:
        if build_dir.is_symlink():
            return failure
        root = build_dir.resolve(strict=True)
        target = root
        for part in parts:
            target = target / part
            if target.is_symlink():
                return failure
        resolved = target.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            return failure
        return StaticResponse(200, resolved.read_bytes(), content_type)
    except (OSError, ValueError, RuntimeError):
        return failure
