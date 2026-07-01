import json
import tempfile
import unittest
from pathlib import Path

import source_approval


class SourceApprovalTests(unittest.TestCase):
    def test_initial_registry_never_auto_approves_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = source_approval.ensure_registry(Path(tmp) / "approved-sources.json")
        self.assertEqual(payload["sources"]["hs300_pe_percentile"]["approval_status"], "PENDING_PROXY_REVIEW")
        self.assertEqual(payload["sources"]["a500_pe_percentile"]["approval_status"], "DISPLAY_ONLY")

    def test_low_confidence_proxy_cannot_be_approved(self):
        payload = source_approval._payload()
        payload["sources"]["a500_pe_percentile"].update({
            "approval_status": "APPROVED_PROXY_PASS",
            "approved_by": "user",
        })
        with self.assertRaises(ValueError):
            source_approval.validate_registry(payload)

    def test_approval_requires_explicit_approver(self):
        payload = source_approval._payload()
        payload["sources"]["hs300_pe_percentile"]["approval_status"] = "APPROVED_PROXY_PASS"
        with self.assertRaises(ValueError):
            source_approval.validate_registry(payload)
