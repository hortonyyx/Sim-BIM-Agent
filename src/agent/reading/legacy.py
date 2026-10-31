"""Legacy → canonical reading-view migration adapter (M1, short-term + flagged).

Old reading JSONs (e.g. sm20_anchor/0_reading/*.json) predate the P1a dimension
chain and the P1b image-local facade fields. They carry instead:
  - bare ``dimensions[]`` with ``text`` (no value_m / text_verbatim / chain_id), and
  - a free-text ``facade_axis_note`` that mixes image and world semantics
    (e.g. "East facade: local x = world y (increasing northward); local y = world z").

``load_reading_view`` accepts either shape. For a legacy artifact it back-fills
the new fields conservatively and records every inference in ``migration_flags``
so a consumer never mistakes a migrated guess for a first-class reading claim.
The world-direction hints in a legacy note are preserved as
``orientation_evidence`` (source="legacy_note", confidence="low") — NOT promoted
into image-local fields, keeping P1b's "don't mix image/world" rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.agent.reading.schema import (
    Dimension,
    FacadeOrientation,
    OrientationEvidence,
    ReadingView,
)

READING_RAW_METADATA_ATTR = "_reading_raw_metadata"

# "15.00", "8", "3.6 m", "12,500" (mm-style comma kept simple: strip thousands).
_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")
_FACADE_IN_TEXT = re.compile(r"\b(north|south|east|west)\b", re.IGNORECASE)


def parse_value_m(text: str | None) -> float | None:
    """Best-effort numeric metres from a legacy dimension ``text``."""
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _migrate_dimension(d: dict) -> Dimension:
    text = d.get("text")
    if d.get("value_m") is None and text is not None:
        d = {**d, "value_m": parse_value_m(text)}
    if d.get("text_verbatim") is None and text is not None:
        d["text_verbatim"] = text
    return Dimension.model_validate(d)


def _migrate_facade(view: dict, flags: list[str]) -> FacadeOrientation | None:
    """Build an image-local FacadeOrientation from a legacy view.

    The legacy ``facade_axis_note`` is world-flavoured; we only mine it for which
    facade the elevation depicts (view_facade) and keep the note as low-confidence
    evidence. mirrored stays "unknown" (legacy never declared it)."""
    kind = str(view.get("image_kind", "")).lower()
    note = view.get("facade_axis_note")
    label = view.get("image_label", "")
    if kind != "elevation" and not note:
        return None

    view_facade = None
    for src in (label, note or ""):
        m = _FACADE_IN_TEXT.search(src)
        if m:
            view_facade = m.group(1).capitalize()
            break

    evidence = []
    if view_facade:
        evidence.append(
            OrientationEvidence(
                source="image_name" if _FACADE_IN_TEXT.search(label) else "legacy_note",
                detail=f"facade inferred from {'label' if _FACADE_IN_TEXT.search(label) else 'facade_axis_note'}",
                confidence="medium" if _FACADE_IN_TEXT.search(label) else "low",
            )
        )
    if note:
        evidence.append(
            OrientationEvidence(
                source="legacy_note", detail=note, confidence="low"
            )
        )
        flags.append(
            "facade_axis_note migrated as low-confidence world hint; "
            "world axis/sign must be re-derived by 1_correction"
        )
    return FacadeOrientation(
        view_facade=view_facade,
        local_x_positive="image_left_to_right",
        mirrored="unknown",
        orientation_evidence=evidence,
    )


def _is_legacy(view: dict) -> bool:
    """A view is legacy if it has dimensions but none carry a P1a field, or it
    has a facade_axis_note but no canonical ``facade`` block."""
    if view.get("facade_axis_note") and not view.get("facade"):
        return True
    dims = view.get("dimensions") or []
    if dims and not any(
        ("chain_id" in d or "value_m" in d or "text_verbatim" in d) for d in dims
    ):
        return True
    return False


def _raw_metadata(view: dict, *, legacy_migrated: bool) -> dict:
    return {
        "raw_has_dimensions": "dimensions" in view,
        "raw_has_uncaptured": "uncaptured" in view,
        "legacy_migrated": legacy_migrated,
    }


def attach_raw_metadata(view: ReadingView, metadata: dict) -> ReadingView:
    """Attach non-contract loader metadata for validators/report evidence."""
    object.__setattr__(view, READING_RAW_METADATA_ATTR, dict(metadata))
    return view


def reading_raw_metadata(view: ReadingView) -> dict:
    meta = getattr(view, READING_RAW_METADATA_ATTR, None)
    if isinstance(meta, dict):
        return dict(meta)
    return {
        "raw_has_dimensions": None,
        "raw_has_uncaptured": None,
        "legacy_migrated": bool(getattr(view, "migrated_from_legacy", False)),
    }


def migrate_view(view: dict) -> ReadingView:
    """Migrate a legacy reading-view dict into a canonical ReadingView."""
    flags: list[str] = []
    out = dict(view)
    out["dimensions"] = [
        _migrate_dimension(d).model_dump(by_alias=True)
        for d in (view.get("dimensions") or [])
    ]
    if view.get("dimensions"):
        flags.append("dimensions back-filled value_m/text_verbatim from legacy `text`")
    facade = _migrate_facade(view, flags)
    if facade is not None:
        out["facade"] = facade.model_dump()
    if "uncaptured" not in out:
        out["uncaptured"] = []
    out["migrated_from_legacy"] = True
    out["migration_flags"] = flags
    migrated = ReadingView.model_validate(out)
    return attach_raw_metadata(migrated, _raw_metadata(view, legacy_migrated=True))


def parse_reading_view(data: dict) -> ReadingView:
    """Parse an already-loaded reading-view dict, migrating legacy artifacts
    transparently — the path-independent core of :func:`load_reading_view`, so
    an aggregate payload (e.g. isolation's ``{"views": {stem: {...}}}``) can be
    checked without a per-view file on disk."""
    if _is_legacy(data):
        return migrate_view(data)
    view = ReadingView.model_validate(data)
    return attach_raw_metadata(view, _raw_metadata(data, legacy_migrated=False))


def load_reading_view(path: Path | str) -> ReadingView:
    """Load a reading-view JSON, migrating legacy artifacts transparently."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_reading_view(data)
