"""The shared secret of the integration link (design section 6).

The app creates the secret on the first start and keeps it in the data
directory. Only the owner can read the file (mode 0600). The integration
receives the secret through the config flow: by Supervisor discovery, or by
hand from ``GET api/integration``.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

SECRET_FILE = "link_secret"  # noqa: S105 - a file name, not a secret
# 32 random bytes give a 43-character URL-safe text.
SECRET_BYTES = 32
MIN_SECRET_CHARS = 32


def load_or_create_secret(data_dir: Path) -> str:
    """Return the link secret. Create it if the file does not exist."""
    path = data_dir / SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if len(value) >= MIN_SECRET_CHARS:
        return value
    value = secrets.token_urlsafe(SECRET_BYTES)
    # O_EXCL is not used: a short or empty file is replaced.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    path.chmod(0o600)
    return value


def secret_matches(expected: str, header: str | None) -> bool:
    """True if ``header`` is ``Bearer <expected>``. The compare takes constant time."""
    if not header or not expected:
        return False
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(given.strip().encode(), expected.encode())
