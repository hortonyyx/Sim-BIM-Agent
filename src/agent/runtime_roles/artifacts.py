"""Immutable reader deliveries and task identities, backed by the run store."""

from __future__ import annotations

import hashlib
import json
import re
import time

from src.agent_runtime.store import json_bytes
from src.harness_contracts import HashedBlobRef


def valid_task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("task_id must contain 1-64 letters, digits, underscores or hyphens")
    return value


class ArtifactRegistry:
    """References never accept an arbitrary filename from the model."""

    def __init__(self, store):
        self.store = store
        self.records = {}
        for path in sorted((store.directory / "tasks").glob("*/reader_record.json")):
            record = json.loads(self._read_bytes(path))
            task_id = valid_task_id(record["task_id"])
            child = self.child(task_id)
            if path.resolve() != (child.task_directory / "reader_record.json").resolve():
                raise ValueError("reader record has a different task identity")
            self.records[task_id] = record
            self._verify_record(record)
            if record.get("artifact"):
                self.read(task_id)

    def _read_bytes(self, path):
        # A continuation source records every direct file dependency without
        # acquiring an EventStore writer or changing the old run.
        if hasattr(self.store, "read_bytes"):
            return self.store.read_bytes(path.resolve().relative_to(self.store.directory.resolve()))
        return path.read_bytes()

    def _verify_record(self, record):
        reference = record.get("record_blob")
        if reference is None:
            raise ValueError("reader record has no immutable metadata receipt")
        body = {key: value for key, value in record.items() if key != "record_blob"}
        if self.store.get_bytes(HashedBlobRef.model_validate(reference)) != json_bytes(body):
            raise ValueError("reader record metadata hash mismatch")
        task = self.task(record["task_id"])
        if hashlib.sha256(json_bytes(task)).hexdigest() != record["task_sha256"]:
            raise ValueError("reader task definition changed after admission")
        for field in ("task_id", "role_id", "image", "target", "input_sha256"):
            if record[field] != task[field]:
                raise ValueError(f"reader record {field} disagrees with admitted task")

    def child(self, task_id):
        valid_task_id(task_id)
        if task_id == self.store.task_id:
            raise ValueError("reader task_id cannot equal the coordinator task_id")
        return self.store.for_task(task_id, parent_task_id=self.store.task_id)

    def admit(self, task):
        child = self.child(task["task_id"])
        path = child.task_directory / "reader_task.json"
        if path.is_file():
            if json.loads(path.read_bytes()) != task:
                raise ValueError("task_id already belongs to different input, instructions or budget; use a new task_id for rework")
        else:
            child.write_json("reader_task.json", task)
        return child

    def save(self, task, *, status, artifact=None, validation=None, runtime=None, reason=None,
             delivered_at_ns=None):
        from types import SimpleNamespace
        from .continuation import MANIFEST, imported_task
        if (self.store.directory / MANIFEST).is_file():
            imported = imported_task(SimpleNamespace(store=self.store, registry=self), task["task_id"])
            if imported and self.records[task["task_id"]]["status"] != "completed":
                raise ValueError("imported reader evidence is immutable; use a new task_id")
        child = self.admit(task)
        previous = self.records.get(task["task_id"])
        if previous and previous["status"] == "completed":
            existing = self.read(task["task_id"])
            if status != "completed" or artifact != existing or validation != previous["validation"]:
                raise ValueError("a completed reader delivery cannot be overwritten; use a new task_id")
            return previous
        record = {"task_id": task["task_id"], "role_id": task["role_id"],
                  "target": task["target"], "image": task["image"], "status": status,
                  "input_sha256": task["input_sha256"], "artifact": None,
                  "validation": validation, "reason": reason,
                  "runtime_status": runtime.get("status") if runtime else None,
                  "runtime_started_epoch": runtime.get("started_epoch") if runtime else None,
                  "runtime_elapsed_seconds": runtime.get("elapsed_seconds") if runtime else None,
                  "task_sha256": hashlib.sha256(json_bytes(task)).hexdigest()}
        if artifact is not None:
            if delivered_at_ns is not None and (type(delivered_at_ns) is not int or delivered_at_ns <= 0):
                raise ValueError("delivery ordering requires a positive original timestamp")
            record["delivered_at_ns"] = time.time_ns() if delivered_at_ns is None else delivered_at_ns
            raw = json_bytes(artifact)
            path = child.task_directory / "reader_artifact.json"
            if path.exists() and path.read_bytes() != raw:
                raise ValueError("a completed reader artifact is immutable; dispatch rework with a new task_id")
            ref = child.put_bytes(raw, "application/json")
            child.write_json("reader_artifact.json", artifact)
            # write_json uses canonical JSON, the same byte representation as the blob.
            if path.read_bytes() != raw:
                raise ValueError("reader artifact persistence changed canonical bytes")
            record["artifact"] = {"path": path.relative_to(self.store.directory).as_posix(),
                                  "sha256": ref.sha256, "blob": ref.model_dump(mode="json")}
            plan = artifact.get("plan", {})
            record["summary"] = ({"partitions": len(plan.get("partitions", [])),
                                  "space_seeds": len(plan.get("space_seeds", [])),
                                  "openings": len(plan.get("openings", []))}
                                 if task["role_id"] == "plan_reader" else
                                 {"orientation": artifact.get("orientation"), "counts": artifact.get("counts"),
                                  "openings": len(artifact.get("openings", []))})
            record["summary"]["unresolved"] = artifact.get("unresolved", [])
            review = (validation or {}).get("local_review")
            if review:
                record["summary"]["local_review"] = {key: review[key] for key in
                    ("status", "summary", "independently_verified")}
        record["record_blob"] = child.put_json(record).model_dump(mode="json")
        child.write_json("reader_record.json", record)
        self.records[task["task_id"]] = record
        return record

    def read(self, task_id, *, sha256=None, role_id=None):
        record = self.records[valid_task_id(task_id)]
        self._verify_record(record)
        if record["status"] != "completed" or not record.get("artifact"):
            raise ValueError(f"reader {task_id} has no validated artifact: {record['status']}")
        if role_id is not None and record["role_id"] != role_id:
            raise ValueError(f"reader {task_id} is not a {role_id}")
        ref = record["artifact"]
        if sha256 is not None and sha256 != ref["sha256"]:
            raise ValueError("artifact reference hash does not match its immutable delivery")
        path = (self.store.directory / ref["path"]).resolve()
        expected = self.child(task_id).task_directory / "reader_artifact.json"
        if path != expected.resolve():
            raise ValueError("artifact path does not belong to its reader task")
        raw = self._read_bytes(path)
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("reader artifact hash mismatch")
        if self.store.get_bytes(HashedBlobRef.model_validate(ref["blob"])) != raw:
            raise ValueError("reader artifact differs from its original delivery")
        return json.loads(raw)

    def task(self, task_id):
        return json.loads(self._read_bytes(self.child(task_id).task_directory / "reader_task.json"))

    def state(self):
        return [self.records[key] for key in sorted(self.records)]
