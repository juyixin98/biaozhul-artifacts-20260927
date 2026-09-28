"""Deterministically build the synthetic snapshot fixture.

Every secret in this fixture is a *fake* value invented for this test
repository. Nothing here is a real credential, and no test ever performs
network validation.

Re-running this script reproduces the fixture byte-for-byte (fixed seed,
fixed sizes). ``no-read.log`` additionally needs chmod 000; on filesystems
that map the mode faithfully it then fails to open and is recorded as
unreadable.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP = HERE / "fixtures" / "snapshot"

# Fake candidate material (hand-invented, clearly labelled).
FAKE_AWS = "AKIAFAKE000000000001"
FAKE_GHP = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"
FAKE_APP_TOKEN = "aB3xK9zPq7vRm2TnY8wCdEfGhJk4MsU"
FAKE_PROSE_TOKEN = "Xq7vKp2mZbW8xNdR4tYcL6sHgFjAeQ3uVwBnCaQ"
# Repeating-pattern digest: high-entropy-looking hex but Shannon entropy 4.0
# (below the 4.5 threshold) -> negative control.
FAKE_DIGEST_LOW_ENTROPY = "0123456789abcdef" * 4

PGP_BLOCK = (
    "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
    "xsBNBGfakeFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE\n"
    "=fak1\n"
    "-----END PGP PRIVATE KEY BLOCK-----\n"
)

MAX_FILE_BYTES = 1_048_576  # keep in sync with config/rules.yaml


def write_text(rel: str, content: str, *, mode: int | None = None) -> Path:
    p = SNAP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        os.chmod(p, 0o644)  # make rebuilds idempotent for the mode-000 fixture
    p.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(p, mode)
    return p


def main() -> None:
    SNAP.mkdir(parents=True, exist_ok=True)

    # ---- scanned text files -------------------------------------------
    write_text(
        "src/app.py",
        "#!/usr/bin/env python3\n"
        "# Demo application with deliberately planted FAKE test values.\n"
        'REGION = "us-east-1"\n'
        f"AWS_ACCESS_KEY_ID = {FAKE_AWS!r}  # FAKE, fixture-only\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "print(add(2, 3))\n",
    )
    write_text(
        "config/app.env",
        f"APP_TOKEN={FAKE_APP_TOKEN}\n"
        f"{PGP_BLOCK}",
    )
    write_text(
        "notes/random.txt",
        "Meeting notes from the offline review.\n"
        f"Unrelated prose around this random-looking token {FAKE_PROSE_TOKEN} "
        "which nobody assigned to a key name.\n"
        f"A checksum value {FAKE_DIGEST_LOW_ENTROPY} that is ordinary structured text.\n",
    )

    # ---- ignored paths -------------------------------------------------
    write_text("config/app.example.env", f"APP_TOKEN={FAKE_APP_TOKEN}\n")
    write_text(
        "vendor/lib/dep.txt",
        f"vendored blob containing AKIAFAKE000000000002 which must be ignored\n",
    )
    write_text(
        ".git/config",
        "[core]\n\trepositoryformatversion = 0\n"
        "token = ghp_0123456789abcdefghijklmnopqrstuvwxyz\n",
    )

    # ---- binary file with embedded fake token --------------------------
    rng = random.Random(20260927)
    filler = bytes(rng.randrange(0, 16) for _ in range(128))
    prefix = b"ZZZ="
    blob = filler + prefix + FAKE_GHP.encode("ascii") + b"\x00" + b"\x01\x02tail"
    p = SNAP / "data/blob.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)

    # ---- symlink (not followed) ----------------------------------------
    link = SNAP / "link-to-app"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("src/app.py")

    # ---- oversize file (never opened; decoy secret must not be found) --
    huge = SNAP / "huge.log"
    huge.parent.mkdir(parents=True, exist_ok=True)
    with huge.open("wb") as fh:
        # Low-byte filler (0x00..0x0f) so it is also binary-classified.
        fh.write(bytes(rng.randrange(0, 16) for _ in range(MAX_FILE_BYTES + 1024)))
        fh.write(b"AKIAFAKE000000000003")

    # ---- unreadable file -----------------------------------------------
    noread = write_text(
        "no-read.log",
        "secrets that cannot be read\nAKIAFAKE000000000004\n",
        mode=0o000,
    )

    print(f"fixture built at {SNAP}")
    print(f"oversize: {huge.stat().st_size} bytes (limit {MAX_FILE_BYTES})")
    print(f"unreadable mode: {noread.stat().st_mode & 0o777:03o}")


if __name__ == "__main__":
    main()
