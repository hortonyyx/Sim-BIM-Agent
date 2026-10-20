"""New-run continuation conserves source evidence and execution allowance."""

import asyncio
import hashlib
import json

import pytest

from src.agent_runtime.continuation import ReadOnlyRun, claim_continuation, continuation_allowance
from src.agent_runtime.failures import ModelServiceError
from src.agent_runtime.loop import RunLimits
from src.harness_contracts import HashedBlobRef, UsageMissing
from test_agent_runtime import MESSAGES, response, runtime


def stopped_run(tmp_path, *, category="service_unavailable", tokens=None, seconds=90):
    limits = RunLimits(model_calls=8, tool_calls=6, seconds=seconds, tokens=tokens,
                       max_model_retries=0, retry_backoff_seconds=2)
    failure = ModelServiceError({"category": category, "retryable": False,
        "service_error_type": "service_unavailable_error", "service_error_code": "server_is_overloaded",
        "retry_hint": "NO_MORE_RETRY", "usage_received": False}, UsageMissing(reason="terminal usage absent"))
    engine = runtime(tmp_path, [response(("write-once", "save", {"value": 7})), failure], limits=limits)
    with engine.store:
        engine.store.write_json("versions.json", engine.versions.model_dump(mode="json"))
        result = asyncio.run(engine.run(MESSAGES))
    return engine.store.directory, limits, result


def inspect(path, caps):
    source = ReadOnlyRun(path)
    limits, record = continuation_allowance(source, caps,
        expected_events_sha256=hashlib.sha256((path / "events.jsonl").read_bytes()).hexdigest())
    return source, limits, record


def test_new_run_allowance_preserves_used_calls_tools_and_unknown_usage(tmp_path):
    path, caps, result = stopped_run(tmp_path)
    before = {p.relative_to(path): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in path.rglob("*") if p.is_file()}
    source, available, record = inspect(path, caps)
    assert result["status"] == "service_unavailable"
    assert available.model_calls == 6 and available.tool_calls == 5
    assert 0 < available.seconds < caps.seconds
    assert available.tokens is None
    assert record["source_request_count"] == 2 and record["source_tool_invocation_count"] == 1
    assert record["source_ledger"]["settlements"][-1]["actual"]["tokens"] is None
    assert record["source_ledger"]["settlements"][-1]["usage"]["kind"] == "missing"
    assert record["replays_failed_request"] is False
    assert source.verify_unchanged()
    after = {p.relative_to(path): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in path.rglob("*") if p.is_file()}
    assert before == after


def test_continuation_does_not_renew_allowance_when_requested_caps_are_larger(tmp_path):
    path, caps, _ = stopped_run(tmp_path)
    larger = caps.model_copy(update={"model_calls": 800, "tool_calls": 600, "seconds": 9000.0})
    _, available, record = inspect(path, larger)
    assert available.model_calls == 6 and available.tool_calls == 5
    assert available.seconds < 90
    assert record["source_limits"]["model_calls"] == 8


def test_source_version_sidecar_cannot_replace_durable_identity(tmp_path):
    path, caps, _ = stopped_run(tmp_path)
    stamp = json.loads((path / "versions.json").read_bytes())
    stamp["code_commit"]["identifier"] = "fabricated-source-version"
    (path / "versions.json").write_text(json.dumps(stamp), encoding="utf-8")
    with pytest.raises(ValueError, match="durable root request identity"):
        inspect(path, caps)


def test_continuation_rejects_changed_approved_journal_identity(tmp_path):
    path, caps, _ = stopped_run(tmp_path)
    source = ReadOnlyRun(path)
    with pytest.raises(ValueError, match="approved identity"):
        continuation_allowance(source, caps, expected_events_sha256="0" * 64)


@pytest.mark.parametrize("field,value", [("max_model_retries", 20), ("retry_backoff_seconds", 0.0)])
def test_continuation_cannot_reset_retry_policy(tmp_path, field, value):
    path, caps, _ = stopped_run(tmp_path)
    with pytest.raises(ValueError, match="retry and protection"):
        inspect(path, caps.model_copy(update={field: value}))


@pytest.mark.parametrize("category", ["permission_denied", "quota_exhausted", "invalid_request", "context_length_exceeded"])
def test_continuation_does_not_turn_other_terminal_errors_into_service_recovery(tmp_path, category):
    path, caps, _ = stopped_run(tmp_path, category=category)
    with pytest.raises(ValueError, match="stop does not admit"):
        inspect(path, caps)


def test_changed_receipt_cannot_refund_elapsed_time(tmp_path):
    path, caps, _ = stopped_run(tmp_path)
    receipt = json.loads((path / "receipt.json").read_bytes())
    receipt["elapsed_seconds"] = 0.0
    (path / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="durable terminal summary"):
        inspect(path, caps)


def test_readonly_reader_rejects_torn_journal_and_path_escape(tmp_path):
    path, _, _ = stopped_run(tmp_path)
    source = ReadOnlyRun(path)
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        source.read_bytes("../outside.txt")
    ref = HashedBlobRef(uri="../outside.txt", sha256=hashlib.sha256(b"external").hexdigest(), media_type="text/plain")
    with pytest.raises(ValueError, match="escapes"):
        source.get_bytes(ref)
    raw = (path / "events.jsonl").read_bytes()
    (path / "events.jsonl").write_bytes(raw.rstrip(b"\n"))
    with pytest.raises(ValueError, match="complete nonempty"):
        ReadOnlyRun(path)


def test_source_change_after_inspection_is_detected(tmp_path):
    path, caps, _ = stopped_run(tmp_path)
    source, _, _ = inspect(path, caps)
    (path / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        source.verify_unchanged()


def test_task_facade_never_creates_source_directories(tmp_path):
    path, _, _ = stopped_run(tmp_path)
    source = ReadOnlyRun(path)
    child = source.for_task("never-created", source.task_id)
    assert child.events == [] and not child.task_directory.exists()
    assert not (path / "tasks").exists()


def test_source_allowance_cannot_fund_two_continuations(tmp_path):
    path, _, _ = stopped_run(tmp_path)
    source = ReadOnlyRun(path)
    claim = claim_continuation(source, tmp_path / "continued")
    assert claim_continuation(source, tmp_path / "continued") == claim
    with pytest.raises(ValueError, match="another continuation"):
        claim_continuation(source, tmp_path / "other")
    assert not (path / ".continuation-claims").exists()
    assert source.verify_unchanged()


def test_unlimited_time_continuation_keeps_none_and_original_spending(tmp_path):
    path, caps, _ = stopped_run(tmp_path, seconds=None)
    _, available, record = inspect(path, caps)
    assert available.seconds is None
    assert available.model_calls == 6 and available.tool_calls == 5
    assert record["source_elapsed_seconds"] >= 0


def test_recovery_policy_change_needs_explicit_authority_and_cannot_expand_budget(tmp_path):
    path, caps, _ = stopped_run(tmp_path, seconds=None)
    changed = caps.model_copy(update={"max_model_retries": 5, "model_reconnect_seconds": 180,
                                     "model_calls": 1000, "tool_calls": 1000})
    source = ReadOnlyRun(path)
    with pytest.raises(ValueError, match="retry and protection"):
        continuation_allowance(source, changed,
            expected_events_sha256=source.evidence_hashes["events.jsonl"])
    available, record = continuation_allowance(source, changed,
        expected_events_sha256=source.evidence_hashes["events.jsonl"],
        allow_recovery_policy_change=True)
    assert available.model_calls == 6 and available.tool_calls == 5
    assert available.model_reconnect_seconds == 180
    assert record["recovery_policy_change_authorized"]
    assert record["reconnect_consumed_before_seconds"] == 0
    with pytest.raises(ValueError, match="retry and protection"):
        continuation_allowance(source, changed.model_copy(update={"max_total_truncations": 99}),
            expected_events_sha256=source.evidence_hashes["events.jsonl"],
            allow_recovery_policy_change=True)
