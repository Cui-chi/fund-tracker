#!/usr/bin/env python3
"""Build run-scoped QDII browser fixtures without mutating the live snapshot."""

import argparse
import datetime as dt
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-snapshot", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--build-active-html", action="store_true")
    args = parser.parse_args()
    run_dir = Path(args.run_dir)
    if args.build_active_html:
        fresh = run_dir / "html" / "ndx-fixture-fresh-freeze.html"
        active = run_dir / "html" / "ndx-fixture-fresh-active.html"
        text = fresh.read_text(encoding="utf-8")
        if "CONTROLLED TEST FIXTURE" not in text:
            text = text.replace(
                "<body>",
                '<body><div style="padding:8px;background:#fff3cd;color:#664d03;text-align:center;font-weight:700;">CONTROLLED TEST FIXTURE · FRESH / AVAILABLE / FREEZE · NOT FORMAL OUTPUT</div>',
                1,
            )
            fresh.write_text(text, encoding="utf-8")
        text = text.replace('data-cash-pool-status="FREEZE"', 'data-cash-pool-status="ACTIVE"', 1)
        text = text.replace("FRESH / AVAILABLE / FREEZE", "FRESH / AVAILABLE / ACTIVE", 1)
        active.write_text(text, encoding="utf-8")
        return

    source = json.loads(Path(args.source_snapshot).read_text(encoding="utf-8"))
    generated = dt.datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
    if generated.tzinfo is not None:
        generated = generated.astimezone().replace(tzinfo=None)
    stale = dict(source)
    stale["generated_at"] = (generated - dt.timedelta(minutes=90)).isoformat(sep=" ", timespec="seconds")
    fresh = dict(source)
    fresh["generated_at"] = generated.isoformat(sep=" ", timespec="seconds")
    json_dir = run_dir / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / "formal-stale-carrier-snapshot.json").write_text(
        json.dumps(stale, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (json_dir / "fixture-fresh-carrier-snapshot.json").write_text(
        json.dumps(fresh, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
