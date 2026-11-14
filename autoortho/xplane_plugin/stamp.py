"""Record the plugin source's digest next to the built binaries.

Run after building (build_win.bat and the Makefile do). A test fails when
ao_gate.c changed but the committed binaries were not rebuilt.
"""
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def source_digest():
    with open(os.path.join(HERE, "ao_gate.c"), "rb") as handle:
        # Line endings normalised: git may check the file out as CRLF.
        return hashlib.sha256(handle.read().replace(b"\r\n", b"\n")).hexdigest()


def main():
    os.makedirs(os.path.join(HERE, "bin"), exist_ok=True)
    with open(os.path.join(HERE, "bin", "SOURCE_SHA256"), "w", newline="\n") as handle:
        handle.write(source_digest() + "\n")
    print("Plugin source digest recorded")


if __name__ == "__main__":
    main()
