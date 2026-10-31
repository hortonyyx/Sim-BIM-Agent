"""Unified MEP IDF-fragment parser (M1, build plan §M1 P2).

The 4_mep stage emits its specs as raw IDF-fragment text (``material_specs``,
``construction_specs``, ``schedule_specs``, ``hvac_specs``, ``people_specs``,
``lights_specs``) — eppy-parseable objects, not natural language. Every MEP check
(reference completeness, schedule completeness, object semantics) needs the SAME
parse of that bundle. This module is that single parse: concatenate the fragments,
parse once with eppy, and expose a typed object index + diagnostics. Checks
consume :class:`IdfFragmentIndex`; **no check writes its own regex** (施工 H3).

A parse failure is itself a fact the caller can act on (4_mep block + resample) —
``parse_mep_fragments`` returns the index with ``parse_error`` set rather than
raising, so the check layer maps it to a CheckStatus.ERROR / fail-closed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from io import StringIO

from eppy.modeleditor import IDF

from src.agent._share import ensure_schema_initialized

# The MepOutput fields whose values are IDF-fragment text, in a stable bundle
# order. zone/surface/fenestration specs come from the geometry serializer and
# are added by the caller when a full-IDF view is needed.
MEP_FRAGMENT_FIELDS = (
    "material_specs",
    "construction_specs",
    "schedule_specs",
    "hvac_specs",
    "people_specs",
    "lights_specs",
)


@dataclass
class IdfObject:
    """One parsed IDF object, normalised for cross-object reference checks."""

    obj_type: str                 # upper-case IDD type, e.g. "CONSTRUCTION"
    name: str                     # field 1 (the Name field), as authored
    fields: list[str]             # all field values as strings (incl. name)
    raw: object = None            # the eppy idfobject (for field-by-name access)


@dataclass
class IdfFragmentIndex:
    """Typed index over the parsed MEP fragment bundle."""

    objects: list[IdfObject] = field(default_factory=list)
    by_type: dict[str, list[IdfObject]] = field(default_factory=lambda: defaultdict(list))
    names_by_type: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    parse_error: str | None = None
    idf: IDF | None = None

    def of_type(self, *types: str) -> list[IdfObject]:
        out: list[IdfObject] = []
        for t in types:
            out.extend(self.by_type.get(t.upper(), []))
        return out

    def has_name(self, *types: str) -> set[str]:
        out: set[str] = set()
        for t in types:
            out |= self.names_by_type.get(t.upper(), set())
        return out

    @property
    def ok(self) -> bool:
        return self.parse_error is None


def _fields_of(obj) -> list[str]:
    """All field values as strings, dropping the leading object-type token."""
    fv = list(obj.fieldvalues)
    return [str(v) for v in fv[1:]] if fv else []


def parse_idf_text(text: str) -> IdfFragmentIndex:
    """Parse an arbitrary IDF-fragment string into an :class:`IdfFragmentIndex`.

    Never raises on malformed input: a parse failure is recorded in
    ``parse_error`` so the check layer can fail-closed (block + resample)."""
    ensure_schema_initialized()
    idx = IdfFragmentIndex()
    try:
        idf = IDF(StringIO(text))
    except Exception as e:  # noqa: BLE001 — parse failure is a reported fact
        idx.parse_error = f"{type(e).__name__}: {e}"
        return idx

    idx.idf = idf
    for key in idf.idfobjects:
        for obj in idf.idfobjects[key]:
            fields = _fields_of(obj)
            name = fields[0] if fields else ""
            io = IdfObject(obj_type=key.upper(), name=name, fields=fields, raw=obj)
            idx.objects.append(io)
            idx.by_type[key.upper()].append(io)
            if name:
                idx.names_by_type[key.upper()].add(name)
    return idx


def bundle_mep_text(mep: dict | object) -> str:
    """Concatenate the MEP fragment fields into one IDF text blob.

    Accepts a dict or a MepOutput-like object (attribute access)."""
    parts: list[str] = []
    for fld in MEP_FRAGMENT_FIELDS:
        val = mep.get(fld) if isinstance(mep, dict) else getattr(mep, fld, None)
        if val:
            parts.append(str(val).strip())
    return "\n\n".join(parts) + "\n"


def parse_mep_fragments(mep: dict | object, *, extra_idf: str = "") -> IdfFragmentIndex:
    """Parse the full MEP bundle (optionally plus geometry IDF text) once.

    ``extra_idf`` lets a caller fold in the geometry specs (zone/surface/
    fenestration) so the geometry→construction reference graph can be checked
    against the same parse."""
    text = bundle_mep_text(mep)
    if extra_idf:
        text = extra_idf.strip() + "\n\n" + text
    return parse_idf_text(text)
