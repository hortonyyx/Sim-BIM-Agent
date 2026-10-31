"""⭐ The reading TOOLBOX -- one subcommand per question a reader actually asks.

Why this exists (2026-08-25, user): "整体架构还是要由模型驱动，因为图纸千奇百怪，
确定的代码工序很多都接不了" / "该交给模型的还是交给模型，可以给配工具，但是不能因为
某种情况下代码可以做好就给烤死了" / "以及配足够多好用适用的工具".

``as_drawn_v2.build()`` still runs the ONE fixed route (the user's ruling:
"现在先代码固定编排吧，后期要改成模型驱动").  This file is the same stages exposed
so a caller -- a person today, a ``reading-agent`` later -- can run them one at a
time, look at what came out, and decide what to do next.

⛔ Every subcommand is a CALLIPER, never a procedure: it measures and prints.
None of them names a family, picks a pair, or classifies a gap -- that is 认, and
it comes from outside (`perception/<case>.json`).  ⛔ And none of them is a way
around the exit: the eleven gates and the grade run on the PRODUCT, whatever
route produced it.

  pens   <image>        which pens does this drawing use, and what do their
                        marks look like?      ⛔ names nothing
  ruler  <cfg>          what is the scale and where is world zero?
  faces  <cfg>          every candidate face line: where, how far it runs,
                        where it breaks
  pairs  <cfg>          every same-axis pair that COULD be one wall, grouped by
                        face line -- the menu perception chooses from
  gaps   <cfg>          every blank stretch, with the measured ink of every
                        family across it -- the menu perception names
  build  <cfg> <out>    the default route, end to end (what run_all.py calls)

⚠️ `ruler`/`faces`/`pairs`/`gaps` need a cfg because the drawing box, the
dimension chains and the family roles are inputs to them; `pens` needs only the
image, and is the one to run FIRST on a drawing nobody has configured yet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import src.agent.reading.as_drawn.as_drawn_v2 as A


def _cfg(path: str) -> dict:
    return json.loads(Path(path).read_text())


def _prepared(cfg_path: str):
    """cfg -> everything the measuring stages need, without running them."""
    cfg, percept = A.load_perception(_cfg(cfg_path))
    _a, pal, masks = A.discover_pens(cfg)
    roles = A.resolve_roles(cfg, pal, masks)
    return cfg, percept, pal, masks, roles


def cmd_pens(image: str) -> dict:
    a = A.load_rgb(image)
    pal = A.palette(a)
    return {"image": image,
            "achromatic_only": pal["achromatic_only"],
            "unassigned_pct": pal["unassigned_pct"],
            "families": [{"id": f["id"], "chromaticity": f["chromaticity"],
                          "pct_of_ink": f["pct_of_ink"], "shape": f["shape"]}
                         for f in pal["families"]],
            "⛔": "no family is named here. Which one is the wall is 认 -- yours."}


def cmd_ruler(cfg_path: str) -> dict:
    cfg, _p, _pal, masks, roles = _prepared(cfg_path)
    r = A.fit_ruler(cfg, masks[roles["annotation"]])
    return {"mm_per_px": round(r.mm_per_px, 6),
            "world_zero_px": [round(r.x_zero, 3), round(r.y_zero, 3)],
            "x": {k: r.fx.as_dict()[k] for k in
                  ("chain_closure_mm", "rmse_px", "max_abs_residual_px",
                   "unmatched_ticks_px") if k in r.fx.as_dict()},
            "y": {k: r.fy.as_dict()[k] for k in
                  ("chain_closure_mm", "rmse_px", "max_abs_residual_px",
                   "unmatched_ticks_px") if k in r.fy.as_dict()},
            "ticks_x": len(r.tick_map["x"]), "ticks_y": len(r.tick_map["y"])}


def _faces(cfg_path: str):
    cfg, percept, pal, masks, roles = _prepared(cfg_path)
    r = A.fit_ruler(cfg, masks[roles["annotation"]])
    st = A.structure_mask(cfg, masks, roles)
    return cfg, percept, r, A.trace_face_lines(cfg, st, masks, r)


def cmd_faces(cfg_path: str) -> dict:
    _cfg_, _p, _r, fl = _faces(cfg_path)
    return {"face_lines": len(fl),
            "rows": [{"id": f["id"], "axis": f["constant_world_axis"],
                      "pos_m": f["pos_m"], "edges_m": f["edges_m"],
                      "runs": len(f["runs_m"]), "gaps": len(f["gaps"]),
                      "drawn_m": round(sum(abs(b - a) for a, b in f["runs_m"]), 3),
                      "extent_m": [min(v for r in f["runs_m"] for v in r),
                                   max(v for r in f["runs_m"] for v in r)]}
                     for f in fl]}


def cmd_pairs(cfg_path: str) -> dict:
    """The MENU perception chooses from -- ⛔ not a recommendation.

    ⚠️ Measured 2026-08-25: 'mutually nearest neighbour' looks like it could pick
    for you (20/22, 20/21, 8/8 of the chosen pairs are exactly that) -- but it
    would ALSO pair two strokes of the '240' callout TEXT into a 120 mm wall
    (L035-L033 at 0.1190 m, L049-L048 at 0.1082 m on sm25 1F).  Code cannot tell
    those apart from the real ones; that is why the choice is 认.
    """
    cfg, _p, r, fl = _faces(cfg_path)
    cands, by_face = A.enumerate_pair_candidates(cfg, fl, r)
    pos = {f["id"]: f["pos_m"] for f in fl}
    nearest = {}
    for c in cands:
        for me, other in ((c["face_a"], c["face_b"]), (c["face_b"], c["face_a"])):
            cur = nearest.get(me)
            if cur is None or c["spacing_px"] < cur[0]:
                nearest[me] = (c["spacing_px"], other)
    rows = []
    for c in cands:
        a, b = c["face_a"], c["face_b"]
        rows.append({**c, "pos_a_m": pos[a], "pos_b_m": pos[b],
                     "mutually_nearest": (nearest.get(a, (0, None))[1] == b
                                          and nearest.get(b, (0, None))[1] == a)})
    return {"candidates": len(rows), "faces_with_a_candidate": len(by_face),
            "basis": "same axis + disjoint support columns + shared run overlap; "
                     "⛔ NO spacing threshold; declared callouts are a LABEL only.",
            "rows": rows}


def cmd_gaps(cfg_path: str) -> dict:
    """The MENU perception names: door / window / not-an-opening / I cannot tell."""
    _c, _p, _r, fl = _faces(cfg_path)
    oc = A.enumerate_opening_candidates(fl)
    return {"gaps": len(oc),
            "basis": "every blank stretch of a face line, with the measured ink of "
                     "every discovered family across it; ⛔ no classification, "
                     "⛔ no threshold.",
            "rows": oc}


def cmd_build(cfg_path: str, out: str) -> dict:
    d = A.build(_cfg(cfg_path))
    Path(out).write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    return {"out": out, **d["ledger"]}


CMDS = {"pens": (cmd_pens, 1), "ruler": (cmd_ruler, 1), "faces": (cmd_faces, 1),
        "pairs": (cmd_pairs, 1), "gaps": (cmd_gaps, 1), "build": (cmd_build, 2)}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        raise SystemExit(__doc__)
    fn, n = CMDS[sys.argv[1]]
    args = sys.argv[2:2 + n]
    if len(args) != n:
        raise SystemExit(f"⛔ {sys.argv[1]} needs {n} argument(s)\n{__doc__}")
    print(json.dumps(fn(*args), ensure_ascii=False, indent=1))
