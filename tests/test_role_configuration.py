"""Offline checks for the D1 role-division routing contract."""

from __future__ import annotations

import copy
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.agent.runtime_configuration import argv_for, load_configuration
from src.agent.runtime_roles.config import ROLE_NAMES, load_roles


ROOT = Path(__file__).resolve().parents[1]


def role(provider="glm-subscription-anthropic", model="glm-5.3-flash",
         effort="medium", output_tokens=32_000):
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "output_tokens": output_tokens,
    }


def roles(**overrides):
    value = {name: role() for name in ROLE_NAMES}
    for name, replacement in overrides.items():
        value[name] = replacement
    return value


def case(mode="role_division"):
    value = {
        "case_id": "role-config-test",
        "mode": mode,
        "provider": "glm-subscription-anthropic",
        "model": "glm-5.3-flash",
        "credentials_file": "private-test.env",
        "input_kind": "images",
        "input": "tests/fixtures/role_current/sm24/images",
        "image_kind": "drawings",
        "floor_plan_images": ["1f_view.png"],
        "output": "runs/role-config-test",
        "scope": "offline configuration test",
        "limits": {
            "model_calls": 10,
            "tool_calls": 20,
            "tokens": 500_000,
            "seconds": 60,
            "model_retries": 2,
            "retry_backoff_seconds": 1.0,
        },
        "max_candidates": 24,
        "context_tokens": 900_000,
        "compact_at_tokens": 150_000,
        "output_tokens": 32_000,
        "reasoning_effort": "medium",
    }
    if mode == "role_division":
        value["roles"] = roles()
    return value


def configuration(one_case):
    return {
        "schema_version": 1,
        "batch_id": "role-config-test",
        "approval_required_before_launch": True,
        "maximum_seconds_per_case": 60,
        "cases": [one_case],
    }


def load(tmp_path, one_case):
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(configuration(one_case)), encoding="utf-8")
    return load_configuration(path)


def test_continuation_identity_survives_configuration_and_cli(tmp_path):
    selected = case()
    source = str((tmp_path / "terminal-source").resolve())
    selected["continuation"] = {"source_run": source, "source_events_sha256": "a" * 64}
    from src.agent.runtime_roles.entry import parser
    parsed = parser().parse_args(argv_for(load(tmp_path, selected)["cases"][0])[3:])
    assert parsed.continue_from == Path(source)
    assert parsed.source_events_sha256 == "a" * 64
    assert not parsed.resume  # New run, not checkpoint recovery.


@pytest.mark.parametrize("change", [
    {"source_run": "relative/source"}, {"source_events_sha256": "not-a-hash"},
    {"source_events_sha256": "A" * 64}, {"source_events_sha256": 123},
    {"extra": True},
])
def test_invalid_continuation_identity_is_rejected_before_launch(tmp_path, change):
    selected = case()
    selected["continuation"] = {"source_run": str(tmp_path.resolve()),
                                "source_events_sha256": "a" * 64, **change}
    with pytest.raises(ValueError, match="continuation needs"):
        load(tmp_path, selected)


def test_single_model_cannot_silently_ignore_reader_continuation(tmp_path):
    selected = case("single_model")
    selected["continuation"] = {"source_run": str(tmp_path.resolve()),
                                "source_events_sha256": "a" * 64}
    with pytest.raises(ValueError, match="role_division"):
        load(tmp_path, selected)


@pytest.mark.parametrize("settings", [
    {"model_reconnect_seconds": 180},
    {"allow_recovery_policy_change": True},
])
def test_drawing_roles_do_not_silently_ignore_mesh_recovery_settings(settings):
    from src.agent.runtime_roles.entry import execute

    with pytest.raises(ValueError, match="require single_model"):
        asyncio.run(execute(SimpleNamespace(**settings)))


def test_roles_require_all_fixed_identities_and_reject_unknown_identity():
    missing = roles()
    missing.pop("elevation_reader")
    with pytest.raises(ValueError, match="missing roles: elevation_reader"):
        load_roles(missing)
    extra = roles(auditor=role())
    with pytest.raises(ValueError, match="unexpected roles: auditor"):
        load_roles(extra)
    with pytest.raises(ValueError, match="explicit roles object"):
        load_roles(None)


@pytest.mark.parametrize("field", ["provider", "model", "output_tokens"])
def test_role_route_has_no_field_inheritance(field):
    incomplete = role()
    incomplete.pop(field)
    with pytest.raises(ValidationError, match=field):
        load_roles(roles(plan_reader=incomplete))


@pytest.mark.parametrize(("provider", "model", "effort", "tokens"), [
    ("glm-subscription-anthropic", "glm-5.3-flash", "medium", 32_000),
    ("glm-subscription", "glm-5.3-flash", "medium", 32_000),
    ("paratera", "Qwen3.8-27B", "high", 16_384),
])
def test_existing_anthropic_and_openai_compatible_routes_are_accepted(
        provider, model, effort, tokens):
    parsed = load_roles({name: role(provider, model, effort, tokens) for name in ROLE_NAMES})
    assert parsed["plan_reader"].provider == provider
    assert parsed["elevation_reader"].model == model


def test_sol_coordinator_and_luna_readers_survive_configuration_and_cli(tmp_path):
    selected = case()
    selected.update(provider="chatgpt-subscription", model="gpt-6-sol",
                    reasoning_effort="high", max_concurrent_readers=6)
    selected.pop("credentials_file")
    selected["roles"] = {
        name: role("chatgpt-subscription", "gpt-6-sol" if name == "coordinator"
                   else "gpt-6-luna", "high") for name in ROLE_NAMES}
    parsed = load(tmp_path, selected)["cases"][0]
    argv = argv_for(parsed)
    from src.agent.runtime_roles.entry import parser
    command = parser().parse_args(argv[3:])
    routes = load_roles(json.loads(command.roles_json))
    assert command.model == routes["coordinator"].model == "gpt-6-sol"
    assert routes["plan_reader"].model == routes["elevation_reader"].model == "gpt-6-luna"
    assert all(route.reasoning_effort == "high" for route in routes.values())
    assert command.max_concurrent_readers == 6
    assert command.credentials_file is None
    selected["roles"]["plan_reader"]["model"] = "gpt-6.1-luna"
    with pytest.raises(ValidationError, match="no reviewed estimation profile"):
        load(tmp_path, selected)


def test_role_context_overrides_are_explicit_and_strict():
    configured = roles()
    configured["plan_reader"]["context"] = {
        "context_tokens": 180_000, "compact_at_tokens": 90_000, "max_images": 2}
    parsed = load_roles(configured)
    assert parsed["plan_reader"].model_dump(mode="json")["context"] == configured["plan_reader"]["context"]
    configured["plan_reader"]["context"]["unknown"] = 1
    with pytest.raises(ValidationError, match="unknown"):
        load_roles(configured)


def test_role_context_overrides_survive_configuration_cli(tmp_path):
    selected = case()
    selected["roles"]["elevation_reader"]["context"] = {
        "context_tokens": 180_000, "compact_at_tokens": 90_000, "max_image_bytes": 8_000_000}
    parsed = load(tmp_path, selected)["cases"][0]
    argv = argv_for(parsed)
    encoded = json.loads(argv[argv.index("--roles-json") + 1])
    assert encoded["elevation_reader"]["context"] == selected["roles"]["elevation_reader"]["context"]


@pytest.mark.parametrize("mode", ["single_model", "role_division"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_automatic_context_configuration_does_not_invent_a_ceiling(tmp_path, mode, explicit_null):
    selected = case(mode)
    for key in ("context_tokens", "compact_at_tokens"):
        selected.pop(key)
        if explicit_null:
            selected[key] = None
    argv = argv_for(load(tmp_path, selected)["cases"][0])
    assert "--context-tokens" not in argv and "--compact-at-tokens" not in argv
    from src.agent.runtime_entry import parser as single_parser
    from src.agent.runtime_roles.entry import parser as role_parser
    parsed = (role_parser if mode == "role_division" else single_parser)().parse_args(argv[3:])
    assert parsed.context_tokens is None and parsed.compact_at_tokens is None


@pytest.mark.parametrize("key", ["context_tokens", "compact_at_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "200000"])
def test_invalid_context_setting_fails_before_launch(tmp_path, key, value):
    selected = case()
    selected[key] = value
    with pytest.raises(ValueError, match="positive integer or null"):
        load(tmp_path, selected)


def test_explicit_role_auto_survives_serialization_and_cli(tmp_path):
    selected = case()
    selected["roles"]["plan_reader"]["context"] = {
        "context_tokens": None, "compact_at_tokens": None}
    argv = argv_for(load(tmp_path, selected)["cases"][0])
    parsed_roles = load_roles(json.loads(argv[argv.index("--roles-json") + 1]))
    from src.agent.runtime_roles.context_policy import effective_role_context
    resolved = effective_role_context("plan_reader", global_defaults={
        "context_tokens": selected["context_tokens"], "compact_at_tokens": selected["compact_at_tokens"]},
        role_overrides=parsed_roles["plan_reader"].context)
    assert resolved.context_tokens is None and resolved.compact_at_tokens is None


def test_unreviewed_route_deepseek_and_low_output_are_rejected():
    with pytest.raises(ValidationError, match="reviewed live provider"):
        load_roles(roles(coordinator=role("unknown-route")))
    with pytest.raises(ValidationError, match="DeepSeek"):
        load_roles(roles(coordinator=role("paratera", "DeepSeek-V3.2", "high", 32_000)))
    with pytest.raises(ValidationError, match="recommended minimum 32000"):
        load_roles(roles(coordinator=role(output_tokens=1024)))


def test_scripted_routes_need_the_explicit_offline_switch():
    scripted = {name: role("scripted", "scripted-model", "offline", 128)
                for name in ROLE_NAMES}
    with pytest.raises(ValidationError, match="explicit offline runtime"):
        load_roles(scripted)
    parsed = load_roles(scripted, allow_scripted=True)
    assert parsed["coordinator"].output_tokens == 128
    with pytest.raises(ValidationError, match="explicit offline runtime"):
        load_roles({**scripted, "plan_reader": role(
            "scripted", "glm-5.3-flash", "offline", 128)}, allow_scripted=True)


def test_role_configuration_is_drawing_only_and_coordinator_is_unambiguous(tmp_path):
    mesh = case()
    mesh["input_kind"] = "mesh"
    with pytest.raises(ValueError, match="drawing images only"):
        load(tmp_path, mesh)
    photograph = case()
    photograph["image_kind"] = "photographs"
    with pytest.raises(ValueError, match="drawing images only"):
        load(tmp_path, photograph)
    mismatch = case()
    mismatch["roles"]["coordinator"]["reasoning_effort"] = "high"
    with pytest.raises(ValueError, match="top-level coordinator routing.*reasoning_effort"):
        load(tmp_path, mismatch)


def test_role_argv_carries_canonical_inline_routes_and_default_concurrency(tmp_path):
    selected = load(tmp_path, case())["cases"][0]
    argv = argv_for(selected)
    assert argv[1:3] == ["-m", "src.agent.runtime_roles.entry"]
    encoded = argv[argv.index("--roles-json") + 1]
    assert json.loads(encoded) == roles()
    assert encoded == json.dumps(roles(), ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
    assert argv[argv.index("--max-concurrent-readers") + 1] == "8"
    assert argv[argv.index("--max-candidates") + 1] == "24"
    assert argv[argv.index("--context-tokens") + 1] == "900000"
    assert argv[argv.index("--compact-at-tokens") + 1] == "150000"
    assert argv[argv.index("--model-retries") + 1] == "2"
    assert argv[argv.index("--retry-backoff-seconds") + 1] == "1.0"
    from src.agent.runtime_roles.entry import parser
    parsed = parser().parse_args(argv[3:])
    assert parsed.roles_json == encoded and parsed.max_concurrent_readers == 8


def test_invalid_reader_concurrency_is_explicit(tmp_path):
    for value in (0, -1, 1.5, True):
        selected = case()
        selected["max_concurrent_readers"] = value
        with pytest.raises(ValueError, match="positive integer"):
            load(tmp_path, selected)


def test_single_model_argv_preserves_current_configuration_semantics(tmp_path):
    from src.agent.runtime_entry import parser
    selected = load(tmp_path, case("single_model"))["cases"][0]
    argv = argv_for(selected)
    assert argv[1:3] == ["-m", "src.agent.runtime_entry"]
    parsed = parser().parse_args(argv[3:])
    assert parsed.provider == selected["provider"] and parsed.model == selected["model"]
    assert parsed.images == ROOT / selected["input"]
    assert parsed.out == ROOT / selected["output"]
    assert parsed.floor_plan_images == selected["floor_plan_images"]
    assert parsed.scope == selected["scope"]
    assert parsed.output_tokens == selected["output_tokens"]
    assert parsed.model_calls == selected["limits"]["model_calls"]
    assert parsed.tool_calls == selected["limits"]["tool_calls"]
    assert parsed.tokens == selected["limits"]["tokens"]
    assert parsed.seconds == selected["limits"]["seconds"]
    assert "--roles-json" not in argv and "--max-concurrent-readers" not in argv


def test_single_model_ignores_role_only_comparison_metadata(tmp_path):
    selected = case("single_model")
    baseline = argv_for(load(tmp_path, selected)["cases"][0])
    with_metadata = copy.deepcopy(selected)
    with_metadata["max_concurrent_readers"] = 4
    assert argv_for(load(tmp_path, with_metadata)["cases"][0]) == baseline


def test_local_drawing_process_routes_only_to_single_model_drawings(tmp_path):
    from src.agent.runtime_entry import parser
    selected = case("single_model")
    selected["process_reviews"] = True
    loaded = load(tmp_path, selected)["cases"][0]
    assert parser().parse_args(argv_for(loaded)[3:]).process_reviews is True
    for changes in ({"mode": "role_division", "roles": roles()},
                    {"image_kind": "photos"}, {"process_reviews": "true"}):
        with pytest.raises(ValueError):
            load(tmp_path, {**selected, **changes})


def _connection_preflight_args(tmp_path, configured, *, name="run"):
    from src.agent.runtime_roles.entry import parser
    primary = configured["coordinator"]
    return parser().parse_args([
        "--out", str(tmp_path / name), "--run-root", str(tmp_path), "--images", str(tmp_path),
        "--provider", primary["provider"], "--model", primary["model"],
        "--roles-json", json.dumps(configured), "--seconds", "60",
    ])


@pytest.mark.parametrize("selections, expected", [
    ([("chatgpt-subscription", "gpt-6-sol")] * 3,
     [("chatgpt-subscription", "gpt-6-sol")]),
    ([("chatgpt-subscription", "gpt-6-sol"), ("chatgpt-subscription", "gpt-6-luna"),
      ("chatgpt-subscription", "gpt-6-sol")],
     [("chatgpt-subscription", "gpt-6-sol"), ("chatgpt-subscription", "gpt-6-luna")]),
    ([("glm-subscription", "glm-5.3-flash"), ("glm-subscription-anthropic", "glm-5.3-flash"),
      ("glm-subscription", "glm-5.3-flash")],
     [("glm-subscription", "glm-5.3-flash"), ("glm-subscription-anthropic", "glm-5.3-flash")]),
])
def test_execute_validates_each_connection_model_once_per_run(tmp_path, monkeypatch, selections, expected):
    from src.agent.runtime_roles import entry

    configured = {name: role(provider, model, "high")
                  for name, (provider, model) in zip(ROLE_NAMES, selections, strict=True)}
    checked = []

    def resolve(provider, *args, **kwargs):
        return SimpleNamespace(descriptor=SimpleNamespace(
            route_id=provider, model_dump=lambda **unused: {"route_id": provider}))

    async def validate(connection, model):
        checked.append((connection.descriptor.route_id, model))

    class PreflightComplete(Exception):
        pass

    def stop_after_preflight(*args, **kwargs):
        raise PreflightComplete

    monkeypatch.setattr(entry, "resolve_connection", resolve)
    monkeypatch.setattr(entry, "validate_connection_model", validate)
    monkeypatch.setattr(entry, "prepare_inputs", stop_after_preflight)
    # Fresh executions must check again; this is not an account/model cache.
    for index in range(2):
        with pytest.raises(PreflightComplete):
            asyncio.run(entry.execute(_connection_preflight_args(tmp_path, configured, name=f"run-{index}")))
        assert checked == expected * (index + 1)


def test_execute_model_validation_error_still_blocks_before_run_preparation(tmp_path, monkeypatch):
    from src.agent.runtime_roles import entry

    configured = {name: role("chatgpt-subscription", "gpt-6-sol" if name != "elevation_reader"
                            else "gpt-6-luna", "high") for name in ROLE_NAMES}
    checked = []
    connection = object()
    monkeypatch.setattr(entry, "resolve_connection", lambda *a, **k: connection)

    async def validate(selected, model):
        assert selected is connection
        checked.append(model)
        if model == "gpt-6-luna":
            raise ValueError("offline fixture: selected model is not granted")

    monkeypatch.setattr(entry, "validate_connection_model", validate)
    monkeypatch.setattr(entry, "prepare_inputs", lambda *a, **k: pytest.fail("Failed validation must stop setup"))
    args = _connection_preflight_args(tmp_path, configured)
    with pytest.raises(ValueError, match="selected model is not granted"):
        asyncio.run(entry.execute(args))
    assert checked == ["gpt-6-sol", "gpt-6-luna"]
    assert not args.out.exists()
