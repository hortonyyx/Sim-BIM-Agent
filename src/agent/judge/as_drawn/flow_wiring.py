"""⭐ J wiring — flow's reading stage picks its judge BY THE PRODUCT CONTRACT.

Measured before this file (dispatch J §一, 2026-09-06): the new as-drawn
graders (``reading_grade.grade`` for plans, ``elevation_grade.grade`` for
facades) had ZERO production callers — flow still scored every reading run
through the legacy stroke scorer.  This module is the missing wire, and the
ONE decision it makes is deliberately not its own:

  WHICH judge a view gets is decided by ``reading/vector_contract.py``'s
  per-file classifier — the same discriminator 1_correction already trusts,
  imported read-only.  ⛔ Not by filename (``1f_view`` is a stem, not a
  contract), ⛔ not by a second classifier written here (that file's own
  discipline #1: a second definition of "which contract is this" is how two
  consumers silently disagree about one product).

What rides on the decision:

  * ``as_drawn_plan``      → plan path: denominator from the case's SIGNED
    source DXF + request (hash-matched, never by filename), then
    ``reading_grade.grade``, then ``render_reading_grade``.  The plan grader
    itself is untouched (dispatch: the grader is not the gap); the wiring
    feeds it and consumes J-2's declarations as its tolerance floor.
  * ``as_drawn_elevation_v0`` → facade path: targets derived from gt only
    (``elevation_grade.elevation_targets``), graded, rendered.
  * anything else          → NOT consumed here.  The legacy/typed path keeps
    it exactly as today (⛔ the old scorer still serves historical runs); a
    MIXED run gets its non-as-drawn views NAMED in the bundle, never silently
    dropped (the ledger-always discipline).

Identity (which gt view a product belongs to) is resolved by DECLARATION
first (``view_id`` on the product, the field future producers will carry),
then by the product's own ``image`` stem against the gt's view ids — for
elevations the stems ARE the gt view ids today (``East_view``…), and for
plans ``1f_view``/``2f_view`` map through floor position.  An unresolvable
identity is a LOUD failure (⛔ never a guess, never a quiet skip).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

from src.agent.judge.as_drawn.elevation_grade import (
    elevation_targets,
    grade as grade_elevation,
)
from src.agent.judge.as_drawn.resolutions import (
    read_gt_resolution,
    read_product_resolution,
    quantization_band_m,
)
from src.agent.reading import vector_contract

#: the two contracts this wire serves; everything else is NOT ours to judge.
AS_DRAWN_CONTRACTS = frozenset({
    vector_contract.CONTRACT_AS_DRAWN_PLAN,
    vector_contract.CONTRACT_AS_DRAWN_ELEVATION_V0,
})

BUNDLE_SCHEMA = "as_drawn_grade_bundle_v1"

_PLAN_STEM = re.compile(r"^(\d+)f_view$")


class UnknownAsDrawnView(RuntimeError):
    """A product whose gt view identity cannot be resolved — loud, ⛔ no guess.

    Grading a product against the WRONG view's answer is worse than not
    grading it (it launders one facade's answer onto another), so an identity
    that does not resolve stops the branch here.
    """

    def __init__(self, stem: str, reason: str) -> None:
        self.stem = stem
        super().__init__(f"unknown_as_drawn_view[{stem}]: {reason}")


class SignedSourceDxfNotFound(RuntimeError):
    """The case's signed source DXF could not be bound by the request's hash.

    The denominator must run on the ANSWER'S OWN source drawing; locating it
    by filename would let a stale sibling (``*_as_received.dxf`` ships next to
    the signed one) silently stand in.
    """

    def __init__(self, case: str, wanted_sha256: str) -> None:
        super().__init__(
            f"signed_source_dxf_not_found case={case}: no *.dxf under"
            f" gt_sources/{case}/ hashes to the request's"
            f" source_dxf_sha256={wanted_sha256}")


def split_output_by_contract(output: Mapping) -> dict[str, object]:
    """Classify every view in a flat ``{stem: view}`` reading output.

    ⭐ Reuses ``vector_contract.classify_vector_json`` verbatim — never raises
    (that module's discipline #6), so a malformed product yields an ``unknown``
    decision, which this wire simply does not consume.
    """
    if not isinstance(output, Mapping):
        return {}
    decisions = {}
    for stem, view in output.items():
        decisions[stem] = vector_contract.classify_vector_json(view)
    return decisions


def _gt_views(gt: Mapping) -> list[dict]:
    return [view for source in gt.get("sources") or []
            for view in source.get("views") or []]


def _image_stem(doc: Mapping) -> str | None:
    image = doc.get("image")
    return Path(str(image)).stem if image else None


def resolve_elevation_view_id(doc: Mapping, gt: Mapping) -> str:
    """Elevation identity: explicit declaration, then the image stem (today the
    stems ARE gt view ids: East_view/North_view/South_view/West_view)."""
    if doc.get("view_id"):
        return str(doc["view_id"])
    stem = _image_stem(doc)
    for view in _gt_views(gt):
        if view.get("kind") == "elevation" and view.get("id") == stem:
            return str(view["id"])
    raise UnknownAsDrawnView(
        stem or "?", "no elevation view id declared and the image stem matches"
        " no gt elevation view")


def resolve_plan_view_id(doc: Mapping, gt: Mapping) -> str:
    """Plan identity: explicit declaration, then the image stem, then the
    ``<n>f_view`` floor-position rule (``1f_view`` → the plan view of floor 1)."""
    if doc.get("view_id"):
        return str(doc["view_id"])
    stem = _image_stem(doc)
    direct = [v for v in _gt_views(gt) if v.get("kind") == "plan"
              and v.get("id") == stem]
    if direct:
        return str(direct[0]["id"])
    if stem:
        match = _PLAN_STEM.match(stem)
        if match:
            floors = [f.get("id") for f in gt.get("floors") or []]
            index = int(match.group(1)) - 1
            if 0 <= index < len(floors):
                floor_id = floors[index]
                for view in _gt_views(gt):
                    if (view.get("kind") == "plan"
                            and list(view.get("floor_ids") or []) == [floor_id]):
                        return str(view["id"])
                raise UnknownAsDrawnView(
                    stem, f"floor {floor_id} has no plan view in gt")
    raise UnknownAsDrawnView(
        stem or "?", "no plan view id declared and the image stem resolves to"
        " nothing (expected '<n>f_view' or an exact gt plan view id)")


def signed_source_dxf(case_sources_dir: Path, request: Mapping) -> Path:
    """Bind the case's signed DXF by the REQUEST'S OWN hash — ⛔ not by name."""
    wanted = str(request["source_dxf_sha256"])
    for candidate in sorted(case_sources_dir.glob("*.dxf")):
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest == wanted:
            return candidate
    raise SignedSourceDxfNotFound(case_sources_dir.name, wanted)


def grade_as_drawn_plan(doc: Mapping, *, gt: Mapping, case_sources_dir: Path,
                        request_path: Path) -> dict:
    """Plan path: denominator from the signed source, J-2 floor on the band,
    ``reading_grade.grade`` (the grader itself untouched)."""
    from src.agent.judge.as_drawn.denominator import denominator
    from src.agent.judge.as_drawn.reading_grade import POS_TOL_M, grade

    view_id = resolve_plan_view_id(doc, gt)
    request = json.loads(Path(request_path).read_text())
    dxf = signed_source_dxf(Path(case_sources_dir), request)
    den = denominator(dxf, request_path, view_id)

    # ⭐ J-2 consumed on the plan side as a TOLERANCE FLOOR: the semantic band
    # (half the thinnest wall, the grader's own declaration) must never sit
    # below what the two declared grids make unmeetable.  The grader's params
    # report the effective value, so the consumption is visible per view.
    band = quantization_band_m(read_gt_resolution(gt).value_m,
                               read_product_resolution(doc).value_m)
    return {"view_id": view_id,
            "grade": grade(doc, den, pos_tol=max(POS_TOL_M, band))}


def grade_as_drawn_elevation(doc: Mapping, *, gt: Mapping) -> dict:
    """Facade path: targets from gt only, graded whole across every floor."""
    view_id = resolve_elevation_view_id(doc, gt)
    return {"view_id": view_id,
            "grade": grade_elevation(doc, elevation_targets(gt, view_id), gt=gt)}


def grade_as_drawn_attempt(flat: Mapping, *, gt: Mapping, gt_sources_dir: Path,
                           attempt_dir: Path) -> dict:
    """Grade every as-drawn view of one attempt; NAME the views left behind.

    ``gt_sources_dir`` is the case's signed-inputs root (``gt_sources/<case>/``
    holding ``request.json`` + the signed DXF) — the caller resolves it; this
    module never guesses a case layout.  Writes ``<stem>.grade.json`` +
    ``<stem>.grade.png`` per view and the bundle ``score_vs_gt.json`` into
    ``attempt_dir``.  A view whose contract is neither as-drawn shape is
    listed in ``leftover_views`` with its contract id — ⛔ never silently
    skipped.
    """
    attempt_dir = Path(attempt_dir)
    decisions = split_output_by_contract(flat)
    views, leftovers = [], []
    for stem, decision in decisions.items():
        (views if decision.contract_id in AS_DRAWN_CONTRACTS
         else leftovers).append((stem, decision))

    entries = []
    for stem, decision in views:
        doc = flat[stem]
        out_json = attempt_dir / f"{stem}.grade.json"
        out_png = attempt_dir / f"{stem}.grade.png"
        if decision.contract_id == vector_contract.CONTRACT_AS_DRAWN_PLAN:
            graded = grade_as_drawn_plan(
                doc, gt=gt, case_sources_dir=gt_sources_dir,
                request_path=gt_sources_dir / "request.json")
            _render_plan_png(doc, graded["grade"], out_png)
        else:
            graded = grade_as_drawn_elevation(doc, gt=gt)
            _render_elevation_png(doc, graded["grade"], out_png)
        out_json.write_text(json.dumps(graded["grade"], ensure_ascii=False,
                                       indent=1) + "\n")
        entries.append({
            "stem": stem, "contract": decision.contract_id,
            "view_id": graded["view_id"],
            "grade_json": str(out_json), "grade_png": str(out_png),
            "grade_version": graded["grade"]["grade_version"],
            "scores": graded["grade"]["scores"],
            "denominator": graded["grade"]["denominator"],
        })

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "case": gt.get("case"),
        "views": entries,
        "leftover_views": [
            {"stem": stem, "contract": decision.contract_id,
             "reason": decision.reason or "not an as-drawn product contract"}
            for stem, decision in leftovers],
        "leftover_disposition": ("named here and left to the legacy/typed"
                                 " scoring path — ⛔ not silently dropped"),
    }
    (attempt_dir / "score_vs_gt.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=1) + "\n")
    return bundle


#: products carry the image path REPO-RELATIVE; the renderer opens it from
#: the process cwd, so absolutise against the repo root before rendering.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _render_png(doc: Mapping, graded: Mapping, out_png: Path,
                module: str) -> str | None:
    """The grade PICTURE — reuses the existing renderer, view-only.

    The picture must never fail a grade: any renderer problem returns None
    and the score artifacts still land.  """
    import sys
    scripts = str(_REPO_ROOT / "scripts" / "tool_scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    try:
        renderer = __import__(module).render
    except Exception:
        return None
    import tempfile
    staged = dict(doc)
    if doc.get("image") and not Path(str(doc["image"])).is_absolute():
        staged["image"] = str(_REPO_ROOT / str(doc["image"]))
    with tempfile.TemporaryDirectory() as tmp:
        doc_path = Path(tmp) / "doc.json"
        grade_path = Path(tmp) / "grade.json"
        doc_path.write_text(json.dumps(staged, ensure_ascii=False))
        grade_path.write_text(json.dumps(graded, ensure_ascii=False))
        try:
            renderer(str(doc_path), str(grade_path), str(out_png))
            return str(out_png)
        except Exception:
            return None


def _render_plan_png(doc: Mapping, graded: Mapping, out_png: Path) -> str | None:
    return _render_png(doc, graded, out_png, "render_reading_grade")


def _render_elevation_png(doc: Mapping, graded: Mapping, out_png: Path) -> str | None:
    return _render_png(doc, graded, out_png, "render_elevation_grade")
