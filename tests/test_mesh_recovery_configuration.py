"""Explicit mesh continuation and reconnect limits survive the public config path."""

import json
from pathlib import Path

import pytest

from src.agent.runtime_configuration import argv_for, load_configuration


def configuration(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "building.glb").write_bytes(b"configuration-only GLB fixture")
    return {
        "schema_version": 1,
        "batch_id": "mesh-recovery",
        "approval_required_before_launch": True,
        "maximum_seconds_per_case": None,
        "input_root": str(inputs),
        "run_root": str(tmp_path / "runs"),
        "cases": [{
            "case_id": "mesh",
            "mode": "single_model",
            "provider": "chatgpt-subscription",
            "model": "gpt-6.1-sol",
            "reasoning_effort": "high",
            "reasoning_history": "all",
            "input": "building.glb",
            "input_kind": "mesh",
            "image_kind": "drawings",
            "output": "continued",
            "scope": "Continue saved work",
            "max_candidates": 64,
            "output_tokens": 32000,
            "limits": {
                "model_calls": 48, "tool_calls": 240, "seconds": None,
                "tokens": None, "model_retries": 1, "retry_backoff_seconds": 2,
                "model_reconnect_seconds": 180,
            },
            "continuation": {
                "source_run": str(tmp_path / "source"),
                "source_events_sha256": "a" * 64,
                "allow_recovery_policy_change": True,
            },
        }],
    }


def load(tmp_path, value):
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return load_configuration(path)["cases"][0]


def test_mesh_continuation_and_reconnect_reach_native_entry(tmp_path):
    from src.agent.runtime_entry import parser

    configured = configuration(tmp_path)
    args = parser().parse_args(argv_for(load(tmp_path, configured))[3:])
    assert args.continue_from == Path(configured["cases"][0]["continuation"]["source_run"])
    assert args.source_events_sha256 == "a" * 64
    assert args.allow_recovery_policy_change
    assert args.model_reconnect_seconds == 180
    assert args.model_calls == 48 and args.tool_calls == 240  # Import deducts old consumption.
    assert args.seconds is None and args.tokens is None
    assert not args.resume


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "180"])
def test_invalid_reconnect_budget_rejected_before_launch(tmp_path, value):
    configured = configuration(tmp_path)
    configured["cases"][0]["limits"]["model_reconnect_seconds"] = value
    with pytest.raises(ValueError):
        load(tmp_path, configured)


@pytest.mark.parametrize("change", [
    {"source_run": "relative/source"},
    {"source_events_sha256": "wrong"},
    {"allow_recovery_policy_change": "yes"},
    {"extra": True},
])
def test_mesh_continuation_identity_and_policy_are_explicit(tmp_path, change):
    configured = configuration(tmp_path)
    configured["cases"][0]["continuation"].update(change)
    with pytest.raises(ValueError, match="continuation needs"):
        load(tmp_path, configured)


def test_recovery_policy_permission_is_not_invented(tmp_path):
    configured = configuration(tmp_path)
    configured["cases"][0]["continuation"].pop("allow_recovery_policy_change")
    selected = load(tmp_path, configured)
    assert "--allow-recovery-policy-change" not in argv_for(selected)


def test_ordinary_cold_start_is_not_turned_into_continuation(tmp_path):
    configured = configuration(tmp_path)
    configured["cases"][0].pop("continuation")
    configured["cases"][0]["limits"].pop("model_reconnect_seconds")
    args = argv_for(load(tmp_path, configured))
    assert "--continue-from" not in args
    assert "--allow-recovery-policy-change" not in args
    assert "--model-reconnect-seconds" not in args


def test_wall_time_mode_preserves_the_formal_limits_through_cli(tmp_path):
    from src.agent.runtime_entry import parser
    from src.agent.runtime_configuration import validate_budget

    configured = configuration(tmp_path)
    configured["maximum_seconds_per_case"] = 5400
    case = configured["cases"][0]
    case.pop("continuation")
    case["mesh_workers"] = {"max_concurrent": 8}
    case["limits"].update(model_calls=80, tool_calls=400, seconds=5400, time_budget_mode="wall")
    selected = load(tmp_path, configured)
    command = argv_for(selected)
    args = parser().parse_args(command[3:])
    assert args.time_budget_mode == "wall" and args.seconds == 5400
    assert args.model_calls == 80 and args.tool_calls == 400 and args.tokens is None
    assert args.mesh_workers == 8 and args.model_reconnect_seconds == 180
    assert "--no-time-limit" not in command and "--no-token-limit" in command
    assert validate_budget(selected).ledger_limit().seconds is None
    case["limits"].pop("time_budget_mode")
    selected = load(tmp_path, configured)
    args = parser().parse_args(argv_for(selected)[3:])
    assert args.time_budget_mode == "wall_and_requests"
    assert validate_budget(selected).ledger_limit().seconds == 5400
    assert "time_budget_mode" not in validate_budget(selected).model_dump(mode="json")


@pytest.mark.parametrize("value", ["elapsed", None, True, 5400])
def test_invalid_time_mode_fails_before_launch(tmp_path, value):
    configured = configuration(tmp_path)
    configured["cases"][0]["limits"]["time_budget_mode"] = value
    with pytest.raises(ValueError):
        load(tmp_path, configured)


def test_wall_time_mode_cannot_be_silently_ignored_by_another_entrypoint(tmp_path):
    from src.agent.runtime_configuration import validate_budget

    case = configuration(tmp_path)["cases"][0]
    case["mode"] = "role_division"
    case["limits"].update(seconds=5400, time_budget_mode="wall")
    case["limits"].pop("model_reconnect_seconds")
    with pytest.raises(ValueError, match="time_budget_mode currently supports single_model only"):
        validate_budget(case)


def test_resume_of_continuation_verifies_source_without_reimporting(tmp_path):
    from src.agent.runtime_entry import parser

    configured = configuration(tmp_path)
    selected = load(tmp_path, configured)
    output = Path(selected["run_root"]) / selected["output"]
    output.mkdir(parents=True)
    identity = selected["continuation"]
    manifest = output / "continuation_manifest.json"
    manifest.write_text(json.dumps(identity), encoding="utf-8")
    args = parser().parse_args(argv_for(selected, resume=True)[3:])
    assert args.resume and args.continue_from is None
    assert args.source_events_sha256 is None
    assert not args.allow_recovery_policy_change
    manifest.write_text(json.dumps({**identity, "source_events_sha256": "b" * 64}), encoding="utf-8")
    with pytest.raises(ValueError, match="configured identity"):
        argv_for(selected, resume=True)
