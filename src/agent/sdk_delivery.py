"""Publish a byte-bound native handoff, without another model or render pass."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def publication_target(run: Path, output_directory) -> Path:
    target = Path(output_directory).resolve()
    if target == run or target.is_relative_to(run):
        raise ValueError("public output must be a new directory outside the native run")
    if target.exists():
        raise FileExistsError(f"public output already exists; choose a new directory: {target}")
    return target


def publish_finished(run: Path, candidate: str, reply, output_directory) -> dict:
    from src.agent.runtime_delivery import attest_finished_handoff
    run = Path(run).resolve()
    target = publication_target(run, output_directory)
    reply.require_ok()
    attested = attest_finished_handoff(run, reply.data, candidate)
    if attested is None:
        raise ValueError("finish is incomplete, invalid or stale; public delivery was not created")
    files = {}
    for name, expected in attested["artifact_sha256"].items():
        raw = (run / name).read_bytes()
        if _sha(raw) != expected:
            raise ValueError(f"finished artifact changed before publication: {name}")
        files[name] = raw
    files["source_model.json"] = files[f"{candidate}/source_model.json"]
    # The candidate viewer's relative report/proposal links stay usable.
    source = json.loads(files["source_model.json"])
    delivery = json.loads(files["delivery.json"])
    for projection in delivery.get("source_image_feedback", {}).get("current_source_projections", []):
        name = projection.get("overlay_image")
        if (not isinstance(name, str) or not name.startswith("image_overlays/")
                or not (run / name).resolve().is_relative_to(run / "image_overlays")
                or projection.get("source_model_sha256") != attested["source_model_sha256"]):
            raise ValueError("current projection is missing or outside the bound source/run")
        files[name] = (run / name).read_bytes()
    for name in ("proposal.json", "report.json"):
        files[f"{candidate}/{name}"] = (run / candidate / name).read_bytes()
    proposal_sha = _sha(files[f"{candidate}/proposal.json"])
    report = json.loads(files[f"{candidate}/report.json"])
    if (source.get("generation", {}).get("proposal_sha256") != proposal_sha
            or report.get("proposal_sha256") != proposal_sha
            or report.get("source_model_sha256") != attested["source_model_sha256"]):
        raise ValueError("candidate report/proposal no longer matches the finished source")
    root = Path(__file__).resolve().parents[2]
    renderer_names = ("src/agent/execution/source_proposal.py", "src/agent/geometry/source_bim.py",
                      "scripts/tool_scripts/render_geometry_viewer.py")
    publication = {
        "schema": "bim_sdk_publication_v1", "candidate": candidate,
        "source_model_sha256": attested["source_model_sha256"],
        "native_run": str(run), "finish_receipt_file": reply.receipt_file,
        "native_artifact_sha256": attested["artifact_sha256"],
        "public_files": {name: _sha(raw) for name, raw in files.items()},
        "supporting_file_scope": "Report/proposal and linked projection images are captured at publication; native_artifact_sha256 identifies the original finish-bound subset.",
        "renderer_files_sha256": {name: _sha((root / name).read_bytes()) for name in renderer_names},
        "architectural_quality": "model_review_only; independent acceptance not implied",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}-{uuid.uuid4().hex}"
    try:
        # Windows restricted tokens need the workspace's inherited ACL. Python's
        # mode-0700 tempfile directory excludes that token from its own child
        # writes. Keep ordinary inherited permissions on Windows; private mode
        # on POSIX. This changes no sandbox policy or existing directory ACL.
        staging.mkdir(mode=0o777 if os.name == "nt" else 0o700)
        for name, raw in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            if _sha(path.read_bytes()) != publication["public_files"][name]:
                raise OSError(f"published copy verification failed: {name}")
        (staging / "publication.json").write_text(
            json.dumps(publication, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # One writer per run/output. No existing user output is replaced.
        if target.exists():
            raise FileExistsError(f"public output appeared during publication: {target}")
        staging.rename(target)
    except BaseException as error:
        # Do not mask a copy failure with cleanup failure, or rerun native finish.
        # A partial private staging folder is evidence, never a public delivery.
        error.add_note(f"Native finish remains at {run}; finish receipt: {reply.receipt_file}; "
                       f"publication target: {target}; staging (possibly partial): {staging}. "
                       "Publication outcome may be partial or unknown; inspect target and staging "
                       "before retrying. Do not rerun native finish merely to copy its artifacts.")
        raise
    return {"output_directory": str(target), "source_model": str(target / "source_model.json"),
            "viewer": str(target / "delivery.html"), "publication_file": str(target / "publication.json"),
            "candidate": candidate, "source_model_sha256": attested["source_model_sha256"]}
