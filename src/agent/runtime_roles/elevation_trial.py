"""Saved elevation trials: structured readings, clean diagram and adopted overlay."""

import base64
import hashlib
import io
import json
import re

from PIL import Image

from .elevation_views import reading_views
from .reader_delivery import READER_REVIEW_SCHEMA, local_review, verify_views
from .submission import _bytes


DELIVERY_TOOL = {
    "name": "submit_elevation_reading",
    "description": "After inspecting the trial's adopted elevation diagram and original overlay, accept its exact trial_id with a short local_review.summary. Fix concrete errors by another trial first. Unresolved evidence stays in the trial. This ends the reader task without another reply.",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {"trial_id": {"type": "string", "pattern": "^elevation_[a-f0-9]{16}$"},
                       "local_review": READER_REVIEW_SCHEMA},
        "required": ["trial_id", "local_review"]},
}


def _local_checks(record):
    issues = [*record["geometry"]["issues"],
              *record["value"]["validation"].get("local_issues", []),
              *record["value"]["artifact"].get("ink_alignment", {}).get("issues", [])]
    blocking = [row for row in issues if row["type"] in {
        "adopted_projection_outside_image", "vertical_calibration_level_conflict",
        "opening_geometry_without_image_support"}]
    return {"status": "needs_correction" if blocking else "ready_for_visual_review",
            "blocking_issues": blocking, "issues": issues,
            "coverage": record["geometry"].get("coverage", {"facade_outline": "not_provided"})}


def load_trial(submission, trial_id):
    if not isinstance(trial_id, str) or not re.fullmatch(r"elevation_[a-f0-9]{16}", trial_id):
        raise ValueError("use the trial_id returned by trial_elevation_reading")
    path = submission.directory / "elevation_trials" / f"{trial_id}.json"
    if not path.is_file():
        raise ValueError("elevation trial is not saved in this task")
    record = json.loads(path.read_bytes())
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    if hashlib.sha256(_bytes(body)).hexdigest() != record["record_sha256"]:
        raise ValueError("elevation trial hash mismatch")
    value = record["value"]
    if (value["role_id"], value["image"], value["target"]) != (
            submission.role_id, submission.image_name, submission.target):
        raise ValueError("elevation trial scope changed")
    if hashlib.sha256(_bytes(value["artifact"])).hexdigest() != value["artifact_sha256"]:
        raise ValueError("elevation trial artifact hash mismatch")
    _, digest = submission._verified_image()
    if digest != value["validation"]["image_sha256"]:
        raise ValueError("elevation trial original changed")
    verify_views(submission.directory, record["views"])
    return record


def prepare_trial(submission, arguments):
    from .session import envelope
    if submission.read() is not None:
        raise ValueError("this reader already delivered; rework uses a new task_id")
    value = submission.prepare(arguments)
    trial_id = "elevation_" + hashlib.sha256(_bytes(value)).hexdigest()[:16]
    folder = submission.directory / "elevation_trials"
    folder.mkdir(exist_ok=True)
    path = folder / f"{trial_id}.json"
    if path.is_file():
        record = load_trial(submission, trial_id)
    else:
        with Image.open(submission._verified_image()[0]) as image:
            diagram, overlay, geometry = reading_views(image.convert("RGB"), value["artifact"])
        views = []
        for kind, picture in (("elevation", diagram), ("overlay", overlay)):
            stream = io.BytesIO()
            picture.save(stream, format="PNG")
            raw = stream.getvalue()
            view_path = folder / f"{trial_id}_{kind}.png"
            view_path.write_bytes(raw)
            views.append({"kind": kind, "file": view_path.relative_to(submission.directory).as_posix(),
                          "sha256": hashlib.sha256(raw).hexdigest()})
        record = {"trial_id": trial_id, "value": value, "views": views, "geometry": geometry}
        record["record_sha256"] = hashlib.sha256(_bytes(record)).hexdigest()
        path.write_bytes(_bytes(record))
    (submission.directory / "elevation_latest.json").write_bytes(_bytes({"trial_id": trial_id}))
    checks = _local_checks(record)
    result = envelope({"status": "prepared", "trial_id": trial_id,
        "artifact_sha256": value["artifact_sha256"], "counts": value["artifact"]["counts"],
        "views": record["views"], "issues": checks["issues"],
        "ready_for_delivery": not checks["blocking_issues"],
        "blocking_issues": checks["blocking_issues"], "coverage": checks["coverage"],
        "unresolved": value["artifact"]["unresolved"], "independently_verified": False,
        "next_action": ("Correct the named blocking issues and prepare another trial; inspect these views to locate the conflict."
            if checks["blocking_issues"] else
            "Inspect these adopted-value views. Revise concrete errors with trial_elevation_reading; otherwise submit this trial_id and local_review.summary.")})
    for view in record["views"]:
        raw = (submission.directory / view["file"]).read_bytes()
        result["content"].append({"type": "image", "data": base64.b64encode(raw).decode("ascii"), "mimeType": "image/png"})
    return result


def reviewed_value(submission, arguments):
    import jsonschema
    jsonschema.validate(arguments, DELIVERY_TOOL["inputSchema"])
    latest = json.loads((submission.directory / "elevation_latest.json").read_bytes())
    if arguments["trial_id"] != latest["trial_id"]:
        raise ValueError("submit the latest prepared elevation trial")
    record = load_trial(submission, arguments["trial_id"])
    checks = _local_checks(record)
    if checks["blocking_issues"]:
        raise ValueError("elevation trial still has blocking local geometry errors: " +
                         json.dumps(checks["blocking_issues"], ensure_ascii=False, separators=(",", ":")))
    value = record["value"]
    value["validation"]["local_checks"] = checks
    value["validation"]["local_review"] = local_review(arguments["local_review"],
        artifact_sha256=value["artifact_sha256"], views=record["views"],
        image_sha256=value["validation"]["image_sha256"])
    value["validation"]["trial_id"] = record["trial_id"]
    value["validation"]["trial_record_sha256"] = record["record_sha256"]
    return value
