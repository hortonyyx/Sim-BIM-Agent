"""Pinned OpenStudio classification and project presentation data; no physics."""
from __future__ import annotations

import json
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("data") / "room_types.json"
CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
ROOM_TYPES = {row["code"]: row for row in CATALOG["types"]}
CANONICAL_ROLES = frozenset(ROOM_TYPES)
ALIASES = CATALOG["aliases"]


def _key(label: str) -> str:
    return re.sub(r"[\s_/-]+", " ", label.strip().lower())


_LOOKUP = {_key(code): code for code in ROOM_TYPES}
_LOOKUP.update({_key(alias): code for alias, code in ALIASES.items()})
_LOOKUP.update({_key(row["label_zh"]): code for code, row in ROOM_TYPES.items()})


def normalize(label: str | None) -> str:
    """Resolve catalog names/known historical aliases; retain unknown text."""
    text = _key(str(label or ""))
    return _LOOKUP.get(text, text)


def require_role(label: str | None) -> str:
    """Reject invented types; an absent declaration is explicitly unknown."""
    role = normalize(label) or "unknown"
    if role not in ROOM_TYPES:
        raise ValueError(f"room role {label!r} is not in the room_types catalog; "
                         "read get_bim_reference('room_types') and choose a listed code, "
                         "prefer a plausible inferred use over unknown. Do not suffix types with '_inferred'.")
    return role


def room_types_reference() -> str:
    return ("Choose role from this pinned OpenStudio level-1 catalog (code | 中文 | fixed color). "
            "Prefer a reasonable listed use based on building context, layout and furniture, "
            "even when uncertain; use a broader plausible type with explicit inference. "
            "Precise use identification is low priority for this lightweight BIM and users can revise it later. "
            "Reserve unknown for cases with no defensible listed choice; ambiguity alone is not enough. "
            "No invented roles; keep uncertainty and original "
            "drawing labels in source_refs/assumptions. This does not assign physical properties.\n"
            + CATALOG["source"]["url"] + "\n"
            + "\n".join(f"{r['code']} | {r['label_zh']} | {r['color']}"
                        + (f" | {r['annotation']}" if r["annotation"] else "")
                        for r in CATALOG["types"]))


def room_use_review(source: dict, *, include_next_action: bool = True) -> dict:
    """Separate missing records from actual use questions; never certify fidelity."""
    counts = {basis: 0 for basis in ("observed", "inferred", "unknown")}
    unrecorded = []
    unknown = []
    decisions = []
    for space in source.get("spaces", []):
        role = normalize(space.get("role")) or "unknown"
        reasons = []
        if role == "unknown":
            unknown.append(space["id"])
            reasons.append("unknown_role")
        elif role not in CANONICAL_ROLES:
            reasons.append("role_not_in_catalog")
        evidence = space.get("role_evidence")
        if evidence is None:
            unrecorded.append(space["id"])
        else:
            basis = evidence.get("basis") if isinstance(evidence, dict) else None
            if isinstance(basis, str) and basis in counts:
                counts[basis] += 1
                if (role == "unknown") != (basis == "unknown"):
                    reasons.append("role_evidence_basis_conflict")
            else:
                reasons.append("invalid_role_evidence_basis")
            if isinstance(evidence, dict) and "role" in evidence and normalize(evidence["role"]) != role:
                reasons.append("role_evidence_assignment_conflict")
        if reasons:
            decisions.append({"space_id": space["id"], "reasons": reasons})
    decision = {
        "status": "needs_judgment" if decisions else "no_issue_identified",
        "space_count": len(decisions),
        "examples": decisions[:20],
        "examples_truncated": len(decisions) > 20,
        "meaning": "Only explicit unknown roles or inconsistent role declarations identify a use question. "
                   "Missing structured evidence alone does not identify wrong use or require an edit.",
    }
    result = {
        "source_model_sha256": source.get("source_model_sha256"),
        "summary": {"total_count": len(source.get("spaces", [])),
                    "recorded_count": sum(counts.values()), "unrecorded_count": len(unrecorded),
                    **{basis + "_count": count for basis, count in counts.items()}},
        "unrecorded_space_ids": unrecorded[:20],
        "unrecorded_ids_truncated": len(unrecorded) > 20,
        "unknown_space_ids": unknown[:20],
        "unknown_ids_truncated": len(unknown) > 20,
        "use_decision": decision,
        "next_action": ("Judge the listed use questions against available context in the current closeout decision. "
                        "Correct an unsupported assignment locally when needed for the request; a truthful unknown "
                        "may remain when no defensible listed use fits. Read additional evidence only for a "
                        "material unresolved question. No separate review or metadata registration round is required."
                        if decisions else
                        "No use change is indicated by these records. Retain existing plausible assignments and "
                        "missing-evidence counts; proceed with normal closeout unless substantive contrary evidence appears."),
        "interpretation": "Counts cover structured role_evidence only, not semantic correctness. "
                          "Legacy source_refs may contain other evidence. Unknown remains a fallback when "
                          "no defensible listed use fits; it is not a preferred response to ambiguity. "
                          "No-issue status does not establish that roles are correct.",
        "drawing_fidelity": "not_evaluated", "delivery_blocked": False,
    }
    if not include_next_action:
        # Every save reports the same factual coverage. Action guidance belongs
        # to explicit inspection/delivery, without inferring a fixed work phase.
        result.pop("next_action")
    return result
