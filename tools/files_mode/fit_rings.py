"""Fit files mode's rings from FUSE-mode read telemetry.

Usage::

    python tools/files_mode/fit_rings.py reads-*.jsonl [-o rings.json] [--report report.json]

Telemetry files are in ``~/.autoortho-data/telemetry`` on each user's
machine (``[diagnostics] read_telemetry``). Put the resulting ``rings.json``
in ``<files root>/_ao/rings.json`` (or ship it as the default table).
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "autoortho"))

from files_mode import ring_fit  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", help="reads-*.jsonl files (globs allowed)")
    parser.add_argument("-o", "--output", default="rings.json")
    parser.add_argument("--report", default=None, help="write the full report here")
    args = parser.parse_args(argv)
    paths = []
    for pattern in args.files:
        paths.extend(glob.glob(pattern) or [pattern])
    samples = ring_fit.load(paths)
    if not samples:
        print("No samples found.")
        return 1
    rings_json, report = ring_fit.fit(samples)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(rings_json, handle, indent=1)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1, default=str)
    print(f"{len(samples)} reads from {len(paths)} files, {report['ring_samples']} used for rings")
    print(f"Best: scaled by {report['best_variant']['scaling']}, "
          f"{'3-D' if report['best_variant']['three_d'] else 'ground'} distance")
    print("Rings (km, ZL16, 2704 px / 65 deg):", rings_json["rings_zl16_km"])
    print("Default rings:                     ", report["default_rings"])
    print("A1 opens per level read (median):  ", report["A1_opens_per_level_median"])
    print("A2 first level read per texture:   ", report["A2_first_read_levels"])
    print("A6 finest level by quality notch:  ", report["A6_finest_level_by_quality"])
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
