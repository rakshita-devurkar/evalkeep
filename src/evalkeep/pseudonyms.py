"""Deterministic pseudonyms for identifiers that may carry customer data.

Redaction deliberately leaves identifiers alone, because rewriting them would
break the links the pipeline runs on. That is fine when a `trace_id` is a UUID
and dangerous when it is `order-jane@example.com-2026-06-01`.

Pseudonymization resolves the tension instead of trading one problem for the
other. Each identifier becomes a token derived from a per-project secret salt:

* **Deterministic**, so the same original always yields the same token and every
  link in the pipeline survives.
* **Not reversible from the database**, because the original is never stored --
  only the token is.
* **Still usable by hand**, because a lookup can hash whatever the user typed
  and search for that. You keep using the IDs your own systems know.
* **Scoped to one project**, because the salt is per-project and never
  committed. Two projects produce different tokens for the same original, so a
  shared export leaks nothing about another project's data.
"""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from evalkeep.errors import CommandError

SALT_FILENAME = "salt"
SALT_BYTES = 32
TOKEN_LENGTH = 12

#: Which identifier gets which readable prefix, so a pseudonym still looks like
#: the kind of thing it replaced.
PREFIXES: dict[str, str] = {
    "trace_id": "trace",
    "event_id": "event",
    "call_id": "call",
}


class Pseudonymizer:
    """Turns an identifier into a stable token, given a project's salt."""

    def __init__(self, salt: bytes) -> None:
        if len(salt) < 16:
            raise ValueError("the salt must be at least 16 bytes")
        self._salt = salt

    def token(self, value: str, *, field: str) -> str:
        """A stable pseudonym for ``value``, prefixed by the kind of ID it is."""
        digest = hashlib.blake2b(
            value.encode("utf-8"), digest_size=TOKEN_LENGTH // 2, key=self._salt
        ).hexdigest()
        return f"{PREFIXES.get(field, 'id')}-{digest}"

    @classmethod
    def load(cls, path: Path) -> Pseudonymizer:
        """Read a project's salt, creating one on first use."""
        if not path.is_file():
            return cls(_create_salt(path))
        try:
            salt = bytes.fromhex(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            raise CommandError(
                f"Could not read the pseudonymization salt at {path}: {exc}.",
                hint="If it was lost, previously stored identifiers cannot be "
                "matched again; re-ingest into a fresh project.",
            ) from exc
        return cls(salt)


def _create_salt(path: Path) -> bytes:
    salt = secrets.token_bytes(SALT_BYTES)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(salt.hex(), encoding="utf-8")
        # The salt is what makes the tokens unguessable; treat it like a key.
        path.chmod(0o600)
    except OSError as exc:
        raise CommandError(f"Could not write the salt to {path}: {exc}") from exc
    return salt
