import copy

import pytest

from src.agent.roles import ALIASES, CANONICAL_ROLES, normalize, room_use_review


def test_normalize_aliases_and_canonical_roles():
    assert "unknown" in CANONICAL_ROLES
    assert ALIASES["meeting room"] == "conference/meeting/multipurpose"
    assert ALIASES["entrance lobby"] == "lobby"
    assert normalize(" Meeting Room ") == "conference/meeting/multipurpose"
    assert normalize("entrance-lobby") == "lobby"
    assert normalize("Office") == "office"
    assert normalize("not a real role") == "not a real role"


def test_missing_use_records_do_not_request_a_semantic_edit():
    source = {"spaces": [
        {"id": "office", "role": "office", "source_refs": ["legacy inferred office use"]},
        {"id": "meeting", "role": "meeting room"},
    ]}
    before = copy.deepcopy(source)
    inspected = room_use_review(source)
    saved = room_use_review(source, include_next_action=False)
    assert inspected["summary"]["unrecorded_count"] == 2
    assert inspected["use_decision"]["status"] == "no_issue_identified"
    assert inspected["use_decision"]["space_count"] == 0
    assert inspected["delivery_blocked"] is False
    assert inspected["drawing_fidelity"] == "not_evaluated"
    assert saved == {key: value for key, value in inspected.items() if key != "next_action"}
    assert source == before


@pytest.mark.parametrize(("space", "reasons"), [
    ({"id": "unknown", "role": "unknown"}, ["unknown_role"]),
    ({"id": "explained", "role": "unknown", "role_evidence": {"role": "unknown", "basis": "unknown"}},
     ["unknown_role"]),
    ({"id": "conflict", "role": "office", "role_evidence": {"role": "corridor", "basis": "observed"}},
     ["role_evidence_assignment_conflict"]),
    ({"id": "basis", "role": "office", "role_evidence": {"role": "office", "basis": "unknown"}},
     ["role_evidence_basis_conflict"]),
    ({"id": "invalid", "role": "office", "role_evidence": {"basis": "invalid"}},
     ["invalid_role_evidence_basis"]),
    ({"id": "malformed", "role": "office", "role_evidence": {"basis": []}},
     ["invalid_role_evidence_basis"]),
])
def test_real_use_questions_remain_explicit_without_automatic_blocking(space, reasons):
    review = room_use_review({"spaces": [space]})
    assert review["use_decision"]["status"] == "needs_judgment"
    assert review["use_decision"]["space_count"] == 1
    assert review["use_decision"]["examples"] == [{"space_id": space["id"], "reasons": reasons}]
    assert review["delivery_blocked"] is False
    assert review["drawing_fidelity"] == "not_evaluated"


def test_normalized_evidence_assignment_does_not_create_a_false_conflict():
    review = room_use_review({"spaces": [
        {"id": "meeting", "role": "conference/meeting/multipurpose",
         "role_evidence": {"role": "meeting room", "basis": "inferred"}},
    ]})
    assert review["use_decision"]["status"] == "no_issue_identified"
    assert review["summary"]["inferred_count"] == 1


def test_many_use_questions_retain_total_count_and_bound_examples():
    review = room_use_review({"spaces": [{"id": f"room-{i}", "role": "unknown"} for i in range(100)]})
    assert review["use_decision"]["space_count"] == 100
    assert len(review["use_decision"]["examples"]) == 20
    assert review["use_decision"]["examples_truncated"] is True
