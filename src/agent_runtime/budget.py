"""Recoverable root-run budgeting with conservative request reservations."""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Literal

from pydantic import Field, ValidationError, model_validator

from src.harness_contracts import (
    BudgetAmounts,
    BudgetEventPayload,
    BudgetLedger,
    BudgetRelease,
    BudgetReconciliation,
    BudgetReservation,
    BudgetSettlement,
    CostUnavailable,
    EstimatedCostUpperBound,
    UsageMissing,
)
from src.harness_contracts.base import ContractModel, NonEmptyStr
from src.harness_contracts.budget import CostEvidence, UsageEvidence
from .accounting import CnyPriceSchedule


class PriceSchedule(ContractModel):
    """Configured USD prices used only to calculate conservative upper bounds."""

    model_route: NonEmptyStr
    source: NonEmptyStr
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)

    def upper_bound(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * self.input_usd_per_million
            + Decimal(output_tokens) * self.output_usd_per_million
        ) / million


class RequestEstimate(ContractModel):
    """A pre-request hold; token fields are estimates, never billing evidence."""

    purpose: Literal["primary_task", "child_task", "context_summary", "retry"]
    task_id: NonEmptyStr
    input_token_upper_bound: int = Field(ge=0)
    image_input_tokens_estimate: int = Field(default=0, ge=0)
    additional_image_tokens_estimate: int = Field(default=0, ge=0)
    output_token_limit: int = Field(ge=1)
    reasoning_token_allowance: int = Field(default=0, ge=0)
    seconds: Decimal | None = Field(gt=0)
    calls: int = Field(default=1, ge=1)
    money_usd_upper_bound: Decimal | None = Field(default=None, ge=0)
    money_cny_upper_bound: Decimal | None = Field(default=None, ge=0)
    estimate_source: NonEmptyStr

    @model_validator(mode="after")
    def image_estimate_is_part_of_input_hold(self) -> RequestEstimate:
        if self.image_input_tokens_estimate > self.input_token_upper_bound:
            raise ValueError(
                "image_input_tokens_estimate cannot exceed the input token upper bound"
            )
        return self

    @classmethod
    def for_model_call(
        cls,
        *,
        purpose: Literal[
            "primary_task", "child_task", "context_summary", "retry"
        ],
        task_id: str,
        input_token_upper_bound: int,
        image_input_tokens_estimate: int = 0,
        additional_image_tokens_estimate: int = 0,
        output_token_limit: int,
        reasoning_token_allowance: int = 0,
        seconds: Decimal | None,
        estimate_source: str,
        pricing: PriceSchedule | None = None,
        cny_pricing: CnyPriceSchedule | None = None,
    ) -> RequestEstimate:
        money = None
        if pricing is not None:
            money = pricing.upper_bound(
                input_tokens=input_token_upper_bound,
                output_tokens=output_token_limit + reasoning_token_allowance,
            )
        return cls(
            purpose=purpose,
            task_id=task_id,
            input_token_upper_bound=input_token_upper_bound,
            image_input_tokens_estimate=image_input_tokens_estimate,
            additional_image_tokens_estimate=additional_image_tokens_estimate,
            output_token_limit=output_token_limit,
            reasoning_token_allowance=reasoning_token_allowance,
            seconds=seconds,
            money_usd_upper_bound=money,
            money_cny_upper_bound=(cny_pricing.upper_bound(
                input_tokens=input_token_upper_bound,
                output_tokens=output_token_limit + reasoning_token_allowance,
                image_tokens=image_input_tokens_estimate) if cny_pricing else None),
            estimate_source=estimate_source,
        )

    @property
    def amounts(self) -> BudgetAmounts:
        return BudgetAmounts(
            tokens=self.input_token_upper_bound + self.additional_image_tokens_estimate + self.output_token_limit + self.reasoning_token_allowance,
            money_usd=self.money_usd_upper_bound,
            money_cny=self.money_cny_upper_bound,
            seconds=self.seconds,
            calls=self.calls,
        )

    def with_output_token_limit(
        self, output_token_limit: int, pricing: PriceSchedule | None,
        cny_pricing: CnyPriceSchedule | None = None,
    ) -> RequestEstimate:
        if self.money_usd_upper_bound is not None and pricing is None:
            raise ValueError("priced estimate cannot be reduced without its price schedule")
        if self.money_cny_upper_bound is not None and cny_pricing is None:
            raise ValueError("CNY estimate cannot be reduced without its price schedule")
        return self.model_copy(
            update={
                "output_token_limit": output_token_limit,
                "money_cny_upper_bound": (cny_pricing.upper_bound(
                    input_tokens=self.input_token_upper_bound,
                    output_tokens=output_token_limit + self.reasoning_token_allowance,
                    image_tokens=self.image_input_tokens_estimate) if cny_pricing else None),
                "money_usd_upper_bound": (
                    pricing.upper_bound(
                        input_tokens=self.input_token_upper_bound,
                        output_tokens=output_token_limit + self.reasoning_token_allowance,
                    )
                    if pricing is not None
                    else None
                ),
            }
        )


class BudgetDecision(ContractModel):
    """Explicit admission, degradation, settlement, or stop result."""

    action: Literal["allow", "reduce_output", "stop"]
    reason: NonEmptyStr
    available: BudgetAmounts
    reservation: BudgetReservation | None = None
    settlement: BudgetSettlement | None = None
    effective_estimate: RequestEstimate | None = None
    output_token_limit: int | None = Field(default=None, ge=1)
    exceeded_dimensions: tuple[
        Literal["tokens", "money_usd", "money_cny", "seconds", "calls"], ...
    ] = ()

    @model_validator(mode="after")
    def admission_needs_a_reservation(self) -> BudgetDecision:
        if self.action in {"allow", "reduce_output"} and (
            self.reservation is None and self.settlement is None
        ):
            raise ValueError("an admitted budget action needs a reservation or settlement")
        if self.action == "reduce_output" and (
            self.effective_estimate is None
            or self.output_token_limit != self.effective_estimate.output_token_limit
        ):
            raise ValueError("output reduction must expose the effective output token limit")
        return self


class RuntimeBudget:
    """One root ledger shared by primary, child, summary, and retry requests."""

    def __init__(
        self,
        total_limit: BudgetAmounts,
        *,
        near_limit_policy: Literal["stop", "reduce_output"] = "stop",
        min_output_tokens: int = 1,
        pricing: PriceSchedule | None = None,
        cny_pricing: CnyPriceSchedule | None = None,
    ) -> None:
        if near_limit_policy not in {"stop", "reduce_output"}:
            raise ValueError("near_limit_policy must be stop or reduce_output")
        if type(min_output_tokens) is not int or min_output_tokens < 1:
            raise ValueError("min_output_tokens must be a positive integer")
        self.total_limit = total_limit
        self.near_limit_policy = near_limit_policy
        self.min_output_tokens = min_output_tokens
        self.pricing = pricing
        self.cny_pricing = cny_pricing
        self._reservations: list[BudgetReservation] = []
        self._settlements: list[BudgetSettlement] = []
        self._releases: list[BudgetRelease] = []
        self._reconciliations: list[BudgetReconciliation] = []
        self._fatal_reason: str | None = None
        # Validate the total even before the first request.
        self.ledger

    @property
    def ledger(self) -> BudgetLedger:
        return BudgetLedger(
            total_limit=self.total_limit,
            reservations=tuple(self._reservations),
            settlements=tuple(self._settlements),
            releases=tuple(self._releases), reconciliations=tuple(self._reconciliations),
        )

    def clone(self) -> RuntimeBudget:
        """Independent mutable admission state over immutable journal contracts."""
        copied = type(self)(self.total_limit, near_limit_policy=self.near_limit_policy,
            min_output_tokens=self.min_output_tokens, pricing=self.pricing,
            cny_pricing=self.cny_pricing)
        copied._reservations = list(self._reservations)
        copied._settlements = list(self._settlements)
        copied._releases = list(self._releases)
        copied._reconciliations = list(self._reconciliations)
        copied._fatal_reason = self._fatal_reason
        return copied

    copy = clone

    @property
    def available(self) -> BudgetAmounts:
        return self.ledger.available

    @property
    def fatal_reason(self) -> str | None:
        return self._fatal_reason

    def reserve(
        self, reservation_id: str, estimate: RequestEstimate
    ) -> BudgetDecision:
        if self._fatal_reason is not None:
            return self._stop(self._fatal_reason)
        if any(item.reservation_id == reservation_id for item in self._reservations):
            return self._stop("duplicate_reservation_id")
        if (
            self.total_limit.money_usd is not None
            and estimate.money_usd_upper_bound is None
        ):
            return self._stop("money_estimate_unavailable")
        if self.total_limit.money_cny is not None and estimate.money_cny_upper_bound is None:
            return self._stop("money_cny_estimate_unavailable")

        effective = estimate
        action: Literal["allow", "reduce_output"] = "allow"
        if not _fits(estimate.amounts, self.available):
            if self.near_limit_policy == "stop":
                return self._stop(
                    "request_estimate_exceeds_available_budget",
                    exceeded=_exceeded(estimate.amounts, self.available),
                )
            effective = self._reduced_estimate(estimate)
            if effective is None:
                return self._stop(
                    "minimum_output_cannot_fit_available_budget",
                    exceeded=_exceeded(estimate.amounts, self.available),
                )
            action = "reduce_output"

        reservation = BudgetReservation(
            reservation_id=reservation_id,
            purpose=effective.purpose,
            task_id=effective.task_id,
            amounts=effective.amounts,
        )
        candidate = [*self._reservations, reservation]
        BudgetLedger(
            total_limit=self.total_limit,
            reservations=tuple(candidate),
            settlements=tuple(self._settlements),
            releases=tuple(self._releases), reconciliations=tuple(self._reconciliations),
        )
        self._reservations.append(reservation)
        return BudgetDecision(
            action=action,
            reason=(
                "request_budget_reserved"
                if action == "allow"
                else "output_limit_reduced_to_fit_root_budget"
            ),
            available=self.available,
            reservation=reservation,
            effective_estimate=effective,
            output_token_limit=effective.output_token_limit,
        )

    def settle(
        self,
        reservation_id: str,
        *,
        actual: BudgetAmounts,
        usage: UsageEvidence,
        cost: CostEvidence | None = None,
        image_tokens_estimate: int = 0,
        reported_usage_includes_image_tokens: bool = False,
        additional_image_tokens: int | None = None,
        estimated_cost_cny: Decimal | None = None,
    ) -> BudgetDecision:
        reservation = next(
            (
                item
                for item in self._reservations
                if item.reservation_id == reservation_id
            ),
            None,
        )
        if reservation is None:
            self._fatal_reason = "settlement_without_reservation"
            return self._stop(self._fatal_reason)
        if any(item.reservation_id == reservation_id for item in self._releases):
            self._fatal_reason = "settlement_after_release"
            return self._stop(self._fatal_reason, reservation=reservation)
        if any(
            item.reservation_id == reservation_id for item in self._settlements
        ):
            self._fatal_reason = "reservation_settled_twice"
            return self._stop(self._fatal_reason, reservation=reservation)

        effective_actual = actual.model_copy(update={
            "tokens": (
                None
                if actual.tokens is None
                else actual.tokens + (
                    additional_image_tokens if additional_image_tokens is not None
                    else 0 if reported_usage_includes_image_tokens else image_tokens_estimate
                )
            )
        })
        exceeded = tuple(name for name in _exceeded_reservation(effective_actual, reservation.amounts)
                         if name not in {"tokens", "money_cny"}
                         and not (name == "seconds" and self.total_limit.seconds is None
                                  and reservation.amounts.seconds is None))
        if exceeded:
            self._fatal_reason = "actual_usage_exceeds_reservation"
            return self._stop(
                self._fatal_reason,
                reservation=reservation,
                exceeded=exceeded,
            )
        if cost is not None and cost.kind != "unavailable":
            reserved_money = reservation.amounts.money_usd
            if reserved_money is None or cost.usd > reserved_money:
                self._fatal_reason = "actual_or_estimated_cost_exceeds_reservation"
                return self._stop(
                    self._fatal_reason,
                    reservation=reservation,
                    exceeded=("money_usd",),
                )

        if cost is None:
            if reservation.amounts.money_usd is not None:
                cost = EstimatedCostUpperBound(
                    usd=reservation.amounts.money_usd,
                    reason=(
                        "configured price estimate retained because no provider bill "
                        "was supplied"
                    ),
                )
            else:
                cost = CostUnavailable(
                    reason="no configured price estimate or provider bill was supplied"
                )
        try:
            settlement = BudgetSettlement(
                reservation_id=reservation_id,
                actual=actual,
                usage=usage,
                cost=cost,
                image_tokens_estimate=image_tokens_estimate,
                additional_image_tokens=additional_image_tokens,
                reported_usage_includes_image_tokens=(
                    reported_usage_includes_image_tokens
                ),
                token_overrun=max(
                    0,
                    (effective_actual.tokens or 0)
                    - (reservation.amounts.tokens or 0),
                ),
                estimated_cost_cny=estimated_cost_cny,
                money_cny_overrun=max(Decimal(0), estimated_cost_cny - reservation.amounts.money_cny)
                    if estimated_cost_cny is not None and reservation.amounts.money_cny is not None
                    else Decimal(0),
            )
            BudgetLedger(
                total_limit=self.total_limit,
                reservations=tuple(self._reservations),
                settlements=tuple([*self._settlements, settlement]),
                releases=tuple(self._releases), reconciliations=tuple(self._reconciliations),
            )
        except ValidationError:
            self._fatal_reason = "invalid_settlement_evidence"
            return self._stop(self._fatal_reason, reservation=reservation)
        self._settlements.append(settlement)
        if self._fatal_reason == "unsettled_reported_usage":
            self._fatal_reason = None
        if reservation.amounts.money_cny is not None and estimated_cost_cny is None:
            self._fatal_reason = "money_cny_usage_unavailable"
            return BudgetDecision(action="stop", reason=self._fatal_reason,
                available=self.available, reservation=reservation, settlement=settlement)
        if (self.total_limit.money_cny is not None
                and (self.ledger.committed.money_cny or 0) >= self.total_limit.money_cny):
            return BudgetDecision(action="stop", reason="money_budget_exhausted",
                available=self.available, reservation=reservation, settlement=settlement,
                exceeded_dimensions=("money_cny",))
        if (self.total_limit.tokens is not None
                and (self.ledger.committed.tokens or 0) > self.total_limit.tokens):
            return BudgetDecision(action="stop", reason="token_budget_exhausted",
                available=self.available, reservation=reservation, settlement=settlement,
                exceeded_dimensions=("tokens",))
        return BudgetDecision(
            action="allow",
            reason="reservation_settled",
            available=self.available,
            reservation=reservation,
            settlement=settlement,
        )

    @classmethod
    def from_events(
        cls,
        total_limit: BudgetAmounts,
        events: Iterable[object],
        *,
        near_limit_policy: Literal["stop", "reduce_output"] = "stop",
        min_output_tokens: int = 1,
        pricing: PriceSchedule | None = None,
        cny_pricing: CnyPriceSchedule | None = None,
    ) -> RuntimeBudget:
        """Recover full settled charges and conservative outstanding holds."""

        budget = cls(
            total_limit,
            near_limit_policy=near_limit_policy,
            min_output_tokens=min_output_tokens,
            pricing=pricing,
            cny_pricing=cny_pricing,
        )
        materialized = list(events)
        reservation_events = {}
        settlement_events = {}
        request_events = {}
        for item in materialized:
            payload = getattr(item, "payload", item)
            if getattr(payload, "event_type", None) == "adapter_request" and payload.reservation_id:
                if any(r.reservation_id == payload.reservation_id for r in budget._releases):
                    raise ValueError("released reservation cannot be dispatched")
                request_events[payload.reservation_id] = item
            if not isinstance(payload, BudgetEventPayload):
                continue
            if payload.action in {"release", "reconcile"}:
                from src.harness_contracts.validation import validate_budget_recovery_event
                record = payload.release if payload.action == "release" else payload.reconciliation
                validate_budget_recovery_event(item, reservation_events.get(record.reservation_id),
                    settlement_events.get(record.reservation_id), request_events.get(record.reservation_id))
            if payload.action == "reserve":
                if not _fits(payload.reservation.amounts, budget.available):
                    raise ValueError("recorded reservation exceeds available budget")
                candidate = [*budget._reservations, payload.reservation]
                BudgetLedger(
                    total_limit=total_limit,
                    reservations=tuple(candidate),
                    settlements=tuple(budget._settlements),
                    releases=tuple(budget._releases), reconciliations=tuple(budget._reconciliations),
                )
                budget._reservations.append(payload.reservation)
                reservation_events[payload.reservation.reservation_id] = item
            elif payload.action == "settle":
                candidate = [*budget._settlements, payload.settlement]
                BudgetLedger(
                    total_limit=total_limit,
                    reservations=tuple(budget._reservations),
                    settlements=tuple(candidate),
                    releases=tuple(budget._releases), reconciliations=tuple(budget._reconciliations),
                )
                budget._settlements.append(payload.settlement)
                settlement_events[payload.settlement.reservation_id] = item
            elif payload.action == "release":
                budget._releases.append(payload.release)
                budget.ledger
            elif payload.action == "reconcile":
                budget._reconciliations.append(payload.reconciliation)
                budget.ledger
        budget._recover_response_overrun(materialized)
        reservations = {r.reservation_id: r for r in budget._reservations}
        if any(reservations[s.reservation_id].amounts.money_cny is not None
               and s.estimated_cost_cny is None for s in budget.ledger.effective_settlements):
            budget._fatal_reason = "money_cny_usage_unavailable"
        elif total_limit.tokens is not None and (budget.ledger.committed.tokens or 0) > total_limit.tokens:
            budget._fatal_reason = "token_budget_exhausted"
        elif total_limit.money_cny is not None and (budget.ledger.committed.money_cny or 0) >= total_limit.money_cny:
            budget._fatal_reason = "money_budget_exhausted"
        return budget

    def _reduced_estimate(
        self, estimate: RequestEstimate
    ) -> RequestEstimate | None:
        if estimate.output_token_limit <= self.min_output_tokens:
            return None
        minimum = estimate.with_output_token_limit(
            self.min_output_tokens, self.pricing, self.cny_pricing
        )
        if not _fits(minimum.amounts, self.available):
            return None
        low, high = self.min_output_tokens, estimate.output_token_limit - 1
        best = minimum
        while low <= high:
            middle = (low + high) // 2
            candidate = estimate.with_output_token_limit(middle, self.pricing, self.cny_pricing)
            if _fits(candidate.amounts, self.available):
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    def _recover_response_overrun(self, events: list[object]) -> None:
        settled = {item.reservation_id for item in self._settlements}
        request_reservations: dict[str, str] = {}
        for item in events:
            payload = getattr(item, "payload", item)
            event_id = getattr(item, "event_id", None)
            reservation_id = getattr(payload, "reservation_id", None)
            if (
                getattr(payload, "event_type", None) == "adapter_request"
                and event_id is not None
                and reservation_id is not None
            ):
                request_reservations[event_id] = reservation_id
        reservations = {item.reservation_id: item for item in self._reservations}
        for item in events:
            payload = getattr(item, "payload", item)
            if getattr(payload, "event_type", None) != "model_response":
                continue
            reservation_id = request_reservations.get(payload.request_event_id)
            if reservation_id is None or reservation_id in settled:
                continue
            reservation = reservations.get(reservation_id)
            tokens = _reported_tokens(payload.usage)
            if (
                reservation is not None
                and (reservation.amounts.money_cny is not None or (
                    tokens is not None and reservation.amounts.tokens is not None
                    and tokens > reservation.amounts.tokens))
            ):
                # A durable response must be settled by Runtime recovery before
                # admitting more requests. Exceeding its estimate is not itself
                # a permanent failure once that full charge is journaled.
                self._fatal_reason = "unsettled_reported_usage"
                return

    def _stop(
        self,
        reason: str,
        *,
        reservation: BudgetReservation | None = None,
        exceeded: tuple[str, ...] = (),
    ) -> BudgetDecision:
        return BudgetDecision(
            action="stop",
            reason=reason,
            available=self.available,
            reservation=reservation,
            exceeded_dimensions=exceeded,
        )


def _fits(requested: BudgetAmounts, available: BudgetAmounts) -> bool:
    for name in ("tokens", "money_usd", "money_cny", "seconds", "calls"):
        ceiling = getattr(available, name)
        if ceiling is None:
            continue
        value = getattr(requested, name)
        if value is None or value > ceiling:
            return False
    return True


def _exceeded(
    requested: BudgetAmounts, available: BudgetAmounts
) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("tokens", "money_usd", "money_cny", "seconds", "calls")
        if getattr(available, name) is not None
        and (
            getattr(requested, name) is None
            or getattr(requested, name) > getattr(available, name)
        )
    )


def _exceeded_reservation(
    actual: BudgetAmounts, reservation: BudgetAmounts
) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("tokens", "money_usd", "money_cny", "seconds", "calls")
        if getattr(actual, name) is not None
        and (
            getattr(reservation, name) is None
            or getattr(actual, name) > getattr(reservation, name)
        )
    )


def _reported_tokens(usage: UsageEvidence) -> int | None:
    from src.harness_contracts.usage import reported_total_tokens
    return reported_total_tokens(usage.raw_usage) if usage.kind == "reported" else None


__all__ = [
    "BudgetDecision",
    "PriceSchedule",
    "RequestEstimate",
    "RuntimeBudget",
]
