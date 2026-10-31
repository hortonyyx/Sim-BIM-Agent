"""Compose an explicit save and its requested views; never infer or approve.

Each operation keeps its ordinary native receipt. A saved candidate survives a
later projection failure: this composition is deliberately not a transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any
import uuid


@dataclass
class BuildResult:
    build_reply: Any
    requested_drawings: list[dict]
    views: list[Any] = field(default_factory=list)
    process_review: dict | None = None
    exception: dict | None = None
    receipt_file: str = ""

    @property
    def candidate(self):
        data = self.build_reply.data if self.build_reply is not None else None
        return data.get("candidate") if isinstance(data, dict) else None

    @property
    def source_geometry_ready(self):
        return bool(self.build_reply is not None and not self.build_reply.is_error
                    and isinstance(self.build_reply.data, dict)
                    and self.build_reply.data.get("source_geometry_ready") is True)

    @property
    def requested_views_complete(self):
        return bool(self.candidate and self.source_geometry_ready and not self.exception
                    and len(self.views) == len(self.requested_drawings)
                    and all(not reply.is_error for reply in self.views))

    @property
    def images(self):
        replies = ([self.build_reply] if self.build_reply is not None else []) + self.views
        seen, result = set(), []
        for reply in replies:
            for image in reply.images:
                identity = (image.get("path"), image.get("sha256"))
                if identity not in seen:
                    seen.add(identity)
                    result.append(dict(image))
        return result

    def presentation(self):
        build = self.build_reply.presentation() if self.build_reply is not None else None
        views = [reply.presentation() for reply in self.views]
        # Images are preserved once in the combined response, not repeated in
        # both nested receipts and the common list.
        for reply in ([build] if build is not None else []) + views:
            reply.pop("images", None)
        return {"candidate": self.candidate, "source_geometry_ready": self.source_geometry_ready,
                "requested_views_complete": self.requested_views_complete,
                "requested_drawings": [{"image": row["image"], "floor_id": row["floor_id"]}
                                       for row in self.requested_drawings],
                "build": build, "views": views, "images": self.images,
                "process_review": self.process_review, "exception": self.exception,
                "workflow_receipt": self.receipt_file,
                "next_action": (
                    "Inspect these current views against all relevant inputs. Submit your actual judgments "
                    "with finish_to(reviews=...) when the required scope is fulfilled; repair concrete "
                    "architectural/source problems. View generation is not review or acceptance."
                    if self.requested_views_complete else
                    "Read the failed operation. A saved candidate is retained; repair only failed views "
                    "with overlay_candidate when geometry is ready. Do not rebuild to retry a view. "
                    "An interrupted operation has an unknown outcome; inspect its receipts before retrying.")}

    def __str__(self):
        return json.dumps(self.presentation(), ensure_ascii=False, indent=2, allow_nan=False)

    def require_ready(self):
        """Require construction and requested views only, never architectural approval."""
        if not self.requested_views_complete:
            raise RuntimeError(str(self))
        return self


def _drawing_requests(session, drawings):
    from jsonschema import Draft202012Validator
    if not isinstance(drawings, (list, tuple)):
        raise ValueError("drawings must be a list of explicit image/floor calibrations")
    if not drawings:
        return []
    definition = session.describe("overlay_candidate")["inputSchema"]
    keys = {"image", "floor_id", "x_anchors", "y_anchors", "basis"}
    schema = {"type": "object", "properties": {
        key: definition["properties"][key] for key in keys},
        "required": sorted(keys), "additionalProperties": False,
        **({"$defs": definition["$defs"]} if "$defs" in definition else {})}
    # Freeze caller data before any mutation. The actual geometry/calibration
    # validators remain in the native tool; this only catches interface errors.
    rows = json.loads(json.dumps(drawings, allow_nan=False))
    seen = set()
    for index, row in enumerate(rows):
        error = next(Draft202012Validator(schema).iter_errors(row), None)
        if error:
            raise ValueError(f"drawings[{index}]: {error.message}")
        identity = (row["image"], row["floor_id"])
        if identity in seen:
            raise ValueError("declare each image/floor calibration once")
        seen.add(identity)
    return rows


def build_with_views(session, plan, *, drawings=(), mesh_observations=None):
    session._ensure_open()
    rows = _drawing_requests(session, drawings)
    folder = session.run / "sdk" / "workflows"
    if not folder.resolve().is_relative_to(session.run):
        raise ValueError("workflow artifacts must stay within this run")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"build_{uuid.uuid4().hex}.json"
    result = BuildResult(None, rows, receipt_file=str(path))
    started = time.time()
    operation = "build_parametric_bim"
    failure = None

    def recovery_note():
        native = ([result.build_reply.receipt_file] if result.build_reply is not None else [])
        native.extend(reply.receipt_file for reply in result.views)
        return (f"Saved candidate: {result.candidate}; native receipts: {native}; "
                f"workflow receipt: {path}. An interrupted operation or journal has an unknown "
                "outcome; inspect the native state. Do not blindly retry the build.")

    def save():
        record = {"schema": "bim_sdk_build_views_v1", "started_epoch": started,
                  "candidate": result.candidate, "source_geometry_ready": result.source_geometry_ready,
                  "requested_views_complete": result.requested_views_complete,
                  "requested_drawings": rows,
                  "build_receipt": result.build_reply.receipt_file if result.build_reply else None,
                  "view_receipts": [reply.receipt_file for reply in result.views],
                  "process_review": result.process_review, "exception": result.exception,
                  "atomic": False, "automatic_review": False, "automatic_retry": False}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    save()
    try:
        arguments = {"plan_json": plan}
        if mesh_observations is not None:
            arguments["mesh_observations"] = mesh_observations
        result.build_reply = session.call(operation, **arguments)
        save()
        if not result.source_geometry_ready or not result.candidate:
            return result
        for row in rows:
            operation = f"overlay_candidate:{row['image']}:{row['floor_id']}"
            result.views.append(session.call("overlay_candidate", candidate=result.candidate, **row))
            save()
        if session._server.toolkit.manifest.get("drawing_review_policy"):
            operation = "read_current_process_review"
            from scripts.tool_scripts.bim_agent_process_reviews import process_status
            result.process_review = process_status(session._server.toolkit, result.candidate)
        return result
    except BaseException as error:
        failure = error
        result.exception = {"operation": operation, "type": type(error).__name__,
                            "message": str(error), "outcome": "unknown_inspect_native_receipts_before_retry"}
        error.add_note(recovery_note())
        raise
    finally:
        try:
            save()
        except BaseException as journal_error:
            if failure is not None:
                failure.add_note(f"Workflow journal could not be updated: {journal_error}. " + recovery_note())
            else:
                journal_error.add_note(recovery_note())
                raise
