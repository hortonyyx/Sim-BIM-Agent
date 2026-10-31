"""A8 correction preflight for reading evidence debt.

The authoritative facts stay in the 0_reading ``CheckReport``. This module only
projects the evidence-check subset into a lightweight handoff artifact for
1_correction, re-evaluating disposition under the *current* run profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agent.execution.manifest import hash_obj

from src.validator.checks.reading_product import check_reading_product
from src.validator.checks.schema import (
    CheckReport,
    Disposition,
    RunProfile,
    disposition,
    is_evidence_check_id,
)

EVIDENCE_DEBT_SCHEMA_VERSION = "2"
EVIDENCE_DEBT_PRODUCER = "src.agent.execution.evidence_preflight"


class EvidenceDebtItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    canonical_check_id: str
    view: str | None = None
    status: str
    layer: str
    disposition: str
    message: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    scope: str = "view_global"
    offender_ids: list[str] = Field(default_factory=list)
    debt_id: str = ""

    @model_validator(mode="after")
    def _stable_id(self):
        payload = {
            "check_id": self.check_id, "canonical_check_id": self.canonical_check_id,
            "view": self.view, "scope": self.scope,
            "offender_ids": sorted(self.offender_ids),
        }
        computed = hash_obj(payload)
        if self.debt_id and self.debt_id != computed:
            raise ValueError("debt_id does not match canonical evidence-debt identity")
        self.debt_id = computed
        return self


class EvidenceDebt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EVIDENCE_DEBT_SCHEMA_VERSION
    producer: str = EVIDENCE_DEBT_PRODUCER
    run_profile: RunProfile = "exploratory"
    capability_profile: str = "rectangular"
    source_stage: str = "0_reading"
    debts: list[EvidenceDebtItem] = Field(default_factory=list)

    @property
    def blocking(self) -> list[EvidenceDebtItem]:
        return [item for item in self.debts if item.disposition == Disposition.BLOCK.value]


def canonical_evidence_check_id(check_id: str) -> str:
    marker = ".reading."
    if check_id.startswith("reading."):
        return check_id
    if marker in check_id:
        return check_id[check_id.index(marker) + 1 :]
    return check_id


def evidence_view_name(check_id: str) -> str | None:
    marker = ".reading."
    if marker not in check_id:
        return None
    prefix = check_id[: check_id.index(marker)]
    return prefix or None


def _offender_ids(evidence: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    offenders = evidence.get("offenders")
    if isinstance(offenders, list):
        for offender in offenders:
            if not isinstance(offender, dict):
                continue
            value = offender.get("stroke_id")
            if isinstance(value, str) and value:
                ids.append(value)
    return sorted(dict.fromkeys(ids))


def project_evidence_debt(
    report: CheckReport,
    *,
    run_profile: RunProfile = "exploratory",
) -> EvidenceDebt:
    """Project evidence-check failures from a CheckReport under current policy."""
    items: list[EvidenceDebtItem] = []
    for result in report.results:
        if not is_evidence_check_id(result.check_id):
            continue
        disp = disposition(
            result,
            capability_profile=report.capability_profile,
            run_profile=run_profile,
        )
        if disp not in (Disposition.BLOCK, Disposition.FLAG):
            continue
        offender_ids = _offender_ids(result.evidence)
        items.append(
            EvidenceDebtItem(
                check_id=result.check_id,
                canonical_check_id=canonical_evidence_check_id(result.check_id),
                view=evidence_view_name(result.check_id),
                status=result.status.value,
                layer=result.layer.value,
                disposition=disp.value,
                message=result.message,
                evidence=dict(result.evidence),
                scope="element_local" if offender_ids else "view_global",
                offender_ids=offender_ids,
            )
        )
    return EvidenceDebt(
        run_profile=run_profile,
        capability_profile=report.capability_profile,
        source_stage=report.stage,
        debts=items,
    )


def dimensioned_view_names_from_testdata_text(testdata_text: str | None) -> set[str]:
    if not testdata_text:
        return set()
    try:
        data = json.loads(testdata_text)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, dict):
        return set()

    names: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        p = Path(value)
        names.add(p.stem if p.suffix else value)

    for value in data.get("dimensioned_views") or []:
        add(value)

    for item in data.get("Floor plans") or []:
        if isinstance(item, dict) and item.get("dimensioned") is True:
            add(item.get("path"))
            floor = item.get("floor")
            if floor is not None:
                add(f"{floor}f_view")

    views = data.get("views") or {}
    if isinstance(views, dict):
        for key, item in views.items():
            if isinstance(item, dict) and item.get("dimensioned") is True:
                add(key)
                add(item.get("path"))

    return names


def compute_evidence_debt_from_vector_dir(
    vector_dir: Path,
    *,
    run_profile: RunProfile = "exploratory",
    capability_profile: str = "rectangular",
    dimensioned_views: set[str] | None = None,
    dimensioned_states: dict[str, str] | None = None,
) -> EvidenceDebt:
    """Run reading checks and project only evidence debt for correction."""
    report = compute_reading_report_from_vector_dir(
        vector_dir,
        run_profile=run_profile,
        capability_profile=capability_profile,
        dimensioned_views=dimensioned_views,
        dimensioned_states=dimensioned_states,
    )
    return project_evidence_debt(report, run_profile=run_profile)


def compute_reading_report_from_vector_dir(
    vector_dir: Path,
    *,
    run_profile: RunProfile = "exploratory",
    capability_profile: str = "rectangular",
    dimensioned_views: set[str] | None = None,
    dimensioned_states: dict[str, str] | None = None,
) -> CheckReport:
    """Run the full S0 reading checks once and aggregate per-view results.

    R1-3 (派工单 §1.3): ``dimensioned_states`` (per-stem 4-state) is the
    fidelity-carrying input — it is forwarded to ``check_reading_view`` as the
    authoritative ``dimensioned_state`` instead of being folded to a bool.
    ``dimensioned_views`` (a bool set) is kept for backward compatibility: when
    no 4-state map is given it is promoted to ``{stem: "declared_true"}``, which
    matches the pre-R1-3 behavior exactly (only an affirmative declared_true
    counted as dimensioned).
    """
    vector_dir = Path(vector_dir)
    if dimensioned_states is None:
        dimensioned_states = {stem: "declared_true" for stem in (dimensioned_views or set())}
    merged = CheckReport(
        stage="0_reading",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    for path in sorted(vector_dir.glob("*_view.json")):
        sub = check_reading_product(
            json.loads(path.read_text(encoding="utf-8")),
            capability_profile=capability_profile,
            run_profile=run_profile,
            dimensioned_state=dimensioned_states.get(path.stem, "legacy_default"),
        )
        for result in sub.results:
            merged.results.append(
                result.model_copy(update={"check_id": f"{path.stem}.{result.check_id}"})
            )
    return merged


def write_evidence_debt(path: Path, debt: EvidenceDebt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(debt.model_dump_json(indent=2), encoding="utf-8")


# ── W-1 T2-⑤ / rework BLK-A (2026-09-07): the as_drawn leg's channel-split debt ── #
WINDOW_EVIDENCE_CHANNEL_SPLIT_DEBT_ID = "WINDOW_EVIDENCE_ON_CHAIN_NOT_ON_LEDGER"
#: The evidence-chain profile axis this debt's disposition is decided on —
#: the CHAIN's own axis (``decision_executor``'s Literal), ⛔ not RunProfile.
_CHAIN_PROFILE_AXIS = frozenset({"exploratory", "strict"})


def window_evidence_channel_split_debt(*, chain_profile: str) -> EvidenceDebt:
    """The as_drawn leg's window-evidence channel split, as a FILED debt.

    W-1 T2-⑤ (ratified 2026-09-07i) accepted that on the evidence-chain leg
    the window evidence travels as the chain's ``opening_claims`` while the
    legacy pen-stroke window ledger stays empty — under the explicit
    condition that the absence is a RECORDED fact, ⛔ never a silent
    emptiness.  This factory IS that record (rework BLK-A, 2026-09-07l: for
    a while the identifier existed only in two docstrings — zero code, so
    the acceptance premise was false on disk).

    ⭐ 2026-09-08h 退休条件（实测，管线侧 ``pipeline.py`` 的 S1 接线执行）：
    补窗落地后这条腿的窗证据有了自己的台账载体（平面 opening 目录 +
    claim links），且有 ``window_position_evidence_shadow`` 这道独立门在
    行使 ⇒ 「on chain NOT on ledger」不再为真，S1 改为**只在平面产物零
    window 洞口时**调用本工厂；退休时写**空债账**（``debts=[]``）记账，
    ⛔ 不是删机制 —— 平面侧 window 证据退回为零的那一刻，债会重新落盘。

    Disposition follows the EVIDENCE-CHAIN profile axis the wiring runs
    under: ``exploratory`` flags it, ``strict`` blocks it (the wiring fails
    closed AFTER filing, so the refusal itself is auditable).  The ledger's
    own ``run_profile`` field speaks the READING-debt policy axis
    (RunProfile); the two axes share only the word "exploratory".  The
    chain's word travels VERBATIM in ``evidence["evidence_chain_profile"]``
    and the field gets the same-side RunProfile ("exploratory" permissive /
    "regression" non-permissive) — a recorded translation, ⛔ not a second
    debt mechanism.
    """
    if chain_profile not in _CHAIN_PROFILE_AXIS:
        raise ValueError(
            "chain_profile must be one of the evidence-chain axis words "
            f"{sorted(_CHAIN_PROFILE_AXIS)}, got {chain_profile!r}"
        )
    item = EvidenceDebtItem(
        check_id=WINDOW_EVIDENCE_CHANNEL_SPLIT_DEBT_ID,
        canonical_check_id=WINDOW_EVIDENCE_CHANNEL_SPLIT_DEBT_ID,
        view=None,
        status="fail",
        layer="cross_check",
        disposition=(
            Disposition.FLAG.value
            if chain_profile == "exploratory"
            else Disposition.BLOCK.value
        ),
        message=(
            "the as_drawn evidence-chain leg carries its window evidence on "
            "the chain (opening_claims); the legacy pen-stroke window ledger "
            "is an ACCOUNTED empty set — the channel split is this filed "
            "debt, ⛔ never a silent emptiness"
        ),
        evidence={
            "evidence_chain_profile": chain_profile,
            "channel": "window_evidence",
            "on_chain": "opening_claims",
            "on_ledger": 0,
            "reason": "as_drawn products carry no pen strokes, so the legacy "
            "ledger gate has no teeth on this leg (ratified 2026-09-07i "
            "T2-⑤, under this filed-debt condition)",
        },
        scope="view_global",
        offender_ids=[],
    )
    return EvidenceDebt(
        run_profile=(
            "exploratory" if chain_profile == "exploratory" else "regression"
        ),
        source_stage="1_correction",
        debts=[item],
    )


def load_evidence_debt(path: Path) -> EvidenceDebt | None:
    if not path.exists():
        return None
    return EvidenceDebt.model_validate_json(path.read_text(encoding="utf-8"))
