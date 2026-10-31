"""Build a manifest-bound GT v3 *candidate* from a graphics-export DXF.

This is intentionally a thin build-only judge tool.  It only writes a new,
explicit candidate path after the build has passed validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))  # Own checkout precedes any editable install.

from src.agent.judge.gt_extraction import ExtractionInputs, extract_gt_v3
from src.agent.judge.gt import DEFAULT_GT_DIR
from src.agent.judge.gt_manifest import (GtExtractionManifestV1,
                                          load_gt_tooling_config)
from src.agent.judge.gt_schema import (REPO_ROOT,
                                       compute_gt_implementation_hashes,
                                       write_gt_v3_candidate)


def _protected_dxf_source(path: Path) -> bool:
    source = Path(path).resolve()
    protected = (Path(DEFAULT_GT_DIR).resolve(), (REPO_ROOT / "case_tests/test_baseline/gt_sources").resolve())
    if any(source.is_relative_to(root) for root in protected):
        return True
    try:
        relative = source.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return len(relative.parts) >= 4 and relative.parts[:2] == ("case_tests", "e2e_tests") and relative.parts[3] == "case_data"


def build_candidate(*, dxf: Path, manifest: Path, config: Path, vg_config: Path):
    """Run preflight and build a typed candidate without writing it."""
    if _protected_dxf_source(dxf):
        raise ValueError("gt_dxf_source_protected_path")
    manifest_doc = GtExtractionManifestV1.model_validate(json.loads(Path(manifest).read_bytes()))
    tooling = load_gt_tooling_config(Path(config), Path(vg_config))
    hashes = compute_gt_implementation_hashes(REPO_ROOT)
    inputs = ExtractionInputs(Path(dxf), manifest_doc, tooling, hashes)
    return extract_gt_v3(inputs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--vg-config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True,
                        help="new candidate path outside GT/source/case-data roots")
    args = parser.parse_args(argv)
    doc = build_candidate(dxf=args.dxf, manifest=args.manifest, config=args.config, vg_config=args.vg_config)
    write_gt_v3_candidate(doc, args.out)
    print(json.dumps({"case": doc.case, "content_sha256": doc.content_sha256, "out": str(args.out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
