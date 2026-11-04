"""Tests for Deliverable Mode (C08, C12, C14)."""
from __future__ import annotations

from pathlib import Path
import pytest

from agentcore.deliverable_mode import (
    extract_deliverables,
    sanitize_text_for_delivery,
    attach_deliverables_to_completion,
    handle_clarification_or_patch,
    Deliverable,
)


def test_extract_deliverables_ignores_code_blocks_and_code_snippets(tmp_path: Path):
    chart_file = tmp_path / "revenue_chart.png"
    chart_file.write_bytes(b"\x89PNG\r\n\x1a\nfakeimage")

    report_file = tmp_path / "q3_report.pdf"
    report_file.write_bytes(b"%PDF-1.4 fakepdf")

    sample_script = tmp_path / "script.py"
    sample_script.write_bytes(b"print('hello')")

    text = f"""
Here is the deliverable: {chart_file}
And the formal documentation: {report_file}

Do not extract these sample references:
`{tmp_path}/inline_code.png`
```bash
python {sample_script}
cat {tmp_path}/snippet.pdf
```
"""
    deliverables = extract_deliverables(text, check_exists=True)
    paths = {d.path for d in deliverables}

    assert str(chart_file.resolve()) in paths
    assert str(report_file.resolve()) in paths
    assert len(deliverables) == 2

    # Check categories
    cat_by_path = {d.path: (d.category, d.delivery_mode) for d in deliverables}
    assert cat_by_path[str(chart_file.resolve())] == ("image", "inline")
    assert cat_by_path[str(report_file.resolve())] == ("document", "upload")


def test_extract_deliverables_excludes_source_code_and_logs(tmp_path: Path):
    code_file = tmp_path / "test.py"
    code_file.write_bytes(b"def foo(): pass")

    log_file = tmp_path / "debug.log"
    log_file.write_bytes(b"2026-11-03 ERROR test")

    data_file = tmp_path / "metrics.csv"
    data_file.write_bytes(b"col1,col2\n1,2")

    text = f"Output: {code_file} and {log_file} and data: {data_file}"
    deliverables = extract_deliverables(text, check_exists=True)

    assert len(deliverables) == 1
    assert deliverables[0].path == str(data_file.resolve())
    assert deliverables[0].category == "data"
    assert deliverables[0].delivery_mode == "upload"


def test_extract_deliverables_size_and_existence_checks(tmp_path: Path):
    missing_file = tmp_path / "non_existent.pdf"
    empty_file = tmp_path / "empty.docx"
    empty_file.touch()

    valid_file = tmp_path / "valid.xlsx"
    valid_file.write_bytes(b"data" * 10)

    oversized_file = tmp_path / "huge.zip"
    oversized_file.write_bytes(b"huge" * 100)

    text = f"Files: {missing_file} {empty_file} {valid_file} {oversized_file}"

    # max_size_bytes set to 200 bytes so oversized_file is rejected
    deliverables = extract_deliverables(text, check_exists=True, max_size_bytes=200)
    assert len(deliverables) == 1
    assert deliverables[0].path == str(valid_file.resolve())


def test_sanitize_text_for_delivery(tmp_path: Path):
    doc_path = str((tmp_path / "design_document.docx").resolve())
    deliverable = Deliverable(
        path=doc_path,
        category="document",
        size_bytes=1024,
        display_name="design_document.docx",
        delivery_mode="upload",
    )
    raw_text = f"I generated the document at {doc_path} for your review."
    sanitized = sanitize_text_for_delivery(raw_text, [deliverable])

    assert doc_path not in sanitized
    assert "[design_document.docx]" in sanitized


def test_attach_deliverables_to_completion(tmp_path: Path):
    file1 = str((tmp_path / "chart.png").resolve())
    d1 = Deliverable(path=file1, category="image", size_bytes=500, display_name="chart.png", delivery_mode="inline")

    completion = {"task_id": "t_999", "summary": "Done", "artifacts": ["/already/there.pdf"]}
    updated = attach_deliverables_to_completion(completion, [d1])

    assert "/already/there.pdf" in updated["artifacts"]
    assert file1 in updated["artifacts"]
    assert len(updated["deliverables"]) == 1
    assert updated["deliverables"][0]["display_name"] == "chart.png"


def test_handle_clarification_or_patch_c08():
    # Irreversible phase: done task rejects clarification
    rejected = handle_clarification_or_patch("t_1", "Add login auth", "done")
    assert rejected["accepted"] is False
    assert rejected["action_required"] == "create_follow_up_task"

    # Running task: receives clarification pending worker application
    pending = handle_clarification_or_patch("t_2", "Change port to 8080", "running", already_applied=False)
    assert pending["accepted"] is True
    assert pending["applied"] is False
    assert pending["status"] == "pending_worker_application"

    # Applied clarification
    applied = handle_clarification_or_patch("t_3", "Apply patch v2", "running", already_applied=True)
    assert applied["accepted"] is True
    assert applied["applied"] is True
    assert applied["status"] == "applied"
