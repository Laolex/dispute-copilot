"""Evidence documents: real files in a local folder, referenced by bare file name from the order store.

Only bare names with an allowed extension and a size cap are accepted, so an order record can never make
the app read an arbitrary path (no ../ or absolute paths).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

ALLOWED = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
           ".jpeg": "image/jpeg", ".gif": "image/gif"}
MAX_BYTES = 4 * 1024 * 1024  # conservative; PayPal's real limits are unverified


class DocError(ValueError):
    pass


def load_document(docs_dir: Path, name: str) -> tuple[str, bytes, str]:
    """Return (filename, bytes, mime) or raise DocError with a human-readable reason."""
    if Path(name).name != name or name.startswith("."):
        raise DocError(f"'{name}' is not a plain file name")
    mime = ALLOWED.get(Path(name).suffix.lower())
    if not mime:
        raise DocError(f"'{name}': only {', '.join(sorted(ALLOWED))} files are accepted")
    f = Path(docs_dir) / name
    if not f.is_file():
        raise DocError(f"'{name}' not found in {docs_dir}")
    data = f.read_bytes()
    if len(data) > MAX_BYTES:
        raise DocError(f"'{name}' is larger than {MAX_BYTES // 1024 // 1024} MB")
    return name, data, mime


def missing_documents(docs_dir: Path, names) -> list[str]:
    out = []
    for n in names:
        try:
            load_document(docs_dir, n)
        except DocError as e:
            out.append(str(e))
    return out


def evidence_fingerprint(ev, docs_dir: Path | None) -> str:
    """Hash of the merchant evidence a proposal was built on: the order record AND the bytes of every document.
    Recomputed by the executor, so an edited record or a replaced file invalidates an earlier approval."""
    parts = {"record": asdict(ev), "files": {}}
    for name in ev.documents:
        try:
            parts["files"][name] = hashlib.sha256(load_document(Path(docs_dir), name)[1]).hexdigest() if docs_dir else "unchecked"
        except DocError as e:
            parts["files"][name] = f"unreadable: {e}"
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
