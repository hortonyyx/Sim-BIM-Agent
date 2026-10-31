"""Optional Python construction check using the same adopted source kernel.

No candidate, viewer, review or delivery is created. The full result is saved
for programmatic inspection, while printing it keeps geometry out of context.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from time import perf_counter
import uuid


@dataclass
class PlanCheck:
    source: dict | None
    report: dict
    receipt_file: str

    @property
    def geometry_ready(self):
        return self.report.get("source_geometry_ready") is True

    def __str__(self):
        # The full source and adoption audit remain in the receipt and object.
        summary = {key: value for key, value in self.report.items()
                   if key != "architectural_regularization"}
        audit = self.report.get("architectural_regularization")
        if audit is not None:
            summary["architectural_regularization"] = {
                key: value for key, value in audit.items() if key not in {"changes", "attempted_changes"}}
            summary["architectural_regularization"]["full_audit_pointer"] = "/report/architectural_regularization"
        return json.dumps({**summary, "receipt_file": self.receipt_file}, ensure_ascii=False)


def check_parametric_plan(toolkit, submitted: dict) -> PlanCheck:
    """Check an explicitly supplied plan under this run's actual geometry policy.

    Passing is not candidate admission or architectural acceptance. In
    particular, subsequent saving still enforces current budgets and bindings.
    """
    from scripts.tool_scripts.bim_agent_budget import time_status
    from scripts.tool_scripts.bim_agent_regularization import reject_source_proposal
    from src.agent.execution.source_proposal import compile_source_proposal
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal

    remaining = time_status(toolkit).get("remaining_seconds")
    if remaining is not None and remaining <= 0:
        raise ValueError("Run deadline reached; no further construction checks")
    started = perf_counter()
    # Exactly the ordinary JSON boundary; no mutable alias back to the caller.
    raw = json.dumps(submitted, ensure_ascii=False, allow_nan=False).encode("utf-8")
    plan = json.loads(raw)
    source = None
    report = {"source_geometry_ready": False}
    audit = None
    try:
        from scripts.tool_scripts.bim_agent_architectural import prepare_parametric_plan
        draft_folder = Path(toolkit.run) / "sdk" / "checks"
        draft_folder.mkdir(parents=True, exist_ok=True)
        draft_path = draft_folder / f"draft_{uuid.uuid4().hex}.json"
        with draft_path.open("xb") as stream:
            stream.write(raw)
        plan, regularization = prepare_parametric_plan(toolkit, plan, draft_path=draft_path)
        if regularization is not None:
            audit = json.loads((Path(toolkit.run) / regularization["audit"]["file"]).read_bytes())
        proposal = expand_parametric_proposal(plan)
        if "mesh_frame" in proposal:
            from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
            frame = validate_mesh_frame(proposal["mesh_frame"])
            if frame["mesh_sha256"] != toolkit.manifest.get("mesh_input", {}).get("sha256"):
                raise ValueError("candidate mesh_frame must refer to the admitted original mesh")
        rejection = reject_source_proposal(toolkit, proposal, architectural_regularization=regularization)
        source, report = compile_source_proposal(proposal,
            vertical_continuity_policy=toolkit.manifest.get("vertical_continuity_policy"))
        if rejection is not None:
            report.update(source_geometry_ready=False, run_admission_rejection=rejection)
        if source is not None:
            from collections import defaultdict
            from scripts.tool_scripts.bim_agent_inference import _door_fact, door_width_summary
            connections = defaultdict(list)
            for row in source.get("connections", []):
                if row.get("opening_id"):
                    connections[row["opening_id"]].append(row)
            boundaries = {row["id"]: row for row in source["boundaries"]}
            facts = [_door_fact(row, boundaries, connections) for row in source["openings"]
                     if row.get("kind") == "door"]
            report["door_geometry"] = door_width_summary(facts)
    except (ValueError, TypeError, KeyError) as error:
        report.update(status="error", source_geometry_ready=False,
                      error=f"{type(error).__name__}: {error}")
        if hasattr(error, "audit"):
            audit = error.audit
    if audit is not None:
        report["architectural_regularization"] = audit
    report.update(check_only=True, candidate_created=False, architectural_quality="not_evaluated",
                  plan_sha256=hashlib.sha256(raw).hexdigest(), elapsed_seconds=perf_counter() - started,
                  meaning="Construction facts only. Build and review the intended final plan before finish.")
    folder = Path(toolkit.run) / "sdk" / "checks"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"check_{uuid.uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"plan": submitted, "source": source, "report": report,
                   "model_calls": 0}, stream, ensure_ascii=False, allow_nan=False)
    toolkit.log("check_parametric_plan", {"check_file": path.relative_to(toolkit.run).as_posix(),
        "check_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_geometry_ready": report["source_geometry_ready"]})
    return PlanCheck(source, copy.deepcopy(report), str(path))
