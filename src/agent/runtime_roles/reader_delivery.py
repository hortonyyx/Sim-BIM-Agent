"""Local drawing review bound to the exact saved reader result and its views."""

import copy
import hashlib
import json
from pathlib import Path


LOCAL_REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string", "minLength": 1}},
    "required": ["summary"],
}


def local_review(review, *, artifact_sha256, views, image_sha256=None):
    import jsonschema
    jsonschema.validate(review, LOCAL_REVIEW_SCHEMA)
    if not review["summary"].strip():
        raise ValueError("local_review.summary must describe the drawing/overlay check")
    if not views:
        raise ValueError("local review requires saved result views; prepare or trial the reading first")
    return {"status": "reviewed_by_reader", "summary": review["summary"],
            "artifact_sha256": artifact_sha256, "image_sha256": image_sha256,
            "views": views, "independently_verified": False}


def verify_views(root, views):
    root = Path(root).resolve()
    for row in views:
        path = (root / row["file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("reader view is missing or outside its task")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("reader view hash mismatch")


def require_observed_trial(store, tool_name, identity_key, identity, view_hashes):
    """A review must follow a model request that actually contained these views.

    Pure validators have no model journal. RoleSession always supplies its child
    store, so real/scripted readers cannot trial and pre-approve in one response.
    Earlier observations of unchanged views remain usable after context eviction.
    """
    if store is None:
        return
    events = list(store.events)
    prepared = None
    for event in reversed(events):
        payload = event.payload
        if (payload.event_type == "tool_execution" and payload.tool_name == tool_name
                and payload.outcome == "succeeded"):
            raw = store.resolve(payload.raw_result)
            if (raw.get("structuredContent") or {}).get(identity_key) == identity:
                prepared = event
                break
    expected = set(view_hashes)
    if prepared is not None and expected:
        for event in events:
            if event.sequence > prepared.sequence and event.payload.event_type == "adapter_request":
                if expected <= {image.sent.sha256 for image in event.payload.images}:
                    return
    raise ValueError("The exact trial views have not reached a model request yet. Inspect the returned views before submitting; do not trial and approve in the same response.")


def require_observed_plan_trial(store, trial, receipt):
    """Reuse an observed 2D result only for a verified wall-attribute-only edit.

    Identical pixels alone are insufficient: input, adopted coordinates and the
    saved product must also be unchanged outside wall metadata. All current and
    prior files retain their normal integrity checks; the journal must prove the
    prior views actually reached this reader before they can be reused.
    """
    hashes = [row["sha256"] for row in receipt.get("returned_images", [])]
    try:
        require_observed_trial(store, "trial_plan_bim", "plan_sha256", receipt["plan_sha256"], hashes)
        return None
    except ValueError as unobserved:
        if trial.workspace is None or not receipt.get("reading_geometry_ready"):
            raise
        plan, checked = trial.verified_plan(receipt["plan_sha256"])
        if plan.get("reading_type") != "plan":
            raise
        verify_views(trial.workspace, checked["returned_images"])

        def without_metadata(value):
            return {key: item for key, item in value.items() if key != "wall_metadata"}

        def adopted_facts(value):
            value = copy.deepcopy(without_metadata(value))
            # This digest includes wall attributes. Retain every actual grid
            # decision and coordinate; only its derived identity may differ.
            value.get("regularization_inputs", {}).get("reading_alignment", {}).get(
                "lite_bim", {}).pop("adopted_plan_sha256", None)
            value.get('regularization_inputs', {}).get('reading_alignment', {}).get(
                'lite_bim', {}).get('wall_merging', {}).pop('plan_sha256', None)
            return value

        def product_facts(row):
            # verified_plan has already checked the product path and hash.
            value = json.loads((trial.workspace / row["reading_product_file"]).read_bytes())
            return {**value, "walls": [], "mapping": adopted_facts(value["mapping"])}

        numeric = adopted_facts(trial.numeric_plan(checked))
        product = product_facts(checked)
        for prior in reversed(trial.receipts):
            if (prior.get("status") != "passed" or not prior.get("reading_geometry_ready")
                    or prior["plan_sha256"] == checked["plan_sha256"]
                    or [row["sha256"] for row in prior.get("returned_images", [])] != hashes):
                continue
            try:
                old_plan, old_checked = trial.verified_plan(prior["plan_sha256"])
                if (without_metadata(old_plan) != without_metadata(plan)
                        or adopted_facts(trial.numeric_plan(old_checked)) != numeric
                        or product_facts(old_checked) != product):
                    continue
                verify_views(trial.workspace, old_checked["returned_images"])
                require_observed_trial(store, "trial_plan_bim", "plan_sha256", prior["plan_sha256"], hashes)
            except (ValueError, KeyError, OSError):
                continue
            return {"reason": "wall_metadata_only_with_identical_geometry_and_views",
                    "plan_sha256": checked["plan_sha256"],
                    "observed_plan_sha256": old_checked["plan_sha256"],
                    "view_sha256": hashes}
        raise unobserved


def completed_reader_handoff(engine):
    """Stop after the accepted, reviewed submission, without an extra model reply."""
    name = "submit_plan_reading" if engine.role.role_id == "plan_reader" else "submit_elevation_reading"
    event = next((e for e in reversed(engine.store.events)
                  if e.payload.event_type == "tool_execution"
                  and e.payload.repeatability != "read_only"), None)
    if event is None or event.payload.tool_name != name or event.payload.outcome != "succeeded":
        return None
    raw = engine.store.resolve(event.payload.raw_result)
    if raw.get("isError"):
        return None
    saved = engine.tools.submission.read()
    if saved is None or not saved["validation"].get("local_review"):
        return None
    reply = raw.get("structuredContent") or {}
    if reply.get("artifact_sha256") != saved["artifact_sha256"]:
        return None
    return {"origin": "domain_tool", "tool_name": name, "event_id": event.event_id,
            **engine.tools.submission.snapshot()}
