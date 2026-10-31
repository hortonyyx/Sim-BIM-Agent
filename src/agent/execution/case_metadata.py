"""Case manifest helpers for validator-visible, non-GT metadata."""

from __future__ import annotations

import json
from pathlib import Path


def testdata_path(case_dir: Path | str) -> Path | None:
    case_dir = Path(case_dir)
    for candidate in (
        case_dir / "case_data" / "testdata_prompt.json",
        case_dir / "testdata_prompt.json",
    ):
        if candidate.exists():
            return candidate
    return None


def load_case_metadata(case_dir: Path | str) -> dict:
    path = testdata_path(case_dir)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_testdata_text(text: str) -> dict | None:
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def expected_zone_total_from_testdata(data: dict) -> int | None:
    plans = data.get("Floor plans") or []
    totals = [
        plan.get("thermal_zones")
        for plan in plans
        if isinstance(plan, dict) and isinstance(plan.get("thermal_zones"), int)
    ]
    return sum(totals) if totals else None


def _dimensioned_view_names_from_data(data: dict) -> set[str]:
    """Parse the legacy dimensioned stem set from case metadata (bool-form
    consumers). Pure function over ``data`` so the 4-state
    :func:`dimensioned_states_from_data` can reuse it for the legacy signals.

    Note: ``add()`` returns on non-strings, so a STRUCTURED ``dimensioned_views``
    object list is invisible here — that loss is exactly what R1-3 fixes on the
    offline-audit surface via :func:`dimensioned_states_from_data`."""
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
            add(f"{item.get('floor')}f_view" if item.get("floor") is not None else "")

    views = data.get("views") or {}
    if isinstance(views, dict):
        for key, item in views.items():
            if isinstance(item, dict) and item.get("dimensioned") is True:
                add(key)
                add(item.get("path"))

    return names


def dimensioned_view_names(case_dir: Path | str) -> set[str]:
    return _dimensioned_view_names_from_data(load_case_metadata(case_dir))


def dimensioned_states_from_data(data: dict) -> dict[str, str]:
    """R1-3 (派工单 §1.3): per-view 4-state dimensioned applicability parsed
    from case metadata, WITHOUT folding to bool or dropping structured
    declarations.

    The legacy signals (stem-string ``dimensioned_views`` list, ``Floor plans``
    / ``views`` overlay ``dimensioned: true``) all map to ``declared_true`` —
    the same set :func:`dimensioned_view_names` returns. A STRUCTURED
    ``dimensioned_views`` object list (the form r0's S-3 wire made
    authoritative on the production gate① path) additionally carries
    per-view ``declared_true`` / ``declared_false`` with provenance; that
    declaration is what :func:`_dimensioned_view_names_from_data` silently
    dropped (its ``add()`` returns on non-strings), collapsing a
    ``declared_false`` to ``legacy_default`` and losing a ``declared_true``
    entirely on the validate_case / record_baseline / evidence-preflight
    offline-audit surface. Stems absent from any declaration are NOT in the
    map; callers treat absence as ``legacy_default``.
    """
    states: dict[str, str] = {}
    for stem in _dimensioned_view_names_from_data(data):
        states[stem] = "declared_true"
    # structured object list overrides with the provenance-bound 4-state
    for item in data.get("dimensioned_views") or []:
        if isinstance(item, dict):
            view = item.get("view")
            if isinstance(view, str) and view:
                p = Path(view)
                stem = p.stem if p.suffix else view
                flag = item.get("dimensioned")
                if isinstance(flag, bool):
                    states[stem] = "declared_true" if flag else "declared_false"
    return states


def dimensioned_view_states(case_dir: Path | str) -> dict[str, str]:
    """Per-view 4-state dimensioned map for a case dir (R1-3 fidelity fix for
    the validate_case / record_baseline offline-audit surface)."""
    return dimensioned_states_from_data(load_case_metadata(case_dir))
