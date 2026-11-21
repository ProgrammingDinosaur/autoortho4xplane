#!/usr/bin/env python3
"""Write update-manifest.json (and optionally its signature) for a release.

Run by the release workflow once every platform build has finished:

    python scripts/make_update_manifest.py --tag 3.5.0 --out dist-manifest \
        release-files/*

When AO_UPDATE_SIGNING_KEY holds a base64 (or hex) Ed25519 private key, it
also writes update-manifest.json.sig. See docs/developer/self-updater.md.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "autoortho"))

from updater.manifest import (  # noqa: E402
    MANIFEST_NAME,
    SIGNATURE_NAME,
    build_manifest,
    dump_manifest,
)
from updater.signing import decode_key, sign_manifest  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="release tag, e.g. 3.5.0")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument(
        "--min-client-api",
        type=int,
        default=1,
        help="oldest updater API that may install this release",
    )
    parser.add_argument("files", nargs="+", help="release files")
    args = parser.parse_args(argv)

    document = build_manifest(args.tag, args.files, args.min_client_api)
    data = dump_manifest(document)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST_NAME).write_bytes(data)
    print(f"Wrote {out / MANIFEST_NAME} with {len(document['assets'])} files:")
    for entry in document["assets"]:
        print(f"  {entry['name']}  {entry['size']}  {entry['sha256']}")

    key_text = os.environ.get("AO_UPDATE_SIGNING_KEY", "").strip()
    if key_text:
        signature = sign_manifest(data, decode_key(key_text))
        (out / SIGNATURE_NAME).write_text(signature + "\n", encoding="ascii")
        print(f"Signed it: {out / SIGNATURE_NAME}")
    else:
        print("AO_UPDATE_SIGNING_KEY is not set; the manifest is not signed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
