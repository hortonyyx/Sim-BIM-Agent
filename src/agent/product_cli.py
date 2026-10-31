"""User entry point for a verified Sim-BIM Agent installation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def doctor():
    from src.agent_runtime.agent_registry import release_records, source_commit
    records = release_records(ROOT)
    import PIL
    import shapely
    resources = ("scripts/tool_scripts/vendor/three.min.js",
                 "scripts/tool_scripts/vendor/OrbitControls.js",
                 "src/agent/data/room_types.json")
    missing = [name for name in resources if not (ROOT / name).is_file()]
    if missing:
        raise ValueError("Missing installed resources: " + ", ".join(missing))
    return {"status": "ready", "product": "Sim-BIM Agent", "directory": str(ROOT),
            "runtime_version": records["runtime"]["version_id"],
            "domain_version": records["domain"]["version_id"], "source_commit": source_commit(ROOT),
            "python": sys.executable, "python_version": sys.version.split()[0],
            "dependencies": {"pillow": PIL.__version__, "shapely": shapely.__version__},
            "model_requests": 0, "subscription_connection": "not_checked"}


def demo(output: Path):
    """Exercise the real local BIM tools with a clearly synthetic two-room plan."""
    from src.agent_runtime.run_paths import resolve_run_output
    from PIL import Image, ImageDraw
    from scripts.tool_scripts.bim_agent_guidance import REFERENCES
    from scripts.tool_scripts.run_bim_agent import Toolkit
    output = output.expanduser().resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Save demonstrations outside the product directory")
    resolve_run_output(Path(output.name), repository_root=ROOT, run_root=output.parent)
    if output.exists():
        raise ValueError("Choose a new output directory; existing results are preserved")
    images = output / "images"
    images.mkdir(parents=True)
    picture = Image.new("RGB", (12, 8), "white")
    drawing = ImageDraw.Draw(picture)
    drawing.rectangle((1, 1, 11, 7), outline="black")
    drawing.line((6, 1, 6, 7), fill="black")
    image_path = images / "plan.png"
    picture.save(image_path)
    manifest = {"images": {"plan.png": {"size": [12, 8],
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest()}},
                "provider": "offline deterministic synthetic demonstration",
                "input_mode": "synthetic_offline_demo",
                "generator": "sim-bim synthetic offline demo",
                "scope": "Synthetic two-room offline demonstration; not building reconstruction",
                "only_input": "synthetic drawing and declaration; no GT or prior run"}
    (output / "inputs.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    reference = REFERENCES["plan_partition"]
    plan = json.JSONDecoder().raw_decode(reference[reference.index("{"):])[0]
    result = Toolkit(output).build_plan("plan.png", json.dumps(plan))
    if not result.get("source_geometry_ready"):
        raise ValueError("Offline demonstration did not produce valid geometry")
    candidate = output / result["candidate"]
    source = json.loads((candidate / "source_model.json").read_bytes())
    summary = {"status": "completed", "kind": "synthetic_offline_demo", "model_requests": 0,
               "spaces": len(source["spaces"]), "openings": len(source["openings"]),
               "source_model": str(candidate / "source_model.json"),
               "viewer": str(candidate / "viewer.html"), "drawing_fidelity": "not_evaluated"}
    (output / "demo-result.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(prog="Sim-BIM Agent", description="Generate and inspect lightweight BIM.")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("doctor", help="verify installed files and dependencies without contacting a model")
    sample = commands.add_parser("demo", help="build a small synthetic BIM locally, without a model")
    sample.add_argument("--output", type=Path, required=True)
    for name, explanation in (("check", "validate a run configuration without model calls"),
                              ("run", "run an explicitly configured building task")):
        command = commands.add_parser(name, help=explanation)
        command.add_argument("configuration", type=Path)
        command.add_argument("--case", required=name == "run")
        if name == "run":
            command.add_argument("--resume", action="store_true")
    auth = commands.add_parser("auth", help="manage your own ChatGPT subscription sign-in")
    auth.add_argument("auth_action", choices=("status", "login", "models", "logout"))
    args = parser.parse_args(argv)
    if args.action is None:
        parser.print_help()
        return 0
    try:
        identity = doctor()
        if args.action == "doctor":
            result = identity
        elif args.action == "demo":
            result = demo(args.output)
        elif args.action == "auth":
            from src.agent_runtime.openai_subscription import main as subscription_main
            return subscription_main([args.auth_action])
        else:
            command = [sys.executable, "-B", "-m", "src.agent.runtime_configuration",
                       "launch" if args.action == "run" else "check", str(args.configuration.resolve())]
            if args.case:
                command += ["--case", args.case]
            if getattr(args, "resume", False):
                command.append("--resume")
            environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
                           "PYTHONDONTWRITEBYTECODE": "1"}
            return subprocess.call(command, cwd=ROOT, env=environment)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, ImportError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
