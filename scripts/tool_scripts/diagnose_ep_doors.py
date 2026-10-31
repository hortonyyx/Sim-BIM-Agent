"""Exercise the EP door branch with frozen sm24 source BIM and explicit physics.

No model or GT calls. This is historical assisted source geometry, not a new
reading or a claim of drawing fidelity. Inputs and all outputs use a new run.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SOURCE = ROOT/"AI_agent/logs/experiments/2026-09-09_source_bim_run04/sm24_assisted/source_model.json"
TEMPLATE = ROOT/"AI_agent/logs/experiments/2026-09-09_ep_branch_sm21/inputs/physics.idf"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def prepare_physics(source, out):
    """Diagnostic uniform assumptions, distinct from collaborator deliverables."""
    from eppy.modeleditor import IDF
    from src.agent._share import ensure_schema_initialized

    ensure_schema_initialized()
    idf = IDF(str(TEMPLATE))
    keys = ("PEOPLE", "LIGHTS", "HVACTEMPLATE:THERMOSTAT", "HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM")
    prototypes = {key: idf.idfobjects[key][0] for key in keys}
    for key in keys:
        for obj in list(idf.idfobjects[key]):
            idf.removeidfobject(obj)
    bindings = {}
    for space in source["spaces"]:
        zone = f"EP_Zone_{len(bindings)+1:03d}"
        bindings[space["id"]] = zone
        people = idf.copyidfobject(prototypes["PEOPLE"])
        people.Name = f"{zone}_People"
        people.Zone_or_ZoneList_or_Space_or_SpaceList_Name = zone
        people.Number_of_People_Calculation_Method = "People/Area"
        people.Number_of_People = ""
        people.People_per_Floor_Area = .1
        people.Floor_Area_per_Person = ""
        lights = idf.copyidfobject(prototypes["LIGHTS"])
        lights.Name = f"{zone}_Lights"
        lights.Zone_or_ZoneList_or_Space_or_SpaceList_Name = zone
        thermostat = idf.copyidfobject(prototypes["HVACTEMPLATE:THERMOSTAT"])
        thermostat.Name = f"{zone}_Thermostat"
        system = idf.copyidfobject(prototypes["HVACTEMPLATE:ZONE:IDEALLOADSAIRSYSTEM"])
        system.Zone_Name = zone
        system.Template_Thermostat_Name = thermostat.Name
    idf.newidfobject("MATERIAL:NOMASS", Name="Diagnostic_Door_Resistance", Roughness="MediumSmooth",
                     Thermal_Resistance=.25, Thermal_Absorptance=.9, Solar_Absorptance=.7, Visible_Absorptance=.7)
    idf.newidfobject("CONSTRUCTION", Name="Diagnostic_Door", Outside_Layer="Diagnostic_Door_Resistance")
    idf.idfobjects["BUILDING"][0].Name = "sm24_source_bim_door_branch_diagnostic"
    idf.saveas(str(out/"physics.idf"))
    write_json(out/"zone_bindings.json", bindings)
    policy = {o["id"]: {"state": "closed", "construction": "Diagnostic_Door",
                        "reason": "本次后端连通实验假设该门关闭；源模型开闭状态仍为 unknown，不代表观测。"}
              for o in source["openings"] if o["kind"] == "door"}
    write_json(out/"opening_policy.json", policy)


def run(out, with_ep):
    from src.agent.execution.ep_branch import export_ep_branch

    out = out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    inputs = out/"inputs"
    inputs.mkdir()
    raw = SOURCE.read_bytes()
    source = json.loads(raw)
    (inputs/"source_model.json").write_bytes(raw)
    prepare_physics(source, inputs)
    provenance = {
        "source": str(SOURCE.relative_to(ROOT)), "source_file_sha256": sha256(raw).hexdigest(),
        "physics_base": str(TEMPLATE.relative_to(ROOT)), "physics_base_sha256": sha256(TEMPLATE.read_bytes()).hexdigest(),
        "source_mode": "historical assisted sm24: eight spaces and one manually transcribed door; known partition differences remain",
        "physics_mode": "diagnostic assumptions, not collaborator delivery or calibrated physics",
        "assumptions": ["uniform 0.1 people/m2 and 10 W/m2 lights across all source spaces",
                        "one ideal-loads system per source space; schedules and base constructions reused from sm21",
                        "opaque massless door resistance 0.25 m2 K/W; thermal/solar/visible absorptance 0.9/0.7/0.7",
                        "unknown door state explicitly assumed closed for this simulation only"],
    }
    write_json(inputs/"provenance.json", provenance)
    blocked = export_ep_branch(inputs/"source_model.json", inputs/"physics.idf", inputs/"zone_bindings.json", out/"without_policy")
    assert blocked["status"] == "failed" and blocked["simulation"]["status"] == "not_run", blocked
    argv = [sys.executable, str(ROOT/"scripts/tool_scripts/run_stage.py"), "backend-ep",
            "--source", str(inputs/"source_model.json"), "--physics-template", str(inputs/"physics.idf"),
            "--zone-bindings", str(inputs/"zone_bindings.json"), "--opening-policy", str(inputs/"opening_policy.json"),
            "--out", str(out/"ep")]
    if with_ep:
        argv += ["--with-ep", "--epw", str(ROOT/"data/weather/Shenzhen.epw")]
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    (out/"stdout.txt").write_text(result.stdout+result.stderr)
    report = {"command": argv, "returncode": result.returncode, "model_calls": 0,
              "source_file_unchanged": SOURCE.read_bytes() == raw == (inputs/"source_model.json").read_bytes(),
              "without_policy": blocked, "source_fidelity": "known historical differences; not newly evaluated",
              "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "implementation_sha256": {p: sha256((ROOT/p).read_bytes()).hexdigest() for p in
                  ["src/agent/execution/ep_branch.py", "src/agent/execution/ep_openings.py", "scripts/tool_scripts/run_stage.py", "scripts/tool_scripts/diagnose_ep_doors.py"]}}
    write_json(out/"report.json", report)
    if result.returncode:
        raise RuntimeError(f"EP branch failed; inspect {out/'stdout.txt'}")
    assert report["source_file_unchanged"]
    print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--with-ep", action="store_true", help="run actual EP after deterministic checks")
    args = parser.parse_args()
    run(args.out, args.with_ep)
