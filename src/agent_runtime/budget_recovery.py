"""Offline, append-only budget recovery through the journal's single writer.

Release: ``python -m src.agent_runtime.budget_recovery RUN release ID --reason TEXT``.
Reconcile: ``... RUN reconcile --receipt FILE``. The receipt JSON contains
request_event_id, settlement_event_id and a complete BudgetSettlement object in
settlement. It must be sourced from a late provider receipt; currency estimates
remain estimates. CLI captures the exact input bytes as immutable source evidence.
Neither command sends model requests or changes old journal records.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from src.harness_contracts import (
    BudgetAmounts, BudgetEventPayload, BudgetReconciliation, BudgetRelease,
    BudgetSettlement, EventEnvelope, SourceRef,
)
from .store import EventStore
from .accounting import CnyPriceSchedule, account_request_usage, get_cny_price_schedule


def _reservation_event(store: EventStore, reservation_id: str) -> EventEnvelope:
    event = next((e for e in store.all_events if isinstance(e.payload, BudgetEventPayload)
        and e.payload.action == "reserve" and e.payload.reservation.reservation_id == reservation_id), None)
    if event is None:
        raise ValueError("budget recovery reservation does not exist")
    if not store.is_root_task and store.task_id != event.task_id:
        raise ValueError("task facade cannot recover another task's budget")
    return event


def _append(store: EventStore, reservation_event: EventEnvelope, payload,
            *, source_refs=()) -> EventEnvelope:
    if store.task_id == reservation_event.task_id:
        return store.append(payload, source_refs=source_refs)
    parent = reservation_event.parent_task
    return store.append(payload, source_refs=source_refs, task_id=reservation_event.task_id,
        parent_task_id=parent.task_id if parent.kind == "known" else None)


def release_unsent_reservation(store: EventStore, reservation_id: str,
                              *, reason: str) -> EventEnvelope:
    """Release only the durable reserve-before-request crash window, idempotently."""
    reservation = _reservation_event(store, reservation_id)
    record = BudgetRelease(reservation_id=reservation_id, reason=reason)
    existing = next((e for e in store.all_events if isinstance(e.payload, BudgetEventPayload)
        and e.payload.action == "release" and e.payload.release.reservation_id == reservation_id), None)
    if existing is not None:
        if existing.payload.release != record:
            raise ValueError("conflicting release for the same reservation")
        return existing
    # EventLog also enforces this invariant. This check gives a direct API error.
    if any(e.payload.event_type == "adapter_request"
           and e.payload.reservation_id == reservation_id for e in store.all_events):
        raise ValueError("request intent exists; reservation is not proven unsent")
    return _append(store, reservation, BudgetEventPayload(action="release", release=record))


def reconcile_missing_usage(store: EventStore,
                            receipt: BudgetReconciliation) -> EventEnvelope:
    """Append one evidenced late receipt; identical retries return its event.

    The evidence blob is JSON with request_event_id, settlement_event_id and
    settlement, exactly matching the supplied receipt. Cross-event validation
    binds it to the original reservation/task/request/missing settlement.
    """
    reservation = _reservation_event(store, receipt.reservation_id)
    data = json.loads(store.get_bytes(receipt.evidence.blob))
    expected = {"request_event_id": receipt.request_event_id,
                "settlement_event_id": receipt.settlement_event_id,
                "settlement": receipt.settlement.model_dump(mode="json")}
    if data != expected:
        raise ValueError("source receipt differs from reconciliation evidence")
    existing = next((e for e in store.all_events if isinstance(e.payload, BudgetEventPayload)
        and e.payload.action == "reconcile"
        and e.payload.reconciliation.reservation_id == receipt.reservation_id), None)
    if existing is not None:
        if existing.payload.reconciliation != receipt:
            raise ValueError("conflicting reconciliation for the same reservation")
        return existing
    request = next((e for e in store.all_events if e.event_id == receipt.request_event_id
        and e.payload.event_type == "adapter_request"), None)
    if request is None:
        raise ValueError("reconciliation must bind a prior request")
    identity = request.payload.versions.remote_model
    pricing = get_cny_price_schedule(identity.remote_alias, route_id=identity.route_id)
    if reservation.payload.reservation.amounts.money_cny is not None:
        # Prefer the immutable schedule that admitted this request, even if the
        # checked-in table has changed since dispatch. Old journals may lack it.
        for source in reservation.source_refs:
            if source.source_id != "request-budget-decision" or source.blob is None:
                continue
            recorded = json.loads(store.get_bytes(source.blob)).get("cny_reservation")
            if recorded and recorded.get("price_schedule"):
                schedule = dict(recorded["price_schedule"])
                for name in ("text_input_cny_per_million", "output_cny_per_million",
                             "image_input_cny_per_million", "cached_input_cny_per_million"):
                    if schedule.get(name) is not None:
                        schedule[name] = Decimal(str(schedule[name]))
                pricing = CnyPriceSchedule(**schedule)
                break
    calculated = account_request_usage(receipt.settlement.usage,
        image_tokens_estimate=receipt.settlement.image_tokens_estimate, pricing=pricing,
        billing_mode="subscription" if identity.route_id == "chatgpt-subscription" else "metered_or_unknown",
        input_includes_images=identity.route_id == "chatgpt-subscription")
    if reservation.payload.reservation.amounts.money_cny is not None:
        if (calculated.estimated_cost_cny is None
                or calculated.estimated_cost_cny != receipt.settlement.estimated_cost_cny):
            raise ValueError("reconciliation CNY estimate differs from the request's evidenced usage and rate")
    if calculated.budget_charge_tokens != receipt.settlement.effective_tokens:
        raise ValueError("reconciliation image/token charge differs from the request's billing rule")
    return _append(store, reservation,
        BudgetEventPayload(action="reconcile", reconciliation=receipt),
        source_refs=(receipt.evidence,))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    release = actions.add_parser("release")
    release.add_argument("reservation_id")
    release.add_argument("--reason", required=True)
    reconcile = actions.add_parser("reconcile")
    reconcile.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    metadata = json.loads((args.run_directory / "journal.json").read_bytes())
    limit = BudgetAmounts.model_validate_json(json.dumps(metadata["budget_limit"]))
    with EventStore(args.run_directory, run_id=metadata["run_id"],
                    task_id=metadata["task_id"], budget_limit=limit) as store:
        if args.action == "release":
            event = release_unsent_reservation(store, args.reservation_id, reason=args.reason)
        else:
            raw = args.receipt.read_bytes()
            data = json.loads(raw)
            settlement = BudgetSettlement.model_validate_json(json.dumps(data["settlement"]))
            evidence = SourceRef(source_id="late-provider-receipt", source_kind="user",
                locator=str(args.receipt.resolve()), blob=store.put_bytes(raw, "application/json"))
            event = reconcile_missing_usage(store, BudgetReconciliation(
                reservation_id=settlement.reservation_id,
                request_event_id=data["request_event_id"],
                settlement_event_id=data["settlement_event_id"],
                settlement=settlement, evidence=evidence))
        print(event.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
