#!/usr/bin/env python3
"""Persistent, user-controlled approval registry for non-official score sources."""

import datetime as dt
import json
from pathlib import Path

from utils import output_paths


APPROVAL_STATUSES = {
    "OFFICIAL_PASS", "APPROVED_PROXY_PASS", "PENDING_PROXY_REVIEW",
    "REJECTED", "DISPLAY_ONLY",
}
CANONICAL_PATH = output_paths.PROJECT_ROOT / "data" / "approved-sources.json"

DEFAULT_APPROVALS = {
    "hs300_pe_percentile": {
        "approval_status": "PENDING_PROXY_REVIEW", "confidence": "Medium",
        "used_in_score": True, "source": "AKShare/Legulegu",
        "reason": "Awaiting explicit user review of source lineage and methodology",
    },
    "hs300_pb_percentile": {
        "approval_status": "PENDING_PROXY_REVIEW", "confidence": "Medium",
        "used_in_score": True, "source": "AKShare/Legulegu",
        "reason": "Awaiting explicit user review of source lineage and methodology",
    },
    "nasdaq100_pe_percentile": {
        "approval_status": "PENDING_PROXY_REVIEW", "confidence": "Medium",
        "used_in_score": True, "source": "QQQ trailing PE proxy",
        "reason": "Awaiting explicit user review of proxy comparability and sample window",
    },
    "sp500_pe_percentile": {
        "approval_status": "PENDING_PROXY_REVIEW", "confidence": "Medium",
        "used_in_score": True, "source": "Multpl S&P 500 trailing PE",
        "reason": "Awaiting explicit user review of source methodology and sample window",
    },
    "a500_pe_percentile": {
        "approval_status": "DISPLAY_ONLY", "confidence": "Low",
        "used_in_score": False, "source": "Third-party current valuation snapshot",
        "reason": "No reproducible PE_TTM history",
    },
    "a500_pb": {
        "approval_status": "DISPLAY_ONLY", "confidence": "Low",
        "used_in_score": False, "source": "Third-party current valuation snapshot",
        "reason": "No reproducible PB history",
    },
}


def _payload(records=None):
    values = records or DEFAULT_APPROVALS
    return {
        "schema_version": "source-approval-v1",
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "approval_policy": {
            "requires_explicit_user_confirmation": True,
            "low_confidence_cannot_be_approved": True,
            "pending_proxy_review_allows_execution": False,
            "display_only_used_in_score": False,
        },
        "sources": dict(
            (indicator, dict({
                "indicator": indicator, "approved_by": None, "approved_at": None,
            }, **record))
            for indicator, record in values.items()
        ),
    }


def validate_registry(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), dict):
        raise ValueError("Invalid approved-sources.json structure")
    for indicator, record in payload["sources"].items():
        status = record.get("approval_status")
        if status not in APPROVAL_STATUSES:
            raise ValueError("Invalid approval_status for %s" % indicator)
        if status == "APPROVED_PROXY_PASS" and record.get("confidence") == "Low":
            raise ValueError("Low confidence source cannot be APPROVED_PROXY_PASS: %s" % indicator)
        if status == "DISPLAY_ONLY" and record.get("used_in_score"):
            raise ValueError("DISPLAY_ONLY source cannot be Used In Score: %s" % indicator)
        if status == "APPROVED_PROXY_PASS" and (
            not record.get("approved_by") or not record.get("approved_at")
        ):
            raise ValueError("Explicit approver and approval timestamp are required: %s" % indicator)
    return payload


def ensure_registry(path=None):
    path = Path(path or CANONICAL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(_payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_registry(path)


def load_registry(path=None):
    path = Path(path or CANONICAL_PATH)
    if not path.exists():
        return ensure_registry(path)
    return validate_registry(json.loads(path.read_text(encoding="utf-8")))


def approval_for(indicator, source_type, direct_or_proxy, used_in_score, confidence=None, registry=None):
    if source_type in ("official", "official-distributor"):
        return "OFFICIAL_PASS"
    record = (registry or load_registry()).get("sources", {}).get(indicator)
    if record:
        status = record["approval_status"]
        if status == "APPROVED_PROXY_PASS" and confidence == "Low":
            return "REJECTED"
        return status
    if not used_in_score:
        return "DISPLAY_ONLY"
    if direct_or_proxy == "Proxy Indicator" or source_type == "third-party":
        return "PENDING_PROXY_REVIEW"
    return "REJECTED"


def snapshot_registry(run_dir=None):
    payload = load_registry()
    path = output_paths.get_json_path("approved-sources.json", run_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def write_approval_report(indicators, run_dir=None):
    payload = snapshot_registry(run_dir)
    rows = []
    indicator_map = dict((item["indicator"], item) for item in indicators)
    for indicator, record in payload["sources"].items():
        actual = indicator_map.get(indicator, {})
        rows.append(
            "| %s | %s | %s | %s | %s | %s | %s |" % (
                indicator, actual.get("source") or record.get("source") or "-",
                actual.get("confidence") or record.get("confidence") or "Unknown",
                "Yes" if actual.get("used_in_score", record.get("used_in_score")) else "No",
                record["approval_status"], record.get("approved_by") or "-",
                record.get("reason") or "-",
            )
        )
    report = """# Source Approval Report

## Technical Summary

- No proxy source is auto-approved.
- `PENDING_PROXY_REVIEW` blocks Dynamic Cash Pool execution.
- `APPROVED_PROXY_PASS` is valid only after explicit user confirmation and is prohibited for Low confidence data.
- `DISPLAY_ONLY` indicators cannot be Used In Score.

## Approval Inventory

| Indicator | Source | Confidence | Used In Score | Approval Status | Approved By | Reason |
|---|---|---|---|---|---|---|
%s

## Current Decision Impact

The four Medium confidence valuation proxies remain `PENDING_PROXY_REVIEW`. The current decision must remain `FREEZE` until every Used In Score proxy is either explicitly approved or replaced by an official source. A500 PE/PB remain display-only and cannot affect release decisions.

## Approval Control

Approval is a user-controlled governance action, not an automated data refresh result. Source freshness, reproducibility, and methodology checks continue to apply after approval; approval alone cannot override a stale or failed indicator.
""" % "\n".join(rows)
    path = output_paths.get_report_path("source-approval-report.md", run_dir)
    path.write_text(report, encoding="utf-8")
    return path
