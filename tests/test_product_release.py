"""Current product entry and deterministic distribution boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PIL import Image

from scripts.release.build_product import selected_payload

ROOT = Path(__file__).resolve().parents[1]


def test_payload_keeps_runtime_resources_and_excludes_development_evidence():
    files = ["src/agent_runtime/agent_versions.json", "src/agent/data/room_types.json",
             "scripts/tool_scripts/vendor/three.min.js", "scripts/tool_scripts/affected_tests.py",
             "AI_agent/logs/old.json", "case_tests/gt.json", "tests/test_x.py", ".env",
             "product/README.md", "product/config.example.json", "product/Start.cmd", "sim_bim.py",
             "src/agent/legacy/intake/reading/guide.md", "skills/unused/guide.md"]
    selected = selected_payload(files)
    assert set(selected) == {"src/agent_runtime/agent_versions.json", "src/agent/data/room_types.json",
                             "scripts/tool_scripts/vendor/three.min.js", "README.md", "config.example.json",
                             "Start.cmd", "sim_bim.py", "src/agent/legacy/intake/reading/guide.md"}


def test_cli_help_does_not_import_old_pipeline_or_need_git():
    # argparse exits successfully for --help; check imports in a separate fresh process.
    result = subprocess.run([sys.executable, "-B", str(ROOT / "sim_bim.py"), "--help"],
                            capture_output=True, text=True, check=True)
    assert "doctor" in result.stdout and "demo" in result.stdout
    probe = subprocess.run([sys.executable, "-B", "-c",
        "import sys; import src.agent.product_cli; assert 'src.agent.legacy.graph' not in sys.modules; assert 'langgraph' not in sys.modules"],
        cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize('example', ['config.example.json', 'config.opencode-go.example.json'])
def test_example_configuration_has_explicit_external_storage(tmp_path, example):
    from src.agent.runtime_configuration import load_configuration, argv_for
    value = json.loads((ROOT / "product" / example).read_bytes())
    inputs = tmp_path / "input"
    (inputs / "my-building").mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(inputs / "my-building/plan.png")
    value["input_root"] = str(inputs)
    value["run_root"] = str(tmp_path / "runs")
    config = tmp_path / "run.json"
    config.write_text(json.dumps(value), encoding="utf-8")
    case = load_configuration(config)["cases"][0]
    command = argv_for(case)
    assert command[command.index("--images") + 1] == str(inputs / "my-building")
    assert command[command.index("--run-root") + 1] == str(tmp_path / "runs")
    if case["mode"] == "role_division":
        assert "src.agent.runtime_roles.entry" in command
        routes = json.loads(command[command.index("--roles-json") + 1])
        assert routes == case["roles"]
    else:
        assert "src.agent.runtime_entry" in command
        assert "--roles-json" not in command
    if case["limits"]["seconds"] is None:
        assert "--no-time-limit" in command and "--seconds" not in command
    else:
        assert command[command.index("--seconds") + 1] == str(case["limits"]["seconds"])
        assert "--no-time-limit" not in command
    assert "--no-token-limit" in command
    assert "--context-tokens" not in command and "--compact-at-tokens" not in command
    if "image_history_batches" in case:
        assert command[command.index("--image-history-batches") + 1] == str(case["image_history_batches"])
    else:
        assert "--image-history-batches" not in command
    assert command[command.index("--provider") + 1] == case["provider"]
    assert command[command.index("--model") + 1] == case["model"]


@pytest.mark.parametrize("input_case,expected", [
    ("json_file", "must be a directory"),
    ("empty_directory", "at least one PNG"),
    ("text_floor", "admitted input filenames"),
])
def test_product_configuration_check_rejects_unusable_image_inputs(tmp_path, input_case, expected):
    from src.agent.runtime_configuration import load_configuration

    value = json.loads((ROOT / "product/config.example.json").read_bytes())
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    value.update(input_root=str(inputs), run_root=str(tmp_path / "runs"))
    case = value["cases"][0]
    if input_case == "json_file":
        (inputs / "demo-result.json").write_text("{}", encoding="utf-8")
        case["input"] = "demo-result.json"
    else:
        target = inputs / case["input"]
        target.mkdir()
        if input_case == "text_floor":
            Image.new("RGB", (2, 2), "white").save(target / "plan.png")
            (target / "notes.txt").write_text("not an admitted image", encoding="utf-8")
            case["floor_plan_images"] = ["notes.txt"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match=expected):
        load_configuration(path)
