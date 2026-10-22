"""Filesystem boundaries and append-only tool audit records."""

import json
from pathlib import Path
from datetime import datetime, timezone

PRIVATE_NAMES = {".git", ".env", ".aws", ".ssh", ".gnupg", ".nano-code", ".nanocode", "id_rsa", "id_ed25519"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class SecurityError(ValueError):
    pass


def safe_path(workspace: Path, name: str, *, for_write: bool = False) -> Path:
    root = workspace.resolve(strict=True)
    candidate = Path(name)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise SecurityError("Path leaves the workspace")
    relative_parts = resolved.relative_to(root).parts
    if any(part.lower() in PRIVATE_NAMES or part.lower().startswith(".env.") for part in relative_parts):
        raise SecurityError("Private path is blocked")
    if resolved.suffix.lower() in PRIVATE_SUFFIXES:
        raise SecurityError("Private key file is blocked")
    if for_write and resolved == root:
        raise SecurityError("Cannot write the workspace root")
    return resolved


def audit(data_dir: Path, record: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    entry = {"time": datetime.now(timezone.utc).isoformat(), **record}
    with (data_dir / "audit.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
