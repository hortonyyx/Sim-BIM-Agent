#!/usr/bin/env python3
"""Build the judge-owned ``_run/judge_score_bindings.json`` for a v3 case.

⭐ 2026-08-21：此前**全仓没有任何生成器** —— sm24 那份是手工产的，于是 sm25（第二个 v3
答案）一跑判卷就撞上「required judge sidecar(s) are missing ⇒ v3 scoring layer was skipped」，
**权威判卷被静默跳过**。本工具把这条路补成确定性的：每个字段都从 gt + run 的
``view_manifest.json`` 推导，⛔ 不手抄、不猜。

平面绑定已完整支持。立面绑定 2026-08-22 落地（派工单
``AI_agent/logs/reviews/request/2026-08-22_elevation_score_bindings_dispatch.md``）：
``world_axis``/``sign`` 一律来自 ``facade_convention`` 的函数调用（单一真源，历史上手抄
第五份表真出过一次镜像 bug）；沿墙零点 ``along_origin = lo if sign == 1 else hi``，(lo,hi)
取自**逐层** extents——与校正侧 ``window_sources.materialize_current_ring_va_elevation_bindings``
同一算法：先构造 per-floor extents，层间不一致即 fail closed（⛔ 不许用跨层并集冒充，
GPT 复核 2026-08-22 MAJOR-2：并集 origin 可以和任何一层都不相等，而权威 Va 消费者
按 opening 宿主层逐层严格 ``==`` 比对）。

⛔ S1（多层立面 footprint 指纹取哪一个，派工单 §五）已由用户拍板（2026-08-22）：
**修 gt 生成器，使几何相同的多层轮廓指纹逐位一致**（登记缺陷 plan.md「跨层 footprint
用浮点逐位相等比较」）。因此本工具对层间分歧（指纹或 extents 任一）一律 fail closed
拒产（把各层指纹/extent 打进错误信息）；早期版本的 ``--elevation-fingerprint-union-pending-s1``
过渡旗标已按主控决定**删除**——它会以 exit 0 产出一份能过冻结加载器与 GT companion
校验、却被权威 Va 拒收的绑定（GPT 复核 MAJOR-1：静默不可用路径）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.agent.correction import facade_convention
from src.agent.execution.view_manifest import ViewManifest
from src.agent.judge.score_inputs import frame_transform_sha256
from src.agent.judge.score_schema import (ElevationScoreViewBindingV1,
                                          JudgeScoreViewBindingsV1,
                                          PlanScoreViewBindingV1,
                                          hash_model_without,
                                          load_score_gt_identity)


def build(run_dir: Path, gt_file: Path, input_ids: set[str] | None) -> JudgeScoreViewBindingsV1:
    base = ViewManifest.model_validate_json((run_dir / "_run" / "view_manifest.json").read_text(encoding="utf-8"))
    identity, gt = load_score_gt_identity(gt_file)
    if gt is None:
        raise SystemExit("gt is not a scorable c2 v3 document")

    gt_views = {view.id: view for source in gt.sources for view in source.views}
    plan_view_for_floor: dict[str, list[str]] = {}
    elevation_views_by_family: dict[str, list] = {}
    for view_id, view in sorted(gt_views.items()):
        # GT plan views carry ``floor_ids`` (a list with exactly one entry), not ``floor_id``.
        floor_ids = list(getattr(view, "floor_ids", ()) or ())
        if view.kind == "plan":
            if len(floor_ids) != 1:
                raise SystemExit(f"plan view {view_id!r} must name exactly one floor, got {floor_ids}")
            plan_view_for_floor.setdefault(floor_ids[0], []).append(view_id)
        elif view.kind == "elevation":
            elevation_views_by_family.setdefault(view.facade_family, []).append(view)

    required = [entry for entry in base.required_entries() if entry.view_type in {"plan", "elevation"}]
    if input_ids is not None:
        required = [entry for entry in required if entry.input_id in input_ids]
        missing = input_ids - {entry.input_id for entry in required}
        if missing:
            raise SystemExit(f"input ids not required by the view manifest: {sorted(missing)}")

    bindings = []
    for entry in required:
        if entry.view_type == "plan":
            floor_id = _floor_for_plan(entry.input_id, plan_view_for_floor, gt)
            bindings.append({"kind": "plan", "input_id": entry.input_id, "floor_id": floor_id,
                             "gt_source_view_ids": tuple(plan_view_for_floor[floor_id])})
        elif entry.view_type == "elevation":
            bindings.append(_elevation_binding_fields(entry, gt, elevation_views_by_family))
        else:  # pragma: no cover - required_entries() is filtered to plan/elevation above
            raise SystemExit(f"{entry.input_id}: unsupported view type {entry.view_type!r}")

    payload = {"schema_version": "1", "case_id": base.case_id,
               "gt_content_sha256": identity.content_sha256,
               "case_metadata_sha256": base.case_metadata_sha256,
               "base_view_manifest_sha256": base.content_sha256,
               "bindings": tuple(PlanScoreViewBindingV1.model_validate(b) if b["kind"] == "plan"
                                 else ElevationScoreViewBindingV1.model_validate(b) for b in bindings),
               "content_sha256": "0" * 64}
    draft = JudgeScoreViewBindingsV1.model_construct(**payload)
    payload["content_sha256"] = hash_model_without(draft, "content_sha256")
    return JudgeScoreViewBindingsV1.model_validate(payload)


def _elevation_binding_fields(entry, gt, elevation_views_by_family: dict[str, list]) -> dict:
    """Derive every elevation-binding field from gt + the manifest entry.

    ⛔ Nothing here may hand-type the (family -> axis, sign) table: world_axis and
    sign come from ``facade_convention`` function calls only (dispatch §三).
    """
    input_id = entry.input_id
    if entry.direction_semantics != "building_axis":
        # Only the manifest building-axis route is derivable from gt alone; a
        # true-azimuth/unknown elevation needs the reviewed external direction
        # sidecar, which this builder does not own.
        raise SystemExit(
            f"{input_id}: direction_semantics={entry.direction_semantics!r} requires a reviewed "
            "direction sidecar; this builder only derives manifest_building_axis entries")
    family = entry.building_view_direction
    views = elevation_views_by_family.get(family, ())
    if not views:
        raise SystemExit(f"{input_id}: gt has no elevation view for family {family!r}")
    floor_ids_tuples = {tuple(view.floor_ids) for view in views}
    if len(floor_ids_tuples) != 1:
        raise SystemExit(
            f"{input_id}: gt elevation views for {family!r} disagree on floor_ids: "
            f"{sorted(floor_ids_tuples)}")
    floor_ids = tuple(views[0].floor_ids)
    gt_source_view_ids = tuple(sorted(view.id for view in views))

    segments = [segment for floor in gt.floors if floor.id in floor_ids
                for segment in floor.boundary_segments if segment.facade_family == family]
    if not segments:
        raise SystemExit(f"{input_id}: gt has no {family!r} boundary segments for floors {list(floor_ids)}")
    missing_floors = set(floor_ids) - {segment.floor_id for segment in segments}
    if missing_floors:
        raise SystemExit(
            f"{input_id}: gt floors {sorted(missing_floors)} have no {family!r} boundary segments")
    # Per-floor (fingerprint, extent) first, exactly like the correction ring
    # (window_sources.materialize_current_ring_va_elevation_bindings): the
    # authoritative Va consumer re-derives both PER HOST FLOOR of each opening
    # and compares with strict equality (facade_applicability.py), so the floors
    # must agree bit-for-bit before one binding may speak for all of them.
    per_floor = _per_floor_family_state(input_id, family, segments)
    fingerprints = {state[0] for state in per_floor.values()}
    extents = {state[1] for state in per_floor.values()}
    if len(fingerprints) != 1 or len(extents) != 1:
        detail = "; ".join(f"{floor_id}: fingerprint={state[0]} extent={state[1]}"
                           for floor_id, state in sorted(per_floor.items()))
        raise SystemExit(
            f"{input_id}: {family!r} facade floors disagree across {sorted(per_floor)} — {detail}. "
            "One elevation binding declares ONE fingerprint/along_origin while the authoritative Va "
            "consumer re-derives them per host floor with strict equality (facade_applicability.py; "
            "same contract as correction-side window_sources 'direction_binding_ring_incompatible'), "
            "so a cross-floor union would only produce a binding Va refuses. S1 (dispatch §五) is "
            "ratified as a gt-generator fix — identical multi-floor outlines must carry bit-identical "
            "fingerprints — and this builder refuses to paper over the disagreement.")
    fingerprint = next(iter(fingerprints))
    lo, hi = next(iter(extents))

    # mirrored/local_x_positive evidence (dispatch P8): 8/8 facades across the two
    # anchored buildings (sm25 + sm24) were measured to follow the declared
    # convention UN-mirrored ("drawn as seen from outside", image-x left→right) —
    # AI_agent/logs/experiments/2026-08-22_elevation_mirror_convention/
    # verify_mirror_convention.py exits 0. ``normalize_mirror_flag`` refuses to
    # guess "unknown", so this must stay an evidence-backed choice, never a default.
    mirrored = False
    local_x_positive = "image_left_to_right"
    world_axis = facade_convention.world_axis(family)
    sign = facade_convention.resolve_sign(family, mirrored=mirrored, local_x_positive=local_x_positive)
    along_origin = (lo if sign == 1 else hi) + 0.0  # normalize -0.0 -> 0.0

    draft = ElevationScoreViewBindingV1(
        kind="elevation", input_id=input_id, floor_ids=floor_ids, facade_family=family,
        gt_source_view_ids=gt_source_view_ids, resolved_building_direction=family,
        resolution_source="manifest_building_axis", orientation_output_hash=None,
        adapter_version=None, source_footprint_fingerprint=fingerprint,
        world_axis=world_axis, sign=sign, along_origin=along_origin,
        mirrored=mirrored, local_x_positive=local_x_positive,
        frame_transform_sha256="0" * 64)
    _assert_orientation_paired(input_id, draft.orientation_output_hash, draft.adapter_version)
    return draft.model_dump(mode="python", exclude={"frame_transform_sha256"}) | {
        "frame_transform_sha256": frame_transform_sha256(draft)}


def _assert_orientation_paired(input_id: str, orientation_output_hash, adapter_version) -> None:
    """MINOR-1 (GPT verdict 2026-08-22): the two orientation fields must occur
    as a pair — both ``None`` or both set.

    This patches a seam the schema leaves open: ``ElevationScoreViewBindingV1.
    _frame_source_contract`` (score_schema.py) defines ``has_orientation`` as
    "both fields non-null", so a ONE-SIDED fill (exactly one of them) survives
    the schema AND ``validate_score_view_bindings`` and is only refused much
    later by the authoritative Va consumer (``va_direction_unresolved``) — a
    silent-until-Va path for sidecars this builder must never emit.  The schema
    is not this dispatch's to change; the builder closes the seam at production
    time instead.
    """
    pair = (orientation_output_hash, adapter_version)
    if any(value is not None for value in pair) and not all(value is not None for value in pair):
        raise SystemExit(
            f"{input_id}: orientation fields must be paired (both None or both set) — "
            f"the schema accepts a one-sided fill but the authoritative Va consumer refuses it; "
            f"got orientation_output_hash={orientation_output_hash!r} adapter_version={adapter_version!r}")


def _per_floor_family_state(input_id: str, family: str, segments) -> dict[str, tuple[str, tuple[float, float]]]:
    """Per-floor ``(footprint fingerprint, family world_along extent)``, mirroring
    the correction ring's per-floor pass
    (``window_sources.materialize_current_ring_va_elevation_bindings``).

    Within one floor the fingerprint must be consistent across this family's
    segments (gt signs one digest per footprint); the extent is that floor's
    min-lo / max-hi over the family's segments.
    """
    state: dict[str, tuple[str, tuple[float, float]]] = {}
    for segment in segments:
        fingerprint, extent = state.setdefault(
            segment.floor_id,
            (segment.source_footprint_fingerprint,
             (segment.world_along_interval.lo, segment.world_along_interval.hi)))
        if segment.source_footprint_fingerprint != fingerprint:
            raise SystemExit(
                f"{input_id}: gt floor {segment.floor_id!r} mixes footprint fingerprints within one "
                f"{family!r} facade ({segment.source_footprint_fingerprint!r} vs {fingerprint!r}); "
                "gt is inconsistent, not a binding decision")
        state[segment.floor_id] = (fingerprint, (
            min(extent[0], segment.world_along_interval.lo),
            max(extent[1], segment.world_along_interval.hi)))
    return state


def _floor_for_plan(input_id: str, plan_view_for_floor: dict[str, list[str]], gt) -> str:
    """Bind a plan input to its GT floor by the drawing's own floor ordering.

    The manifest's plan entries and the GT's floors are both in declaration order,
    so the n-th plan input is the n-th floor.  Any mismatch in count is a hard error
    rather than a guess.
    """
    floors = [floor.id for floor in gt.floors if floor.id in plan_view_for_floor]
    plans = sorted(plan_view_for_floor)
    if len(floors) != len(plans):
        raise SystemExit("gt floors and gt plan views do not correspond one-to-one")
    if input_id not in _PLAN_INPUT_ORDER:
        raise SystemExit(f"cannot bind plan input {input_id!r} to a floor; extend _PLAN_INPUT_ORDER")
    index = _PLAN_INPUT_ORDER[input_id]
    if index >= len(floors):
        raise SystemExit(f"plan input {input_id!r} has no matching gt floor")
    return floors[index]


#: 平面输入名 → 楼层序号。天正/本项目约定：``1f_view`` = 第 1 层，依此类推。
_PLAN_INPUT_ORDER = {f"{n}f_view": n - 1 for n in range(1, 21)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--gt", type=Path, required=True)
    ap.add_argument("--input-ids", nargs="*", default=None,
                    help="restrict to these inputs (default: every required plan/elevation entry)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    result = build(args.run_dir, args.gt, set(args.input_ids) if args.input_ids else None)
    out = args.out or (args.run_dir / "_run" / "judge_score_bindings.json")
    out.write_text(json.dumps(result.model_dump(mode="json"), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "content_sha256": result.content_sha256,
                      "bindings": [b.input_id for b in result.bindings]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
