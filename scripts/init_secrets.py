"""Create secrets.local.env with fresh random secrets (local development only).

Run once. Refuses to overwrite an existing file, so the masking salt (and
therefore every mask_hash value) stays stable between runs.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "secrets.local.env"
TOKEN_BYTES = 32


def main() -> int:
    if TARGET.exists():
        print(f"{TARGET.name} already exists; leaving it unchanged.", file=sys.stderr)
        return 0
    TARGET.write_text(
        "# Local secrets for the dev suite. Never commit.\n"
        f"GATEWAY_SESSION_SECRET={secrets.token_urlsafe(TOKEN_BYTES)}\n"
        f"MASK_SALT={secrets.token_urlsafe(TOKEN_BYTES)}\n",
        encoding="utf-8",
    )
    TARGET.chmod(0o600)
    print(f"Wrote {TARGET.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
