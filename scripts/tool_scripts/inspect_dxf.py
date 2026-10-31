"""Inspect a DXF file's structure — the prerequisite step before writing a tailored
CAD->gt extractor for a specific drawing (see architecture/cad_to_gt_extraction_plan.md).

A merged 天正 (TArch) drawing puts every view (floor plans + elevations) in one model
space, with layers split by COMPONENT TYPE (all walls on WALL, all windows on WINDOW),
NOT by view. So extraction must first segment model space into view regions spatially,
then read components by layer within each region. This tool surfaces the facts that
decide whether/how that is feasible for a given file:

  * units + model-space extents
  * per-layer entity-type histogram (which layer holds walls / windows / doors / rooms)
  * PROXY / custom-object count  — the 天正 gotcha: un-exploded TCH_* objects read as
    proxies (geometry invisible to ezdxf). A high count here => re-export via 天正
    「图形导出」 (or EXPLODE) so components become plain LINE/ARC/LWPOLYLINE/TEXT/HATCH.
  * TEXT / MTEXT strings + positions, with 图名 (plan/elevation titles) highlighted
  * INSERT block usage (windows/doors are often block references)
  * a rough spatial clustering of entities into candidate view regions

Read-only. Requires ezdxf. Pairs with (future) gt_from_dxf.py.

Usage:
    python scripts/tool_scripts/inspect_dxf.py path/to/merged.dxf [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import ezdxf
from ezdxf import bbox
from ezdxf.entities import DXFTagStorage  # unsupported/custom (e.g. un-exploded 天正) entities

from src.agent.judge.gt_extraction import (DxfInspectionReportV1,
                                            InspectionInputs,
                                            inspect_extraction_inputs)
from src.agent.judge.gt_manifest import (GtExtractionManifestV1,
                                          load_gt_tooling_config)
from src.agent.judge.gt_schema import REPO_ROOT, compute_gt_implementation_hashes

# 图名 patterns: 天正 view titles. 「图」is optional — 一层平面 / 首层平面 / 南立面 / 1-1剖面
# all occur without it (Codex Low: title detection missed these). 平面/立面/剖面 as substrings
# already cover the floor-number and 东南西北 prefixes. Broad on purpose: the inspector lists
# candidates for a human, so over-matching is cheaper than missing a title.
_TITLE_RE = re.compile(r"(平面图?|立面图?|剖面图?|详图|大样|plan|elevation|section)", re.IGNORECASE)
# structural entity types whose geometry defines view regions (text/dims excluded:
# they sit around views and would blur clustering, and TEXT bbox needs a font)
_STRUCTURAL = {"LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "ELLIPSE",
               "SPLINE", "HATCH", "SOLID", "INSERT", "3DFACE"}
# common 天正 component layer name fragments (hypotheses; the real names come from output)
_LAYER_HINTS = {
    "wall": ("WALL", "墙"),
    "window": ("WINDOW", "窗"),
    "door": ("DOOR", "门"),
    "room": ("ROOM", "AREA", "房间", "PUB_AREA"),
    "axis": ("AXIS", "DOTE", "轴"),
    "dim": ("DIM", "标注", "PUB_DIM"),
    "text": ("TEXT", "PUB_TEXT", "字"),
    "column": ("COLU", "柱"),
}


def _layer(e) -> str:
    """Layer of any entity. `e.dxf.layer` raises for 天正 TCH_* (DXFTagStorage), whose
    layer (group code 8) lives in the raw tags instead — fall back to scanning them."""
    try:
        return e.dxf.layer
    except Exception:
        try:
            for tag in e.xtags:
                if tag.code == 8:
                    return tag.value
        except Exception:
            pass
        return "0"


def _entity_bbox(e):
    try:
        b = bbox.extents([e], fast=True)
        if b.has_data:
            return (b.extmin.x, b.extmin.y, b.extmax.x, b.extmax.y)
    except Exception:
        pass
    return None


def _classify_layer(name: str) -> str:
    up = name.upper()
    for kind, frags in _LAYER_HINTS.items():
        if any(f.upper() in up for f in frags):
            return kind
    return "other"


def _cluster_views(boxes: list[tuple], gap: float) -> list[tuple]:
    """Union bounding boxes whose gap < `gap` -> candidate view regions (simple O(n^2))."""
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def near(a, b):
        ax0, ay0, ax1, ay1 = a
        bx0, by0, bx1, by1 = b
        dx = max(bx0 - ax1, ax0 - bx1, 0.0)
        dy = max(by0 - ay1, ay0 - by1, 0.0)
        return dx <= gap and dy <= gap

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if near(boxes[i], boxes[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[tuple]] = defaultdict(list)
    for i, b in enumerate(boxes):
        groups[find(i)].append(b)
    regions = []
    for members in groups.values():
        xs0 = min(m[0] for m in members); ys0 = min(m[1] for m in members)
        xs1 = max(m[2] for m in members); ys1 = max(m[3] for m in members)
        regions.append((xs0, ys0, xs1, ys1, len(members)))
    regions.sort(key=lambda r: (-(r[4])))   # busiest first
    return regions


def inspect(path: Path) -> dict:
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    insunits = doc.header.get("$INSUNITS", 0)
    unit_name = {0: "unitless", 1: "in", 4: "mm", 5: "cm", 6: "m"}.get(insunits, str(insunits))

    # per-layer entity-type histogram + proxy count + boxes for clustering
    layer_hist: dict[str, Counter] = defaultdict(Counter)
    type_total = Counter()
    custom_types = Counter()         # 天正 TCH_* / proxy objects (geometry not readable)
    proxy = 0
    boxes = []
    texts = []
    inserts = Counter()
    for e in msp:
        t = e.dxftype()
        type_total[t] += 1
        layer_hist[_layer(e)][t] += 1
        # un-exploded 天正 TCH_* objects + ACAD proxies load as DXFTagStorage: ezdxf can read
        # their LAYER but NOT their geometry (locked in proprietary params / cached proxy
        # graphics). A nonzero count => re-export via 天正「图形导出」 to get plain geometry.
        if isinstance(e, DXFTagStorage) or t in ("ACAD_PROXY_ENTITY", "ACAD_PROXY_GRAPHIC"):
            proxy += 1
            custom_types[t] += 1
        if t in ("TEXT", "MTEXT"):
            s = e.plain_text() if t == "MTEXT" else e.dxf.text
            p = e.dxf.insert if e.dxf.hasattr("insert") else getattr(e.dxf, "align_point", None)
            texts.append({"text": s, "layer": e.dxf.layer,
                          "at": [round(p.x, 1), round(p.y, 1)] if p else None,
                          "is_title": bool(_TITLE_RE.search(s or ""))})
        if t == "INSERT":
            inserts[e.dxf.name] += 1
        if t in _STRUCTURAL:
            b = _entity_bbox(e)
            if b:
                boxes.append(b)

    extents = None
    if boxes:
        extents = [round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1),
                   round(max(b[2] for b in boxes), 1), round(max(b[3] for b in boxes), 1)]
        span = max(extents[2] - extents[0], extents[3] - extents[1])
        regions = _cluster_views(boxes, gap=span * 0.02)   # 2% of drawing span
    else:
        regions = []

    layers = []
    for name, hist in sorted(layer_hist.items()):
        layers.append({"layer": name, "kind": _classify_layer(name),
                       "count": sum(hist.values()), "types": dict(hist)})

    recommendation = "ok: geometry is plain entities — proceed to extraction."
    if custom_types:
        recommendation = (
            f"{proxy} live 天正/proxy objects ({dict(custom_types)}): ezdxf reads their LAYER "
            "but not their geometry. Re-export via 天正「图形导出」 so walls/openings become "
            "plain LINE/ARC/LWPOLYLINE on the same layers, then re-run."
        )

    return {
        "file": str(path),
        "dxf_version": doc.dxfversion,
        "units": unit_name,
        "extents": extents,
        "entity_total": sum(type_total.values()),
        "entity_types": dict(type_total.most_common()),
        "proxy_or_unsupported": proxy,
        "custom_objects": dict(custom_types.most_common()),
        "recommendation": recommendation,
        "layers": layers,
        "titles": [t for t in texts if t["is_title"]],
        "text_sample": texts[:40],
        "insert_blocks": dict(inserts.most_common(20)),
        "candidate_view_regions": [
            {"bbox": [round(r[0], 1), round(r[1], 1), round(r[2], 1), round(r[3], 1)],
             "entity_count": r[4]} for r in regions[:12]
        ],
    }


def _print_report(rep: dict) -> None:
    print(f"file          : {rep['file']}")
    print(f"dxf version   : {rep['dxf_version']}   units: {rep['units']}")
    print(f"extents       : {rep['extents']}")
    print(f"entities      : {rep['entity_total']}   "
          f"proxy/unsupported: {rep['proxy_or_unsupported']}"
          f"{'  <-- EXPLODE / 天正图形导出 needed!' if rep['proxy_or_unsupported'] else ''}")
    print(f"entity types  : {rep['entity_types']}")
    if rep.get("custom_objects"):
        print(f"custom objects: {rep['custom_objects']}")
    print(f"recommendation: {rep['recommendation']}")
    print("\nlayers (name | kind | count | types):")
    for l in rep["layers"]:
        print(f"  {l['layer']:<22} {l['kind']:<8} {l['count']:>6}  {l['types']}")
    print(f"\ntitles (图名) found: {len(rep['titles'])}")
    for t in rep["titles"]:
        print(f"  '{t['text']}'  @ {t['at']}  [{t['layer']}]")
    print(f"\ninsert blocks: {rep['insert_blocks']}")
    print(f"\ncandidate view regions: {len(rep['candidate_view_regions'])}")
    for r in rep["candidate_view_regions"]:
        print(f"  bbox={r['bbox']}  entities={r['entity_count']}")


def _protected_output(path: Path) -> bool:
    """Inspection reports are diagnostics, never baseline/case assets."""
    candidate = path if path.is_absolute() else Path.cwd() / path
    suffix = []
    parent = candidate.parent
    while not parent.exists():
        suffix.append(parent.name)
        parent = parent.parent
    resolved = parent.resolve(strict=True).joinpath(*reversed(suffix), candidate.name)
    roots = (REPO_ROOT / "case_tests/test_baseline/gt", REPO_ROOT / "case_tests/test_baseline/gt_sources")
    if any(resolved.is_relative_to(root.resolve()) for root in roots):
        return True
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return len(relative.parts) >= 4 and relative.parts[:2] == ("case_tests", "e2e_tests") and relative.parts[3] == "case_data"


def _protected_dxf_input(path: Path) -> bool:
    resolved = path.resolve()
    if resolved.is_relative_to((REPO_ROOT / "case_tests/test_baseline/gt").resolve()):
        return True
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return len(relative.parts) >= 4 and relative.parts[:2] == ("case_tests", "e2e_tests") and relative.parts[3] == "case_data"


def _write_report(path: Path, report: DxfInspectionReportV1) -> None:
    if path.exists() or _protected_output(path):
        raise ValueError("--json-out must be new and outside protected asset roots")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode())
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only manifest-bound DXF preflight.")
    ap.add_argument("--dxf", required=True, help="DXF source outside GT asset roots")
    ap.add_argument("--manifest", help="signed extraction manifest JSON")
    ap.add_argument("--config", required=True, help="judge GT tolerance profile")
    ap.add_argument("--vg-config", required=True, help="registered correction Vg profile")
    ap.add_argument("--json-out", help="new diagnostic report path")
    args = ap.parse_args()
    try:
        if _protected_dxf_input(Path(args.dxf)):
            raise ValueError("--dxf must be outside GT and e2e case-data roots")
        manifest = GtExtractionManifestV1.model_validate_json(Path(args.manifest).read_bytes()) if args.manifest else None
        tooling = load_gt_tooling_config(Path(args.config), Path(args.vg_config))
        hashes = compute_gt_implementation_hashes(REPO_ROOT)
        report = inspect_extraction_inputs(InspectionInputs(Path(args.dxf), manifest, tooling, hashes))
        if args.json_out:
            _write_report(Path(args.json_out), report)
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True, ensure_ascii=False))
        raise SystemExit({"PASS": 0, "BLOCKED": 1, "UNBOUND": 2}[report.status])
    except SystemExit:
        raise
    except Exception as exc:
        print(f"inspection error: {exc}", file=__import__("sys").stderr)
        raise SystemExit(3)


if __name__ == "__main__":
    main()
