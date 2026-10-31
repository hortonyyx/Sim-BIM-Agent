"""Bounded main-agent follow-up on saved work, without a fixed modelling pipeline.

Only generation-side artifacts are read. A model stop decision is not a fidelity
verdict, and persisted changes are not proof of useful or correct progress.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from scripts.tool_scripts.bim_agent_guidance import FINISHING


def _read(path):
    return json.loads(path.read_bytes())


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def response_completed(record):
    return (bool(record.get("result")) and not record["result"].get("is_error", False)
            and not record.get("routing_error") and not record.get("timed_out")
            and record.get("returncode", 0) == 0)


def current_delivery(toolkit):
    selection = toolkit.run / "delivery_selection.json"
    if selection.exists():
        return toolkit.delivery(_read(selection)["candidate"], selection_origin="agent_selected")
    saved = sorted(toolkit.run.glob("candidate_*/source_model.json"))
    if not saved and (toolkit.run / "seed/source_model.json").is_file():
        saved = [toolkit.run / "seed/source_model.json"]
    if saved:
        return toolkit.delivery(saved[-1].parent.name,
                                selection_origin="latest_saved_fallback_not_agent_selected")
    return None


def persisted_state(run: Path, delivery: dict):
    """Ignore prose replies, view/tool logs and work decisions as progress.

    Include confirmations/reviews even when they correctly leave geometry alone.
    Duplicate evidence can still alter this signature; the round cap remains the
    ultimate bound. No evaluation directory or earlier run is inspected.
    """
    evidence = {}
    for folder in ("claims", "opening_reviews", "space_relation_reviews"):
        for path in sorted((run / folder).glob("*.json*")):
            evidence[path.relative_to(run).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"source_model_sha256": delivery["source_model_sha256"], "evidence": evidence}


def record_work_review(toolkit, candidate, decision, reason, next_action=""):
    if toolkit.readonly:
        raise ValueError("only the main agent may record a work decision")
    turn_path = toolkit.run / "work_turn.json"
    if not turn_path.is_file() or not (turn := _read(turn_path)).get("active"):
        raise ValueError("work decisions are available during an active continuation turn")
    if decision not in {"continue", "stop"}:
        raise ValueError("decision must be continue or stop")
    if not reason.strip() or (decision == "continue" and not next_action.strip()):
        raise ValueError("give a reason and, when continuing, a concrete next_action")
    source = _read(toolkit.candidate_path(candidate) / "source_model.json")
    folder = toolkit.run / "work_reviews"
    folder.mkdir(exist_ok=True)
    row = {"turn": turn["name"], "candidate": candidate,
           "source_model_sha256": source["source_model_sha256"],
           "decision": decision, "reason": reason, "next_action": next_action,
           "drawing_fidelity": "not_evaluated", "task_completion": "model_judgment_only"}
    path = folder / f"review_{len(list(folder.glob('review_*.json'))) + 1:03d}.json"
    _write(path, row)
    toolkit.log("record_work_review", {"file": path.relative_to(toolkit.run).as_posix(), **row})
    return row


def run_continuations(toolkit, initial_record, *, max_rounds, invoke, compact):
    records = [initial_record]
    result = {"max_rounds": max_rounds, "turns": [], "stop_reason": "disabled",
              "task_completion": "not_certified", "drawing_fidelity": "not_evaluated"}
    if not max_rounds:
        return records, result
    for index in range(1, max_rounds + 1):
        if not response_completed(records[-1]):
            result["stop_reason"] = "invocation_interrupted"
            break
        remaining = int(toolkit.manifest["deadline_epoch"] - time.time())
        if remaining < 45:
            result["stop_reason"] = "deadline_reserve"
            break
        before = current_delivery(toolkit)
        if before is None:
            result["stop_reason"] = "no_saved_candidate"
            break
        before_state = persisted_state(toolkit.run, before)
        name = f"continuation_{index:02d}"
        turn = {"name": name, "active": True, "round": index}
        _write(toolkit.run / "work_turn.json", turn)
        prompt = (
            f"Continue the SAME building task as the main agent. Original scope:\n"
            f"{toolkit.manifest['scope']}\nRemaining total budget: {remaining} seconds. "
            f"This is bounded follow-up {index}/{max_rounds}; the deadline is not reset.\n"
            f"Shared candidate export budget: {json.dumps(toolkit.candidate_budget())}.\n"
            "The saved candidate, claims, confirmations, reviews and original inputs in this run "
            "remain available. An earlier response ended, but that does not establish task completion. "
            "Compare the original scope with the actual saved delivery below. Select the most useful "
            "remaining in-scope action yourself; no fixed order or target score is prescribed. "
            "Inspect needed evidence, then call record_work_review with decision=continue and a "
            "concrete next_action, and EXECUTE that action with tools in this turn. A plan or a "
            "list of limitations alone does not execute it. Preserve reliable geometry and evidence; "
            "use local edits or confirmations when suitable. finish_bim selects the resulting candidate. "
            + FINISHING + " Reduce a large remaining task to ONE useful local check that fits the "
            "actual remaining time. "
            "If there is no useful feasible in-scope action, record decision=stop with a specific "
            "reason and finish honestly. Explained unknowns, absent input and out-of-scope work can "
            "justify stopping; missing fields do not require fabricated certainty or meaningless edits. "
            "Feedback measures recorded coverage, not correctness. Use the same tools and model; "
            "do not treat the previous prose or candidate as original evidence.\n"
            "Current saved delivery:\n" + json.dumps(compact(before), ensure_ascii=False)
            + "\nPrevious main-agent response (may be incomplete or wrong):\n"
            + str(records[-1].get("result", {}).get("result", ""))[:6000]
        )
        try:
            record = invoke(prompt, name=name, timeout=remaining)
        finally:
            _write(toolkit.run / "work_turn.json", {**turn, "active": False})
        records.append(record)
        after = current_delivery(toolkit)
        decisions = [_read(path) for path in sorted((toolkit.run / "work_reviews").glob("review_*.json"))]
        decisions = [row for row in decisions if row["turn"] == name]
        decision = decisions[-1] if decisions else None
        changed = persisted_state(toolkit.run, after) != before_state
        row = {"name": name, "before_candidate": before["candidate"],
               "after_candidate": after["candidate"], "decision": decision,
               "persisted_source_or_evidence_changed": changed,
               "response_completed": response_completed(record)}
        result["turns"].append(row)
        if not response_completed(record):
            result["stop_reason"] = "invocation_interrupted"
        elif decision is None:
            result["stop_reason"] = "missing_work_decision"
        elif (decision["decision"] == "stop" and
              decision["source_model_sha256"] == after["source_model_sha256"]):
            result["stop_reason"] = "model_stop"
        elif not changed:
            result["stop_reason"] = "no_persisted_change"
        else:
            result["stop_reason"] = "round_limit"
            _write(toolkit.run / "continuation.json", result)
            continue
        break
    _write(toolkit.run / "continuation.json", result)
    return records, result
