"""Budget reservations, usage evidence, and settlement validation."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from .base import ContractModel, NonEmptyStr
from .refs import SourceRef


class BudgetAmounts(ContractModel):
    tokens: int | None = Field(default=None, ge=0)
    money_usd: Decimal | None = Field(default=None, ge=0)
    money_cny: Decimal | None = Field(default=None, ge=0)
    seconds: Decimal | None = Field(default=None, ge=0)
    calls: int | None = Field(default=None, ge=0)

    def add(self, other: BudgetAmounts) -> BudgetAmounts:
        def add_optional(left: int | Decimal | None, right: int | Decimal | None):
            if left is None and right is None:
                return None
            return (left if left is not None else 0) + (right if right is not None else 0)

        return BudgetAmounts(
            tokens=add_optional(self.tokens, other.tokens),
            money_usd=add_optional(self.money_usd, other.money_usd),
            money_cny=add_optional(self.money_cny, other.money_cny),
            seconds=add_optional(self.seconds, other.seconds),
            calls=add_optional(self.calls, other.calls),
        )

    def subtract(self, other: BudgetAmounts) -> BudgetAmounts:
        """Subtract known dimensions without turning an unbounded one into zero."""

        def subtract_optional(
            left: int | Decimal | None, right: int | Decimal | None
        ) -> int | Decimal | None:
            if left is None:
                return None
            return left - (right or 0)

        return BudgetAmounts(
            tokens=subtract_optional(self.tokens, other.tokens),
            money_usd=subtract_optional(self.money_usd, other.money_usd),
            money_cny=subtract_optional(self.money_cny, other.money_cny),
            seconds=subtract_optional(self.seconds, other.seconds),
            calls=subtract_optional(self.calls, other.calls),
        )


class UsageReported(ContractModel):
    kind: Literal["reported"] = "reported"
    raw_usage: dict[str, JsonValue]

    @model_validator(mode="after")
    def raw_usage_cannot_be_empty(self) -> UsageReported:
        if not self.raw_usage:
            raise ValueError("reported usage must preserve the non-empty raw object")
        return self


class UsageMissing(ContractModel):
    kind: Literal["missing"] = "missing"
    reason: NonEmptyStr


UsageEvidence = Annotated[UsageReported | UsageMissing, Field(discriminator="kind")]


class ReportedCost(ContractModel):
    kind: Literal["reported"] = "reported"
    usd: Decimal = Field(ge=0)


class EstimatedCostUpperBound(ContractModel):
    kind: Literal["estimated_upper_bound"] = "estimated_upper_bound"
    usd: Decimal = Field(ge=0)
    reason: NonEmptyStr


class CostUnavailable(ContractModel):
    """No price or bill was reported; unknown is not a zero-dollar estimate."""

    kind: Literal["unavailable"] = "unavailable"
    reason: NonEmptyStr


CostEvidence = Annotated[
    ReportedCost | EstimatedCostUpperBound | CostUnavailable,
    Field(discriminator="kind"),
]


class BudgetReservation(ContractModel):
    reservation_id: NonEmptyStr
    purpose: Literal["primary_task", "child_task", "context_summary", "retry"]
    amounts: BudgetAmounts
    task_id: NonEmptyStr

    @model_validator(mode="after")
    def require_nonempty_reservation(self) -> BudgetReservation:
        if not _has_positive_dimension(self.amounts):
            raise ValueError("a reservation needs at least one positive budget dimension")
        return self


class BudgetSettlement(ContractModel):
    reservation_id: NonEmptyStr
    actual: BudgetAmounts
    usage: UsageEvidence
    cost: CostEvidence
    image_tokens_estimate: int = Field(default=0, ge=0)
    reported_usage_includes_image_tokens: bool = False
    # None preserves historical ledger semantics; new receipts supply the
    # separately charged amount independently of what raw usage includes.
    additional_image_tokens: int | None = Field(default=None, ge=0)
    token_overrun: int = Field(default=0, ge=0)
    # A rate-based charge, deliberately separate from actual/provider billing.
    estimated_cost_cny: Decimal | None = Field(default=None, ge=0)
    money_cny_overrun: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def missing_usage_requires_estimated_cost(self) -> BudgetSettlement:
        if self.actual.money_cny is not None:
            raise ValueError("CNY estimates must not be stored as actual provider bills")
        if self.estimated_cost_cny is not None and (
            self.usage.kind != "reported"
            or _reported_total_tokens(self.usage.raw_usage) is None
        ):
            raise ValueError("a settled CNY estimate requires reported token usage")
        if self.image_tokens_estimate == 0 and self.reported_usage_includes_image_tokens:
            raise ValueError(
                "reported usage cannot include image tokens when the request had none"
            )
        if self.usage.kind == "missing":
            if self.actual.tokens is not None:
                raise ValueError("missing token usage must remain None, not zero or an estimate")
            if self.cost.kind == "reported":
                raise ValueError(
                    "when raw usage is missing, cost must be an explicit estimated upper bound"
                )
            if self.reported_usage_includes_image_tokens:
                raise ValueError(
                    "missing usage cannot attest that reported usage includes image tokens"
                )
        if self.cost.kind == "reported":
            if self.actual.money_usd != self.cost.usd:
                raise ValueError("reported cost must equal actual.money_usd")
        elif self.actual.money_usd is not None:
            raise ValueError(
                "an estimated cost upper bound must not be stored as actual.money_usd"
            )
        if self.usage.kind == "reported" and (
            self.image_tokens_estimate or self.reported_usage_includes_image_tokens or self.additional_image_tokens
        ):
            reported_tokens = _reported_total_tokens(self.usage.raw_usage)
            if reported_tokens is not None and self.actual.tokens != reported_tokens:
                raise ValueError(
                    "image accounting must preserve provider-reported token usage"
                )
        if self.token_overrun:
            if self.usage.kind != "reported":
                raise ValueError("a token overrun requires reported usage")
            reported_tokens = _reported_total_tokens(self.usage.raw_usage)
            if reported_tokens is None:
                raise ValueError(
                    "a token overrun requires raw total token usage or input/output usage"
                )
            if self.actual.tokens != reported_tokens:
                raise ValueError(
                    "reported raw token usage must equal actual.tokens for an overrun"
                )
        return self

    @property
    def effective_tokens(self) -> int | None:
        """Budget charge while keeping provider-reported usage unmodified."""

        if self.actual.tokens is None:
            return None
        if self.additional_image_tokens is not None:
            return self.actual.tokens + self.additional_image_tokens
        if self.reported_usage_includes_image_tokens:
            return self.actual.tokens
        return self.actual.tokens + self.image_tokens_estimate


class BudgetRelease(ContractModel):
    """Append-only abandonment of a reservation proven never dispatched."""

    reservation_id: NonEmptyStr
    reason: NonEmptyStr


class BudgetReconciliation(ContractModel):
    """A sourced late receipt; the original unknown settlement remains intact."""

    reservation_id: NonEmptyStr
    request_event_id: NonEmptyStr
    settlement_event_id: NonEmptyStr
    evidence: SourceRef
    settlement: BudgetSettlement

    @model_validator(mode="after")
    def require_reported_receipt(self) -> BudgetReconciliation:
        if self.settlement.reservation_id != self.reservation_id:
            raise ValueError("reconciliation settlement must use the same reservation")
        if self.evidence.blob is None or self.evidence.blob.kind != "sha256":
            raise ValueError("reconciliation requires hashed source evidence")
        if (self.settlement.usage.kind != "reported"
                or _reported_total_tokens(self.settlement.usage.raw_usage) is None
                or self.settlement.actual.tokens != _reported_total_tokens(self.settlement.usage.raw_usage)):
            raise ValueError("reconciliation needs complete reported token usage")
        return self


class BudgetLedger(ContractModel):
    total_limit: BudgetAmounts
    reservations: tuple[BudgetReservation, ...]
    settlements: tuple[BudgetSettlement, ...] = ()
    releases: tuple[BudgetRelease, ...] = ()
    reconciliations: tuple[BudgetReconciliation, ...] = ()

    @property
    def effective_settlements(self) -> tuple[BudgetSettlement, ...]:
        replacements = {r.reservation_id: r.settlement for r in self.reconciliations}
        return tuple(replacements.get(s.reservation_id, s) for s in self.settlements)

    @model_validator(mode="after")
    def validate_ledger(self) -> BudgetLedger:
        if not _has_positive_dimension(self.total_limit):
            raise ValueError("total budget needs at least one positive dimension")
        reservation_by_id: dict[str, BudgetReservation] = {}
        for item in self.reservations:
            if item.reservation_id in reservation_by_id:
                raise ValueError(f"duplicate reservation_id: {item.reservation_id}")
            reservation_by_id[item.reservation_id] = item

        originals = {s.reservation_id: s for s in self.settlements}
        if len(originals) != len(self.settlements):
            raise ValueError("reservation settled twice")
        released: set[str] = set()
        for release in self.releases:
            if release.reservation_id not in reservation_by_id:
                raise ValueError("release has no reservation")
            if release.reservation_id in released or release.reservation_id in originals:
                raise ValueError("reservation already released or settled")
            released.add(release.reservation_id)
        reconciled: set[str] = set()
        for receipt in self.reconciliations:
            old = originals.get(receipt.reservation_id)
            new = receipt.settlement
            if old is None or old.usage.kind != "missing":
                raise ValueError("reconciliation must target an original missing-usage settlement")
            if receipt.reservation_id in reconciled:
                raise ValueError("reservation reconciled twice")
            reconciled.add(receipt.reservation_id)
            for name in ("seconds", "calls", "money_usd"):
                if getattr(old.actual, name) is not None and getattr(new.actual, name) != getattr(old.actual, name):
                    raise ValueError("reconciliation cannot change known actual charges")
            for name in ("image_tokens_estimate",):
                if getattr(old, name) != getattr(new, name):
                    raise ValueError("reconciliation cannot change the request image allowance")
            reservation = reservation_by_id[receipt.reservation_id]
            if reservation.amounts.money_cny is not None and new.estimated_cost_cny is None:
                raise ValueError("reconciliation must resolve the unknown CNY charge")
        settled: set[str] = set()
        for item in self.effective_settlements:
            if item.reservation_id in settled:
                raise ValueError(f"reservation settled twice: {item.reservation_id}")
            settled.add(item.reservation_id)
            reservation = reservation_by_id.get(item.reservation_id)
            if reservation is None:
                raise ValueError(f"settlement has no reservation: {item.reservation_id}")
            reserved_tokens = reservation.amounts.tokens
            actual_tokens = item.effective_tokens
            expected_overrun = (
                max(actual_tokens - reserved_tokens, 0)
                if actual_tokens is not None and reserved_tokens is not None
                else 0
            )
            if item.token_overrun != expected_overrun:
                raise ValueError(
                    f"settlement token_overrun differs from actual minus reservation: "
                    f"{item.reservation_id}"
                )
            expected_cny_overrun = (
                max(item.estimated_cost_cny - reservation.amounts.money_cny, Decimal(0))
                if item.estimated_cost_cny is not None and reservation.amounts.money_cny is not None
                else Decimal(0)
            )
            if item.money_cny_overrun != expected_cny_overrun:
                raise ValueError("settlement money_cny_overrun differs from charge minus reservation")
            effective_actual = item.actual.model_copy(
                update={"tokens": item.effective_tokens, "money_cny": item.estimated_cost_cny}
            )
            _ensure_within_reservation(
                effective_actual,
                reservation.amounts,
                f"settlement exceeds reservation: {item.reservation_id}",
                allow_token_overrun=bool(item.token_overrun),
                allow_cny_overrun=bool(item.money_cny_overrun),
                allow_unbounded_seconds=self.total_limit.seconds is None,
            )
            if item.cost.kind == "unavailable":
                if reservation.amounts.money_usd is not None:
                    raise ValueError("unknown cost cannot settle a money-limited reservation")
                continue
            if reservation.amounts.money_usd is None:
                raise ValueError(
                    f"settlement cost has no money reservation: {item.reservation_id}"
                )
            if item.cost.usd > reservation.amounts.money_usd:
                raise ValueError(
                    f"settlement cost exceeds reservation: {item.reservation_id}"
                )
        committed_for_limit = self.committed.model_copy(
            update={
                "tokens": (
                    None
                    if self.committed.tokens is None
                    else self.committed.tokens
                    - sum(item.token_overrun for item in self.effective_settlements)
                ),
                "money_cny": (
                    None if self.committed.money_cny is None else self.committed.money_cny
                    - sum(item.money_cny_overrun for item in self.effective_settlements)
                ),
            }
        )
        _ensure_within_limit(
            committed_for_limit,
            self.total_limit,
            "effective charges and outstanding reservations exceed total budget",
        )
        return self

    @property
    def outstanding(self) -> BudgetAmounts:
        """Full conservative holds for reservations without a settlement."""

        settled = {item.reservation_id for item in self.settlements} | {item.reservation_id for item in self.releases}
        total = BudgetAmounts()
        for reservation in self.reservations:
            if reservation.reservation_id not in settled:
                total = total.add(reservation.amounts)
        return total

    @property
    def charged(self) -> BudgetAmounts:
        """Effective settled charge, retaining holds where evidence is unknown."""

        reservation_by_id = {
            item.reservation_id: item for item in self.reservations
        }
        total = BudgetAmounts()
        for settlement in self.effective_settlements:
            reservation = reservation_by_id.get(settlement.reservation_id)
            if reservation is not None:
                total = total.add(_effective_charge(reservation, settlement))
        return total

    @property
    def committed(self) -> BudgetAmounts:
        """Settled effective charge plus every still-outstanding reservation."""

        return self.charged.add(self.outstanding)

    @property
    def available(self) -> BudgetAmounts:
        """Remaining configured capacity; None continues to mean unbounded."""

        committed = self.committed
        for name in ("tokens", "money_cny"):
            limit, value = getattr(self.total_limit, name), getattr(committed, name)
            if limit is not None and value is not None and value > limit:
                committed = committed.model_copy(update={name: limit})
        return self.total_limit.subtract(committed)


def _effective_charge(
    reservation: BudgetReservation, settlement: BudgetSettlement
) -> BudgetAmounts:
    """Return what remains committed after a settlement, dimension by dimension."""

    def actual_or_hold(name: str):
        actual = getattr(settlement.actual, name)
        return actual if actual is not None else getattr(reservation.amounts, name)

    if settlement.cost.kind == "reported":
        money = settlement.cost.usd
    elif settlement.cost.kind == "estimated_upper_bound":
        money = settlement.cost.usd
    else:
        money = reservation.amounts.money_usd
    return BudgetAmounts(
        tokens=(
            reservation.amounts.tokens
            if settlement.actual.tokens is None
            else settlement.effective_tokens
        ),
        money_usd=money,
        money_cny=(settlement.estimated_cost_cny if settlement.estimated_cost_cny is not None
                   else reservation.amounts.money_cny),
        seconds=actual_or_hold("seconds"),
        calls=actual_or_hold("calls"),
    )


def _ensure_within_reservation(
    actual: BudgetAmounts,
    limit: BudgetAmounts,
    message: str,
    *,
    allow_token_overrun: bool = False,
    allow_cny_overrun: bool = False,
    allow_unbounded_seconds: bool = False,
) -> None:
    for name in ("tokens", "money_usd", "money_cny", "seconds", "calls"):
        value = getattr(actual, name)
        ceiling = getattr(limit, name)
        if name == "seconds" and ceiling is None and allow_unbounded_seconds:
            continue
        if name == "tokens" and allow_token_overrun and ceiling is not None:
            continue
        if name == "money_cny" and allow_cny_overrun and ceiling is not None:
            continue
        if value is not None and (ceiling is None or value > ceiling):
            raise ValueError(f"{message} ({name})")


def _ensure_within_limit(
    actual: BudgetAmounts, limit: BudgetAmounts, message: str
) -> None:
    for name in ("tokens", "money_usd", "money_cny", "seconds", "calls"):
        value = getattr(actual, name)
        ceiling = getattr(limit, name)
        if ceiling is not None and value is not None and value > ceiling:
            raise ValueError(f"{message} ({name})")


def _has_positive_dimension(amounts: BudgetAmounts) -> bool:
    return any(
        value is not None and value > 0
        for value in (
            amounts.tokens,
            amounts.money_usd,
            amounts.money_cny,
            amounts.seconds,
            amounts.calls,
        )
    )


def _reported_total_tokens(raw_usage: dict[str, JsonValue]) -> int | None:
    from .usage import reported_total_tokens
    return reported_total_tokens(raw_usage)
