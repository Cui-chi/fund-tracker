#!/usr/bin/env python3
"""Assemble the immutable-input Shadow Day 0 evidence package."""

import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    output = run_dir / "shadow-readiness-day0"
    output.mkdir(parents=True, exist_ok=True)
    sources = {
        "qdii-carrier-latest.json": run_dir / "inputs" / "qdii-carrier-latest.json",
        "qdii-carrier-snapshot-raw.json": run_dir / "inputs" / "qdii-carrier-snapshot-raw.json",
        "input-manifest.json": run_dir / "inputs" / "input-manifest.json",
        "report.json": run_dir / "json" / "report.json",
        "run-manifest.md": run_dir / "run-manifest.md",
        "shadow-readiness-day0-report.md": run_dir / "reports" / "shadow-readiness-day0-report.md",
        "browser-mcp-verification.md": run_dir / "reports" / "browser-mcp-verification.md",
    }
    for name, source in sources.items():
        if not source.is_file():
            raise SystemExit("missing Day 0 evidence: " + str(source))
        shutil.copyfile(str(source), str(output / name))


if __name__ == "__main__":
    main()
