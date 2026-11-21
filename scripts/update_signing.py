#!/usr/bin/env python3
"""Manage the Ed25519 key that signs AutoOrtho release manifests.

    python scripts/update_signing.py keygen
        Print a new private key (for the AO_UPDATE_SIGNING_KEY repository
        secret) and its public key (for UPDATE_PUBLIC_KEYS in
        autoortho/updater/signing.py). Nothing is written to disk.

    python scripts/update_signing.py public <private-key>
        Print the public key for a private key.

    python scripts/update_signing.py verify <manifest> <signature> <public-key>
        Check a downloaded manifest and signature.

Keep the private key out of the repository.
"""

import base64
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "autoortho"))

from updater import ed25519  # noqa: E402
from updater.signing import decode_key, verify_manifest  # noqa: E402


def _b64(data):
    return base64.b64encode(data).decode("ascii")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command = args[0]
    if command == "keygen" and len(args) == 1:
        secret = secrets.token_bytes(32)
        print("Private key (repository secret AO_UPDATE_SIGNING_KEY):")
        print(f"  {_b64(secret)}")
        print("Public key (add to UPDATE_PUBLIC_KEYS):")
        print(f"  {_b64(ed25519.public_key(secret))}")
        return 0
    if command == "public" and len(args) == 2:
        print(_b64(ed25519.public_key(decode_key(args[1]))))
        return 0
    if command == "verify" and len(args) == 4:
        data = Path(args[1]).read_bytes()
        signature = Path(args[2]).read_text(encoding="ascii")
        ok = verify_manifest(data, signature, keys=[decode_key(args[3])])
        print("valid" if ok else "INVALID")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
