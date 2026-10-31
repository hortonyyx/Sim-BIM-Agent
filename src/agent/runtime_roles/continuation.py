"""Import verified reader evidence into a new run, without replaying its journal."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from pydantic import TypeAdapter

from src.agent_runtime.store import json_bytes
from src.harness_contracts import HashedBlobRef
from src.harness_contracts.events import CapturedValue

from .artifacts import ArtifactRegistry
from .elevation import validate_elevation_artifact
from .plan_reading import product_identity
from .submission import parse_target


MANIFEST = "reader_continuation.json"
PENDING = "reader_continuation.pending.json"
SCHEMA = "reader-continuation.v1"
COMPATIBLE_DOMAIN_PAIRS = {("domain-v67-20261012", "domain-v68-20261012")}
COMPATIBLE_CHANGES = frozenset({
    "src/agent/runtime_roles/entry.py", "src/agent/runtime_roles/session.py",
    "src/agent/runtime_roles/artifacts.py", "src/agent/runtime_roles/continuation.py",
    "src/agent/runtime_configuration.py",
    "src/agent/version_fingerprints.py",
})
NEW_COMPATIBLE_FILES = frozenset({"src/agent/runtime_roles/continuation.py"})


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _resolve_selection(source, selection):
    return source.resolve(TypeAdapter(CapturedValue).validate_json(json_bytes(selection["raw_result"])))


def verify_domain_compatibility(source, root):
    """Accept exact releases or the reviewed reader-only migration, never kernel drift."""
    from src.agent_runtime.agent_registry import discover_files, load_agent_registry, _registry_path

    root = Path(root).resolve()
    registry_path = _registry_path(root, None)
    registry = load_agent_registry(root, registry_path)
    versions = source.verified_versions()
    stamp = versions.get("domain_version") or {}
    old, current = stamp.get("identifier"), registry["current_domain_version"]
    if versions.get("mode") != "role_division" or old not in registry["domain_versions"]:
        raise ValueError("reader continuation requires a registered source role-division domain")
    registered = registry["domain_versions"][old]
    evidence = (stamp.get("evidence") or {}).get("blob")
    if evidence is None or json.loads(source.get_bytes(HashedBlobRef.model_validate(evidence))) != {
            "version_id": old, **registered}:
        raise ValueError("source domain evidence differs from its immutable registered release")
    if old != current and (old, current) not in COMPATIBLE_DOMAIN_PAIRS:
        raise ValueError("source and destination domains have no explicit reader-evidence compatibility")
    actual = discover_files(root, "domain", scope=registry["scope"], registry_path=registry_path)
    current_record = registry["domain_versions"][current]
    if actual != current_record["files"]:
        raise ValueError("destination domain files differ from the current registered release")
    previous = registered["files"]
    changed = sorted(key for key in set(previous) | set(actual) if previous.get(key) != actual.get(key))
    added, removed = set(actual) - set(previous), set(previous) - set(actual)
    if ((old == current and changed) or removed or added - NEW_COMPATIBLE_FILES
            or set(changed) - COMPATIBLE_CHANGES):
        raise ValueError("reader continuation would cross unreviewed domain or geometry changes: " + ", ".join(changed))
    return {"source_domain": old, "destination_domain": current, "changed_files": changed,
            "rule": "identical_release" if old == current else "reviewed_reader_continuation_only"}


def _relative(source, path):
    path = Path(path).resolve()
    if not path.is_relative_to(source.directory.resolve()) or not path.is_file():
        raise ValueError("reader continuation evidence escapes its source run or is missing")
    return path.relative_to(source.directory.resolve()).as_posix()


def _read(source, path):
    return source.read_bytes(_relative(source, path))


def _manifest(session):
    path = session.store.directory / MANIFEST
    if not path.is_file():
        if (session.store.directory / PENDING).exists():
            raise ValueError("reader continuation import is incomplete; use a fresh destination")
        return None
    read = (lambda item: _read(session.store, item)) if hasattr(session.store, "read_bytes") else lambda item: item.read_bytes()
    raw_manifest = read(path)
    value = json.loads(raw_manifest)
    ref = value.get("manifest_blob")
    body = {key: item for key, item in value.items() if key != "manifest_blob"}
    if (body.get("schema_version") != SCHEMA or ref is None
            or session.store.get_bytes(HashedBlobRef.model_validate(ref)) != json_bytes(body)):
        raise ValueError("reader continuation manifest hash mismatch")
    if hasattr(session.store, "read_bytes"):
        # A source run's self-addressed file alone is not authority: bind it to
        # the approved journal's actual root request version/route evidence.
        versions = session.store.verified_versions()
        route_ref = ((versions.get("model_route") or {}).get("evidence") or {}).get("blob")
        if route_ref is None:
            raise ValueError("imported reader manifest has no durable root route anchor")
        route = json.loads(session.store.get_bytes(HashedBlobRef.model_validate(route_ref)))
        if (route.get("reader_continuation") or {}).get("manifest_sha256") != _sha(raw_manifest):
            raise ValueError("imported reader manifest differs from its durable root route anchor")
    for relative, expected in body["files"].items():
        item = (session.store.directory / relative).resolve()
        if (not item.is_relative_to(session.store.directory.resolve()) or not item.is_file()
                or _sha(read(item)) != expected):
            raise ValueError("imported reader evidence changed: " + relative)
    return body


def imported_draft_selection(session, task_id):
    """Imported evidence is not a synthetic tool execution in the new journal."""
    manifest = _manifest(session)
    if manifest is None:
        return None
    value = manifest["drafts"].get(task_id)
    if value is None:
        return None
    if session.registry.child(task_id).events:
        raise ValueError("imported evidence task cannot acquire new execution history; use a new task_id")
    return copy.deepcopy(value["selection"])


def imported_task(session, task_id):
    manifest = _manifest(session)
    return manifest is not None and task_id in manifest["imported_ids"]


def _check_images(source, session):
    previous = source.read_json("bim/inputs.json")
    images = previous.get("images")
    if not isinstance(images, dict) or images != session.manifest.get("images"):
        raise ValueError("reader continuation requires identical admitted image names, hashes and dimensions")
    if (previous.get("image_kind") != "drawings" or session.manifest.get("image_kind") != "drawings"
            or previous.get("floor_plan_images") != session.manifest.get("floor_plan_images")):
        raise ValueError("reader continuation drawing and floor scope changed")
    for name, metadata in images.items():
        if Path(name).name != name:
            raise ValueError("reader continuation image must be an admitted filename")
        raw = source.read_bytes("bim/images/" + name)
        target = session.run_directory / "images" / name
        if _sha(raw) != metadata["sha256"] or target.read_bytes() != raw:
            raise ValueError("reader continuation original image changed")
        with Image.open(target) as image:
            if list(image.size) != metadata["size"]:
                raise ValueError("reader continuation image dimensions changed")
    return images


def _task_scope(task, record, images):
    image = task["image"]
    if image not in images or task["input_sha256"] != images[image]["sha256"]:
        raise ValueError("reader continuation task does not bind the admitted original image")
    if record["role_id"] not in {"plan_reader", "elevation_reader"}:
        raise ValueError("unsupported imported reader role")
    parse_target(record["role_id"], record["target"])


def _validate_elevation(artifact, task, validation, image_size):
    from .plan_review import box

    if not validation or validation.get("validation_passed") is not True:
        raise ValueError("imported elevation lacks accepted validation")
    normalized = validate_elevation_artifact(artifact, image_name=task["image"])
    if normalized != artifact:
        raise ValueError("current elevation contract would change the imported delivery")
    orientation, floors = parse_target("elevation_reader", task["target"])
    actual_floors = {row["floor_id"] for key in ("openings", "elevations", "counts")
                     for row in artifact[key] if row.get("floor_id") is not None}
    if (artifact["orientation"] != orientation or (floors and
            (not actual_floors <= floors or {row["floor_id"] for row in artifact["counts"]} != floors))):
        raise ValueError("imported elevation differs from its admitted target")
    for row in artifact["openings"] + artifact["elevations"]:
        box(row["bbox"], image_size)


def _submitted_delivery(source, child, task, artifact, validation, imported_manifest):
    """A completed record must also have its explicit accepted submission."""
    from .submission import _bytes as submission_bytes
    raw = _read(source, child.task_directory / "bim/reader_submission.json")
    receipt = json.loads(raw)
    digest = _sha(submission_bytes(artifact))
    if (any(receipt.get(key) != task[key] for key in ("role_id", "image", "target"))
            or receipt.get("artifact") != artifact or receipt.get("artifact_sha256") != digest
            or receipt.get("validation") != validation):
        raise ValueError("completed reader does not match its accepted submission")
    tool_name = "submit_elevation_reading" if task["role_id"] == "elevation_reader" else "submit_plan_reading"
    for event in reversed(child.events):
        payload = event.payload
        if (payload.event_type != "tool_execution" or payload.tool_name != tool_name
                or payload.outcome != "succeeded"):
            continue
        result = source.resolve(payload.raw_result)
        result = result.get("structuredContent", result) if isinstance(result, dict) else {}
        if result.get("status") == "accepted" and result.get("artifact_sha256") == digest:
            return raw, {"sha256": _sha(raw), "artifact_sha256": digest, "origin": "native_submission",
                         "origin_run_id": source.run_id, "origin_event_id": event.event_id,
                         "accepted_result": result}
    inherited = (imported_manifest or {}).get("deliveries", {}).get(task["task_id"])
    if inherited is not None and not child.events:
        proof = inherited["submission"]
        if (proof["sha256"] != _sha(raw) or proof["artifact_sha256"] != digest
                or proof["accepted_result"].get("status") != "accepted"
                or proof["accepted_result"].get("artifact_sha256") != digest):
            raise ValueError("imported submission proof differs from its accepted delivery")
        return raw, {**proof, "origin": "imported_submission"}
    raise ValueError("completed reader has no durable explicit accepted submission event")


def _plan_delivery(source, registry, task, record, imported_manifest):
    """Keep formal plan submission separate from passed, unsubmitted geometry."""
    from .session import RoleSession
    from .submission import ReaderSubmission
    child = registry.child(task["task_id"])
    inherited = (imported_manifest or {}).get("deliveries", {}).get(task["task_id"])
    selection = inherited.get("trial_selection") if inherited and not child.events else None
    for event in reversed(child.events):
        if event.payload.event_type != "tool_execution" or event.payload.tool_name != "trial_plan_bim":
            continue
        # Existing selection logic checks the entire saved receipt identity.
        one = SimpleNamespace(events=[event], resolve=child.resolve)
        scope = SimpleNamespace(store=source, registry=SimpleNamespace(child=lambda _: one))
        candidate = RoleSession._recoverable_plan_trial_event(scope, task["task_id"])
        if (candidate and candidate.get("draft_status") == "passed"
                and candidate.get("receipt_file") == record["validation"].get("receipt_file")
                and candidate.get("plan_sha256") == record["validation"].get("plan_sha256")):
            selection = candidate
            break
    if selection is None:
        raise ValueError("completed plan has no verified event-selected successful trial")
    _track_trial_reads(source, child.task_directory / "bim/trial_workspace", task["image"])
    prior, receipt = RoleSession._verified_recovery_trial(
        SimpleNamespace(store=source, registry=registry), task["task_id"], selection, task["input_sha256"])
    original_image = child.task_directory / "bim/images" / task["image"]
    if original_image.is_file() and _sha(_read(source, original_image)) != task["input_sha256"]:
        raise ValueError("submitted plan original image differs from its admitted input")
    formal = ReaderSubmission(role_id="plan_reader", image_name=task["image"], trial=prior,
                              directory=child.task_directory / "bim", target=task["target"]).read()
    if formal is None or formal["validation"] != record["validation"]:
        raise ValueError("completed plan review differs from its accepted validation")
    if prior.load_plan(receipt).get("floor_id") != task["target"]:
        raise ValueError("completed plan differs from its admitted floor")
    files = {path.resolve().relative_to(prior.workspace.resolve()).as_posix(): _read(source, path)
             for path in _draft_files(prior, receipt)}
    return selection, _resolve_selection(source, selection), files


def _draft_files(prior, receipt):
    paths = [prior.workspace / "inputs.json", prior.workspace / "images" / prior.image_name]
    for field in ("receipt_file", "input_plan_file", "compiled_numeric_plan_file",
                  "aligned_numeric_input_file", "unaligned_numeric_input_file"):
        relative = receipt.get(field)
        if relative is not None:
            paths.append(prior.workspace / relative)
    if receipt.get("status") == "passed":
        product_file, _ = product_identity(receipt)
        paths.append(prior.workspace / product_file)
    paths.extend(prior.workspace / row["file"] for row in receipt.get("measurement_profiles", []))
    paths.extend(prior.workspace / row["file"] for row in receipt.get("returned_images", []))
    for path in paths:
        if not path.resolve().is_relative_to(prior.workspace.resolve()):
            raise ValueError("imported trial dependency escapes its workspace")
    return paths


def _track_trial_reads(source, workspace, image_name):
    """PlanTrial loads its receipt history; track its narrow dependency set first."""
    prior = SimpleNamespace(workspace=workspace, image_name=image_name)
    for path in sorted((workspace / "trial_receipts").glob("trial_[0-9][0-9][0-9].json")):
        receipt = json.loads(_read(source, path))
        for dependency in _draft_files(prior, receipt):
            _read(source, dependency)


def import_reader_evidence(source, session):
    """Validate before mutation; import immutable deliveries and non-submitted drafts.

    A complete matching import is idempotent. A partial import or an unrelated
    destination is rejected, never guessed/repaired into a successful import.
    """
    from .session import RoleSession

    if source.directory.resolve() == session.store.directory.resolve():
        raise ValueError("reader continuation requires a new run directory")
    compatibility = verify_domain_compatibility(source, session.root)
    images = _check_images(source, session)
    source_identity = {"directory": str(source.directory.resolve()), "run_id": source.run_id,
                       "journal_sha256": _sha(source.read_bytes("events.jsonl")),
                       "versions_sha256": _sha(source.read_bytes("versions.json"))}
    existing = _manifest(session)
    if existing is not None:
        if existing["source"] != source_identity or existing["compatibility"] != compatibility:
            raise ValueError("destination already imported a different reader-evidence source")
        for task_id in existing["imported_ids"]:
            session.registry._verify_record(session.registry.records[task_id])
        source.verify_unchanged()
        return copy.deepcopy(existing["summary"])
    if (session.registry.records or any((session.store.directory / "tasks").glob("*/reader_task.json"))
            or session.store.all_events):
        raise ValueError("reader continuation requires an empty destination before its first event")
    registry = ArtifactRegistry(source)
    imported_manifest = _manifest(SimpleNamespace(store=source))
    # Use the existing event selector and verifier against a strictly read-only
    # store; no old EventStore, lock, checkpoint or budget projection is created.
    prior_session = SimpleNamespace(registry=registry, store=source)
    pending, skipped = [], []
    for task_id, record in registry.records.items():
        task = registry.task(task_id)
        _task_scope(task, record, images)
        task_file = registry.child(task_id).task_directory / "reader_task.json"
        record_file = registry.child(task_id).task_directory / "reader_record.json"
        proof = {"task_id": task_id, "task_sha256": _sha(_read(source, task_file)),
                 "record_sha256": _sha(_read(source, record_file)), "source_record": record}
        if record["status"] == "completed":
            artifact = registry.read(task_id)
            if record["role_id"] == "elevation_reader":
                _validate_elevation(artifact, task, record.get("validation"), images[task["image"]]["size"])
            artifact_raw = _read(source, source.directory / record["artifact"]["path"])
            proof["artifact_sha256"] = _sha(artifact_raw)
            submission_raw, proof["submission"] = _submitted_delivery(source, registry.child(task_id), task,
                                                artifact, record["validation"], imported_manifest)
            row = {"kind": "completed", "task": task, "artifact": artifact,
                   "validation": record["validation"], "proof": proof, "files": {}, "submission_raw": submission_raw}
            if type(record.get("delivered_at_ns")) is not int or record["delivered_at_ns"] <= 0:
                raise ValueError("completed reader has no durable original delivery ordering")
            if record["role_id"] == "plan_reader":
                row["selection"], row["raw_result"], row["files"] = _plan_delivery(
                    source, registry, task, record, imported_manifest)
            pending.append(row)
        elif record["role_id"] == "plan_reader" and record["status"] != "completed":
            selection = RoleSession._recoverable_plan_trial_event(prior_session, task_id)
            if not selection or selection.get("status") != "available" or selection.get("draft_status") != "passed":
                skipped.append({"task_id": task_id, "reason": "no latest known passed unsubmitted draft",
                                "latest_trial": None if selection is None else {
                                    key: selection[key] for key in ("status", "draft_status", "event_id", "reason")
                                    if key in selection}})
                continue
            _track_trial_reads(source, registry.child(task_id).task_directory / "bim/trial_workspace", task["image"])
            prior, receipt = RoleSession._verified_recovery_trial(prior_session, task_id, selection, task["input_sha256"])
            files = {path.resolve().relative_to(prior.workspace.resolve()).as_posix(): _read(source, path)
                     for path in _draft_files(prior, receipt)}
            event = next((row for row in registry.child(task_id).events if row.event_id == selection["event_id"]), None)
            if event is not None:
                proof["event"] = event.model_dump(mode="json")
            elif not imported_manifest or task_id not in imported_manifest["drafts"]:
                raise ValueError("draft has neither native execution nor a verified imported source")
            else:
                proof["source_manifest_sha256"] = _sha(source.read_bytes(MANIFEST))
            raw_result = _resolve_selection(source, selection)
            pending.append({"kind": "draft", "task": task, "selection": selection,
                            "raw_result": raw_result, "proof": proof, "files": files})
        else:
            skipped.append({"task_id": task_id, "reason": "reader is outside supported completed-elevation/passed-draft import"})
    completed_scopes = {(row["task"]["role_id"], row["task"]["image"], row["task"]["target"])
                        for row in pending if row["kind"] == "completed"}
    active = []
    for row in pending:
        scope = tuple(row["task"][key] for key in ("role_id", "image", "target"))
        if row["kind"] == "draft" and scope in completed_scopes:
            skipped.append({"task_id": row["task"]["task_id"],
                            "reason": "superseded by a verified completed delivery for this same input and target"})
        else:
            active.append(row)
    pending = active
    selected_ids = {row["task"]["task_id"] for row in pending}
    # Preserve admitted ancestry as metadata only, so scoped lineage stays
    # resolvable without importing an earlier failed execution or old messages.
    for row in pending:
        previous = row["task"].get("previous_task_id")
        if previous and previous not in selected_ids:
            if previous not in registry.records:
                raise ValueError("selected reader has no verified ancestor record: " + previous)
            task = registry.task(previous)
            record = registry.records[previous]
            _task_scope(task, record, images)
            if any(task[key] != row["task"][key] for key in ("role_id", "image", "target", "input_sha256")):
                raise ValueError("imported reader ancestry changes role, image or target")
            child = registry.child(previous)
            proof = {"task_id": previous, "source_record": record,
                     "task_sha256": _sha(_read(source, child.task_directory / "reader_task.json")),
                     "record_sha256": _sha(_read(source, child.task_directory / "reader_record.json"))}
            pending.append({"kind": "ancestor", "task": task, "proof": proof})
            selected_ids.add(previous)
    # Detect a cycle even if every node was already selected before this walk.
    for row in pending:
        seen, task = set(), row["task"]
        if task["task_id"] == session.store.task_id:
            raise ValueError("source reader identity collides with the destination coordinator")
        while task.get("previous_task_id"):
            if task["task_id"] in seen:
                raise ValueError("imported reader ancestry is cyclic")
            seen.add(task["task_id"])
            previous = registry.task(task["previous_task_id"])
            if any(previous[key] != task[key] for key in ("role_id", "image", "target", "input_sha256")):
                raise ValueError("imported reader ancestry changes role, image or target")
            task = previous
    source.verify_unchanged()
    session.store.write_json(PENDING, {"schema_version": SCHEMA, "source": source_identity})
    hashes, drafts, deliveries, completed = {}, {}, {}, []

    def save_bytes(relative, raw):
        target = (session.store.directory / relative).resolve()
        if not target.is_relative_to(session.store.directory.resolve()) or target.exists():
            raise ValueError("reader continuation destination collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[target.relative_to(session.store.directory).as_posix()] = _sha(raw)

    for row in pending:
        task, proof = row["task"], row["proof"]
        task_id = task["task_id"]
        child = session.registry.admit(task)
        proof_path = f"reader_evidence/{task_id}/source.json"
        save_bytes(proof_path, json_bytes(proof))
        for relative, raw in row.get("files", {}).items():
            destination = child.task_directory / "bim/trial_workspace" / relative
            save_bytes(destination.relative_to(session.store.directory).as_posix(), raw)
        selection = None
        if "selection" in row:
            selection = copy.deepcopy(row["selection"])
            selection["raw_result"] = session.store.capture(row["raw_result"]).model_dump(mode="json")
            selection["imported_from"] = {**source_identity, "task_id": task_id,
                                          "source_event_id": selection["event_id"], "proof": proof_path}
        if row["kind"] == "completed":
            save_bytes((child.task_directory / "bim/reader_submission.json").relative_to(
                session.store.directory).as_posix(), row["submission_raw"])
            session.registry.save(task, status="completed", artifact=row["artifact"], validation=row["validation"],
                                  reason="imported verified delivery; no new model execution",
                                  delivered_at_ns=proof["source_record"]["delivered_at_ns"])
            deliveries[task_id] = {"submission": proof["submission"], "trial_selection": selection}
            completed.append(task_id)
        elif row["kind"] == "draft":
            drafts[task_id] = {"selection": selection, "submission_accepted": False}
            session.registry.save(task, status="failed", reason="imported passed draft requires a new task and explicit submission")
        else:
            session.registry.save(task, status="failed", reason="imported ancestry metadata only; no editable draft or accepted delivery")
        for name in ("reader_task.json", "reader_record.json", "reader_artifact.json"):
            path = child.task_directory / name
            if path.is_file():
                hashes[path.relative_to(session.store.directory).as_posix()] = _sha(path.read_bytes())
    source.verify_unchanged()
    summary = {"completed_ids": completed, "editable_drafts": [
        {"task_id": key, "image": session.registry.records[key]["image"], "target": session.registry.records[key]["target"],
         "status": "passed", "submission_accepted": False,
         "plan_sha256": value["selection"]["plan_sha256"]} for key, value in drafts.items()],
        "skipped": skipped, "source": source_identity, "manifest": MANIFEST,
        "instruction": "Reuse imported completed deliveries. For each imported passed draft, use a NEW task_id with previous_task_id and explicit rework_targets; read, retrial, then explicitly submit. This is a continuation, not a cold start."}
    body = {"schema_version": SCHEMA, "source": source_identity, "compatibility": compatibility,
            "images": images, "imported_ids": sorted(selected_ids), "drafts": drafts, "deliveries": deliveries, "files": hashes,
            "source_files": source.evidence_hashes, "summary": summary}
    session.store.write_json(MANIFEST, {**body, "manifest_blob": session.store.put_json(body).model_dump(mode="json")})
    (session.store.directory / PENDING).unlink()
    _manifest(session)
    return copy.deepcopy(summary)
