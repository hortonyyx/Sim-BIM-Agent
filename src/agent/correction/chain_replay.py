"""The as_drawn leg's writer-side chain replay (W#3, wallhunt 2026-09-08b).

``StageRunner.record``'s B5 lock is a REPLAY lock: the accepted geometry must
be re-derivable, field for field, from frozen upstream bytes through the real
production functions.  The legacy leg replays ``extract_authoritative_
envelope`` + ``apply_deterministic_core``; THIS module is the as_drawn leg's
counterpart of equal strength — it re-drives the whole deterministic chain
the as_drawn leg actually ran (see :mod:`chain_provenance` for the link map
and where every byte comes from):

  manifest + elevation bytes -> ``derive_floor_ladder``            (z rungs)
  per floor: plan bytes -> ``adapt_as_drawn_plan``
             frozen compilation -> ``cut_lines_from_wall_compilation``
             + ``project_cut_lines``                                (geom)
  ``read_plan_calibration_declaration`` + ``snap_footprints_to_reference``
  ``assemble_multifloor_geometry``                                  (producer)
  ``populate_as_drawn_windows``                                    (windows)
  ``build_verified_window_inputs_as_drawn`` +
  ``finalize_as_drawn_chain_geometry``      (final, stopped BEFORE host
                                             resolution — the writer's
                                             prefix/suffix stage contract)

Every function called is the production one — ⛔ no second copy of any
derivation.  The replay refuses BY NAME on every mismatch that matters:

  * ``chain_replay_source_bytes_drift`` — a floor's plan bytes in the marker
    are not the bytes its provenance row froze (sha256 cross-check);
  * ``chain_replay_compilation_hash_drift`` — the embedded compilation bytes
    are not canonical (a tampered body with a stale ``content_sha256``);
  * ``chain_replay_compilation_unbound`` — the compilation is not the one the
    chain minted from THIS plan product (``bundle_content_sha256`` vs the
    re-adapted evidence bundle);
  * ``chain_replay_producer_drift`` — the assembled producer does not
    byte-match the marker's ``producer_draw_canonical_bytes`` (the as_drawn
    counterpart of the legacy lock's footprint/ring/cells authority: the
    producer is REBUILT here from the frozen chain, never read off the
    candidate).

⚠️ Scope note (deliberate): the flow-level elevation cross-check (every
elevation product must derive the SAME storey z sequence — ``_w1_cross_
check_elevation_ladders``) and the channel-split debt ledger are FLOW gates
around the chain, not links of the geometric derivation; they ran before a
producer could exist and the replay does not duplicate them.  The replay
re-derives ``run_multifloor_correction``'s own chain, which is what the
writer must vouch for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.agent.correction.chain_provenance import AsDrawnChainProvenanceV1
from src.agent.correction.config import CoreTolerances, load_core_tolerances
from src.agent.correction.finalize import FinalizeResult
from src.agent.correction.parse import CorrectionTarget
from src.agent.correction.window_sources import (
    VerifiedWindowResolverInputs,
    canonical_sha256,
)

#: N-3, redeclared for the replay exactly as ``run_correction_evidence_chain``
#: declares it for the production chain: the as-drawn ``*_m`` fields are
#: floating-point metres with no declared quantisation.
_RESOLUTION_SOURCE = (
    "production evidence chain: as-drawn *_m fields, "
    "floating-point metres, no declared quantisation "
    "(N-3 redeclared at the wiring)"
)


def _chain_floor_ref(product_filename: str) -> str:
    """The chain's own floor_ref derivation (``run_correction_evidence_chain``
    plan branch): the ``<N>f`` token inside the frozen file name, else the
    stem.  Re-adapting under any OTHER floor_ref would mint a different
    bundle identity and fail the compilation binding below."""
    stem = Path(product_filename).stem
    m = re.search(r"(\d+)\s*f", stem, re.I)
    return m.group(0).lower() if m else stem


def replay_as_drawn_chain(
    marker: VerifiedWindowResolverInputs,
    provenance: AsDrawnChainProvenanceV1,
    *,
    target: CorrectionTarget,
    tol: CoreTolerances | None = None,
) -> FinalizeResult:
    """Re-drive the as_drawn chain from the marker's frozen bytes.

    Returns the ``FinalizeResult`` of the chain's PRE-host-resolution state
    (ruling 2026-09-08f §一): Vg + kernel stamp + typed validation, with the
    31 windows present and every one still carrying its ORIGINAL room/span,
    and ``corrections`` still the core's own rows.  The writer compares it
    against the candidate with the SAME ruler the legacy leg uses
    (``core_owned_projection_v1`` + corrections prefix + stamp): the
    candidate must be EXACTLY this replay plus the host-resolution suffix
    (its ``window_host_resolution`` audit rows) — returning the
    post-resolution state instead would make the suffix length 0 and the
    writer's prefix/suffix contract (:481) red by construction.
    """
    from src.agent.correction.finalize import finalize_as_drawn_chain_geometry
    from src.agent.correction.parse import ensure_corrected_geometry

    tol = tol or load_core_tolerances()
    vwi = derive_as_drawn_chain_producer(marker, provenance, tol=tol)
    producer = ensure_corrected_geometry(json.loads(vwi.producer_draw_canonical_bytes))
    # The producer is REBUILT here — a candidate-side tamper of the producer
    # (re-signed footprint, rewritten rings/cells, every derived artifact
    # re-materialized from the tampered geometry, all internally consistent —
    # the F-22 BLOCKER-1 shape) dies at this byte compare, exactly as it dies
    # at the legacy leg's core-projection compare.
    producer_bytes = vwi.producer_draw_canonical_bytes
    if producer_bytes != marker.producer_draw_canonical_bytes:
        raise ValueError(
            "chain_replay_producer_drift: the chain replayed from the frozen "
            "compilations assembles a different producer than the marker "
            "carries"
        )
    return finalize_as_drawn_chain_geometry(
        producer,
        verified_window_inputs=vwi,
        target=target,
        tol=tol,
        chain_provenance=provenance,
        # Ruling 2026-09-08f §一: hand the writer the PRE-host-resolution
        # state (the core's prefix) — the candidate supplies the suffix.
        stop_before_host_resolution=True,
    )


def derive_as_drawn_chain_producer(
    marker: VerifiedWindowResolverInputs,
    provenance: AsDrawnChainProvenanceV1,
    *, tol: CoreTolerances | None = None, audit: dict | None = None,
) -> VerifiedWindowResolverInputs:
    """Rebuild a producer from frozen inputs and an explicit replay recipe.

    This creates a candidate, not an acceptance. The writer independently
    rebuilds it again and compares the complete producer bytes below.
    """
    import hashlib

    from src.agent.correction.evidence_adapters import (
        adapt_as_drawn_elevation,
        adapt_as_drawn_plan,
    )
    from src.agent.correction.multifloor import (
        assemble_multifloor_geometry,
        derive_floor_ladder,
        read_plan_calibration_declaration,
    )
    from src.agent.correction.projection_bridge import (
        cut_lines_from_wall_compilation,
        opening_spans_from_artifact,
        project_cut_lines,
    )
    from src.agent.correction.wall_compiler import WallCompilationV1
    from src.agent.correction.window_sources import (
        build_verified_window_inputs_as_drawn,
    )
    from src.agent.execution.view_manifest import ViewManifest

    tol = tol or load_core_tolerances()
    manifest = ViewManifest.model_validate_json(
        marker.raw_view_manifest_bytes.decode("utf-8")
    )
    reading_bytes = dict(marker.raw_reading_artifacts)

    entries = manifest.required_entries()
    elevation_entries = [e for e in entries if e.view_type == "elevation"]
    if not elevation_entries:
        raise ValueError(
            "chain_replay_manifest_without_elevation: the producer's ladder "
            "source is missing from the frozen manifest"
        )
    # The ladder source is the manifest-order FIRST elevation, adapted under
    # its manifest slot id — exactly what the flow wiring hands
    # ``run_multifloor_correction``.
    first = elevation_entries[0]
    elev_doc = json.loads(reading_bytes[first.input_id].decode("utf-8"))
    facade_label = (
        elev_doc.get("facade_label")
        if isinstance(elev_doc, dict)
        else None
    )
    elevation_evidence = adapt_as_drawn_elevation(
        reading_bytes[first.input_id],
        input_id=first.input_id,
        facade_ref=(
            facade_label
            if isinstance(facade_label, str) and facade_label
            else first.input_id
        ),
    )
    ladder = derive_floor_ladder(elevation_evidence)

    # Every manifest plan slot must have exactly one provenance row and vice
    # versa — a missing storey cannot be replayed and an extra one never ran.
    plan_slots = {
        e.input_id: e for e in entries if e.view_type == "plan"
    }
    provenance_ids = {row.input_id for row in provenance.floors}
    for decision in provenance.wall_gap_decisions:
        slot = plan_slots.get(decision.input_id)
        if slot is None or slot.image_sha256 != decision.image_sha256:
            raise ValueError("wall_gap_review_manifest_image_drift")
    if provenance_ids != set(plan_slots):
        raise ValueError(
            "chain_replay_plan_slots_drift: provenance rows and manifest plan "
            f"slots disagree ({sorted(provenance_ids)} vs {sorted(plan_slots)})"
        )
    if len(provenance.floors) != len(ladder):
        raise ValueError(
            "chain_replay_storey_count_drift: the frozen ladder has "
            f"{len(ladder)} rungs for {len(provenance.floors)} plan products"
        )

    if audit is not None:
        audit["floors"] = []
    per_floor_lines: list = []
    per_floor_project: list = []
    all_gap_reviews = []
    declarations = []
    for row, level in zip(provenance.floors, ladder):
        raw = reading_bytes.get(row.input_id)
        if raw is None or hashlib.sha256(raw).hexdigest() != row.source_bytes_sha256:
            raise ValueError(
                f"chain_replay_source_bytes_drift: marker bytes for plan slot "
                f"{row.input_id!r} are not the bytes its provenance row froze"
            )
        slot = plan_slots[row.input_id]
        if Path(row.product_filename).stem != slot.expected_output_id:
            raise ValueError(
                f"chain_replay_product_name_drift: provenance names "
                f"{row.product_filename!r} for slot {row.input_id!r} but the "
                f"manifest expects {slot.expected_output_id!r}"
            )
        stem = Path(row.product_filename).stem
        floor_ref = _chain_floor_ref(row.product_filename)
        artifact = adapt_as_drawn_plan(
            raw, input_id=stem, floor_ref=floor_ref, view_type="plan"
        )
        compilation = WallCompilationV1.model_validate_json(
            row.compilation_bytes.decode("utf-8")
        )
        content = compilation.model_dump(mode="python")
        declared_hash = content.pop("content_sha256", None)
        if canonical_sha256(content) != declared_hash:
            raise ValueError(
                f"chain_replay_compilation_hash_drift: floor {floor_ref!r} "
                "compilation bytes are not canonical"
            )
        if compilation.bundle_content_sha256 != artifact.bundle.content_sha256:
            raise ValueError(
                f"chain_replay_compilation_unbound: floor {floor_ref!r} "
                "compilation was not minted from this plan product's "
                "evidence bundle"
            )
        spans = opening_spans_from_artifact(artifact)
        lines, _ = cut_lines_from_wall_compilation(compilation.walls, spans)
        from src.agent.correction.wall_gap_review import resolve_wall_gap_decisions, apply_wall_gap_decisions

        gap_reviews = resolve_wall_gap_decisions(
            provenance.wall_gap_decisions, input_id=row.input_id,
            raw_reading=raw, raw_compilation=row.compilation_bytes,
        )
        lines = apply_wall_gap_decisions(lines, gap_reviews)
        all_gap_reviews.extend(gap_reviews)
        # W#6: the SAME exterior-frame declaration snap the production chain
        # ran before its cut (pipeline.run_correction_evidence_chain) — the
        # replay re-drives it from the marker's own frozen plan bytes, so a
        # caller cannot swap the declaration between the chain and the
        # writer without the producer byte-compare below going red.
        from src.agent.correction.multifloor import read_declared_exterior_frame
        from src.agent.correction.projection_bridge import (
            snap_exterior_walls_to_declared_frame,
            preserve_endpoint_connections,
        )

        frame = read_declared_exterior_frame(
            json.loads(raw.decode("utf-8")), input_id=stem
        )
        before_frame = lines
        lines, _frame_records = snap_exterior_walls_to_declared_frame(
            lines,
            overall_x_m=frame.overall_x_m,
            overall_y_m=frame.overall_y_m,
            thickness_callouts_mm=frame.thickness_callouts_mm,
            input_id=stem,
        )
        _endpoint_records = ()
        if provenance.endpoint_connection_policy is not None:
            lines, _endpoint_records = preserve_endpoint_connections(before_frame, lines)
        # W#6: collect the per-floor lines + the EXACT projection arguments
        # (the same fields the chain's cut_lines sidecar files) and hand the
        # whole set to the SHARED reconciliation the production wiring used,
        # so the replay's producer is the identical byte-level re-derivation.
        per_floor_lines.append(lines)
        per_floor_project.append({
            "resolution_m": 0.0,
            "resolution_source": _RESOLUTION_SOURCE,
            "source_resolved_sha256": compilation.content_sha256,
            "floor_id": floor_ref,
            "floor_name": floor_ref,
            "z_floor_m": level.z_floor_m,
            "ceiling_height_m": level.ceiling_height_m,
            "view_id": stem,
            "floor_ref": floor_ref,
            "origin_label": stem,
        })
        if audit is not None:
            audit["floors"].append({
                "floor_id": floor_ref, "endpoint_connections": list(_endpoint_records),
                "wall_gap_reviews": gap_reviews,
                "walls": [{"wall_id": w.wall_id,
                           "observation_ids": sorted({r.observation_id for r in w.source_refs})}
                          for w in compilation.walls],
                "spaces_before_frame": project_cut_lines(before_frame, **per_floor_project[-1]).face_count,
                "spaces_after_frame": project_cut_lines(lines, **per_floor_project[-1]).face_count,
            })
        declarations.append(
            read_plan_calibration_declaration(
                json.loads(raw.decode("utf-8")), input_id=stem
            )
        )

    from src.agent.correction.multifloor import reconcile_floors_to_reference

    snapped, _account = reconcile_floors_to_reference(
        tuple(per_floor_lines), per_floor_project, declarations,
        preserve_connections=provenance.endpoint_connection_policy is not None,
    )
    if audit is not None:
        audit["cross_floor_alignment"] = _account.to_payload()
        for row, geom in zip(audit["floors"], snapped):
            row["spaces_after_alignment"] = len(geom.floors[0].cells)
    producer = assemble_multifloor_geometry(ladder, tuple(snapped))
    if all_gap_reviews:
        producer.corrections.append({"kind": "wall_gap_continuity_review", "reviews": all_gap_reviews})
    # ⭐ 2026-09-08 补窗：重放必须**镜像生产方的推导**，否则 producer 必然不等。
    # 生产侧在建 marker 之前调 `populate_as_drawn_windows` 把 31 个窗填进几何
    # （投影的 `windows=[]` 是硬写的，窗由「平面 opening_types × 立面 z_range」
    # 确定性推出）。重放若跳过这一步，就会用**无窗**几何去比一个**有窗**的 marker
    # ⇒ 具名 `chain_replay_producer_drift`（实测）。
    # ⛔ 修法是让重放走同一个确定性函数，⛔ 不是放宽这个字节比较 ——
    # 这道比较正是 F-22 BLOCKER-1 那种「整套自洽伪造」的唯一拦截点。
    from src.agent.correction.as_drawn_windows import populate_as_drawn_windows
    from src.agent.correction.facade_visibility import VisibilityTolerances

    producer, _window_account = populate_as_drawn_windows(
        producer,
        raw_view_manifest_bytes=marker.raw_view_manifest_bytes,
        raw_reading_artifacts=reading_bytes,
        visibility_tolerances=VisibilityTolerances(
            depth_epsilon_m=tol.facade_visibility_depth_epsilon_m,
            endpoint_epsilon_m=tol.facade_visibility_endpoint_epsilon_m,
        ),
    )
    # Historical runs did not derive doors. New runs record an explicit recipe
    # so replay reproduces their openings and assumptions from the same bytes.
    if provenance.wall_opening_policy is not None:
        from src.agent.correction.as_drawn_openings import populate_as_drawn_openings

        producer, _opening_account = populate_as_drawn_openings(
            producer, raw_view_manifest_bytes=marker.raw_view_manifest_bytes,
            raw_reading_artifacts=reading_bytes,
            raw_wall_compilations={row.input_id: row.compilation_bytes for row in provenance.floors},
            assumed_height_m=provenance.wall_opening_policy.assumed_height_m,
            wall_gap_decisions=provenance.wall_gap_decisions,
        )
    # Mirror the production marker constructor BEFORE comparing its bytes.
    # Its schema validation derives WindowV3.floor from floor_id; comparing
    # the unvalidated populate result instead leaves every window.floor null.
    # This marker is built solely from the replayed producer + frozen inputs.
    vwi = build_verified_window_inputs_as_drawn(
        producer_draw=producer,
        raw_view_manifest_bytes=marker.raw_view_manifest_bytes,
        raw_reading_artifacts=reading_bytes,
    )
    return vwi


__all__ = ["replay_as_drawn_chain", "derive_as_drawn_chain_producer"]
