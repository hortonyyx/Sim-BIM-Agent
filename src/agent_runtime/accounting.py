"""Separate provider usage, image estimates, budget charges, and CNY estimates.

Provider usage remains raw evidence. A separately billed image line is charged
even when the provider's input total already contains images. Currency values
are estimates from a checked-in price observation, never provider bills.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import io
from typing import Iterable, Mapping

from PIL import Image

from src.harness_contracts import UsageMissing, UsageReported

from .estimation import get_model_profile, qwen_image_tokens


@dataclass(frozen=True)
class CnyPriceSchedule:
    provider: str
    model: str
    text_input_cny_per_million: Decimal
    output_cny_per_million: Decimal
    image_input_cny_per_million: Decimal | None
    cached_input_cny_per_million: Decimal | None
    source: str
    image_billing_status: str

    def upper_bound(self, *, input_tokens: int, output_tokens: int,
                    image_tokens: int) -> Decimal:
        """Reserve without assuming any future cache hit; images are extra."""
        if self.cached_input_cny_per_million is None or self.image_input_cny_per_million is None:
            raise ValueError("CNY ceiling needs a complete registered price schedule")
        # Taking the larger input rate also covers a schedule with costly cache reads.
        input_rate = max(self.text_input_cny_per_million, self.cached_input_cny_per_million)
        return (Decimal(input_tokens) * input_rate
                + Decimal(output_tokens) * self.output_cny_per_million
                + Decimal(image_tokens) * self.image_input_cny_per_million) / Decimal(1_000_000)


_PARATERA_PRICE_SOURCE = (
    "AI_agent/workflow/models.md (2026-10-03 bill-derived rates; estimate only)"
)

_PARATERA_PRICES = {
    "qwen3.8-27b": CnyPriceSchedule(
        provider="paratera",
        model="Qwen3.8-27B",
        text_input_cny_per_million=Decimal("3.0"),
        output_cny_per_million=Decimal("12.0"),
        image_input_cny_per_million=Decimal("3.0"),
        cached_input_cny_per_million=Decimal("0.6"),
        source=_PARATERA_PRICE_SOURCE,
        image_billing_status="bill_observed_separate_at_text_input_rate",
    ),
    "qwen3.8-flash": CnyPriceSchedule(
        provider="paratera",
        model="Qwen3.8-Flash",
        text_input_cny_per_million=Decimal("1.0"),
        output_cny_per_million=Decimal("3.0"),
        image_input_cny_per_million=Decimal("1.0"),
        cached_input_cny_per_million=Decimal("0.1"),
        source=_PARATERA_PRICE_SOURCE,
        image_billing_status="bill_observed_separate_at_text_input_rate",
    ),
    "glm-5.3-flash": CnyPriceSchedule(
        provider="paratera",
        model="GLM-5.3-Flash",
        text_input_cny_per_million=Decimal("0.8"),
        output_cny_per_million=Decimal("2.8"),
        image_input_cny_per_million=None,
        cached_input_cny_per_million=None,
        source=_PARATERA_PRICE_SOURCE,
        image_billing_status="unknown_until_a_glm_image_bill_is_observed",
    ),
}


def get_cny_price_schedule(
    model: str,
    *,
    route_id: str | None = None,
    provider: str | None = None,
) -> CnyPriceSchedule | None:
    """Return an observed CNY schedule without treating it as a provider bill."""

    selected_provider = (provider or route_id or "").casefold()
    if selected_provider != "paratera":
        return None
    try:
        canonical = get_model_profile(model).canonical_name.casefold()
    except (TypeError, ValueError):
        canonical = model.casefold()
    return _PARATERA_PRICES.get(canonical)


def require_cny_price_schedule(model: str, *, route_id: str) -> CnyPriceSchedule:
    """Fail before send for subscription, unregistered or incomplete tariffs."""
    pricing = get_cny_price_schedule(model, route_id=route_id)
    if pricing is None:
        raise ValueError("CNY ceiling requires a registered metered Paratera model")
    pricing.upper_bound(input_tokens=0, output_tokens=0, image_tokens=0)
    return pricing


@dataclass(frozen=True)
class RequestUsageAccounting:
    provider_reported_tokens: int | None
    reported_image_tokens: int | None
    image_tokens_estimate: int
    reported_usage_includes_image_tokens: bool
    additional_image_tokens: int
    image_charge_source: str
    billing_mode: str
    budget_charge_tokens: int | None
    estimated_cost_cny: Decimal | None
    known_cost_components_cny: Decimal | None
    cost_estimate_complete: bool
    image_billing_status: str
    price_source: str | None
    note: str
    reported_input_tokens: int | None
    reported_cache_read_tokens: int | None
    reported_cache_write_tokens: int | None
    reported_output_tokens: int | None
    usage_basis: str

    def receipt_dict(self) -> dict[str, object]:
        result = asdict(self)
        for name in ("estimated_cost_cny", "known_cost_components_cny"):
            value = result[name]
            result[name] = None if value is None else str(value)
        return result


@dataclass(frozen=True)
class StoredRequestAccounting:
    request_event_id: str
    task_id: str
    model: str
    route_id: str
    accounting: RequestUsageAccounting
    original_usage_kind: str | None = None
    original_missing_reason: str | None = None
    reconciliation_event_id: str | None = None
    reconciliation_source: dict[str, object] | None = None

    def receipt_dict(self) -> dict[str, object]:
        return {
            "request_event_id": self.request_event_id,
            "task_id": self.task_id,
            "model": self.model,
            "route_id": self.route_id,
            "original_usage_kind": self.original_usage_kind,
            "original_missing_reason": self.original_missing_reason,
            "reconciliation_event_id": self.reconciliation_event_id,
            "reconciliation_source": self.reconciliation_source,
            **self.accounting.receipt_dict(),
        }


def account_request_usage(
    raw_usage: Mapping[str, object] | UsageReported | UsageMissing | None,
    *,
    image_tokens_estimate: int,
    pricing: CnyPriceSchedule | None,
    billing_mode: str = "metered_or_unknown",
    input_includes_images: bool = False,
) -> RequestUsageAccounting:
    """Reconcile one receipt while preserving reported and estimated quantities."""

    if type(image_tokens_estimate) is not int or image_tokens_estimate < 0:
        raise ValueError("image_tokens_estimate must be a non-negative integer")
    raw = _raw_usage(raw_usage)
    rejected = (raw or {}).get("runtime_usage_basis") == "rejected_before_processing"
    reported_total = _reported_total_tokens(raw) if raw is not None else None
    reported_image, _, image_in_total_attested = (
        _reported_image_tokens(raw)
    )
    # Protocol inclusion prevents double charging; it is not a usage receipt.
    # Missing/partial totals must keep their reservation and cannot attest to
    # a reported image-inclusive total, even on a route that includes images.
    images_in_input = image_in_total_attested or input_includes_images
    includes_images = (image_tokens_estimate > 0 and reported_total is not None
                       and not rejected and images_in_input)
    separate_images = bills_images_separately(pricing)
    image_charge = reported_image if reported_image is not None else image_tokens_estimate
    additional_images = 0 if rejected else image_charge if separate_images else (0 if images_in_input else image_tokens_estimate)
    budget_tokens = (
        None
        if reported_total is None
        else reported_total + additional_images
    )

    known_cost, complete = _estimate_cny(
        raw,
        image_tokens_estimate=image_tokens_estimate,
        reported_image_tokens=reported_image,
        image_in_total_attested=includes_images,
        pricing=None if billing_mode == "subscription" else pricing,
    )
    estimated_cost = known_cost if complete else None
    if rejected:
        # A local pre-send image estimate is not a charge for a refused request.
        known_cost = estimated_cost = None if billing_mode == "subscription" else Decimal(0)
        complete = billing_mode != "subscription"
    if image_tokens_estimate == 0:
        image_status = "not_applicable"
    elif pricing is None:
        image_status = "unknown_price_schedule"
    else:
        image_status = pricing.image_billing_status
    if raw is None:
        note = (
            "Provider usage is unavailable; the ledger retains the conservative "
            "reservation and the image estimate remains separate."
        )
    elif reported_total is None:
        note = (
            "Provider usage has no complete token total; partial counters remain "
            "reported separately and the ledger retains the conservative reservation."
        )
    elif separate_images and image_charge:
        note = (
            "The observed invoice charges the full prompt (including images) and "
            "a separate image line; the token budget adds that image line again."
        )
    elif includes_images:
        note = (
            "Provider usage includes image input by its breakdown or protocol definition, so the image estimate "
            "is reported separately and is not added again to the token charge."
        )
    elif image_tokens_estimate:
        note = (
            "Provider usage exposes no image-token breakdown; the image estimate is "
            "added to the token-budget charge, while raw usage remains unchanged."
        )
    else:
        note = "This request sent no images."
    if billing_mode == "subscription":
        note += " Subscription route: no usage-based currency estimate; token/time budgets still apply."
    if rejected:
        note = "Known unprocessed HTTP 429 rejection: zero token/image charge by classification, not a provider measurement."
    return RequestUsageAccounting(
        provider_reported_tokens=reported_total,
        reported_image_tokens=reported_image,
        image_tokens_estimate=image_tokens_estimate,
        reported_usage_includes_image_tokens=includes_images,
        additional_image_tokens=additional_images,
        image_charge_source=("provider_reported" if separate_images and reported_image is not None
                             else "formula_estimate" if additional_images else "none"),
        billing_mode=billing_mode,
        budget_charge_tokens=budget_tokens,
        estimated_cost_cny=estimated_cost,
        known_cost_components_cny=known_cost,
        cost_estimate_complete=complete,
        image_billing_status=image_status,
        price_source=None if pricing is None else pricing.source,
        note=note,
        reported_input_tokens=_usage_counter(raw, "input_tokens", "prompt_tokens"),
        reported_cache_read_tokens=_cache_read_counter(raw),
        reported_cache_write_tokens=_usage_counter(raw, "cache_creation_input_tokens"),
        reported_output_tokens=_usage_counter(raw, "output_tokens", "completion_tokens"),
        usage_basis="rejected_before_processing" if rejected else "provider_reported" if raw is not None else "missing",
    )


def _usage_counter(raw, *keys):
    for key in keys:
        value = (raw or {}).get(key)
        if type(value) is int and value >= 0:
            return value
    return None


def _cache_read_counter(raw):
    direct = _usage_counter(raw, "cache_read_input_tokens", "prompt_cache_hit_tokens")
    if direct is not None:
        return direct
    for name in ("input_tokens_details", "prompt_tokens_details"):
        details = (raw or {}).get(name)
        if isinstance(details, dict):
            value = _usage_counter(details, "cached_tokens")
            if value is not None:
                return value
    return None


def request_accounting_from_store(
    store,
    request_event_id: str,
    *,
    usage: Mapping[str, object] | UsageReported | UsageMissing | None = None,
) -> StoredRequestAccounting:
    """Rebuild accounting from a durable request, including after a restart."""

    request = next(
        (
            event
            for event in store.all_events
            if event.event_id == request_event_id
            and event.payload.event_type == "adapter_request"
        ),
        None,
    )
    if request is None:
        raise ValueError(f"adapter request event not found: {request_event_id}")
    identity = request.payload.versions.remote_model
    profile = get_model_profile(identity.remote_alias)
    image_tokens = 0
    request_body = None
    for transmission in request.payload.images:
        image_bytes = store.get_bytes(transmission.sent)
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
        if profile.image_estimator in {"qwen_vl_patch32_v1", "glm_vl_patch28_v1"}:
            tokens, _, _ = qwen_image_tokens(width, height, profile)
        elif profile.image_estimator in {"openai_patch32_v1", "openai_patch32_proxy_v1"}:
            from .estimation import openai_image_tokens
            from src.harness_contracts.events import _resolve_json_pointer
            # Verify/reconstruct once for this accounting call, not once per image.
            # A later call must reread the capture so persisted corruption is detected.
            if request_body is None:
                request_body = store.resolve(request.payload.final_request_body)
            block = _resolve_json_pointer(request_body, transmission.request_reference.rsplit("/", 1)[0])
            tokens, _, _ = openai_image_tokens(width, height, profile, detail=block.get("detail", "auto"))
        elif profile.image_estimator == "decoded_pixels_v0":
            tokens = width * height
        elif profile.image_estimator == "deepseek_vision_max1024_v1":
            tokens = 1024
        else:
            raise ValueError(
                f"unsupported image estimator {profile.image_estimator!r}"
            )
        image_tokens += tokens
    if usage is None:
        response = next(
            (
                event
                for event in store.all_events
                if event.payload.event_type == "model_response"
                and event.payload.request_event_id == request_event_id
            ),
            None,
        )
        usage = None if response is None else response.payload.usage
        if usage is None:
            settlement = next((event.payload.settlement for event in store.all_events
                if event.payload.event_type == "budget" and event.payload.action == "settle"
                and event.payload.settlement.reservation_id == request.payload.reservation_id), None)
            usage = None if settlement is None else settlement.usage
    original_usage = usage
    reconciliation = next((event for event in store.all_events
        if event.payload.event_type == "budget" and event.payload.action == "reconcile"
        and event.payload.reconciliation.request_event_id == request_event_id), None)
    if reconciliation is not None:
        receipt = reconciliation.payload.reconciliation
        original_settlement_event = next(event for event in store.all_events
            if event.event_id == receipt.settlement_event_id)
        original_usage = original_settlement_event.payload.settlement.usage
        reconciled_usage = receipt.settlement.usage
        if isinstance(usage, UsageReported) and usage != reconciled_usage:
            raise ValueError("supplied usage conflicts with the journaled reconciliation")
        usage = reconciled_usage
    # A read-only projection of older journals applies the same narrow rule;
    # original events/settlements stay untouched and unknown outcomes stay unknown.
    if usage is None or isinstance(usage, UsageMissing):
        from .failures import is_unprocessed_rejection, rejected_request_usage
        failure = next((e.payload.model_failure for e in store.all_events
            if e.payload.event_type == "run_lifecycle" and e.payload.model_failure
            and e.payload.model_failure.request_event_id == request_event_id), None)
        if failure is not None and is_unprocessed_rejection(failure):
            usage = rejected_request_usage()
    pricing = get_cny_price_schedule(
        identity.remote_alias, route_id=identity.route_id
    )
    accounting = account_request_usage(
        usage, image_tokens_estimate=image_tokens, pricing=pricing,
        billing_mode="subscription" if identity.route_id in {"glm-subscription", "glm-subscription-anthropic", "chatgpt-subscription", "opencode-go"} else "metered_or_unknown",
        input_includes_images=identity.route_id in {"chatgpt-subscription", "opencode-go", "deepseek"})
    if reconciliation is not None:
        receipt = reconciliation.payload.reconciliation
        settled = receipt.settlement
        accounting = replace(accounting,
            budget_charge_tokens=settled.effective_tokens,
            additional_image_tokens=settled.effective_tokens - settled.actual.tokens,
            reported_usage_includes_image_tokens=settled.reported_usage_includes_image_tokens,
            estimated_cost_cny=settled.estimated_cost_cny,
            known_cost_components_cny=settled.estimated_cost_cny,
            cost_estimate_complete=settled.estimated_cost_cny is not None,
            usage_basis="reconciled_provider_reported",
            price_source="journaled reconciliation; see the original request rate evidence",
            note=accounting.note + " Late provider receipt appended; original missing usage is preserved.")
    return StoredRequestAccounting(
        request_event_id=request_event_id,
        task_id=request.task_id,
        model=identity.remote_alias,
        route_id=identity.route_id,
        accounting=accounting,
        original_usage_kind=getattr(original_usage, "kind", None),
        original_missing_reason=getattr(original_usage, "reason", None),
        reconciliation_event_id=None if reconciliation is None else reconciliation.event_id,
        reconciliation_source=None if reconciliation is None else reconciliation.payload.reconciliation.evidence.model_dump(mode="json"),
    )


def summarize_request_accounting(
    records: Iterable[StoredRequestAccounting],
) -> dict[str, object]:
    """Aggregate a root receipt with independently inspectable task/model slices."""

    materialized = tuple(records)

    def summarize(rows: Iterable[StoredRequestAccounting]) -> dict[str, object]:
        selected = tuple(rows)
        reported = [r.accounting.provider_reported_tokens for r in selected]
        charges = [r.accounting.budget_charge_tokens for r in selected]
        costs = [r.accounting.known_cost_components_cny for r in selected]
        complete_usage = all(value is not None for value in reported)
        complete_cost = all(r.accounting.cost_estimate_complete for r in selected)
        known_cost = (
            sum((value for value in costs if value is not None), Decimal(0))
            if any(value is not None for value in costs)
            else None
        )
        return {
            "requests": len(selected),
            "reconciled_requests": sum(r.reconciliation_event_id is not None for r in selected),
            "original_missing_usage_requests": sum(r.original_usage_kind == "missing" for r in selected),
            "rejected_unprocessed_requests": sum(r.accounting.usage_basis == "rejected_before_processing" for r in selected),
            **{field: (sum(values) if values and all(v is not None for v in values) else None)
               for field in ("reported_input_tokens", "reported_cache_read_tokens", "reported_cache_write_tokens", "reported_output_tokens")
               for values in [[getattr(r.accounting, field) for r in selected]]},
            "provider_reported_tokens": (
                sum(value for value in reported if value is not None)
                if complete_usage
                else None
            ),
            "image_tokens_estimate": sum(
                r.accounting.image_tokens_estimate for r in selected
            ),
            "additional_image_tokens": sum(r.accounting.additional_image_tokens for r in selected),
            "billing_modes": sorted({r.accounting.billing_mode for r in selected}),
            "budget_charge_tokens": (
                sum(value for value in charges if value is not None)
                if all(value is not None for value in charges)
                else None
            ),
            "estimated_cost_cny": str(known_cost) if complete_cost and known_cost is not None else None,
            "known_cost_components_cny": None if known_cost is None else str(known_cost),
            "usage_complete": complete_usage,
            "cost_estimate_complete": complete_cost,
        }

    models = sorted({record.model for record in materialized})
    tasks = sorted({record.task_id for record in materialized})
    return {
        **summarize(materialized),
        "by_model": {
            model: summarize(r for r in materialized if r.model == model)
            for model in models
        },
        "by_task": {
            task: summarize(r for r in materialized if r.task_id == task)
            for task in tasks
        },
        "currency_note": "CNY values are estimates from observed rates, not bills. Subscription requests have no usage-based currency estimate.",
    }


def _raw_usage(
    usage: Mapping[str, object] | UsageReported | UsageMissing | None,
) -> Mapping[str, object] | None:
    if usage is None or isinstance(usage, UsageMissing):
        return None
    if isinstance(usage, UsageReported):
        return usage.raw_usage
    return usage


def _reported_total_tokens(raw: Mapping[str, object] | None) -> int | None:
    from src.harness_contracts.usage import reported_total_tokens
    return reported_total_tokens(dict(raw)) if raw is not None else None


def _reported_image_tokens(
    raw: Mapping[str, object] | None,
) -> tuple[int | None, bool, bool]:
    if raw is None:
        return None, False, False
    for name in ("prompt_tokens_details", "input_tokens_details"):
        details = raw.get(name)
        if not isinstance(details, Mapping) or "image_tokens" not in details:
            continue
        value = details.get("image_tokens")
        if type(value) is int and value >= 0:
            prompt = raw.get("prompt_tokens", raw.get("input_tokens"))
            text = details.get("text_tokens")
            included = (
                type(prompt) is int
                and prompt >= 0
                and type(text) is int
                and text >= 0
                and prompt == text + value
            )
            return value, True, included
    return None, False, False


def _estimate_cny(
    raw: Mapping[str, object] | None,
    *,
    image_tokens_estimate: int,
    reported_image_tokens: int | None,
    image_in_total_attested: bool,
    pricing: CnyPriceSchedule | None,
) -> tuple[Decimal | None, bool]:
    if raw is None or pricing is None:
        return None, False
    prompt = raw.get("prompt_tokens", raw.get("input_tokens"))
    completion = raw.get("completion_tokens", raw.get("output_tokens"))
    if type(prompt) is not int or prompt < 0 or type(completion) is not int or completion < 0:
        return None, False
    image_in_prompt = (
        (reported_image_tokens or 0) if image_in_total_attested else 0
    )
    text_input = prompt if bills_images_separately(pricing) else max(0, prompt - image_in_prompt)
    cached = 0
    for name in ("prompt_tokens_details", "input_tokens_details"):
        details = raw.get(name)
        if isinstance(details, Mapping):
            value = details.get("cached_tokens")
            if value is not None:
                if type(value) is not int or not 0 <= value <= text_input:
                    return None, False
                cached = value
                break
    uncached = text_input - cached
    million = Decimal(1_000_000)
    known = (
        Decimal(uncached) * pricing.text_input_cny_per_million
        + Decimal(completion) * pricing.output_cny_per_million
    ) / million
    complete = True
    if cached:
        if pricing.cached_input_cny_per_million is None:
            complete = False
        else:
            known += Decimal(cached) * pricing.cached_input_cny_per_million / million
    image_charge = reported_image_tokens if reported_image_tokens is not None else image_tokens_estimate
    if image_charge:
        if pricing.image_input_cny_per_million is None:
            complete = False
        else:
            known += (
                Decimal(image_charge)
                * pricing.image_input_cny_per_million
                / million
            )
    return known, complete


def bills_images_separately(pricing: CnyPriceSchedule | None) -> bool:
    """Only the observed Paratera Qwen invoice supports this extra charge."""
    return pricing is not None and pricing.image_billing_status == "bill_observed_separate_at_text_input_rate"


__all__ = [
    "CnyPriceSchedule",
    "RequestUsageAccounting",
    "StoredRequestAccounting",
    "account_request_usage",
    "get_cny_price_schedule",
    "request_accounting_from_store",
    "summarize_request_accounting",
]
