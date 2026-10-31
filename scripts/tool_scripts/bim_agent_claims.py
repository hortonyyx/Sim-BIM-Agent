"""A2-T evidence transactions and conservative candidate-local claim binding.

T1 sm25 step 115 rejected unchanged targets after a different window and room
labels changed. Keep the immutable claim and the strict legacy resolver; supply
a temporary binding view only after checking ancestry, every intervening target
context, original views and recomputed values. No geometry computation changes.
"""
from __future__ import annotations

import copy
import json

from src.agent.execution.bim_claims import ClaimStore, parameter_slots, sha
from src.agent.execution.bim_claim_state import context, lineage
from src.agent.geometry.source_model import _digest


class ClaimBindingError(ValueError):
    def __init__(self, report):
        self.report = report
        reasons = sorted({reason for row in report["targets"] for reason in row["reasons"]})
        super().__init__("claim is stale for these targets: " + ", ".join(reasons))


def _context_changes(before, after, kind, identity):
    key = f"{kind}:{identity}"
    if before.get(key) is None or after.get(key) is None:
        return ["target_missing"]
    reasons = []
    host_keys = {key for key in before.keys() | after.keys() if key.startswith("host:")}
    host_fields = ("space_id", "other_space_id", "space_ids", "boundary_id", "boundary_ids", "wall", "edge")
    old_object, new_object = before[key], after[key]
    if (any(before.get(key) != after.get(key) for key in host_keys)
            or any(old_object.get(k) != new_object.get(k) for k in host_fields)):
        reasons.append("host_changed")
    if before != after:
        reasons.append("target_geometry_changed")
    return reasons


class _ValidatedBindings(ClaimStore):
    """Ephemeral resolver input; these rebased records are never saved as claims."""
    def __init__(self, toolkit, records):
        super().__init__(toolkit)
        self.records = records

    def read(self, identity):
        return self.records[identity] if identity in self.records else super().read(identity)


class AgentClaimStore(ClaimStore):
    def binding_report(self, candidate, row, targets):
        ancestry = lineage(self, candidate)
        current, _ = ancestry[candidate]
        origin = row["claim"]["candidate"]
        common = []
        if origin not in ancestry:
            common.append("origin_not_in_candidate_ancestry")
        elif sha(ancestry[origin][0]) != row["parent_proposal_sha256"]:
            common.append("origin_proposal_changed")
        decision = self.decisions().get(row["id"], {})
        if decision.get("disposition") != "adopted":
            common.append("claim_not_adopted")
        try:
            sources = self._sources(row["claim"])
            if sources != row["sources"]:
                common.append("source_view_changed")
            values, computations = self._resolve_values(row["claim"], sources)
            if values != row["resolved_values"] or computations != row["computations"]:
                common.append("values_or_computation_changed")
        except (OSError, ValueError, KeyError, TypeError) as error:
            common.append("source_view_or_value_unavailable: " + str(error))
        rows = []
        for kind, identity in sorted(set(tuple(ref) for ref in targets)):
            reasons = list(common)
            if not common:
                proposal, source = ancestry[origin]
                before = context(proposal, source, [(kind, identity)])
                # A changed-then-restored target is conservatively invalid too.
                for name, (proposal, source) in ancestry.items():
                    if _digest({k: v for k, v in source.items() if k != "source_model_sha256"}) != source.get("source_model_sha256"):
                        reasons.append("source_model_changed")
                        break
                    reasons.extend(_context_changes(before, context(proposal, source, [(kind, identity)]), kind, identity))
                    if name == origin:
                        break
            rows.append(dict(kind=kind, id=identity, eligible=not reasons, reasons=sorted(set(reasons))))
        return dict(claim_id=row["id"], from_candidate=origin, to_candidate=candidate,
                    original_proposal_sha256=row["parent_proposal_sha256"],
                    current_proposal_sha256=sha(current), targets=rows)

    def resolve_operations(self, candidate, operations):
        # Leave all format/value/target checks to the existing resolver. Only the
        # whole-proposal mismatch has a new, more precise acceptance condition.
        if not isinstance(operations, list) or any(not isinstance(o, dict) for o in operations):
            return super().resolve_operations(candidate, operations)
        proposal, _ = self.candidate(candidate)
        originals, rebased, reports = {}, {}, {}
        for operation in operations:
            for container, field, _, targets, _ in parameter_slots(operation):
                ref = container[field]
                if not isinstance(ref, dict) or "claim" not in ref:
                    continue
                row = self.read(ref["claim"])
                if row["claim"]["candidate"] == candidate and row["parent_proposal_sha256"] == sha(proposal):
                    continue
                if self.decisions().get(row["id"], {}).get("disposition") != "adopted":
                    raise ValueError("claim must be explicitly adopted before application")
                report = self.binding_report(candidate, row, targets)
                if not report["targets"] or any(not target["eligible"] for target in report["targets"]):
                    raise ClaimBindingError(report)
                originals[row["id"]] = row
                rebased[row["id"]] = {**row, "parent_proposal_sha256": sha(proposal)}
                if row["id"] in reports:
                    previous = reports[row["id"]]["targets"]
                    previous.extend(target for target in report["targets"] if target not in previous)
                else:
                    reports[row["id"]] = report
        resolved, evidence = _ValidatedBindings(self.toolkit, rebased).resolve_operations(candidate, operations)
        if reports:
            evidence["inheritance"] = list(reports.values())
            for identity, original in originals.items():
                evidence["claims"][identity]["record"] = original
                old = f"claim:{identity}:{sha(rebased[identity])}"
                new = f"claim:{identity}:{sha(original)}"
                for operation in resolved:
                    operation["source_refs"] = [new if ref == old else ref for ref in operation.get("source_refs", [])]
        return resolved, evidence


def _bind_local(value, identity):
    if isinstance(value, list):
        return [_bind_local(item, identity) for item in value]
    if isinstance(value, dict):
        return {key: (identity if key == "claim" and item == "$claim" else _bind_local(item, identity))
                for key, item in value.items()}
    return value


def claim_transaction(toolkit, candidate, entries):
    """Execute independent entries sequentially, keeping partial outcomes explicit.

    Each entry is a record/decision plus confirm or apply. Immutable candidate
    writes are atomic per existing build; failures never roll back earlier entries.
    The journal is saved before effects and after each entry for interrupted runs.
    """
    from scripts.tool_scripts.run_bim_agent import dump
    from scripts.tool_scripts.bim_agent_saved_result import saved_result
    if toolkit.readonly:
        raise ValueError("only the coordinator may transact candidate claims")
    if not isinstance(entries, list) or not entries:
        raise ValueError("entries must be a nonempty list")
    store = toolkit.claims()
    transaction = store._write("transaction", dict(candidate=candidate, status="running", entries=[]))
    path = store.folder / (transaction["id"] + ".json")
    current = candidate
    committed, save_feedback, geometry_applied = [], None, False
    for index, entry in enumerate(entries):
        audit = dict(index=index, candidate=current, status="pending", request=copy.deepcopy(entry))
        transaction["entries"].append(audit)
        dump(path, transaction)
        try:
            if not isinstance(entry, dict) or set(entry) - {"claim", "claim_id", "action", "reason", "disposition", "operations"}:
                raise ValueError("entry requires claim or claim_id, action, reason, optional disposition/operations")
            if ("claim" in entry) == ("claim_id" in entry):
                raise ValueError("entry requires exactly one of claim or claim_id")
            action = entry.get("action", "confirm")
            if action not in {"record", "decide", "confirm", "apply"}:
                raise ValueError("action must be record/decide/confirm/apply")
            audit["action"] = action
            if "claim" in entry:
                data = copy.deepcopy(entry["claim"])
                if not isinstance(data, dict):
                    raise ValueError("claim must be an object")
                if data.get("observation_type") == "facade_count":
                    if action != "record" or "operations" in entry or "disposition" in entry:
                        raise ValueError("facade_count uses action=record without decision or operations")
                    row = toolkit.record_claim(json.dumps(data))
                    audit.update(claim_id=row["id"], reason=data.get("reason"), status="recorded")
                    continue
                data.setdefault("candidate", current)
                row = toolkit.record_claim(json.dumps(data))
                audit["recorded"] = True
            else:
                row = store.read(entry["claim_id"])
            identity = row["id"]
            audit["claim_id"] = identity
            reason = entry.get("reason", row["claim"]["reason"])
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("explicit nonempty decision reason required")
            audit["reason"] = reason
            previous = store.decisions().get(identity, {})
            if action in {"confirm", "apply"} and previous.get("disposition") == "retracted":
                raise ValueError("claim retracted; it cannot be inherited or automatically adopted")
            disposition = entry.get("disposition", "adopted")
            if action in {"confirm", "apply"} and disposition != "adopted":
                raise ValueError("confirm/apply requires adopted disposition")
            decision = toolkit.decide_claim(identity, disposition, reason)
            audit["decision_id"] = decision["id"]
            if action in {"record", "decide"}:
                if "operations" in entry:
                    raise ValueError("record/decide does not accept operations")
                audit["status"] = disposition
                continue
            operations = _bind_local(entry.get("operations"), identity)
            resolved, evidence = store.resolve_operations(current, operations)
            if not evidence["bindings"] or set(evidence["claims"]) != {identity}:
                raise ValueError("each entry must bind its own claim; use $claim for a newly recorded claim")
            # The entry already supplies the reason for these claim-bound intents.
            # Keep explicit values and unrelated edits subject to ordinary validation.
            for operation_index in sorted({b["operation_index"] for b in evidence["bindings"]}):
                operation = operations[operation_index]
                if "reason" not in operation:
                    operation["reason"] = reason
                    audit.setdefault("operation_reason_inheritance", []).append({
                        "path": f"operations[{operation_index}].reason",
                        "from": "entry.reason" if "reason" in entry else f"claim:{identity}.reason",
                    })
            audit["targets"] = [dict(value=b["value_field"], parameter=b["parameter"], targets=b["targets"])
                                for b in evidence["bindings"]]
            if evidence.get("inheritance"):
                audit["inheritance"] = evidence["inheritance"]
            if action == "confirm":
                result = toolkit.confirm_claims(current, json.dumps(operations))
                audit.update(status="confirmed_unchanged", confirmation_id=result["id"])
            else:
                result = toolkit.revise(current, json.dumps(operations))
                app = result["claim_application"]
                audit.update(status=app["status"], application_id=app["id"], error=app.get("error"))
                if app["status"] == "applied":
                    current = result["saved_candidate"]
                    committed.extend(result["save_effects"]["created_candidates"])
                    geometry_applied |= result["save_effects"]["geometry_applied"]
                    save_feedback = result
                    audit["result_candidate"] = current
                    audit["save_effects"] = result["save_effects"]
                else:
                    # A rejected export may still have an auditable candidate
                    # and detailed validation, with no top-level error string.
                    # Keep that attempt separate from the last usable source.
                    audit["attempted_candidate"] = result.get("candidate") or app.get("candidate")
                    for key in ("source_validation", "error_stage", "repair_hint", "regularization"):
                        value = result.get(key) or app.get(key)
                        if value is not None:
                            audit[key] = copy.deepcopy(value)
                    findings = (audit.get("source_validation") or {}).get("findings", [])
                    audit["error"] = (result.get("error") or app.get("error") or
                        ("Source validation rejected the attempted revision: " +
                         json.dumps(findings, ensure_ascii=False, separators=(",", ":")) if findings else
                         "Revision did not produce a usable source; inspect this entry's saved diagnostics."))
        except (OSError, ValueError, KeyError, TypeError) as error:
            audit.update(status="failed", error=str(error))
            if isinstance(error, ClaimBindingError):
                audit["inheritance"] = [error.report]
        finally:
            dump(path, transaction)
    failed = sum(row["status"] == "failed" for row in transaction["entries"])
    transaction.update(status="failed" if failed == len(entries) else "partial" if failed else "completed",
                       result_candidate=current)
    if failed:
        failures = [row for row in transaction["entries"] if row["status"] == "failed"]
        transaction["failure_summary"] = {
            "failed_entry_count": failed,
            "entries": [{"index": row["index"], "attempted_candidate": row.get("attempted_candidate"),
                         "error": str(row["error"])[:400]} for row in failures[:2]],
            "omitted_entry_count": max(0, failed - 2),
            "next_action": "Read entries[index] in details_file for full source_validation and repair diagnostics. "
                           "Correct the evidence or geometry on result_candidate before applying again.",
        }
    saved_result(transaction, candidate=current if committed else None, created=committed,
                 geometry_applied=geometry_applied, audit_written=True)
    dump(path, transaction)
    # Reuse ordinary revision feedback (including its views), then add this
    # batch's audit. The final source remains visible even if a later entry fails.
    reply = {**(save_feedback or {}), **{key: value for key, value in transaction.items() if key != "entries"}}
    reply["entries"] = [{key: value for key, value in row.items() if key != "request"}
                        for row in transaction["entries"]]
    reply.update(audit_file=path.relative_to(toolkit.run).as_posix(),
                 note="Entries commit independently; failed entries keep their recorded decisions. Numerical consistency is not drawing verification.")
    if current:
        reply["height_coverage"] = toolkit.located_heights(current)
    toolkit.log("claim_transaction", reply)
    return reply
