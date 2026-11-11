"""Native build provenance shared by release packaging and crash reports.

This module deliberately imports neither application configuration nor native
libraries, so it can run before the worker loads any C extensions.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re


MANIFEST_NAME = "native-build.json"
WINDOWS_FILES = (
    "aopipeline.dll", "libgomp-1.dll", "libgcc_s_seh-1.dll",
    "libwinpthread-1.dll", "libturbojpeg.dll", "ao-crash-collector.exe",
)
PLATFORM_FILES = {
    "windows": WINDOWS_FILES,
    "linux": ("libaopipeline.so",),
    "macos": ("libaopipeline.dylib",),
}


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_hashes(root, platform_name="windows"):
    root = Path(root)
    pipeline = root / "autoortho" / "aopipeline"
    paths = sorted([*pipeline.glob("*.c"), *pipeline.glob("*.h")])
    if platform_name == "windows":
        paths += [
            pipeline / "Makefile.mingw64",
            root / "autoortho" / "native" / "crash_collector.c",
            root / "autoortho" / "aoimage" / "libjpeg-turbo-gcc64.tgz",
            pipeline / "lib" / "windows" / "libturbojpeg.dll",
        ]
    elif platform_name == "macos":
        paths += [pipeline / "Makefile.macos", pipeline / "lib/macos/maclibomp.a",
                  root / "autoortho/aoimage/lib/maclibturbojpeg.a"]
    elif platform_name == "linux":
        paths += [pipeline / "Makefile.linux", root / "autoortho/aoimage/lib/linlibturbojpeg.a"]
    else:
        raise ValueError("Unknown native build platform")
    return {p.relative_to(root).as_posix(): file_sha256(p) for p in paths}


def record_bundle(directory, root, revision, platform_name="windows"):
    directory = Path(directory)
    manifest = {
        "schema": 1,
        "platform": platform_name,
        "revision": revision,
        "sources": source_hashes(root, platform_name),
        "files": {name: file_sha256(directory / name) for name in PLATFORM_FILES[platform_name]},
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def verify_bundle(directory, source_root=None, *, expected_platform=None):
    directory = Path(directory)
    manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    platform_name = manifest.get("platform", "windows")
    if not isinstance(platform_name, str) or platform_name not in PLATFORM_FILES:
        raise ValueError("Invalid native build platform")
    if expected_platform is not None and platform_name != expected_platform:
        raise ValueError("Native build manifest is for the wrong platform")
    files = PLATFORM_FILES[platform_name]
    if manifest.get("schema") != 1 or set(manifest.get("files", {})) != set(files):
        raise ValueError("Invalid native build manifest")
    for name in files:
        if file_sha256(directory / name) != manifest["files"][name]:
            raise ValueError(f"Native build hash mismatch: {name}")
    if source_root is not None and source_hashes(source_root, platform_name) != manifest.get("sources"):
        raise ValueError("Native binaries were built from different sources")
    return manifest


def verify_versions(directory, root, platform_name):
    """Check all three component versions in the compiled/staged pipeline."""
    import ctypes
    directory = Path(directory).resolve()
    # Calling the version functions does not initialize the decoding pipeline.
    search_path = None
    try:
        if platform_name == "windows":
            import os
            search_path = os.add_dll_directory(str(directory))
        library = ctypes.CDLL(str(directory / PLATFORM_FILES[platform_name][0]))
        versions = {}
        for name in ("aocache", "aodecode", "aodds"):
            source = (Path(root) / "autoortho/aopipeline" / (name + ".c")).read_text(encoding="utf-8")
            match = re.search(r'#define\s+' + name.upper() + r'_VERSION\s+"([^"]+)"', source)
            if match is None:
                raise ValueError(f"Missing source version: {name}")
            function = getattr(library, name + "_version")
            function.argtypes = []
            function.restype = ctypes.c_char_p
            actual = function().decode()
            if not actual.startswith(name + " " + match.group(1) + " "):
                raise ValueError(f"Native version mismatch: {name}: {actual}")
            versions[name] = actual
        return versions
    finally:
        if search_path is not None:
            search_path.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "verify", "versions"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--platform", choices=tuple(PLATFORM_FILES), default="windows")
    args = parser.parse_args()
    if args.action == "record":
        if args.source_root is None or not args.revision:
            parser.error("record requires --source-root and --revision")
        manifest = record_bundle(args.directory, args.source_root, args.revision, args.platform)
    elif args.action == "versions":
        if args.source_root is None:
            parser.error("versions requires --source-root")
        print(json.dumps(verify_versions(args.directory, args.source_root, args.platform), indent=2))
        return
    else:
        manifest = verify_bundle(args.directory, args.source_root)
    print(json.dumps({"revision": manifest["revision"], "files": manifest["files"]}, indent=2))


if __name__ == "__main__":
    main()
