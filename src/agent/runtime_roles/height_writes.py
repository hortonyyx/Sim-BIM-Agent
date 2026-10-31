"""Serialize and combine role-only height writes on the current candidate."""

from __future__ import annotations

import hashlib
import json

from src.agent_runtime.store import json_bytes

from .lineage import guard_replaced_plans, latest_candidate, metadata


def match_ids(value):
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
        raise ValueError("match_id needs one identifier or a nonempty list of identifiers")
    return sorted(set(values))


def load_match(session, identity):
    if identity.startswith("elevation_match:"):
        # Old inner IDs are deterministic aliases, never a second public ID.
        choices = []
        for path in (session.store.directory / "role_matches").glob("*.json"):
            value = json.loads(path.read_bytes())
            report = value["result"]
            legacy = report.get("match_id")
            if legacy is None:
                from .elevation import _canonical_hash
                legacy = "elevation_match:" + _canonical_hash({k: v for k, v in report.items()
                                                               if k != "source_file_sha256"})[:24]
            if identity == legacy:
                choices.append(value["match_id"])
        if len(choices) != 1:
            raise ValueError(f"旧内层对位编号无法唯一对应；请使用外层 match_id，候选对应：{choices}")
        identity = choices[0]
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("match_id must be the hash returned by match_elevation")
    path = session.store.directory / "role_matches" / (identity + ".json")
    if not path.is_file():
        raise ValueError("match_id has no saved match; call match_elevation first")
    value = json.loads(path.read_bytes())
    expected = hashlib.sha256(json_bytes({key: value[key] for key in ("task_id", "candidate", "result")})).hexdigest()
    if identity != expected:
        raise ValueError("match result hash mismatch")
    session._source(value["candidate"])  # Validate the saved candidate path too.
    raw = (session.run_directory / value["candidate"] / "source_model.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != value["result"]["source_file_sha256"]:
        raise ValueError("matched source file changed; compute a new elevation match before applying")
    return value


def saved_application(session, identities):
    """Latest completed receipt for recovery; an unknown outcome stays blocked."""
    found = []
    for path in (session.store.directory / "role_operations").glob("*.json"):
        row = json.loads(path.read_bytes())
        ref = row.get("reference", {})
        previous = ref.get("match_ids", [ref["match_id"]] if "match_id" in ref else [])
        if sorted(previous) == identities:
            found.append((path.stat().st_mtime_ns, row))
    if not found:
        return None
    value = max(found, key=lambda item: item[0])[1]
    if "result" not in value:
        raise ValueError("height application outcome unknown; no repeated mutation")
    return value


async def apply_heights(session, identities, *, candidate=None):
    from scripts.tool_scripts.bim_agent_role_heights import build_role_height_batch_entry
    from .elevation import height_application
    from .height_evidence import carry_height_evidence

    values = [load_match(session, identity) for identity in match_ids(identities)]
    identities = sorted({value["match_id"] for value in values})
    saved_application(session, identities)  # Refuse any unfinished prior write.
    candidate = candidate or latest_candidate(session, values[0]["candidate"])
    guard_replaced_plans(session, candidate)
    source = session._source(candidate)
    entries, types, references, unprocessed = [], [], [], []
    for value in values:
        artifact = session.registry.read(value["task_id"], role_id="elevation_reader")
        application = height_application(artifact, value["result"], candidate, provenance={
            "source_model_sha256": source["source_model_sha256"], "source_bim": source,
            "matched_source_bim": session._source(value["candidate"])})
        location = carry_height_evidence(session, value, artifact, application["entries"])
        entries.extend(application["entries"])
        by_id = {row["id"]: row for row in artifact["openings"]}
        types.extend(by_id[row["artifact_opening_id"]]["evidence_type"] for row in value["result"]["matches"])
        references.append({"match_id": value["match_id"], "task_id": value["task_id"],
                           "artifact_sha256": session.registry.records[value["task_id"]]["artifact"]["sha256"],
                           "height_location": location})
        unprocessed.append({"task_id": value["task_id"], **application["unprocessed"]})
    batch = build_role_height_batch_entry(entries, evidence_types=types)
    by_id = {row["id"]: row for row in source["openings"]}
    unchanged = all(sorted({p[2] for p in by_id[row["claim"]["objects"][0]["id"]]["vertices"]})
                    == row["claim"]["values"]["height"]["value"] for row in entries)
    if unchanged:
        # The ordinary confirmation validates exact values and persists image
        # evidence without rewriting any source content or consuming a candidate.
        batch["entry"]["action"] = "confirm"
    operation_id = "height:" + hashlib.sha256(json_bytes({"match_ids": identities, "candidate": candidate})).hexdigest()
    result = await session._once(operation_id, "claim_transaction", {
        "candidate": candidate, "entries_json": json.dumps([batch["entry"]], ensure_ascii=False, separators=(",", ":"))},
        reference={"match_ids": identities, "readers": references})
    meta = metadata(result)
    completed = meta.get("status") == "completed"
    info = {**meta, "height_write": {"match_ids": identities, "base_candidate": candidate,
        "unchanged": unchanged and completed, "unprocessed": unprocessed,
        "message": "已是该值；保留现稿并记录依据" if unchanged and completed else "高度批次已处理"}}
    return {**result, "structuredContent": info, "content": [
        {"type": "text", "text": json.dumps(info, ensure_ascii=False, sort_keys=True)},
        *[block for block in result.get("content", []) if block.get("type") != "text"]]}
