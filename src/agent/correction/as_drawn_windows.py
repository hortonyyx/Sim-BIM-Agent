"""as_drawn 腿的窗：从「平面×立面两个通道的观测」确定性地推出 ``WindowV3``。

⭐ 本模块**只做配对与组装**。深度拆分、遮挡、绑段、歧义拒绝、朝向符号——
全部复用 C2 已经交付的东西，⛔ 一行都不重写：

  * `facade_visibility`（Vg）已把每面按**深度**拆成多个 `FacadeSegment`，
    并给出 `visible_intervals` —— ⭐ 那是沿面轴的**不重叠划分**（skyline 的定义
    即「每个沿面位置上只有最近的面可见」）⇒ 「这个立面洞口属于哪堵墙」**任意形状唯一可答**；
  * `facade_convention.FACADE_BASE_SIGN` / `project_affine_interval` 给逐面朝向与投影
    （实测：east/south 原样、north/west 镜像，与该表逐条吻合）；
  * `window_host.resolve_window_hosts` 是窗→段绑定的既有 owner
    （`opening_claim_score` 明写 "B5 remains the host resolver owner"）。

⛔ **驱动集是立面洞口，不是平面候选** —— 这不是偏好，是契约硬的：
`WindowV3` 必须有 `z`，而 `sill`/`head` **只准立面通道声称**
（`window_sources._claim_links` 的权限矩阵）⇒ 平面上有、立面上没有的窗
**造不出合法窗对象**，只能作为**记账的缺席**（见返回的第二个元素）。

⚠️ **一个物理洞在墙的两条面线上各留一个缺口**（实测 sm25：62 个 window 候选
= 33 个物理洞）。目录层已按模型自己声明的墙配对（`hypotheses.pairs`）把它们
**收编成一洞一行**（`window_sources._as_drawn_plan_gap_groups`）⇒ 这里的每个
平面候选都是**不同的物理洞**，配对决策（唯一 + 互为最近 + 无复用）因此在
真候选之间进行，⛔ 不再需要按吻合度预选 —— 双胞胎并列曾让「最近邻」不唯一
（21/31 影子校验拒绝，2026-09-08h），收编后唯一性由结构保证。
按平面候选造窗会**静默翻倍**且看上去完全合理 —— 从立面驱动天然避开这一点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from src.agent.correction.facade_convention import (
    project_affine_interval,
    resolve_sign,
)
from src.agent.correction.schema import (
    CorrectedGeometryV3,
    FieldProvenance,
    WindowV3,
)
from src.agent.correction.window_sources import (
    ElevationSourceWindowV1,
    PlanSourceWindowV1,
    SourceWindowV1,
    WindowResolverInputError,
)


@dataclass(frozen=True)
class AsDrawnWindowAccount:
    """⭐ 缺席被做成**信号**，⛔ 不是静默的空白。"""

    elevation_openings: int
    windows_built: int
    #: 立面有、平面没有对应 window 候选的洞口（实测 sm25 = 3 个 door）
    unclassified_elevation_openings: tuple[str, ...] = ()
    #: 平面有 window 候选、立面没有对应洞口的（⇒ 缺 z，造不出窗）
    plan_windows_without_elevation: tuple[str, ...] = ()
    #: 同一物理洞在墙另一条面线上的记录被目录收编（"folded->survivor"）——
    #: ⭐ 收编是**动作**，必须可对账：目录里少掉的观测在这里有名有姓
    plan_records_folded: tuple[str, ...] = ()
    #: 容差球内 ≥2 个并列候选 ⇒ 配对决策不唯一 ⇒ ⛔ 不猜，整洞口不建窗
    ambiguous_pair_openings: tuple[str, ...] = ()
    #: 平面记录的最近洞口不是本洞口（互为最近失败）⇒ ⛔ 不建
    conflicting_pair_openings: tuple[str, ...] = ()

    def to_payload(self) -> dict:
        return {
            "schema": "as_drawn_window_account_v1",
            "elevation_openings": self.elevation_openings,
            "windows_built": self.windows_built,
            "unclassified_elevation_openings": list(self.unclassified_elevation_openings),
            "plan_windows_without_elevation": list(self.plan_windows_without_elevation),
            "plan_records_folded": list(self.plan_records_folded),
            "ambiguous_pair_openings": list(self.ambiguous_pair_openings),
            "conflicting_pair_openings": list(self.conflicting_pair_openings),
        }


def _elevation_along_width_m(doc: dict) -> float:
    """该立面**自己声明**的沿面全长（⛔ 零发明常数）。

    实测四面：east/west 20000 mm、north/south 25000 mm，`chain_closure_mm` 全为 0。
    """
    chain = ((doc.get("calibration") or {}).get("x")) or {}
    overall = chain.get("overall_mm")
    if isinstance(overall, bool) or not isinstance(overall, (int, float)) or float(overall) <= 0.0:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reason": "elevation declares no positive calibration.x.overall_mm"},
            category="input_integrity_error",
        )
    return float(overall) / 1000.0


def _floor_of(geom: CorrectedGeometryV3, z_lo: float, z_hi: float):
    """洞口的 z 区间落在哪一层。⛔ 跨层或落空 = 具名拒绝，不猜。"""
    hits = [
        floor for floor in geom.floors
        if float(floor.z_floor) <= z_lo and z_hi <= float(floor.z_floor) + float(floor.ceiling_height)
    ]
    if len(hits) != 1:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reason": "elevation opening does not sit inside exactly one storey",
             "z": [z_lo, z_hi], "n_floors_matched": len(hits)},
            category="model_draw_error",
        )
    return hits[0]


def _segment_of(geom: CorrectedGeometryV3, *, floor_id: str, facade: str,
                along_lo: float, along_hi: float):
    """哪一段墙面。⭐ 判据 = 落进该段的 **visible_intervals**（skyline 划分）。

    ⛔ 零个或多个 ⇒ 具名拒绝。C2.1 §118 同口径：「刻意对称 → 必须 conflict 不许猜」。
    """
    hits = [
        segment for segment in geom.facade_segments
        if segment.floor_id == floor_id and segment.facade_family == facade
        and any(float(iv.lo) <= along_lo and along_hi <= float(iv.hi)
                for iv in segment.visible_intervals)
    ]
    if len(hits) != 1:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reason": "elevation opening does not fall in exactly one visible facade segment",
             "facade": facade, "floor_id": floor_id,
             "along": [along_lo, along_hi], "n_segments_matched": len(hits)},
            category="model_draw_error",
        )
    return hits[0]


def _room_of(floor, *, facade: str, plane: float, along_lo: float, along_hi: float) -> str:
    """窗所属的 cell。⭐ **零阈值**：按【共享边界边】匹配，⛔ 不用探针点、不用偏移常数。

    cells 精确铺满足迹（实测：ring 面积 == cells∪ 面积，差 **0.0000 m²**，两层都是），
    且 cell 顶点与足迹环出自**同一次 partition** ⇒ 落在该墙面线上的边是**逐位相等**的，
    ⛔ 不需要容差。

    ⛔ 零个或多个 ⇒ 具名拒绝（同 `_segment_of`：C2.1 §118「必须 conflict 不许猜」）。
    """
    const_index = 1 if facade in ("North", "South") else 0
    along_index = 1 - const_index
    hits: list[str] = []
    for cell in floor.cells:
        poly = [(float(x), float(y)) for x, y in cell.polygon]
        if poly and poly[0] == poly[-1]:
            poly.pop()
        n = len(poly)
        for i in range(n):
            a, b = poly[i], poly[(i + 1) % n]
            if a[const_index] != plane or b[const_index] != plane:
                continue
            lo = min(a[along_index], b[along_index])
            hi = max(a[along_index], b[along_index])
            if lo <= along_lo and along_hi <= hi:
                hits.append(cell.id)
                break
    if len(hits) != 1:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reason": "window span does not sit on exactly one cell boundary edge",
             "facade": facade, "plane": plane, "along": [along_lo, along_hi],
             "n_cells_matched": len(hits)},
            category="model_draw_error",
        )
    return hits[0]


def _plan_candidates_for(catalog: Sequence[SourceWindowV1], *, floor_ref: int,
                         facade: str, segment, along_lo: float, along_hi: float,
                         tolerance_m: float) -> tuple[tuple[float, PlanSourceWindowV1], ...]:
    """该洞口在平面侧的候选（**带残差、⛔ 不去重不预选**）。

    判据与 `window_host._plan_source_matches_plane` 同形：跨向区间必须**含住**
    该段所在的平面坐标；沿面区间与立面投影出的区间重合（容差为调用方派生）。

    ⭐ 目录已在上一层把「同一物理洞的两条面线记录」收编成一行
    （`window_sources._as_drawn_plan_gap_groups`）⇒ 这里看到的每个候选都是
    **不同的物理洞**。容差球内出现 ≥2 个候选因此是真歧义（两堵不同墙都说
    这个洞口在自己身上），交由调用方按歧义拒绝 —— ⛔ 不在这里悄悄挑一个
    （旧实现按残差去重预选，把「挑了哪一个」藏在了配对决策之前）。
    """
    plane = float(segment.p1[1]) if facade in ("North", "South") else float(segment.p1[0])
    out = []
    for row in catalog:
        if not isinstance(row, PlanSourceWindowV1) or row.floor_ref != floor_ref:
            continue
        if facade in ("North", "South"):
            along, cross = row.world_x_interval, row.world_y_interval
        else:
            along, cross = row.world_y_interval, row.world_x_interval
        if not (float(cross.lo) <= plane <= float(cross.hi)):
            continue
        d_lo = abs(float(along.lo) - along_lo)
        d_hi = abs(float(along.hi) - along_hi)
        if d_lo <= tolerance_m and d_hi <= tolerance_m:
            out.append((d_lo + d_hi, row))
    return tuple(sorted(out, key=lambda item: item[1].observation_id))


def derive_match_tolerance_m(raw_reading_artifacts: Mapping[str, bytes]) -> float:
    """匹配容差 = **半个最薄声明墙厚**（⛔ 零发明常数）。

    ⭐ 与同日两处裁决**同一个派生量**：ladder 的 `_axis_snap_cap_native`、
    足迹吸附的 BLK-1 `cap` —— 都是「request/产物自己声明的最薄墙厚之半」。
    语义也一致：**一个洞口的边被放错超过半堵墙，它就不再是同一个洞口**。

    ⭐⭐ 而且**实测证明取值不承重**：在真产物上扫容差，
    50 mm / 60 mm / 100 mm 给出**完全相同**的结果（31 窗 + 3 未分类 +
    2 平面孤儿 + 29 折叠）。sm25 声明 [240, 120] ⇒ CAP = **60 mm**，
    **落在这段平台期内** ⇒ ⛔ 不是调出来的边界值。
    ⚠️ 200 mm 一档**不再同数**（31→30）：North_view/O04 在 200 mm 球内同时
    捞到同墙两条记录（L023g2 宽 0.67 m / L024g4 宽 0.80 m，hi 端差 151 mm
    > 半墙 ⇒ 平面自己的语义说它们是**两个不同的洞**）⇒ 歧义门拒绝整洞口。
    旧实现在这档会**静默挑走残差更小的一条**——正是 2026-09-08h 影子校验
    拒绝 21/31 的那个形状；容差一旦宽过「同洞」语义，歧义必须亮出来 ⛔ 不许吞。

    ⚠️ 为什么不能更小：平面记的是**墙段之间的缺口**，立面记的是**可见洞口**，
    两者定义不同（窗框/侧壁），⛔ 这个差不是标定噪声，所以按噪声界派生会过紧。
    """
    caps: list[float] = []
    for raw in raw_reading_artifacts.values():
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        callouts = (doc.get("declarations") or {}).get("thickness_callouts_mm")
        if isinstance(callouts, list) and callouts:
            values = [float(v) for v in callouts
                      if not isinstance(v, bool) and isinstance(v, (int, float)) and float(v) > 0.0]
            if values:
                caps.append(min(values) / 2.0 / 1000.0)
    if not caps:
        raise WindowResolverInputError(
            "source_identity_invalid",
            {"reason": "no product declares thickness_callouts_mm — the match "
                       "tolerance has no declared source and must not be defaulted"},
            category="input_integrity_error",
        )
    return min(caps)


def derive_as_drawn_windows(
    geom: CorrectedGeometryV3,
    *,
    catalog: Sequence[SourceWindowV1],
    manifest,
    raw_reading_artifacts: Mapping[str, bytes],
    match_tolerance_m: float | None = None,
) -> tuple[tuple[WindowV3, ...], AsDrawnWindowAccount]:
    """立面洞口 → `WindowV3`。返回 (窗, 账)。

    ``match_tolerance_m=None``（生产用法）⇒ 由 :func:`derive_match_tolerance_m`
    从产物自己的声明派生；显式传值只供测试扫描平台期。

    ⭐ 配对是**双向**的，分两拍：
      1. 每个立面洞口收集容差球内的平面候选（⛔ 不去重不预选）；
      2. 全局决策 —— 洞口的候选必须**唯一**（≥2 个 ⇒ 歧义拒绝），
         且被引记录的最近洞口必须**指回本洞口**（互为最近；否则冲突拒绝）。
    每条平面观测因此至多佐证一个窗（无复用），每一次拒绝都进账本 ⛔ 不静默。
    """
    if match_tolerance_m is None:
        match_tolerance_m = derive_match_tolerance_m(raw_reading_artifacts)
    elevation_rows = [r for r in catalog if isinstance(r, ElevationSourceWindowV1)]

    # ── Phase 1：立面洞口 → 几何上下文 + 平面候选（含残差） ──────────────────
    openings: list[dict] = []
    for row in sorted(elevation_rows, key=lambda r: (r.source_input_id, r.observation_id)):
        entry = manifest.entry_by_input_id(row.source_input_id)
        facade = entry.building_view_direction
        doc = json.loads(raw_reading_artifacts[row.source_input_id].decode("utf-8"))
        width_m = _elevation_along_width_m(doc)
        # ⭐ 翻转与投影全部走既有约定，⛔ 不在这里发明规则
        sign = resolve_sign(facade, mirrored=False,
                            local_x_positive="image_left_to_right")
        along_lo, along_hi = project_affine_interval(
            along_origin=0.0 if sign > 0 else width_m, sign=sign,
            local_lo=float(row.local_along_interval.lo),
            local_hi=float(row.local_along_interval.hi),
        )
        if row.local_z_interval is None:
            raise WindowResolverInputError(
                "source_identity_invalid",
                {"reason": "elevation opening declares no z interval",
                 "observation_id": row.observation_id},
                category="input_integrity_error",
            )
        z_lo, z_hi = float(row.local_z_interval.lo), float(row.local_z_interval.hi)
        floor = _floor_of(geom, z_lo, z_hi)
        segment = _segment_of(geom, floor_id=floor.id, facade=facade,
                              along_lo=along_lo, along_hi=along_hi)
        candidates = _plan_candidates_for(
            catalog, floor_ref=_floor_ref_of(geom, floor), facade=facade,
            segment=segment, along_lo=along_lo, along_hi=along_hi,
            tolerance_m=match_tolerance_m)
        openings.append(dict(
            key=f"{row.source_input_id}/{row.observation_id}", row=row,
            facade=facade, floor=floor, segment=segment,
            along_lo=along_lo, along_hi=along_hi, z_lo=z_lo, z_hi=z_hi,
            candidates=candidates,
        ))

    # ── Phase 2：反向最近 —— 每条平面观测的最近洞口（互为最近的另一半） ──────
    # 同一条观测有两个等近洞口 ⇒ 该观测自己有歧义（None），引用它的洞口一律
    # 按冲突拒绝，⛔ 不按遍历顺序悄悄定一边。
    row_edges: dict[str, list[tuple[float, str]]] = {}
    for opening in openings:
        for residual, prow in opening["candidates"]:
            ref = f"{prow.source_input_id}/{prow.observation_id}"
            row_edges.setdefault(ref, []).append((residual, opening["key"]))
    best_opening_of_row: dict[str, str | None] = {}
    for ref, edges in row_edges.items():
        edges.sort()
        contested = len(edges) > 1 and abs(edges[0][0] - edges[1][0]) <= 1e-12
        best_opening_of_row[ref] = None if contested else edges[0][1]

    # ── Phase 3：决策 + 造窗 + 记账 ──────────────────────────────────────────
    windows: list[WindowV3] = []
    unclassified: list[str] = []
    ambiguous: list[str] = []
    conflicting: list[str] = []
    matched_plan: set[str] = set()
    for opening in openings:
        candidates = opening["candidates"]
        if not candidates:
            # 平面没把它分类成 window（实测 sm25 = 3 个 door）⇒ ⛔ 不当窗造，
            # 但**记账**：缺席是信号，不是空白。
            unclassified.append(opening["key"])
            continue
        if len(candidates) > 1:
            # 容差球内 ≥2 个候选 = 两堵不同的墙都声称这个洞口 ⇒ 配对决策不
            # 唯一（歧义边际不足：次优与最优同在球内）⇒ ⛔ 不猜。
            cands = ",".join(f"{p.source_input_id}/{p.observation_id}"
                             for _, p in candidates)
            ambiguous.append(f"{opening['key']}->{cands}")
            continue
        elevation_ref = opening["key"]
        plan_ref = (f"{candidates[0][1].source_input_id}/"
                    f"{candidates[0][1].observation_id}")
        back = best_opening_of_row.get(plan_ref)
        if back != opening["key"]:
            # 该记录的最近洞口不是本洞口（或该记录自己有歧义）⇒ 互为最近失败。
            conflicting.append(f"{opening['key']}->{plan_ref}(back={back})")
            continue
        matched_plan.add(plan_ref)
        row, facade = opening["row"], opening["facade"]
        floor, segment = opening["floor"], opening["segment"]
        along_lo, along_hi = opening["along_lo"], opening["along_hi"]
        z_lo, z_hi = opening["z_lo"], opening["z_hi"]
        plane = (float(segment.p1[1]) if facade in ("North", "South")
                 else float(segment.p1[0]))
        room = _room_of(floor, facade=facade, plane=plane,
                        along_lo=along_lo, along_hi=along_hi)
        plan_refs = [plan_ref]
        windows.append(WindowV3(
            id=f"{floor.id}-win-{row.source_input_id}-{row.observation_id}",
            floor_id=floor.id, facade=facade, room=room,
            span=[along_lo, along_hi], z=[z_lo, z_hi],
            provenance={
                # ⭐ 逐条声称只引【有权声称它】的通道（权限矩阵见 _claim_links）
                "existence": FieldProvenance(provenance="observed",
                                             source_ids=[elevation_ref] + plan_refs,
                                             method="as_drawn_plan_x_elevation"),
                "host": FieldProvenance(provenance="observed", source_ids=plan_refs,
                                        method="as_drawn_plan_face_pairing"),
                "along": FieldProvenance(provenance="observed",
                                         source_ids=[elevation_ref] + plan_refs,
                                         method="facade_convention_affine"),
                "width": FieldProvenance(provenance="observed",
                                         source_ids=[elevation_ref] + plan_refs,
                                         method="facade_convention_affine"),
                "sill": FieldProvenance(provenance="observed", source_ids=[elevation_ref],
                                        method="as_drawn_elevation_z_range"),
                "head": FieldProvenance(provenance="observed", source_ids=[elevation_ref],
                                        method="as_drawn_elevation_z_range"),
            },
        ))

    orphan_plan = tuple(sorted(
        f"{r.source_input_id}/{r.observation_id}"
        for r in catalog
        if isinstance(r, PlanSourceWindowV1)
        and f"{r.source_input_id}/{r.observation_id}" not in matched_plan
    ))
    # 物理洞收编账（"folded->survivor"）：只记幸存者确在本次目录里的那部分 ——
    # 目录若被调用方手工构造，收编结构与它对不上时 ⛔ 不虚记账。
    from src.agent.correction.window_sources import as_drawn_plan_record_folds
    catalog_refs = {f"{r.source_input_id}/{r.observation_id}" for r in catalog}
    folds = tuple(f"{folded}->{survivor}"
                  for folded, survivor in
                  as_drawn_plan_record_folds(manifest=manifest,
                                             raw_reading_artifacts=raw_reading_artifacts)
                  if survivor in catalog_refs and folded not in catalog_refs)
    account = AsDrawnWindowAccount(
        elevation_openings=len(elevation_rows), windows_built=len(windows),
        unclassified_elevation_openings=tuple(unclassified),
        plan_windows_without_elevation=orphan_plan,
        plan_records_folded=folds,
        ambiguous_pair_openings=tuple(ambiguous),
        conflicting_pair_openings=tuple(conflicting),
    )
    return tuple(windows), account


def _floor_ref_of(geom: CorrectedGeometryV3, floor) -> int:
    """``floor_ref`` 是 1-based 的层序（与 manifest 同口径）。"""
    for index, candidate in enumerate(geom.floors, start=1):
        if candidate.id == floor.id:
            return index
    raise WindowResolverInputError(
        "source_identity_invalid", {"reason": "floor not in geometry", "floor_id": floor.id},
        category="input_integrity_error",
    )


__all__ = ["AsDrawnWindowAccount", "derive_as_drawn_windows"]


def populate_as_drawn_windows(
    geom: CorrectedGeometryV3,
    *,
    raw_view_manifest_bytes: bytes,
    raw_reading_artifacts: Mapping[str, bytes],
    visibility_tolerances,
) -> tuple[CorrectedGeometryV3, AsDrawnWindowAccount]:
    """S3 编排：把窗填进几何，**在建 vwi 之前**。

    ⚠️ 顺序不是随意的，是被两条既有约束夹出来的：
      * `derive_as_drawn_windows` 需要 `facade_segments`（靠它的 `visible_intervals`
        定「这个洞口属于哪堵墙」）—— 而 Vg 是 finalize 里才跑的；
      * `build_verified_window_inputs_as_drawn` 建的 marker 绑
        `producer_draw_canonical_bytes`，且 `_claim_links` 在那一刻校验
        **producer 已有的窗** —— 窗若在 marker 之后才加，既过不了校验，
        marker 也不再对应这份几何。
    ⇒ 本函数在**建 marker 之前**先跑一次 Vg 拿到段、造好窗；
    finalize 里那次 Vg 是幂等重跑（同一 ring、同一容差），⛔ 不是第二个定义。
    """
    from src.agent.correction.facade_visibility import materialize_all_facade_segments
    from src.agent.correction.window_sources import (
        _parse_manifest,
        build_as_drawn_window_catalog,
    )

    segments = materialize_all_facade_segments(geom, tolerances=visibility_tolerances)
    staged = geom.model_copy(update={"facade_segments": list(segments)})
    manifest = _parse_manifest(raw_view_manifest_bytes)
    catalog = build_as_drawn_window_catalog(
        manifest=manifest, raw_reading_artifacts=raw_reading_artifacts,
    )
    windows, account = derive_as_drawn_windows(
        staged, catalog=catalog, manifest=manifest,
        raw_reading_artifacts=raw_reading_artifacts,
    )
    # ⛔ 只把 windows 带回原几何：`facade_segments` 仍由 finalize 里的 Vg 写，
    # 保持「Vg 是 facade_segments 的唯一写者」这条既有规矩不被本次改动动摇。
    return geom.model_copy(update={"windows": list(windows)}), account


__all__ = ["AsDrawnWindowAccount", "derive_as_drawn_windows",
           "populate_as_drawn_windows"]
