"""Tests for agentcore.memory.memory_admission rationalized governance."""
from __future__ import annotations

import unittest

from agentcore.memory.memory_admission import (
    MEMORY_ADMISSION_SCHEMA_VERSION,
    VERDICT_DEFER,
    VERDICT_KEEP,
    VERDICT_REFUSE,
    evaluate_memory_admission,
    extract_evidence_anchors,
    has_credential_leak,
)


class MemoryAdmissionTests(unittest.TestCase):
    def test_credential_leak_detection(self) -> None:
        self.assertTrue(has_credential_leak("secret_key = 'abcdef1234567890abcdef'"))
        self.assertTrue(has_credential_leak("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."))
        self.assertFalse(has_credential_leak("Normal parameter threshold setting value == 10"))

    def test_evidence_anchor_extraction(self) -> None:
        text = "When signal.speed > 50km/h and register == 0x18, latency must be under 300ms, fixed in abc1234f."
        anchors = extract_evidence_anchors(text)
        self.assertTrue(any(a.startswith("hex:0x18") for a in anchors))
        self.assertTrue(any(a.startswith("metric:50km/h") for a in anchors))
        self.assertTrue(any(a.startswith("metric:300ms") for a in anchors))
        self.assertTrue(any(a.startswith("commit:abc1234f") for a in anchors))

    def test_speculation_without_evidence_refused(self) -> None:
        result = evaluate_memory_admission("可能是因为底层网络偶尔丢包导致的偶发性失败")
        self.assertEqual(result["verdict"], VERDICT_REFUSE)
        self.assertEqual(result["reason_code"], "SPECULATION_WITHOUT_EVIDENCE")

    def test_credential_leak_refused(self) -> None:
        result = evaluate_memory_admission("api_key = 'sk-1234567890abcdef12345678' is used for staging")
        self.assertEqual(result["verdict"], VERDICT_REFUSE)
        self.assertEqual(result["reason_code"], "CREDENTIAL_LEAK")

    def test_verifiable_fact_with_anchors_kept(self) -> None:
        result = evaluate_memory_admission(
            "当制动踏板 level == 2 时，内部门限 0x18 触发，状态机平稳切换 Standby，响应时间 200ms"
        )
        self.assertEqual(result["verdict"], VERDICT_KEEP)
        self.assertEqual(result["reason_code"], "GROUNDED_EVIDENCE_ATTACHED")
        self.assertGreaterEqual(len(result["evidence_anchors"]), 2)

    def test_external_receipt_promotes_admission(self) -> None:
        result = evaluate_memory_admission(
            "测试执行超时退出的情况处理方式",
            source_receipt={"commit": "d9812fa", "task_id": "task-9982", "exit_code": 0},
        )
        self.assertEqual(result["verdict"], VERDICT_KEEP)
        self.assertEqual(result["reason_code"], "GROUNDED_EVIDENCE_ATTACHED")
        self.assertTrue(any("receipt_commit" in a for a in result["evidence_anchors"]))

    def test_unverified_factual_claim_deferred(self) -> None:
        result = evaluate_memory_admission("重构后该接口的入参协议由字典变更为强类型数据类")
        self.assertEqual(result["verdict"], VERDICT_DEFER)
        self.assertEqual(result["reason_code"], "AWAITING_REPRO_EVIDENCE")


if __name__ == "__main__":
    unittest.main()

