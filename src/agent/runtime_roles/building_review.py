"""Coordinator judgment carried by finish, bound to the selected source geometry."""

import hashlib
import json

from scripts.tool_scripts.bim_agent_process_reviews import building_facts

from .reader_delivery import LOCAL_REVIEW_SCHEMA


def prepare_review(session, candidate, judgment):
    import jsonschema
    jsonschema.validate(judgment, LOCAL_REVIEW_SCHEMA)
    if not judgment["summary"].strip():
        raise ValueError("architectural_review.summary must describe the building judgment")
    source = session._source(candidate)
    path = session.run_directory / candidate / "source_model.json"
    return {"candidate": candidate, "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "status": "reviewed_by_coordinator", "summary": judgment["summary"],
            "facts": building_facts(source), "independently_verified": False}


def verify_review(session, candidate):
    path = session.run_directory / "role_architectural_review.json"
    if not path.is_file():
        raise ValueError("finish_bim needs architectural_review.summary for the selected building")
    value = json.loads(path.read_bytes())
    source = session.run_directory / candidate / "source_model.json"
    if value["candidate"] != candidate or value["source_file_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError("architectural review does not cover the selected building")
    return value


def attach_review(session, value):
    from src.agent_runtime.store import json_bytes
    root = session.run_directory
    (root / "role_architectural_review.json").write_bytes(json_bytes(value))
    report = root / "delivery.json"
    delivery = json.loads(report.read_bytes())
    delivery["architectural_review"] = value
    report.write_bytes(json_bytes(delivery))
