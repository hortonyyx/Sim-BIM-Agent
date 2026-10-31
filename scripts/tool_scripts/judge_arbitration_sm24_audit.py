#!/usr/bin/env python3
"""Reproducible sm24 front-door audit for judge-arbitration Slice 4.

The input is derived once from a real accepted correction artifact and then
stored as JSON.  Both revisions consume those exact bytes through
``score_typed_attempt``; no ``PlanSegment`` is supplied by this tool.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import difflib
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_bytes(value))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(_canonical_bytes(row) for row in rows))


def compare_audits(baseline: Path, new: Path, destination: Path) -> None:
    names = (
        "internal_rows.jsonl",
        "public_rows.jsonl",
        "observation_to_targets.jsonl",
        "wall_criteria.jsonl",
        "identity.json",
        "summary.json",
    )
    complete: list[str] = []
    equality: dict[str, bool] = {}
    hashes: dict[str, dict[str, str]] = {}
    for name in names:
        old_text = (baseline / name).read_text(encoding="utf-8")
        new_text = (new / name).read_text(encoding="utf-8")
        equality[name] = old_text == new_text
        hashes[name] = {
            "baseline_sha256": _sha256(baseline / name),
            "new_sha256": _sha256(new / name),
        }
        complete.extend(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"baseline_cce6e83/{name}",
            tofile=f"new_slice4/{name}",
        ))

    old_summary = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
    new_summary = json.loads((new / "summary.json").read_text(encoding="utf-8"))
    old_identity = json.loads((baseline / "identity.json").read_text(encoding="utf-8"))
    new_identity = json.loads((new / "identity.json").read_text(encoding="utf-8"))
    old_helper = old_identity["helpers"].pop("segment_scorer")
    new_helper = new_identity["helpers"].pop("segment_scorer")

    def load_rows(root: Path, name: str) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (root / name).read_text(encoding="utf-8").splitlines()
        ]

    audit_only = {
        "eligible_units",
        "eligible_units_hex",
        "eligible_units_exact",
        "domain_units_exact",
        "cut_ids",
        "mapping_certificate_ids",
    }
    old_internal = [
        {key: value for key, value in row.items() if key not in audit_only}
        for row in load_rows(baseline, "internal_rows.jsonl")
    ]
    new_internal = [
        {key: value for key, value in row.items() if key not in audit_only}
        for row in load_rows(new, "internal_rows.jsonl")
    ]
    eligible_changes: list[dict[str, object]] = []
    eligible_certified = True
    for old_row, new_row in zip(
        load_rows(baseline, "internal_rows.jsonl"),
        load_rows(new, "internal_rows.jsonl"),
        strict=True,
    ):
        if old_row["eligible_units_hex"] == new_row["eligible_units_hex"]:
            continue
        exact_text = new_row["eligible_units_exact"]
        exact = None
        if isinstance(exact_text, str):
            numerator, denominator = exact_text.split("/", 1)
            exact = Fraction(int(numerator), int(denominator))
        certified = (
            exact is not None
            and float(exact).hex() == new_row["eligible_units_hex"]
            and isinstance(new_row["domain_units_exact"], str)
        )
        eligible_certified = eligible_certified and certified
        eligible_changes.append({
            "floor_id": new_row["floor_id"],
            "target_id": new_row["target_id"],
            "observation_id": new_row["observation_id"],
            "status": new_row["status"],
            "baseline_float_hex": old_row["eligible_units_hex"],
            "new_float_hex": new_row["eligible_units_hex"],
            "new_exact": exact_text,
            "domain_exact": new_row["domain_units_exact"],
            "cut_ids": new_row["cut_ids"],
            "certified_rounding": certified,
        })
    comparison = {
        "comparison_schema": "judge_arbitration_sm24_comparison_v1",
        "artifact_hashes": hashes,
        "input_hashes_identical": (
            old_summary["input_hashes"] == new_summary["input_hashes"]
        ),
        "internal_non_measure_fields_identical": old_internal == new_internal,
        "eligible_rounding_changes": eligible_changes,
        "eligible_rounding_changes_certified": eligible_certified,
        "public_rows_identical": equality["public_rows.jsonl"],
        "observation_to_targets_identical": equality["observation_to_targets.jsonl"],
        "wall_criteria_identical": equality["wall_criteria.jsonl"],
        "identity_identical_after_helper_removed": old_identity == new_identity,
        "helper_transition": [old_helper, new_helper],
        "blocking_change": not all((
            old_summary["input_hashes"] == new_summary["input_hashes"],
            old_internal == new_internal,
            eligible_certified,
            equality["public_rows.jsonl"],
            equality["observation_to_targets.jsonl"],
            equality["wall_criteria.jsonl"],
            old_identity == new_identity,
        )),
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "complete.diff").write_text("".join(complete), encoding="utf-8")
    comparison["complete_diff_sha256"] = _sha256(destination / "complete.diff")
    _write_json(destination / "comparison.json", comparison)


def prepare_input(repo: Path, destination: Path) -> None:
    """Atomize the accepted legacy correction's rectangular cells as raw wire."""
    relative = Path(
        "case_tests/e2e_tests/sm24_anchor/"
        "run_2026-06-24_opus_reading/1_correction/correction_geometry.json"
    )
    source = repo / relative
    geometry = json.loads(source.read_text(encoding="utf-8"))
    raw_edges: list[tuple[tuple[float, float], tuple[float, float], str]] = []
    for floor in geometry["floors"]:
        for cell in floor["cells"]:
            x0, x1 = cell["x"]
            y0, y1 = cell["y"]
            points = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            raw_edges.extend(
                (points[index], points[(index + 1) % 4], str(cell["id"]))
                for index in range(4)
            )

    cuts: dict[tuple[str, float], set[float]] = defaultdict(set)
    for first, second, _owner in raw_edges:
        axis = "h" if first[1] == second[1] else "v"
        fixed = first[1] if axis == "h" else first[0]
        lo = min(first[0], second[0]) if axis == "h" else min(first[1], second[1])
        hi = max(first[0], second[0]) if axis == "h" else max(first[1], second[1])
        cuts[(axis, fixed)].update((lo, hi))

    atoms: dict[tuple[str, float, float, float], set[str]] = {}
    for first, second, owner in raw_edges:
        axis = "h" if first[1] == second[1] else "v"
        fixed = first[1] if axis == "h" else first[0]
        lo = min(first[0], second[0]) if axis == "h" else min(first[1], second[1])
        hi = max(first[0], second[0]) if axis == "h" else max(first[1], second[1])
        local_cuts = sorted(value for value in cuts[(axis, fixed)] if lo <= value <= hi)
        for atom_lo, atom_hi in zip(local_cuts, local_cuts[1:], strict=False):
            if atom_lo < atom_hi:
                atoms.setdefault((axis, fixed, atom_lo, atom_hi), set()).add(owner)

    segments: list[dict[str, object]] = []
    for index, ((axis, fixed, lo, hi), owners) in enumerate(sorted(atoms.items())):
        p1 = (lo, fixed) if axis == "h" else (fixed, lo)
        p2 = (hi, fixed) if axis == "h" else (fixed, hi)
        footprint_axis = geometry["footprint_y"] if axis == "h" else geometry["footprint_x"]
        segments.append({
            "id": f"accepted-sm24:{index:03d}",
            "floor_id": "F1",
            "p1": p1,
            "p2": p2,
            "zone_ids": sorted(owners),
            "source_ids": [relative.as_posix()],
            "exterior": fixed in footprint_axis,
        })

    payload = {
        "audit_input_schema": "sm24_accepted_product_v1",
        "accepted_product_source": relative.as_posix(),
        "accepted_product_source_sha256": _sha256(source),
        "product_payload": {
            "segments": segments,
            "openings": [],
            "elevation_observations": [],
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_json(destination, payload)


def _fraction_text(value: object) -> str | None:
    if not isinstance(value, Fraction):
        return None
    return f"{value.numerator}/{value.denominator}"


def run_audit(repo: Path, input_path: Path, output: Path, label: str, repeat: int) -> None:
    from src.agent.execution.view_manifest import ViewManifest
    from src.agent.judge.score_config import load_judge_score_config
    from src.agent.judge.score_schema import (
        JudgeScoreViewBindingsV1,
        ProductIdentityV8,
        canonical_sha256,
        load_score_gt_identity,
    )
    from src.agent.judge.score_service import score_typed_attempt
    import src.agent.judge.segment_score as segment_score

    source = json.loads(input_path.read_text(encoding="utf-8"))
    product_payload = source["product_payload"]
    accepted_source = repo / source["accepted_product_source"]
    if _sha256(accepted_source) != source["accepted_product_source_sha256"]:
        raise RuntimeError("accepted product source hash mismatch")

    gt_path = repo / "case_tests/test_baseline/gt/sm24_anchor/gt.json"
    run_root = (
        repo / "case_tests/e2e_tests/sm24_anchor/"
        "run_2026-07-27_haiku_e2e/_run"
    )
    manifest_path = run_root / "view_manifest.json"
    bindings_path = run_root / "judge_score_bindings.json"
    config_path = repo / "src/configs/judge_score.yaml"
    gt_identity, gt = load_score_gt_identity(gt_path)
    manifest = ViewManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    bindings = JudgeScoreViewBindingsV1.model_validate_json(
        bindings_path.read_text(encoding="utf-8")
    )
    config = load_judge_score_config(config_path)
    product_hash = canonical_sha256(product_payload)
    product_identity = ProductIdentityV8(
        stage="reading",
        attempt=2,
        output_sha256=product_hash,
        output_schema="3",
        accepted=True,
        accepted_stage_record_sha256=source["accepted_product_source_sha256"],
        source="accepted_attempt",
    )

    captured: dict[str, object] = {}
    durations: list[float] = []
    memory_peaks: list[int] = []
    claims: list[object] = []
    target_ledgers: dict[str, object] = {}
    observation_ledgers: dict[str, object] = {}
    original_match = segment_score.match_plan_segments
    original_claim = getattr(segment_score, "build_coverage_claim", None)
    original_target_ledger = getattr(segment_score, "_build_target_ledger", None)
    original_observation_ledger = getattr(
        segment_score,
        "_build_observation_ledger",
        None,
    )

    if original_claim is not None:
        def capture_claim(*args: object, **kwargs: object) -> object:
            claim = original_claim(*args, **kwargs)
            if claim is not None:
                claims.append(claim)
            return claim
        segment_score.build_coverage_claim = capture_claim

    if original_target_ledger is not None:
        def capture_target_ledger(*args: object, **kwargs: object) -> object:
            ledger = original_target_ledger(*args, **kwargs)
            target_ledgers[ledger.target_key] = ledger
            return ledger
        segment_score._build_target_ledger = capture_target_ledger

    if original_observation_ledger is not None:
        def capture_observation_ledger(*args: object, **kwargs: object) -> object:
            ledger = original_observation_ledger(*args, **kwargs)
            observation_ledgers[ledger.observation_key] = ledger
            return ledger
        segment_score._build_observation_ledger = capture_observation_ledger

    def capture_match(*, targets: object, observations: object, config: object):
        target_rows = tuple(targets)
        observation_rows = tuple(observations)
        tracemalloc.reset_peak()
        memory_before = tracemalloc.get_traced_memory()[0]
        started = time.perf_counter()
        result = original_match(
            targets=target_rows,
            observations=observation_rows,
            config=config,
        )
        durations.append(time.perf_counter() - started)
        memory_peaks.append(max(0, tracemalloc.get_traced_memory()[1] - memory_before))
        captured.update({
            "targets": target_rows,
            "observations": observation_rows,
            "rows": result[0],
            "mapping": result[1],
        })
        return result

    segment_score.match_plan_segments = capture_match
    tracemalloc.start()
    results = []
    try:
        for _index in range(repeat):
            claims.clear()
            results.append(score_typed_attempt(
                gt_identity=gt_identity,
                gt=gt,
                stage="reading",
                product_payload=product_payload,
                product_identity=product_identity,
                base_view_manifest=manifest,
                score_bindings=bindings,
                completeness_overlay=None,
                c2_config=config,
            ))
    finally:
        tracemalloc.stop()
        segment_score.match_plan_segments = original_match
        if original_claim is not None:
            segment_score.build_coverage_claim = original_claim
        if original_target_ledger is not None:
            segment_score._build_target_ledger = original_target_ledger
        if original_observation_ledger is not None:
            segment_score._build_observation_ledger = original_observation_ledger

    result = results[-1]
    if any(item.sidecar.content_sha256 != result.sidecar.content_sha256 for item in results):
        raise RuntimeError("repeated front-door results are not deterministic")

    claim_index: dict[tuple[str, str], list[object]] = defaultdict(list)
    for claim in claims:
        claim_index[(claim.target_key, claim.observation_key)].append(claim)
    internal_rows: list[dict[str, Any]] = []
    for row in captured["rows"]:
        target_id = None if row.target is None else row.target.key
        observation_id = None if row.observation is None else row.observation.key
        row_claims = claim_index.get((target_id, observation_id), [])
        certificates = sorted(
            claim.mapping_certificate.certificate_id for claim in row_claims
        )
        claim_cut_ids = {
            mapped.geometry_cut_id
            for claim in row_claims
            for mapped in claim.cuts
        }
        ledger = (
            target_ledgers.get(target_id)
            if target_id is not None
            else observation_ledgers.get(observation_id)
        )
        atoms = ()
        if ledger is not None:
            if target_id is not None and observation_id is not None:
                atoms = tuple(
                    atom for atom in ledger.atoms
                    if observation_id in atom.observation_ids
                )
            elif target_id is not None:
                atoms = tuple(
                    atom for atom in ledger.atoms
                    if atom.status == row.status
                )
            else:
                atoms = tuple(
                    atom for atom in ledger.atoms
                    if atom.status == "extra"
                )
        ledger_cut_ids = {
            cut_id
            for atom in atoms
            for cut_id in (*atom.lo_cut_ids, *atom.hi_cut_ids)
        }
        segment = row.target or row.observation
        internal_rows.append({
            "floor_id": segment.floor_id,
            "target_id": target_id,
            "observation_id": observation_id,
            "exterior": segment.exterior,
            "status": row.status,
            "axis_alignment_error_m": row.axis_alignment_error_m,
            "axis_alignment_error_hex": None if row.axis_alignment_error_m is None else row.axis_alignment_error_m.hex(),
            "position_error_m": row.position_error_m,
            "position_error_hex": None if row.position_error_m is None else row.position_error_m.hex(),
            "extent_symmetric_difference_m": row.extent_symmetric_difference_m,
            "extent_symmetric_difference_hex": None if row.extent_symmetric_difference_m is None else row.extent_symmetric_difference_m.hex(),
            "eligible_units": row.eligible_units,
            "eligible_units_hex": row.eligible_units.hex(),
            "eligible_units_exact": _fraction_text(
                getattr(row, "eligible_units_exact", None)
            ),
            "domain_units_exact": _fraction_text(
                None if ledger is None else ledger.domain_exact
            ),
            "cut_ids": sorted(claim_cut_ids | ledger_cut_ids),
            "mapping_certificate_ids": certificates,
        })
    key = lambda row: (
        row["floor_id"],
        row["target_id"] or "",
        row["observation_id"] or "",
        row["exterior"],
        row["status"],
    )
    internal_rows.sort(key=key)
    public_rows = sorted(
        (row.model_dump(mode="json") for row in result.payload.segment_rows),
        key=key,
    )
    mapping_rows = [
        {"observation_id": obs, "target_ids": list(targets)}
        for obs, targets in sorted(captured["mapping"].items())
    ]
    wall_criteria = sorted(
        (
            dict(item)
            for item in result.payload.score_criteria
            if item["criterion_id"] in {
                "walls_complete",
                "no_extra_walls",
                "no_duplicate_wall_strokes",
            }
        ),
        key=lambda row: row["criterion_id"],
    )
    identity = result.identity.model_dump(mode="json")
    input_hashes = {
        "accepted_product_source_sha256": _sha256(accepted_source),
        "audit_input_file_sha256": _sha256(input_path),
        "product_payload_canonical_sha256": product_hash,
        "gt_file_sha256": _sha256(gt_path),
        "gt_content_sha256": gt_identity.content_sha256,
        "config_file_sha256": _sha256(config_path),
        "config_content_sha256": canonical_sha256(config.model_dump(mode="json")),
        "view_manifest_file_sha256": _sha256(manifest_path),
        "view_manifest_content_sha256": manifest.content_sha256,
        "score_bindings_file_sha256": _sha256(bindings_path),
        "score_bindings_content_sha256": bindings.content_sha256,
    }
    summary = {
        "audit_schema": "judge_arbitration_sm24_audit_v1",
        "label": label,
        "input_hashes": input_hashes,
        "helper_identity": identity["helpers"]["segment_scorer"],
        "identity_contract": (
            "1" if identity["helpers"]["segment_scorer"] == "b4b_segment_score_v3_ic1"
            else None
        ),
        "sidecar_content_sha256": result.sidecar.content_sha256,
        "row_count": len(internal_rows),
        "target_count": len(captured["targets"]),
        "observation_count": len(captured["observations"]),
        "coverage_claim_count": len(claims),
        "canonical_cut_count": len({
            mapped.geometry_cut_id
            for claim in claims
            for mapped in claim.cuts
        }),
        "performance": {
            "repeat": repeat,
            "matcher_seconds": durations,
            "matcher_seconds_median": statistics.median(durations),
            "matcher_peak_bytes": memory_peaks,
            "matcher_peak_bytes_max": max(memory_peaks),
        },
        "artifact_hashes": {},
    }

    output.mkdir(parents=True, exist_ok=True)
    files = {
        "internal_rows.jsonl": internal_rows,
        "public_rows.jsonl": public_rows,
        "observation_to_targets.jsonl": mapping_rows,
        "wall_criteria.jsonl": wall_criteria,
    }
    for name, rows in files.items():
        _write_jsonl(output / name, rows)
    _write_json(output / "identity.json", identity)
    for name in (*files, "identity.json"):
        summary["artifact_hashes"][name] = _sha256(output / name)
    _write_json(output / "summary.json", summary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--prepare-input", type=Path)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--new", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.compare:
        if args.baseline is None or args.new is None or args.output is None:
            parser.error("--compare requires --baseline, --new, and --output")
        compare_audits(
            args.baseline.resolve(),
            args.new.resolve(),
            args.output.resolve(),
        )
        return
    if args.prepare_input is not None:
        prepare_input(repo, args.prepare_input.resolve())
        return
    if args.input is None or args.output is None or args.label is None:
        parser.error("--input, --output, and --label are required for audit")
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    run_audit(
        repo,
        args.input.resolve(),
        args.output.resolve(),
        args.label,
        args.repeat,
    )


if __name__ == "__main__":
    main()
