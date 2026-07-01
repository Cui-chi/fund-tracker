#!/usr/bin/env python3
"""Move legacy root-level generated artifacts into a timestamped archive."""

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils import output_paths


EXPLICIT_FILES = {
    "Asset Allocation Copilot V7.html", "dashboard.html", "report.json",
    "validation-summary.md", "validation-result.json", "data-quality-report.md",
    "model-risk-report.md", "freeze-report.md", "decision-gate-report.md",
    "decision-snapshot-report.md", "macro-review-pack.md",
    "fund-nav-audit-report.md", "fund-drawdown-report.md", "fund-drawdown-result.json",
    "source-approval-report.md", "macro-data-audit-report.md", "macro-data-result.json",
    "macro-fetch-log.md", "data-audit-report.md", "data-audit-result.json",
    "a-share-valuation-source-feasibility.md", "a-share-valuation-result.json",
    "a-share-valuation-sample.csv", "audit-scheduler-design.md",
    "audit-scheduler-implementation-plan.md", "pe_history.json", "pe_history_quality.json",
}


def unique_target(directory, name):
    candidate = directory / name
    if not candidate.exists():
        return candidate
    source = Path(name)
    for index in range(1, 10000):
        candidate = directory / ("%s-%d%s" % (source.stem, index, source.suffix))
        if not candidate.exists():
            return candidate
    raise RuntimeError("No available legacy filename for %s" % name)


def organize(project_root, timestamp=None):
    project_root = Path(project_root).resolve()
    timestamp = timestamp or dt.datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
    legacy_dir = output_paths.LEGACY_ROOT / timestamp
    legacy_dir.mkdir(parents=True, exist_ok=False)
    moved = []
    try:
        candidates = set(EXPLICIT_FILES)
        for path in project_root.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() in (".csv", ".log"):
                candidates.add(path.name)
            elif path.suffix.lower() == ".json" and path.name not in ("config.json", "package.json"):
                candidates.add(path.name)
            elif path.suffix.lower() == ".md" and path.name.lower() != "readme.md":
                candidates.add(path.name)
        for name in sorted(candidates):
            source = project_root / name
            if not source.is_file():
                continue
            target = unique_target(legacy_dir, source.name)
            shutil.move(str(source), str(target))
            moved.append((source, target))
        manifest = legacy_dir / "legacy-manifest.md"
        lines = [
            "# Legacy Output Manifest", "",
            "- migrated_at: `%s`" % dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "- source_directory: `%s`" % project_root,
            "- overwrite_policy: `Never overwrite; append numeric suffix`", "",
            "## Moved Files", "",
        ]
        lines.extend("- `%s` -> `%s`" % (src.name, dst.name) for src, dst in moved)
        if not moved:
            lines.append("- None")
        manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return legacy_dir, moved
    except Exception:
        # Never delete or partially hide files on failure: move completed items back.
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(output_paths.PROJECT_ROOT))
    parser.add_argument("--timestamp")
    args = parser.parse_args()
    try:
        legacy_dir, moved = organize(args.project_root, args.timestamp)
    except Exception as exc:
        try:
            output_paths.write_blocked_outputs(exc, {
                "phase": "output-migration", "task_name": "organize legacy outputs",
            })
        finally:
            print("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED", file=sys.stderr)
        raise SystemExit(2)
    print("%s (%d files)" % (legacy_dir, len(moved)))


if __name__ == "__main__":
    main()
