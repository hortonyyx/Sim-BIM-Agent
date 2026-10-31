"""Small model-facing views; immutable reader and assembly receipts stay complete."""

from __future__ import annotations

import copy
import hashlib
import json


def reader_artifact_reply(record, artifact, *, pointer=None, offset=0):
    """Present verified data without sending complete geometry audit ledgers at once.

    The registry verifies the immutable record and artifact before this view is
    built. Assembly continues to read the complete artifact from that registry.
    Pointers select JSON values, never filesystem paths.
    """
    from .trial_feedback import bounded_json_view

    document = {"record": record, "artifact": artifact}

    def encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")

    delivery = record.get("artifact") or {}

    def arguments(selected, page=0):
        value = {"task_id": record["task_id"], "pointer": selected, "offset": page}
        if delivery.get("sha256"):
            value["sha256"] = delivery["sha256"]
        return value

    def reference_for(selected, value):
        raw = encoded(value)
        result = {"json_pointer": selected, "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw), "read": {"tool": "read_role_artifact",
                  "arguments": arguments(selected)}}
        if delivery.get("sha256"):
            result["artifact_sha256"] = delivery["sha256"]
        if isinstance(value, (dict, list)):
            result["item_count"] = len(value)
        return result

    if pointer is None and offset == 0 and len(encoded(document)) <= 8_000:
        return copy.deepcopy(document)
    selected = "" if pointer is None else pointer
    result = bounded_json_view(document, pointer=selected, offset=offset,
                               reference_for=reference_for)
    if "next_offset" in result:
        result["next"] = {"tool": "read_role_artifact",
                          "arguments": arguments(selected, result.pop("next_offset"))}
    summary = record.get("summary") or {}
    result.update(task_id=record["task_id"], role_id=record["role_id"], target=record["target"],
                  reader_status=record["status"],
                  summary={key: summary[key] for key in ("partitions", "space_seeds", "openings", "orientation")
                           if key in summary},
                  unresolved_count=len((artifact or {}).get("unresolved", [])),
                  details="Complete verified data remains saved. Follow JSON pointer references and next pages; "
                          "assemble_from_readers reads the complete artifact without retranscription.")
    return result


def reader_batch_reply(value, first_submissions):
    results = []
    for record in value["results"]:
        summary = record.get("summary") or {}
        if record.get("artifact"):
            if record["role_id"] == "plan_reader":
                sentence = (f"{summary.get('space_seeds', 0)} spaces, "
                            f"{summary.get('partitions', 0)} partitions, "
                            f"{summary.get('openings', 0)} openings")
            else:
                sentence = f"{summary.get('orientation', record['target'])}: {summary.get('openings', 0)} openings"
            sentence += f"; {len(summary.get('unresolved', []))} unresolved notes."
        else:
            sentence = " ".join(str(record.get("reason") or record["status"]).split())[:400]
        artifact = record.get("artifact")
        row = {"task_id": record["task_id"], "role_id": record["role_id"],
               "target": record["target"], "status": record["status"],
               "artifact": {key: artifact[key] for key in ("path", "sha256")} if artifact else None,
               "summary": sentence,
               "first_submission_passed": first_submissions.get(record["task_id"])}
        if record.get("reused_saved_result"):
            row["reused_saved_result"] = True
        if summary.get("local_review"):
            row["local_review"] = summary["local_review"]
        results.append(row)
    reply = {"status": value["status"], "results": results,
             "details": "read_role_artifact(task_id) reads verified data; large fields provide JSON pointer references "
                        "and pages. The complete record and artifact remain saved."}
    if value.get("auto_added"):
        reply["auto_added"] = copy.deepcopy(value["auto_added"])
    return reply


def assembly_reply(value, receipt_path, *, receipt_file=None):
    """Factor identical decision guidance without discarding located evidence."""
    reply = copy.deepcopy(value)
    receipt = {"file": receipt_file or str(receipt_path),
               "sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    reply["receipt"] = receipt
    reply["reader_notes"] = {"count": len(value.get("reader_notes", [])),
        "reference": "receipt.response.reader_notes",
        "read": "read_role_artifact(task_id) includes each delivery's unresolved notes."}
    guidance = {}
    for row in reply.get("decisions", []):
        pair = {key: row[key] for key in ("reason", "action") if key in row}
        if not pair:
            continue
        identity = next((key for key, item in guidance.items() if item == pair), None)
        if identity is None:
            identity = f"g{len(guidance) + 1}"
            guidance[identity] = pair
        row["guidance"] = identity
        for key in pair:
            del row[key]
    if guidance:
        reply["decision_guidance"] = guidance
    return reply
