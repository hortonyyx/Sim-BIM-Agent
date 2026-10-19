"""Bind architectural adoption to frozen run policy and original declarations.

Observations and parametric drafts remain untouched.  Only the adopted copy is
expanded; its policy, audit and proposal are independently hash-bound.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(toolkit, reference):
    root = Path(toolkit.run).resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        raise ValueError("architectural regularization reference escapes this run")
    return path


def _save(toolkit, path, value):
    path = _path(toolkit, path)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
            indent=2, allow_nan=False) + "\n")
    return {"file": path.relative_to(Path(toolkit.run).resolve()).as_posix(),
            "sha256": _sha(path)}


def _read(toolkit, reference):
    path = _path(toolkit, reference["file"])
    if _sha(path) != reference["sha256"]:
        raise ValueError("architectural regularization artifact changed")
    return json.loads(path.read_bytes())


def _policy(toolkit):
    return getattr(toolkit, "manifest", {}).get("architectural_modelling_policy")


def prepare_parametric_plan(toolkit, plan, *, draft_path):
    policy = _policy(toolkit)
    if policy is None:
        return plan, None
    from src.agent.geometry.architectural_regularization import regularize_architectural_plan
    from src.agent.geometry.parametric_proposal import expand_parametric_proposal
    try:
        adopted, audit = regularize_architectural_plan(plan, policy=policy)
    except ValueError as error:
        if hasattr(error, "audit"):
            error.audit_reference = _save(toolkit,
                draft_path.with_suffix(".regularization.json"), error.audit)
        raise
    reference = {"schema_version": 1, "kind": "parametric", "policy": copy.deepcopy(policy),
        "modelling": copy.deepcopy(plan.get('modelling', {})),
        "original_sha256": _hash(plan), "adopted_sha256": _hash(adopted),
        "proposal_sha256": _hash(expand_parametric_proposal(adopted)),
        "adopted": _save(toolkit, draft_path.with_suffix(".adopted.json"), adopted),
        "audit": _save(toolkit, draft_path.with_suffix(".regularization.json"), audit)}
    return adopted, reference


def validate_build_regularization(toolkit, proposal, reference):
    if reference.get("policy") != _policy(toolkit):
        raise ValueError("architectural regularization policy changed")
    adopted = _read(toolkit, reference["adopted"])
    _read(toolkit, reference["audit"])
    if _hash(adopted) != reference["adopted_sha256"]:
        raise ValueError("architectural adopted declaration binding changed")
    if _hash(proposal) != reference["proposal_sha256"]:
        raise ValueError("proposal does not match architectural adoption")


def validate_parametric_regularization(toolkit, state):
    reference = state["architectural_regularization"]
    folder = toolkit.candidate_path(state["candidate"])
    plan = json.loads((folder / "parametric_plan.json").read_bytes())
    if reference.get("kind") != "parametric" or _hash(plan) != reference["original_sha256"]:
        raise ValueError("architectural original declaration binding changed")
    proposal = json.loads((folder / "proposal.json").read_bytes())
    validate_build_regularization(toolkit, proposal, reference)


def _inherited_modelling(toolkit, parent, proposal):
    """Carry adopted local frames through source edits without replaying old plans."""
    if parent is None:
        return {}
    from src.agent.geometry.source_image_overlay import _source_hash
    folder = toolkit.candidate_path(parent)
    source = json.loads((folder/'source_model.json').read_bytes())
    _source_hash(source)
    reference = source.get('generation', {}).get('provenance', {}).get('architectural_regularization')
    if not reference:
        return {}
    original = json.loads((folder/'proposal.json').read_bytes())
    validate_build_regularization(toolkit, original, reference)
    context = copy.deepcopy(reference.get('modelling', {}))
    # Merge groups describe the adoption already performed. They do not grant
    # permission to keep re-merging levels after a later explicit source edit.
    context.pop('vertical', None)
    if reference.get('modelling', {}).get('vertical', {}).get('datum_m') is not None:
        context['vertical'] = {'datum_m': reference['modelling']['vertical']['datum_m']}
    names = {row['name'] for row in proposal['geometry']['floors']}
    if reference['kind'] == 'parametric':
        from src.agent.geometry.parametric_proposal import expand_parametric_instances
        adopted = _read(toolkit, reference['adopted'])
        instances = expand_parametric_instances(adopted)
        for frame in context.get('frames', []):
            templates = set(frame['templates'])
            frame['templates'] = [row['id'] for row in instances
                                  if row['template'] in templates and row['id'] in names]
        intents = context.get('space_intents', {})
        context['space_intents'] = {
            f"{row['id']}:{row['id']}:{space['id']}": intents[f"{row['template']}:{space['id']}"]
            for row in instances if row['id'] in names
            for space in adopted['templates'][row['template']]['spaces']
            if f"{row['template']}:{space['id']}" in intents}
    else:
        for frame in context.get('frames', []):
            frame['templates'] = [name for name in frame['templates'] if name in names]
    if 'frames' in context:
        context['frames'] = [row for row in context['frames'] if row['templates']]
    if context.get('space_intents'):
        present = {f"{floor['name']}:{cell['id']}" for floor in proposal['geometry']['floors']
                   for cell in floor['cells']}
        context['space_intents'] = {key: value for key,value in context['space_intents'].items() if key in present}
    return context


def prepare_source_proposal(toolkit, proposal, *, parent=None):
    policy = _policy(toolkit)
    if policy is None:
        return proposal, None
    from src.agent.geometry.architectural_regularization import regularize_architectural_proposal
    folder = Path(toolkit.run) / "architectural_drafts"
    folder.mkdir(exist_ok=True)
    number = max((int(p.stem.split("_")[-1]) for p in folder.glob("draft_*.json")
                  if p.stem.split("_")[-1].isdigit()), default=0) + 1
    path = folder / f"draft_{number:03d}.json"
    original_ref = _save(toolkit, path, proposal)
    effective = copy.deepcopy(proposal)
    context = _inherited_modelling(toolkit, parent, proposal)
    context.update(copy.deepcopy(proposal.get('modelling', {})))
    if context:
        effective['modelling'] = context
    try:
        adopted, audit = regularize_architectural_proposal(effective, policy=policy)
    except ValueError as error:
        if hasattr(error, "audit"):
            error.audit_reference = _save(toolkit, path.with_suffix(".regularization.json"), error.audit)
        raise
    reference = {"schema_version": 1, "kind": "proposal", "policy": copy.deepcopy(policy),
        "modelling": context, "frame_context_parent": parent,
        "original": original_ref, "original_sha256": _hash(proposal),
        "adopted_sha256": _hash(adopted), "proposal_sha256": _hash(adopted),
        "adopted": _save(toolkit, path.with_suffix(".adopted.json"), adopted),
        "audit": _save(toolkit, path.with_suffix(".regularization.json"), audit)}
    return adopted, reference
