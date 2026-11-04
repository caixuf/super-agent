import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("recall_diagnostics", Path(__file__).parents[1] / "tools/inspect_recall_diagnostics.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_missing_stale_and_recent_failure_are_distinct(tmp_path):
    path = tmp_path / "health.json"
    assert module.inspect(path, now=100, max_age=10) == {"status": "unknown", "reason": "not_observed"}
    path.write_text(json.dumps({"at": 80, "code": "hit", "count": 1, "query": "private"}))
    path.chmod(0o600)
    assert module.inspect(path, now=100, max_age=10)["reason"] == "stale"
    path.write_text(json.dumps({"at": 99, "code": "backend_query_failed", "count": 0, "query": "private"}))
    result = module.inspect(path, now=100, max_age=10)
    assert result["status"] == "failed" and "private" not in repr(result)


def test_success_is_not_inferred_from_unsafe_or_invalid_data(tmp_path):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"at": 99, "code": "no_match", "count": 0}))
    path.chmod(0o600)
    assert module.inspect(path, now=100, max_age=10)["status"] == "healthy"
    path.chmod(0o644)
    assert module.inspect(path, now=100, max_age=10)["reason"] == "unsafe_file"
    path.chmod(0o600)
    path.write_text(json.dumps({"at": 99, "code": "invented", "count": 0}))
    assert module.inspect(path, now=100, max_age=10)["status"] == "unknown"
