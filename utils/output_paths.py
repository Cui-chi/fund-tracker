#!/usr/bin/env python3
"""Single source of truth for versioned run output paths."""

import datetime as dt
import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_ROOT = PROJECT_ROOT / "reports"
RUNS_ROOT = REPORTS_ROOT / "runs"
LEGACY_ROOT = REPORTS_ROOT / "legacy"
DIST_ROOT = PROJECT_ROOT / "dist"
RUN_ENV = "ASSET_COPILOT_RUN_DIR"
RUN_ID_ENV = "ASSET_COPILOT_RUN_ID"

_SUBDIRS = {
    "report": "reports",
    "json": "json",
    "html": "html",
    "csv": "csv",
    "log": "logs",
}


class OutputDirectoryError(RuntimeError):
    pass


def _slug(value):
    text = str(value or "run").strip().lower().replace("_", "-").replace(" ", "-")
    return "".join(ch for ch in text if ch.isalnum() or ch == "-").strip("-") or "run"


def _ensure_run_tree(run_dir):
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        for subdir in _SUBDIRS.values():
            (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED") from exc


def create_run_dir(phase, version=None, now=None, force_new=False):
    """Create a unique run directory and publish it for child processes."""
    inherited = os.environ.get(RUN_ENV)
    if inherited and not force_new:
        run_dir = Path(inherited).resolve()
        if not run_dir.is_dir():
            raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED")
        return run_dir
    now = now or dt.datetime.now().astimezone()
    suffix = _slug(version or phase)
    base_id = "%s_%s" % (now.strftime("%Y-%m-%d_%H%M%S"), suffix)
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    for index in range(1000):
        run_id = base_id if index == 0 else "%s-%d" % (base_id, index)
        run_dir = RUNS_ROOT / run_id
        if run_dir.exists():
            continue
        _ensure_run_tree(run_dir)
        os.environ[RUN_ENV] = str(run_dir)
        os.environ[RUN_ID_ENV] = run_id
        return run_dir
    raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED")


def use_run_dir(run_dir):
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED")
    for subdir in _SUBDIRS.values():
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    os.environ[RUN_ENV] = str(run_dir)
    os.environ[RUN_ID_ENV] = run_dir.name
    return run_dir


def current_run_dir(required=True):
    value = os.environ.get(RUN_ENV)
    if value:
        path = Path(value).resolve()
        if path.is_dir():
            return path
    candidates = sorted(RUNS_ROOT.glob("*"), reverse=True) if RUNS_ROOT.exists() else []
    candidates = [path for path in candidates if path.is_dir()]
    if candidates:
        return candidates[0]
    if required:
        raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED")
    return None


def get_output_path(kind, filename, run_dir=None):
    if kind not in _SUBDIRS:
        raise ValueError("Unknown output kind: %s" % kind)
    base = Path(run_dir) if run_dir else current_run_dir()
    path = base / _SUBDIRS[kind] / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_report_path(filename, run_dir=None):
    return get_output_path("report", filename, run_dir)


def get_json_path(filename, run_dir=None):
    return get_output_path("json", filename, run_dir)


def get_html_snapshot_path(filename, run_dir=None):
    return get_output_path("html", filename, run_dir)


def get_csv_path(filename, run_dir=None):
    return get_output_path("csv", filename, run_dir)


def get_log_path(filename, run_dir=None):
    return get_output_path("log", filename, run_dir)


def get_dist_path(filename):
    path = DIST_ROOT / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def git_commit_hash():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(PROJECT_ROOT),
            stderr=subprocess.DEVNULL, universal_newlines=True,
        ).strip() or "Unavailable"
    except (OSError, subprocess.CalledProcessError):
        return "Unavailable"


def write_run_manifest(metadata, run_dir=None):
    base = Path(run_dir) if run_dir else current_run_dir()
    path = base / "run-manifest.md"
    output_files = metadata.get("output_files", [])
    sources = metadata.get("source_data_used", [])
    lines = [
        "# Run Manifest", "",
        "- run_id: `%s`" % base.name,
        "- generated_at: `%s`" % metadata.get("generated_at", dt.datetime.now().astimezone().isoformat(timespec="seconds")),
        "- phase: `%s`" % metadata.get("phase", "unknown"),
        "- task_name: `%s`" % metadata.get("task_name", "unknown"),
        "- decision_status: `%s`" % metadata.get("decision_status", "NOT_RUN"),
        "- data_status: `%s`" % metadata.get("data_status", "NOT_RUN"),
        "- model_status: `%s`" % metadata.get("model_status", "NOT_RUN"),
        "- validation_stage: `%s`" % metadata.get("validation_stage", "NOT_RUN"),
        "- blocked_reason: `%s`" % (metadata.get("blocked_reason") or "None"),
        "- next_action: `%s`" % (metadata.get("next_action") or "None"),
        "- git_commit_hash: `%s`" % metadata.get("git_commit_hash", git_commit_hash()),
        "- whether_root_directory_was_modified: `%s`" % metadata.get("whether_root_directory_was_modified", "No"),
        "- output_files:",
    ]
    lines.extend("  - `%s`" % item for item in output_files) if output_files else lines.append("  - `None`")
    lines.append("- source_data_used:")
    lines.extend("  - `%s`" % item for item in sources) if sources else lines.append("  - `None`")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise OutputDirectoryError("BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED") from exc
    return path


def write_blocked_outputs(error, metadata=None, run_dir=None):
    base = Path(run_dir) if run_dir else current_run_dir()
    message = str(error) or "BLOCKED: OUTPUT_DIRECTORY_SETUP_FAILED"
    get_report_path("blocked-report.md", base).write_text(
        "# Blocked Report\n\n%s\n" % message, encoding="utf-8",
    )
    get_log_path("error.log", base).write_text(message + "\n", encoding="utf-8")
    payload = dict(metadata or {})
    existing_outputs = list(payload.get("output_files", []))
    payload.update({
        "decision_status": "BLOCKED", "data_status": "BLOCKED",
        "model_status": "NOT_RUN", "blocked_reason": message,
        "next_action": "Fix output directory setup before rerunning",
        "output_files": existing_outputs + ["reports/blocked-report.md", "logs/error.log"],
    })
    return write_run_manifest(payload, base)
