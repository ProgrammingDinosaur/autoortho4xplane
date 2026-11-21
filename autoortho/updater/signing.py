"""Trust for release manifests.

Each release can carry ``update-manifest.json`` and a detached Ed25519
signature, ``update-manifest.json.sig`` (base64, one line). CI signs it with
the private key stored in the ``AO_UPDATE_SIGNING_KEY`` repository secret.

With a key listed in :data:`UPDATE_PUBLIC_KEYS`, a release must carry a
manifest signed by one of the listed keys or the updater refuses to install
it (it still offers the release page). With the tuple empty, the updater
would rely on SHA-256 checksums alone. List a new key next to the
old one for a release or two before removing the old one.

Create a key pair with ``python scripts/update_signing.py keygen`` (see
docs/developer/self-updater.md).
"""

import base64
import binascii
import os

from . import ed25519

# Base64-encoded 32-byte Ed25519 public keys. The matching private key is
# the AO_UPDATE_SIGNING_KEY repository secret (created 2026-11-21).
UPDATE_PUBLIC_KEYS = (
    "VBEec0JPmen38MahUopSOb195wXkxO3y5MdYj7L2jkY=",
)

# For testing against a fork (only honoured together with AO_UPDATE_REPO).
_TEST_KEY_ENV = "AO_UPDATE_PUBLIC_KEY"


def decode_key(text):
    """32 key bytes from base64 or hex text; ValueError otherwise."""
    value = str(text or "").strip()
    raw = None
    if len(value) == 64:
        try:
            raw = bytes.fromhex(value)
        except ValueError:
            raw = None
    if raw is None:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raw = None
    if raw is None or len(raw) != 32:
        raise ValueError("An Ed25519 key is 32 bytes (base64 or hex).")
    return raw


def trusted_keys(environ=None):
    environ = os.environ if environ is None else environ
    keys = [decode_key(text) for text in UPDATE_PUBLIC_KEYS]
    if environ.get("AO_UPDATE_REPO") and environ.get(_TEST_KEY_ENV):
        keys = [decode_key(environ[_TEST_KEY_ENV])]
    return keys


def signing_required(environ=None):
    return bool(trusted_keys(environ))


def decode_signature(text):
    try:
        raw = base64.b64decode(str(text or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 64 else None


def verify_manifest(data, signature_text, keys=None):
    """True when ``data`` carries a valid signature by a trusted key."""
    keys = trusted_keys() if keys is None else keys
    signature = decode_signature(signature_text)
    if signature is None:
        return False
    return any(ed25519.verify(key, data, signature) for key in keys)


def sign_manifest(data, secret):
    """Base64 signature text for ``data`` (used by the CI script)."""
    return base64.b64encode(ed25519.sign(secret, data)).decode("ascii")
