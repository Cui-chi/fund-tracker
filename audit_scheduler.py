import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import data_layer_audit
import fund_tracker
from utils import output_paths


BASE_DIR = Path(__file__).resolve().parent
AUDIT_HOUR = 18
AUDIT_MINUTE = 10
POLL_SECONDS = 60
_LOCK = threading.Lock()


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_runs (
            audit_run_id TEXT PRIMARY KEY,
            trigger_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            failed_sources TEXT NOT NULL,
            generated_reports TEXT NOT NULL,
            next_scheduled_run TEXT
        )
    """)


def next_scheduled_run(now=None):
    now = now or dt.datetime.now()
    candidate = now.replace(
        hour=AUDIT_HOUR, minute=AUDIT_MINUTE, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


def _run(command, env=None):
    completed = subprocess.run(
        command, cwd=str(BASE_DIR), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, universal_newlines=True, timeout=900,
    )
    if completed.returncode:
        raise RuntimeError(completed.stdout.strip() or "audit command failed")
    return completed.stdout.strip()


def _modern_python():
    configured = os.environ.get("AA_AUDIT_PYTHON")
    if configured:
        return configured
    bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    return str(bundled) if bundled.exists() else sys.executable


def run_audit_cycle(trigger_type="manual"):
    if not _LOCK.acquire(False):
        raise RuntimeError("an audit run is already active")
    # Cooldown: skip if last audit completed less than 60 minutes ago
    skipped = False
    try:
        conn_check = fund_tracker.connect_db()
        ensure_schema(conn_check)
        last_row = conn_check.execute(
            "SELECT finished_at FROM audit_runs WHERE status='SUCCESS' ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        conn_check.close()
        if last_row and last_row["finished_at"]:
            last_finish = dt.datetime.fromisoformat(last_row["finished_at"])
            if (dt.datetime.now() - last_finish).total_seconds() < 3600:
                skipped = True
    except Exception:
        pass
    if skipped:
        return {"audit_run_id": "skipped-cooldown", "status": "SKIPPED",
                "failed_sources": [], "generated_reports": []}
    run_id = str(uuid.uuid4())
    started = dt.datetime.now()
    run_dir = output_paths.create_run_dir("scheduled-audit", force_new=True)
    generated = []
    failed = []
    conn = fund_tracker.connect_db()
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO audit_runs
        (audit_run_id, trigger_type, started_at, status, failed_sources,
         generated_reports, next_scheduled_run)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id, trigger_type, started.isoformat(timespec="seconds"),
            "RUNNING", "[]", "[]",
            next_scheduled_run(started).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    try:
        config = fund_tracker.load_config()
        try:
            fund_tracker.update_nav_history(conn, config, days=390)
            phase1 = data_layer_audit.audit_fund_nav(conn, config)
            data_layer_audit.write_phase1_reports(run_dir, phase1)
            generated += [
                "reports/fund-nav-audit-report.md", "reports/fund-drawdown-report.md",
                "json/fund-drawdown-result.json",
            ]
            if phase1["status"] != "PASS":
                failed.append("fund_nav")
        except Exception as exc:
            failed.append(f"fund_nav:{type(exc).__name__}:{exc}")

        try:
            fund_tracker.update_index_price_history(conn, cache_hours=0)
            generated += ["data/index_price_history"]
        except Exception as exc:
            failed.append(f"a_share_price:{type(exc).__name__}:{exc}")

        # Release the writer before the macro subprocess opens the same DB.
        conn.commit()
        conn.close()
        conn = None
        try:
            _run([sys.executable, str(BASE_DIR / "run_phase3_macro_audit.py")])
            macro_result = json.loads(output_paths.get_json_path("macro-data-result.json", run_dir).read_text(encoding="utf-8"))
            generated += [
                "reports/macro-data-audit-report.md", "json/macro-data-result.json",
                "logs/macro-fetch-log.md",
            ]
            if macro_result["status"] == "BLOCKED":
                failed.append("macro_data")
        except Exception as exc:
            failed.append(f"macro_data:{type(exc).__name__}:{exc}")

        status = "FAIL" if failed else "SUCCESS"
        output_paths.get_log_path("audit-run.log", run_dir).write_text(
            "\n".join(["trigger=%s" % trigger_type, "status=%s" % status] + failed) + "\n",
            encoding="utf-8",
        )
        manifest_metadata = {
            "phase": "scheduled-audit", "task_name": "automatic data audit cycle",
            "decision_status": "NOT_RUN", "data_status": "FAIL" if failed else "PASS",
            "model_status": "NOT_RUN",
            "output_files": sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*") if path.is_file() and path.name != "run-manifest.md"),
            "blocked_reason": "; ".join(failed),
            "next_action": "Fix failed sources" if failed else "Review audit outputs",
            "source_data_used": ["Eastmoney", "AKShare/Legulegu", "FRED", "PBOC", "data/fund_tracker.sqlite"],
            "whether_root_directory_was_modified": "No",
        }
        if failed:
            output_paths.write_blocked_outputs("; ".join(failed), manifest_metadata, run_dir)
        else:
            output_paths.write_run_manifest(manifest_metadata, run_dir)
    finally:
        finished = dt.datetime.now()
        if conn is not None:
            conn.commit()
            conn.close()
        conn = fund_tracker.connect_db()
        ensure_schema(conn)
        conn.execute(
            """UPDATE audit_runs SET finished_at=?, status=?, failed_sources=?,
               generated_reports=?, next_scheduled_run=? WHERE audit_run_id=?""",
            (
                finished.isoformat(timespec="seconds"),
                "FAIL" if failed else "SUCCESS",
                json.dumps(failed, ensure_ascii=False),
                json.dumps(generated, ensure_ascii=False),
                next_scheduled_run(finished).isoformat(timespec="seconds"),
                run_id,
            ),
        )
        conn.commit()
        conn.close()
        _LOCK.release()
    return {
        "audit_run_id": run_id,
        "status": status,
        "failed_sources": failed,
        "generated_reports": generated,
    }


def latest_audit_status():
    conn = fund_tracker.connect_db()
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM audit_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    successful = conn.execute(
        "SELECT finished_at FROM audit_runs WHERE status='SUCCESS' ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "last_audit_time": row["finished_at"] if row else None,
        "next_scheduled_audit": next_scheduled_run().isoformat(timespec="seconds"),
        "last_successful_data_refresh": successful["finished_at"] if successful else None,
        "current_data_status": row["status"] if row else "NOT_RUN",
        "latest_run": dict(row) if row else None,
    }


def trigger_background(trigger_type):
    thread = threading.Thread(
        target=run_audit_cycle, args=(trigger_type,), daemon=True,
        name=f"data-audit-{trigger_type}",
    )
    thread.start()
    return thread


def scheduler_loop():
    last_run_date = None
    while True:
        now = dt.datetime.now()
        due = now.hour > AUDIT_HOUR or (
            now.hour == AUDIT_HOUR and now.minute >= AUDIT_MINUTE
        )
        if due and last_run_date != now.date():
            trigger = "monthly_review" if now.day == 1 else "scheduled_daily"
            try:
                run_audit_cycle(trigger)
            except Exception:
                pass
            last_run_date = now.date()
        time.sleep(POLL_SECONDS)


def start_scheduler():
    thread = threading.Thread(
        target=scheduler_loop, daemon=True, name="data-audit-scheduler"
    )
    thread.start()
    return thread
