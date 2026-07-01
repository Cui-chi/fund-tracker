#!/usr/bin/env python3
"""Copy a generated HTML page into a clearly labelled Day 0 test fixture."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    source, output = Path(args.source), Path(args.output)
    text = source.read_text(encoding="utf-8")
    banner = (
        '<div data-test-fixture="qdii-day0" style="padding:8px;background:#fff3cd;'
        'color:#664d03;text-align:center;font-weight:700;">'
        + args.label + " · NOT FORMAL OUTPUT</div>"
    )
    text = text.replace("<body>", "<body>" + banner, 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
