import argparse

import data_layer_audit
import fund_tracker
from utils import output_paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", type=int, choices=(1,), default=1)
    args = parser.parse_args()
    run_dir = output_paths.create_run_dir("phase-p0-drawdown")
    config = fund_tracker.load_config()
    conn = fund_tracker.connect_db()
    try:
        if args.phase == 1:
            result = data_layer_audit.audit_fund_nav(conn, config)
            data_layer_audit.write_phase1_reports(run_dir, result)
            conn.commit()
            print(f"Phase 1: {result['status']}")
            if result["block_code"]:
                print(f"BLOCKED: {result['block_code']}")
            output_paths.write_run_manifest({
                "phase": "phase-p0-drawdown", "task_name": "fund NAV and drawdown audit",
                "decision_status": "NOT_RUN", "data_status": result["status"],
                "model_status": "NOT_RUN",
                "output_files": ["reports/fund-nav-audit-report.md", "reports/fund-drawdown-report.md", "json/fund-drawdown-result.json"],
                "blocked_reason": result.get("block_code") or "",
                "next_action": "Review phase 1 outputs",
                "source_data_used": ["data/fund_tracker.sqlite", "Eastmoney NAV history"],
                "whether_root_directory_was_modified": "No",
            }, run_dir)
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        output_paths.write_blocked_outputs(exc, {"phase": "phase-p0-drawdown", "task_name": "fund NAV and drawdown audit"})
        raise
