import unittest

import fund_tracker


class NdxActivationGuardTests(unittest.TestCase):
    def test_pending_first_confirmation_blocks_formal_decision(self):
        gate = fund_tracker.ndx_activation_gate_status({
            "status": "SHADOW_COMPLETE", "shadow_days_completed": 5,
            "required_complete_days": 5, "activation_status": "ACTIVE",
            "first_activation_guard": True,
            "first_activation_guard_status": "PENDING_MANUAL_CONFIRMATION",
        })
        self.assertTrue(gate["activation_active"])
        self.assertTrue(gate["first_activation_confirmation_required"])
        self.assertFalse(gate["allow_formal_decision"])
        self.assertEqual(gate["blocking_reason"], "NDX_FIRST_ACTIVATION_CONFIRMATION_REQUIRED")

    def test_confirmed_first_activation_opens_normal_decision_gate(self):
        gate = fund_tracker.ndx_activation_gate_status({
            "status": "SHADOW_COMPLETE", "shadow_days_completed": 5,
            "required_complete_days": 5, "activation_status": "ACTIVE",
            "first_activation_guard": False,
            "first_activation_guard_status": "CONFIRMED_MANUAL",
        })
        self.assertTrue(gate["activation_active"])
        self.assertFalse(gate["first_activation_confirmation_required"])
        self.assertTrue(gate["allow_formal_decision"])

    def test_pending_status_blocks_even_when_boolean_is_missing(self):
        gate = fund_tracker.ndx_activation_gate_status({
            "status": "SHADOW_COMPLETE", "shadow_days_completed": 5,
            "required_complete_days": 5, "activation_status": "ACTIVE",
            "first_activation_guard_status": "PENDING_MANUAL_CONFIRMATION",
        })
        self.assertFalse(gate["allow_formal_decision"])
