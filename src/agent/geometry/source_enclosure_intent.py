"""Bind explicit enclosure intent to the source just constructed in this build.

Unlike a frozen observation sidecar, intent does not supply a guessed source
digest. Selectors must resolve uniquely on the adopted geometry. The existing
enclosure validator remains responsible for evidence, region geometry, opening
conflicts and reciprocal declarations; this module never infers an open side.
"""
from __future__ import annotations

import copy

from src.agent.geometry.source_enclosure import apply_source_enclosure, _digest_matches
from src.agent.geometry.source_model import _digest


SCHEMA = "source_enclosure_intent_v1"


def _fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise ValueError(f"enclosure intent: {label} must be an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"enclosure intent: {label}: missing {sorted(missing)}, unknown {sorted(unknown)}")


def _resolve(source, selector):
    if not isinstance(selector, dict):
        raise ValueError("enclosure intent: boundary selector must be an object")
    if set(selector) == {"boundary_id"}:
        matches = [row for row in source["boundaries"] if row["id"] == selector["boundary_id"]]
    elif set(selector) == {"space_id", "geometry_type"}:
        if selector["geometry_type"] not in {"wall", "floor", "ceiling"}:
            raise ValueError("enclosure intent: selector geometry_type must be wall, floor, or ceiling")
        matches = [row for row in source["boundaries"]
                   if row["space_id"] == selector["space_id"] and row["geometry_type"] == selector["geometry_type"]]
    else:
        raise ValueError("enclosure intent: selector requires boundary_id or space_id plus geometry_type")
    if len(matches) != 1:
        raise ValueError(f"enclosure intent: selector {selector!r} resolved to {len(matches)} boundaries; "
                         "select one exact boundary, without guessing or nearest-boundary matching")
    return matches[0]["id"]


def apply_source_enclosure_intent(base_source: dict, intent: dict) -> dict:
    """Resolve and apply an authored intent without changing any geometry.

    ``whole`` follows the actual selected boundary; ``partial`` vertices remain
    explicit world coordinates and must pass the original exact-plane checks.
    No counterpart, evidence kind or space enclosure classification is invented.
    """
    if (not isinstance(base_source, dict) or base_source.get("schema_version") != "source_bim_v2"
            or not _digest_matches(base_source)):
        raise ValueError("enclosure intent: base source must be an intact source_bim_v2")
    _fields(intent, {"schema_version"}, {"spaces", "boundaries"}, "declaration")
    if intent["schema_version"] != SCHEMA:
        raise ValueError(f"enclosure intent: expected {SCHEMA}")
    spaces, boundaries = intent.get("spaces", []), intent.get("boundaries", [])
    if not isinstance(spaces, list) or not isinstance(boundaries, list):
        raise ValueError("enclosure intent: spaces and boundaries must be lists")
    for row in spaces:
        _fields(row, {"space_id", "enclosure", "evidence_kind"}, {"source_refs", "assumptions"}, "space")
    resolved, bindings = [], []
    for index, row in enumerate(boundaries):
        _fields(row, {"boundary", "condition", "scope", "evidence_kind"},
                {"source_refs", "assumptions", "vertices"}, f"boundary {index}")
        bid = _resolve(base_source, row["boundary"])
        resolved.append({"boundary_id": bid, **copy.deepcopy({k: v for k, v in row.items() if k != "boundary"})})
        bindings.append({"index": index, "selector": copy.deepcopy(row["boundary"]), "boundary_id": bid})
    declaration = {"schema_version": "source_enclosure_input_v1",
                   "base_source_model_sha256": base_source["source_model_sha256"],
                   "spaces": copy.deepcopy(spaces), "boundaries": resolved}
    result = apply_source_enclosure(base_source, declaration)
    result["source_enclosure"]["intent_binding"] = {
        "schema_version": "source_enclosure_intent_binding_v1", "intent_sha256": _digest(intent),
        "boundary_resolutions": bindings,
    }
    result["source_model_sha256"] = _digest({k: v for k, v in result.items() if k != "source_model_sha256"})
    return result
