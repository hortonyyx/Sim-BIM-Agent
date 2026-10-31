"""B4 -- opening synthesis: cross-view identity pairing (dispatch 2026-09-03ag).

WHAT THIS MODULE OWNS
---------------------
The three steps of the measured zero-parameter plan (see the cross-view
probe, ``AI_agent/logs/experiments/2026-09-02b_b4_cross_view_identity``),
plus the named premise they stand on:

1. **The anchor (T1).**  The elevation's local ``x = 0`` is the LEFT END of
   that drawing's dimension chain -- not a world origin.  Which WORLD end
   of the along-wall axis that is, is NOT guessed here: it comes from the
   project's one signed sign convention (``facade_convention``: looking at
   the facade from outside, local x running left to right), so
   ``world = along_origin + sign * local_x`` with ``sign`` from
   :func:`facade_convention.resolve_sign`.  On the real four sm25 facades
   that convention resolves exactly as measured (South/East +1,
   North/West -1 -- the observer's-left rule; the probe's own pairing data
   confirms it, see the B4 test file).  ``along_origin`` is the outer-skin
   end of the along-wall axis that the chain starts from.

   The outer-skin span is DERIVED, ⛔ never a constant and ⛔ never a
   single global offset: it is the midline bbox plus the two end walls'
   OWN half thicknesses, EACH END TAKING THE THICKNESS OF THE WALL(S)
   THAT END THE BBOX AT THAT END (dispatch §三: sm25's uniform 240 is a
   reading, not a theorem).  A fixture with four different edge
   thicknesses must and does come out right.

2. **The equality gate (T2, handed over from B3 by dispatch 2026-09-03).**
   ``chain_total_length == outer_skin_span`` is an EQUALITY, ⛔ never a
   threshold: B3 could not close this (it had no typed plan-side span and
   would have needed a nobody-signed tolerance against 0.01-0.5 px ink
   jitter), B4 can, because the plan side is an input here.  Mismatch is
   a loud, named failure -- and it is simultaneously the detector of the
   named premise below, at zero extra cost.

3. **The pairing (T3).**  Both sides are converted to world along-wall
   intervals and paired by **interval equality** -- ⛔ no nearest-distance,
   no order-based pairing, no "within tolerance" matching.  An elevation
   opening whose interval has no plan counterpart is named unmatched;
   it is NEVER guessed onto a neighbour.

THE NAMED PREMISE (T5)
----------------------
``ELEVATION_CHAIN_SPANS_WHOLE_BUILDING`` (defined by B3 in
``evidence_adapters``; imported, ⛔ not re-typed here -- one source): "the
elevation's dimension chain spans the whole building".  A property of how
this family of drawings is drawn, ⛔ not a theorem.  It must hold for the
anchor to mean anything; the equality gate (step 2) is its detector, so a
partial/one-bay elevation fails loudly by name instead of being silently
treated as whole-building.

THE ARITHMETIC DOMAIN (why every comparison here is integer)
------------------------------------------------------------
All comparisons run on the project's DECLARED coordinate grid, 0.1 mm
integer units (``DECLARED_GRID_UNITS_PER_M``; the gt-revision-ledger's
signed "coordinates are 0.1 mm integers" ruling).  Both families of input
already live on that grid -- the plan side natively (as-measured units),
the elevation side because its dimension-chain totals are drawn mm
integers and its interval readings are emitted at 0.1 mm granularity.
:func:`grid_units` enforces that membership EXACTLY (a round-trip float
equality, ⛔ no epsilon): a value off the grid is a loud input fault, so
the equality gate and the pairing are exact integer comparisons with zero
constants anywhere near them.  The elevation's pixel-quantised readings
(mm_per_px ~ 13.6 mm on the real facades) mean real-data intervals are
typically a few grid units off the plan side; those land in ``unmatched``,
which is the honest reading, ⛔ not a defect of the pairing rule.  Making
them match is the reading side's business (chain-tick evidence), never a
tolerance here.

THE DEBT WIRING (T4-b / T4-c; ⛔ structural, never textual)
-----------------------------------------------------------
B3 ships the span gap as an ``EvidenceDebtV1`` whose ``obligation``
field carries the enum value ``"elevation_chain_spans_whole_building"``
(the lower-case snake form of the named premise below; the debt's
``debt_id`` still carries the historical type prefix, but ⛔ that prefix
is NO LONGER the wiring criterion anywhere -- dispatch 2026-09-04e T3).
The cross-review's N-1 finding was that "owned by B4" lived only in the
FREE TEXT ``description`` -- locking a word, not a structure
(``OWNER_TEXT_REMOVED=GREEN``).  This module's
:data:`DEBT_REDEMPTION_REGISTRY` wires debt OBLIGATIONS (the closed
``DebtObligationV1`` domain the producer mints into ``obligation``) to
the gate OBJECT that redeems them; ``description`` is never read and
``debt_id`` is never matched.

⭐ Rework 1 (2026-09-03, cross-review B-1): the registry's gate column is
LOAD-BEARING.  ``synthesize_openings`` does not hard-call the span gate
-- it looks the gate up in the registry BY PREMISE (the product's named
premise is the execution-side key) and calls what it finds, so the
registry is the single source of the wiring: point an obligation at a
wrong existing callable and the call itself fails loudly (import-time
teeth name it ``DEBT_REGISTRY_GATE_SIGNATURE_MISMATCH`` / runtime
``DEBT_GATE_CALL_FAILED``), ⛔ never a silently accepted ornament.

⭐ Dispatch 2026-09-04e T4: an obligation is a PROMISE that a handler
redeems it.  Two layers hold that promise: the import-time teeth refuse
any registry key outside the ``DebtObligationV1`` domain (a key no real
debt can ever carry) and any domain value without a registry row (a
mintable obligation nobody redeems); at runtime
:func:`assert_obligations_backed` and the retirement both refuse, loudly
(``OBLIGATION_UNBACKED``), a debt whose obligation has no row.

⭐ Rework 1 of T4-a (2026-09-04o, cross-review B-1): the DEBT-side
resolution -- one debt's ``obligation`` -> ONE registry row -- is a
single seam, :func:`redemption_row_for_obligation`, and it is exact
single-value BY CONSTRUCTION: a PLAIN-``dict`` carrier (``type() is
dict``, ⛔ not ``isinstance`` -- a mapping subclass's ``__getitem__`` /
``__missing__`` is exactly where an alias or normalisation fallback
hides) and a PLAIN-``str`` exact key (⛔ not a ``str`` subclass with a
loose ``__eq__`` claiming several obligations).  Every entry that
resolves debt wiring routes through that one seam, so a widening of the
resolution -- alias / case-or-space normalisation / a compat table / a
one-to-many resolver -- is refused by the seam's own teeth and by the
resolution-lock battery, ⛔ never silently picked from.  The premise
direction (one premise -> one row) is ⛔ NOT this seam's business: it
keeps its own two teeth (import-time ``DEBT_REGISTRY_PREMISE_AMBIGUOUS``
and runtime ``PREMISE_GATE_AMBIGUOUS``), so no two error codes point at
one thing.

A debt whose obligation this stage redeemed is RETIRED (:T4-c): it
travels into the product's ``retired_debt_ids`` once the equality gate
has actually passed for that product AND the debt's ``affected_refs``
name the ONE source instance the gate ran against (rework 1,
cross-review B-2: South passing retires South's debt, ⛔ never East's or
West's) -- a debt that failed the gate, or one from another facade, is
NOT retired (the obligation stays exactly as open as before).
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Literal, Sequence, get_args

from pydantic import BaseModel, ConfigDict

from src.agent.correction.evidence_adapters import (
    ELEVATION_CHAIN_SPANS_WHOLE_BUILDING,
)
from src.agent.correction.evidence_contract import (
    ArtifactPointerV1,
    DebtObligationV1,
    EvidenceDebtV1,
)
from src.agent.correction.facade_convention import resolve_sign, world_axis
from src.agent.correction.projection_bridge import CutLineV1
from src.agent.reading.vector_contract import (
    CONTRACT_AS_DRAWN_ELEVATION_V0,
    classify_vector_json,
)

#: The project's signed coordinate granularity: 0.1 mm integer units per
#: metre (gt revision ledger, "coordinates are 0.1 mm integers").  This is
#: the ONE unit convention this module's arithmetic runs on; it is a
#: declared GRID, ⛔ not a tolerance.
DECLARED_GRID_UNITS_PER_M = 10_000

#: millimetres per grid unit (10 u = 1 mm).
_GRID_UNITS_PER_MM = 10

SYNTHESIS_SCHEMA_VERSION = "opening_synthesis_v1"

_FAMILIES = ("North", "South", "East", "West")


class OpeningSynthesisError(ValueError):
    """A loud, named rejection with a stable ``code`` (same shape as
    ``EvidenceContractError`` / ``ProjectionBridgeError``).  Everything
    this module rejects is an input-integrity or premise fault: never a
    model's job to fix, never silently absorbed."""

    def __init__(self, code: str, context: dict | None = None):
        self.code = code
        self.context = dict(context or {})
        super().__init__(f"{code}: {self.context}")


# ── the declared-grid arithmetic (exact, ⛔ no epsilon) ─────────────────────── #
def grid_units(value_m: float, *, what: str) -> int:
    """Metres -> 0.1 mm grid units, EXACTLY on the declared grid.

    The membership test is a round-trip float equality: ``u / 10_000 ==
    value_m``.  A value that is a grid point's nearest double round-trips
    bit-identically (measured on every real reading this module consumes:
    wall midlines, half thicknesses, chain totals, elevation interval
    readings); a value BETWEEN grid points does not, and that is exactly
    the fault this raises.  ⛔ No epsilon anywhere: this is the grid the
    project signed, enforced as a premise, not approximated.
    """
    scaled = value_m * DECLARED_GRID_UNITS_PER_M
    unit = round(scaled)
    if unit / DECLARED_GRID_UNITS_PER_M != value_m:
        raise OpeningSynthesisError(
            "VALUE_OFF_DECLARED_GRID",
            {"what": what, "value_m": value_m},
        )
    return unit


def grid_units_from_mm(value_mm: float, *, what: str) -> int:
    """Millimetres -> 0.1 mm grid units, same exact-membership discipline
    as :func:`grid_units` (chain totals are drawn mm integers; a chain
    value off the 0.1 mm grid is a malformed chain, loudly so)."""
    scaled = value_mm * _GRID_UNITS_PER_MM
    unit = round(scaled)
    if unit / _GRID_UNITS_PER_MM != value_mm:
        raise OpeningSynthesisError(
            "VALUE_OFF_DECLARED_GRID",
            {"what": what, "value_mm": value_mm},
        )
    return unit


def _mm_display(unit: int) -> float:
    """Grid units -> mm for error contexts and product readings only;
    ⛔ never an input to a comparison."""
    return unit / _GRID_UNITS_PER_MM


# ── step 2 (T2): the span equality gate, named for the registry ────────────── #
def span_equality_gate(
    *, chain_total_mm: float, skin_lo_u: int, skin_hi_u: int
) -> int:
    """``chain_total == outer_skin_span`` -- an EQUALITY on the declared
    grid, ⛔ not a threshold.

    Returns the span in grid units on success.  On mismatch this raises
    ``ELEVATION_CHAIN_SPAN_MISMATCH`` naming the premise whose failure it
    is (a partial/one-bay elevation is the loud case, T5) and BOTH
    readings -- a mismatch report without both numbers is an invitation
    for someone downstream to "fix" it with a tolerance, which is the one
    thing this gate exists to prevent.
    """
    chain_u = grid_units_from_mm(chain_total_mm, what="chain_total_mm")
    span_u = skin_hi_u - skin_lo_u
    if chain_u != span_u:
        raise OpeningSynthesisError(
            "ELEVATION_CHAIN_SPAN_MISMATCH",
            {
                "premise": ELEVATION_CHAIN_SPANS_WHOLE_BUILDING,
                "chain_total_mm": chain_total_mm,
                "skin_span_mm": _mm_display(span_u),
                "skin_lo_mm": _mm_display(skin_lo_u),
                "skin_hi_mm": _mm_display(skin_hi_u),
                "difference_grid_units": chain_u - span_u,
            },
        )
    return span_u


# ── T4-b: the debt redemption registry (structural, ⛔ never textual) ───────── #
@dataclass(frozen=True)
class DebtRedemption:
    """One wiring row of :data:`DEBT_REDEMPTION_REGISTRY`.

    ⭐ Rework 1 (2026-09-03, cross-review B-1): the row carries the gate
    OBJECT, ⛔ not its name, and ``synthesize_openings`` executes the gate
    THROUGH the registry (looked up by :attr:`premise`) -- the gate column
    is load-bearing, ⛔ never decoration.  ``premise`` is the
    execution-side key: the product's named premise selects the row whose
    gate must run, so the same table serves both the execution (premise ->
    gate) and the retirement (obligation -> gate) and the two can never
    silently disagree.
    """

    premise: str
    gate: Callable[..., int]


#: The closed ``DebtObligationV1`` domain (dispatch 2026-09-04e T3): the
#: registry's keys are EXACTLY this domain -- a debt is wired to this
#: module by its ``obligation`` field, ⛔ never by a ``debt_id`` prefix.
#: The import-time teeth below refuse the two ways the domain and the key
#: set can drift apart.
_OBLIGATION_DOMAIN: frozenset[str] = frozenset(get_args(DebtObligationV1))

#: debt OBLIGATION (the enum value minted into ``obligation`` by the
#: producer; the lower-case snake form of the premise it stands for) -> the
#: wiring row (named premise + gate object) of THIS module that redeems it.
#: ⛔⛔ ``description`` is never consulted and ``debt_id`` is never matched:
#: the cross-review measured that "Owner: B4" living in free text locks a
#: WORD, not a structure (``OWNER_TEXT_REMOVED=GREEN``).
DEBT_REDEMPTION_REGISTRY: dict[str, DebtRedemption] = {
    "elevation_chain_spans_whole_building": DebtRedemption(
        premise=ELEVATION_CHAIN_SPANS_WHOLE_BUILDING,
        gate=span_equality_gate,
    ),
}

#: The keyword form :func:`synthesize_openings` calls every registry gate
#: with -- the one call shape the import-time signature teeth bind-check
#: against.  A gate that cannot take exactly this call is wrong wiring,
#: loudly, at import (and again, transposed, at the real call site).
_GATE_CALL_KEYWORDS: dict[str, object] = {
    "chain_total_mm": 0.0,
    "skin_lo_u": 0,
    "skin_hi_u": 0,
}


def _assert_registry_well_formed() -> None:
    """Import-time teeth for the registry itself (rework 1, cross-review
    B-1 -- the old check only asked that the value NAME some callable,
    so pointing the prefix at ``grid_units`` passed while the gate column
    carried no weight):

    * every value is a :class:`DebtRedemption` row whose gate is a
      callable;
    * the gate is a NAMED function of THIS module (``globals()`` resolves
      its own ``__name__`` back to the same object -- a lambda, a builtin
      or somebody else's import is refused, ⛔ not just "any callable");
    * the gate accepts THE call shape the execution side makes -- a
      signature bind against :data:`_GATE_CALL_KEYWORDS`, so a wrong
      existing callable like ``grid_units`` is loud HERE, at import,
      before any debt can be minted against it;
    * one premise per row and one row per premise (the premise is the
      execution-side lookup key; two rows for one premise would be
      ambiguous wiring);
    * no key is a proper prefix of another key (two obligations where one
      is a prefix of the other is ambiguous wiring -- exactly the
      ``case``-path-through flavour of smuggling this project keeps
      finding);
    * (dispatch 2026-09-04e T4) every key IS a ``DebtObligationV1`` value
      -- a key outside the domain can never be carried by any real debt,
      so it is dead wiring, loud at import;
    * (dispatch 2026-09-04e T4) every ``DebtObligationV1`` value HAS a
      row -- a mintable obligation with no handler is an unwritten
      promise, loud at import (this is the structural form of "⛔ no
      slots for values nobody redeems");
    * (rework 1 of T4-a, 2026-09-04o) the carrier IS a plain ``dict``
      (``type() is dict``) -- a mapping subclass is where an alias /
      normalisation fallback ``__getitem__`` / ``__missing__`` hides, and
      the debt-side resolution below is exact single-value ONLY on a
      plain dict;
    * (rework 1 of T4-a, 2026-09-04o) every key IS a plain ``str``
      (``type(key) is str``) -- a ``str`` subclass with a loose
      ``__eq__`` is one row claiming SEVERAL obligations, i.e. alias
      wiring wearing the exact-key costume, loud at import.
    """
    if type(DEBT_REDEMPTION_REGISTRY) is not dict:
        raise OpeningSynthesisError(
            "DEBT_REGISTRY_CARRIER_NOT_PLAIN_DICT",
            {
                "carrier": type(DEBT_REDEMPTION_REGISTRY).__name__,
                "because": (
                    "the debt-side resolution is exact single-value on a "
                    "plain dict; a mapping subclass is where an alias / "
                    "normalisation fallback hides"
                ),
            },
        )
    seen: dict[str, str] = {}
    premises: dict[str, str] = {}
    for key, row in DEBT_REDEMPTION_REGISTRY.items():
        if type(key) is not str:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_KEY_NOT_PLAIN_STR",
                {
                    "key": repr(key),
                    "key_type": type(key).__name__,
                    "because": (
                        "a str subclass with a loose __eq__ is one row "
                        "claiming several obligations -- alias wiring, "
                        "⛔ not an exact key"
                    ),
                },
            )
        if not isinstance(row, DebtRedemption):
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_ROW_MALFORMED",
                {"key": key, "got": type(row).__name__},
            )
        gate = row.gate
        gate_name = getattr(gate, "__name__", None)
        if not callable(gate):
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_HANDLER_MISSING",
                {"key": key, "handler": gate_name},
            )
        if (
            not isinstance(gate_name, str)
            or globals().get(gate_name) is not gate
        ):
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_GATE_NOT_MODULE_FUNCTION",
                {
                    "key": key,
                    "gate": gate_name,
                    "reason": (
                        "the gate must be a named function of THIS "
                        "module, ⛔ not a lambda/builtin/foreign callable"
                    ),
                },
            )
        try:
            inspect.signature(gate).bind(**_GATE_CALL_KEYWORDS)
        except TypeError as exc:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_GATE_SIGNATURE_MISMATCH",
                {
                    "key": key,
                    "gate": gate_name,
                    "call_keywords": sorted(_GATE_CALL_KEYWORDS),
                    "because": str(exc),
                },
            ) from exc
        if not isinstance(row.premise, str) or not row.premise:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_PREMISE_MISSING", {"key": key}
            )
        if row.premise in premises:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_PREMISE_AMBIGUOUS",
                {
                    "premise": row.premise,
                    "key_a": premises[row.premise],
                    "key_b": key,
                },
            )
        premises[row.premise] = key
        for other in seen:
            if other.startswith(key) or key.startswith(other):
                raise OpeningSynthesisError(
                    "DEBT_REGISTRY_PREFIX_AMBIGUOUS",
                    {"key_a": other, "key_b": key},
                )
        seen[key] = gate_name
    # -- (dispatch 2026-09-04e T4) domain <-> key-set coverage, both ways --
    for key in sorted(DEBT_REDEMPTION_REGISTRY):
        if key not in _OBLIGATION_DOMAIN:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_KEY_NOT_OBLIGATION",
                {
                    "key": key,
                    "obligation_domain": sorted(_OBLIGATION_DOMAIN),
                },
            )
    for obligation in sorted(_OBLIGATION_DOMAIN):
        if obligation not in DEBT_REDEMPTION_REGISTRY:
            raise OpeningSynthesisError(
                "DEBT_REGISTRY_OBLIGATION_UNCOVERED",
                {
                    "obligation": obligation,
                    "registry_keys": sorted(DEBT_REDEMPTION_REGISTRY),
                },
            )


_assert_registry_well_formed()


def redemption_row_for_premise(premise: str) -> tuple[str, DebtRedemption]:
    """The registry row whose gate detects THIS premise -- the single
    source of the wiring between a named premise and its gate.

    Zero rows means the premise is UNWIRED (the gate this product's
    ``premise`` field promises does not exist -- loud, ⛔ never a silent
    skip); more than one row is ambiguous wiring and is equally loud.
    """
    rows = [
        (key, row)
        for key, row in DEBT_REDEMPTION_REGISTRY.items()
        if row.premise == premise
    ]
    if not rows:
        raise OpeningSynthesisError(
            "PREMISE_GATE_UNWIRED",
            {
                "premise": premise,
                "known_premises": sorted(
                    {r.premise for r in DEBT_REDEMPTION_REGISTRY.values()}
                ),
            },
        )
    if len(rows) > 1:
        raise OpeningSynthesisError(
            "PREMISE_GATE_AMBIGUOUS",
            {"premise": premise, "keys": sorted(k for k, _ in rows)},
        )
    return rows[0]


def redemption_row_for_obligation(obligation: str) -> tuple[str, DebtRedemption]:
    """⭐ THE single DEBT-side resolution point (rework 1 of T4-a,
    2026-09-04o, cross-review B-1): one debt's ``obligation`` -> exactly
    ONE registry row, by EXACT single-value lookup.  This is the one
    place debt wiring resolves, and the one place a WIDENING of that
    resolution -- an alias, a case/space normalisation, a compat table,
    a one-to-many resolver -- is refused:

    * the carrier is a PLAIN ``dict`` (``type() is dict``, ⛔ not
      ``isinstance`` -- a mapping subclass's ``__getitem__`` /
      ``__missing__`` is exactly where an alias fallback hides) --
      otherwise loud ``DEBT_REGISTRY_CARRIER_NOT_PLAIN_DICT``;
    * the obligation IS a stored key, EXACTLY -- a value no row is
      stored under is an unwritten promise (``OBLIGATION_UNBACKED``),
      ⛔ never normalised or aliased into a hit;
    * the rows CLAIMING this obligation, counted the way any matching
      rule would count them (``key == obligation``), are exactly ONE --
      the plain-``str`` key equal to it.  A ``str``-subclass key with a
      loose ``__eq__`` (one row claimable by several obligation strings)
      or two keys claiming one obligation is ambiguous DEBT-side wiring
      -- loud ``DEBT_TYPE_AMBIGUOUS``, the debt direction this code
      name has always belonged to (the debt_id-prefix world held the
      same tooth: one debt must never match two rows).  The premise
      direction is :func:`redemption_row_for_premise`'s, ⛔ not this
      code's.

    Locked by ``tests/test_t4a_rework1_resolution_lock.py``: a battery
    of near-miss obligations (case / spacing / prefix / suffix /
    separator variants of every live key) must ALL be refused here and
    on both callers, and each of the four canonical widenings, installed
    in-process, turns that battery red.
    """
    if type(DEBT_REDEMPTION_REGISTRY) is not dict:
        raise OpeningSynthesisError(
            "DEBT_REGISTRY_CARRIER_NOT_PLAIN_DICT",
            {
                "carrier": type(DEBT_REDEMPTION_REGISTRY).__name__,
                "because": (
                    "the debt-side resolution is exact single-value on a "
                    "plain dict; a mapping subclass is where an alias / "
                    "normalisation fallback hides"
                ),
            },
        )
    if obligation not in DEBT_REDEMPTION_REGISTRY:
        raise OpeningSynthesisError(
            "OBLIGATION_UNBACKED",
            {
                "obligation": obligation,
                "registry_keys": sorted(DEBT_REDEMPTION_REGISTRY),
            },
        )
    claimants = [key for key in DEBT_REDEMPTION_REGISTRY if key == obligation]
    if len(claimants) != 1 or type(claimants[0]) is not str:
        raise OpeningSynthesisError(
            "DEBT_TYPE_AMBIGUOUS",
            {
                "obligation": obligation,
                "claimant_keys": [
                    f"{key!r}<{type(key).__name__}>"
                    for key in DEBT_REDEMPTION_REGISTRY
                    if key == obligation
                ],
                "because": (
                    "exactly ONE row may claim an obligation -- the "
                    "plain-str key equal to it; a loose-equality key "
                    "claiming several obligations is alias wiring"
                ),
            },
        )
    return obligation, DEBT_REDEMPTION_REGISTRY[obligation]


# ── T4-c: the per-run binding (rework 1, cross-review B-2) ──────────────────── #
@dataclass(frozen=True)
class ElevationSourceIdentity:
    """WHO one gate run was actually checked against: the caller-declared
    identity of the ONE frozen elevation source behind ``elevation_doc``.

    Three fields, exactly the identity vocabulary of an
    ``ArtifactPointerV1`` / ``SourceArtifactV1`` (input slot + contract +
    frozen-byte hash); ``json_pointer`` is deliberately absent -- it names
    a place INSIDE a source, and this is the source itself.

    ⚠️ The sha is CALLER-DECLARED: a parsed ``dict`` carries no bytes to
    re-hash, so this type is a trust boundary the caller signs, ⛔ not a
    fact this module re-derives.  What this module does enforce, on it,
    is the retirement binding below.
    """

    input_id: str
    source_contract_id: str
    source_output_sha256: str

    def binds(self, ref: ArtifactPointerV1) -> bool:
        """Does this ref point INTO the source instance this identity
        names?  (Any json pointer inside it -- B3's span debt points at
        ``/calibration``, exactly the node the gate reads.)"""
        return (
            ref.input_id == self.input_id
            and ref.source_contract_id == self.source_contract_id
            and ref.source_output_sha256 == self.source_output_sha256
        )


@dataclass(frozen=True)
class ExecutedRedemption:
    """What ONE ``synthesize_openings`` run actually discharged.

    All three fields are binding for a retirement (rework 1):

    * ``obligation`` / ``row`` -- the registry row whose gate RAN AND
      RETURNED in that run (object identity with the registry's row, so
      the retirement cites the gate that actually carried the check);
      ``obligation`` names the registry KEY this run executed -- the same
      field a debt is wired by since dispatch 2026-09-04e T3 (⛔ the
      ``debt_id`` prefix is no longer the wiring criterion);
    * ``source`` -- the ONE source instance that gate ran against.  A run
      without a declared source (``None``) can retire nothing: no
      binding, no retirement -- the obligation stays open, ⛔ never a
      coincidence deletion of another facade's real debt.
    """

    obligation: str
    row: DebtRedemption
    source: ElevationSourceIdentity | None


def _resolve_backed_obligation(obligation: str) -> tuple[str, DebtRedemption]:
    """The debt-side EXIT postcondition (rework 2 of T4-a, cross-review
    2026-09-04v B-1): the set of obligations this module will back and
    retire is EXACTLY the set of stored registry keys -- ⛔ never a
    superset that an alias / case-or-space normalisation / compat table /
    one-to-many resolver widened the (swappable) seam to accept.

    :func:`redemption_row_for_obligation` is the ONE place the resolution
    may be extended on its INSIDE.  This postcondition is the one the
    CALLERS trust, and it re-derives membership DIRECTLY from the
    immutable plain-``dict`` registry -- ⛔ NOT from the seam's return
    value.  So a widened seam that returns a canonical ``(key, row)`` for
    an input the registry never stored is refused HERE, whatever the seam
    returned.  This is the check the cross-review found missing: no lock
    quantified "the set of inputs that resolve successfully == the live
    key set", and the binding trusted the resolver's canonical key instead
    of re-checking the ORIGINAL ``obligation`` value (a compat map from a
    lexically-dissimilar string to the live key slipped through with all 28
    near-miss locks green).

    Two teeth, both on the ORIGINAL value, both BEFORE the seam is
    consulted for membership:

    * ``type(obligation) is str`` -- ⛔ not a ``str`` subclass whose loose
      ``__eq__``/``__hash__`` a ``dict`` membership test resolves to a
      live key by REFLECTED equality (cross-review B-2 non-blocking #2,
      now fixed at the exit): a smuggling subclass is refused
      (``OBLIGATION_TYPE_NOT_PLAIN_STR``);
    * ``obligation in DEBT_REDEMPTION_REGISTRY`` -- an EXACT plain-dict
      membership of the original value, ⛔ never the seam's normalised /
      aliased hit (``OBLIGATION_UNBACKED``).

    The carrier tooth (``type() is dict``) still guards that membership
    test's own exactness.  The seam is then still exercised, so its
    claimant tooth (``DEBT_TYPE_AMBIGUOUS``) and the row it returns stay
    part of the contract -- but the SUCCESS/FAILURE decision no longer
    depends on what the seam returns.
    """
    if type(DEBT_REDEMPTION_REGISTRY) is not dict:
        raise OpeningSynthesisError(
            "DEBT_REGISTRY_CARRIER_NOT_PLAIN_DICT",
            {
                "carrier": type(DEBT_REDEMPTION_REGISTRY).__name__,
                "because": (
                    "the debt-side membership is exact single-value on a "
                    "plain dict; a mapping subclass is where an alias / "
                    "normalisation fallback hides"
                ),
            },
        )
    if type(obligation) is not str:
        raise OpeningSynthesisError(
            "OBLIGATION_TYPE_NOT_PLAIN_STR",
            {
                "obligation": repr(obligation),
                "obligation_type": type(obligation).__name__,
                "because": (
                    "a str subclass with a loose __eq__/__hash__ resolves "
                    "to a live key by reflected equality; the backed set is "
                    "plain-str exact keys only"
                ),
            },
        )
    if obligation not in DEBT_REDEMPTION_REGISTRY:
        raise OpeningSynthesisError(
            "OBLIGATION_UNBACKED",
            {
                "obligation": obligation,
                "registry_keys": sorted(DEBT_REDEMPTION_REGISTRY),
            },
        )
    return redemption_row_for_obligation(obligation)


def assert_obligations_backed(debts: Sequence[EvidenceDebtV1]) -> None:
    """(dispatch 2026-09-04e T4) A debt that carries an ``obligation`` is
    a PROMISE that this module's registry redeems it.

    ``obligation is None`` = no downstream obligation (the honest shape
    of every non-span debt today) and passes untouched.  A non-``None``
    obligation with NO registry row is an unwritten promise -- loud
    (``OBLIGATION_UNBACKED``), ⛔ never a silent skip that strands the
    debt forever while its bundle still records it as owed.

    (rework 2 of T4-a, cross-review B-1) Every non-``None`` obligation
    goes through :func:`_resolve_backed_obligation` -- THE EXIT
    POSTCONDITION -- which re-checks the ORIGINAL value against the plain
    dict directly, so a widened seam cannot make a non-key obligation pass
    here by returning a canonical row for it.
    """
    for debt in debts:
        if debt.obligation is None:
            continue
        _resolve_backed_obligation(debt.obligation)


def redeemable_debt_ids(
    debts: Sequence[EvidenceDebtV1],
    *,
    executed: ExecutedRedemption,
) -> tuple[str, ...]:
    """Which of these debts THIS run actually discharged.

    Three bindings, all required (rework 1; cross-review B-1 + B-2 -- the
    old prefix-only view let a South gate run retire REAL East/West
    debts):

    1. **obligation** (dispatch 2026-09-04e T3: ⛔ the ``debt_id`` prefix
       is never matched) -- the debt's ``obligation`` is backed through
       THE EXIT POSTCONDITION (:func:`_resolve_backed_obligation`, rework
       2 of T4-a): ``None`` = no downstream obligation, not this stage's,
       the caller keeps it; the ORIGINAL value must itself be an EXACT
       stored key of the plain-dict registry (⛔ never the seam's
       normalised / aliased hit), else loud (``OBLIGATION_UNBACKED``); a
       ``str``-subclass smuggling value is loud
       (``OBLIGATION_TYPE_NOT_PLAIN_STR``); the seam's own claimant tooth
       still fires (``DEBT_TYPE_AMBIGUOUS``, ⛔ the premise direction is
       ``redemption_row_for_premise``'s two teeth, not this code's: two
       error codes must not point at one thing);
    2. **execution** -- the seam-resolved row IS the row whose gate ran
       and returned in ``executed`` (object identity, ⛔ never a name
       match);
    3. **source** -- the debt's ``affected_refs`` name the ONE source
       instance that run checked (``executed.source``); a debt from
       another facade, or one whose ``affected_refs`` name nothing, is
       kept exactly as open as before.

    ⛔ The free-text ``description`` is never read; ⛔ ``debt_id`` is
    never matched.
    """
    assert_obligations_backed(debts)
    redeemed: list[str] = []
    for debt in debts:
        if debt.obligation is None:
            continue
        key, row = _resolve_backed_obligation(debt.obligation)
        if key != executed.obligation or row is not executed.row:
            # a redemption this run never executed: not ours to retire
            continue
        if executed.source is None or not any(
            executed.source.binds(ref) for ref in debt.affected_refs
        ):
            # B-2: no binding to the source instance THIS gate run
            # checked -- a foreign facade's REAL debt, or a debt that
            # names no source.  Kept open, ⛔ never retired by coincidence.
            continue
        redeemed.append(debt.debt_id)
    return tuple(sorted(redeemed))


# ── the product ────────────────────────────────────────────────────────────── #
class OpeningPairingV1(BaseModel):
    """One cross-view identity: a plan opening and an elevation opening
    whose WORLD along-wall intervals are EQUAL on the declared grid.  The
    z travels from the elevation side -- that is the whole point of the
    synthesis (the plan side has no z)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    plan_opening_id: str
    elevation_opening_id: str
    span_lo_u: int
    span_hi_u: int
    z_low_u: int
    z_high_u: int


class OpeningSynthesisV1(BaseModel):
    """The B4 product.  Everything geometric is in 0.1 mm grid units --
    the module never emits a bare float metre that a consumer would have
    to compare with a tolerance.  ``unmatched_*`` name every opening the
    equality rule did NOT pair: an honest refusal list, ⛔ not an error
    and ⛔ never silently dropped."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["opening_synthesis_v1"]
    facade_family: str
    world_axis: Literal["x", "y"]
    sign: Literal[-1, 1]
    #: the anchor (T1): the world grid coordinate the elevation chain's
    #: ``x = 0`` maps to -- the outer-skin end the chain starts from.
    along_origin_u: int
    chain_total_u: int
    skin_lo_u: int
    skin_hi_u: int
    premise: str
    pairings: tuple[OpeningPairingV1, ...] = ()
    unmatched_plan_opening_ids: tuple[str, ...] = ()
    unmatched_elevation_opening_ids: tuple[str, ...] = ()
    #: openings that share one world along-wall interval with at least one
    #: OTHER opening (either side): interval equality cannot decide 1:1
    #: identity there (the real shape: stacked-storey windows directly
    #: above each other read as identical intervals), so EVERY member of
    #: such a group is refused, never guessed.  Storey identity is B2's
    #: dimension; this field is the honest "not decidable here" ledger.
    same_interval_groups: tuple[tuple[str, ...], ...] = ()
    #: T4-c: debts whose obligation THIS product actually discharged (the
    #: equality gate passed for this facade AND the debt's ``affected_refs``
    #: name this run's ``elevation_source`` instance -- rework 1, cross-
    #: review B-2: "per product, per facade" is a BINDING here, ⛔ not a
    #: comment).  ⛔ Not a claim to have closed the debt type in general.
    retired_debt_ids: tuple[str, ...] = ()


# ── input views ─────────────────────────────────────────────────────────────── #
@dataclass(frozen=True)
class _SkinEnvelope:
    """The along-wall outer-skin span, per-end sourced (T1's arithmetic)."""

    lo_u: int
    hi_u: int
    #: each end's OWN half thickness, read from the walls that end the
    #: bbox AT THAT END (audit: which walls, which thickness -- so a
    #: reader can see the span was never one global offset).
    lo_end_half_u: int
    hi_end_half_u: int
    lo_end_wall_ids: tuple[str, ...]
    hi_end_wall_ids: tuple[str, ...]


def _skin_envelope(
    walls: Sequence[CutLineV1], *, across_axis: str, family: str
) -> _SkinEnvelope:
    """Midline bbox + each end's own ``t/2`` (T1), per-end sourced.

    ⚠️ AXIS VOCABULARY (the transposition trap this file's neighbours have
    already bitten on -- see ``projection_bridge._run_axis``): the along
    axis of a facade is ``world_axis(family)``; the walls that DECIDE a
    span along that axis are the ones whose MIDLINE is constant there,
    i.e. the walls RUNNING ON THE OTHER AXIS (``CutLineV1.axis`` names the
    run axis).  East/West span y ⇒ decided by x-running walls' ``pos_m``;
    North/South span x ⇒ decided by y-running walls' ``pos_m``.

    Per end: the walls whose midline ENDS the bbox there must agree on
    ONE half thickness (a disagreement is a contradiction in the data,
    loud), and the skin the end wall computes must be the skin of the
    WHOLE wall set on that side -- otherwise "outer skin = midline bbox +
    each end's own t/2" is false for this input (some other wall already
    pokes past the end wall), and that too is loud, ⛔ never silently
    absorbed into a wider envelope.
    """
    deciders = [w for w in walls if w.axis != across_axis]
    if not deciders:
        raise OpeningSynthesisError(
            "NO_WALLS_ALONG_FACADE_AXIS",
            {"family": family, "across_axis": across_axis},
        )
    pos_u = {
        w.origin_id: grid_units(w.pos_m, what=f"wall {w.origin_id} pos_m")
        for w in deciders
    }
    half_u = {
        w.origin_id: grid_units(
            w.half_thickness_m, what=f"wall {w.origin_id} half_thickness_m"
        )
        for w in deciders
    }
    mid_lo = min(pos_u.values())
    mid_hi = max(pos_u.values())

    def _end(end: Literal["lo", "hi"]) -> tuple[int, tuple[str, ...]]:
        edge = mid_lo if end == "lo" else mid_hi
        ids = tuple(
            sorted(w.origin_id for w in deciders if pos_u[w.origin_id] == edge)
        )
        halves = {half_u[i] for i in ids}
        if len(halves) != 1:
            raise OpeningSynthesisError(
                "SKIN_END_WALL_THICKNESS_AMBIGUOUS",
                {
                    "family": family,
                    "end": end,
                    "wall_ids": ids,
                    "half_thicknesses_u": sorted(halves),
                },
            )
        return halves.pop(), ids

    lo_half, lo_ids = _end("lo")
    hi_half, hi_ids = _end("hi")
    skin_lo = mid_lo - lo_half
    skin_hi = mid_hi + hi_half

    envelope_lo = min(pos_u[i] - half_u[i] for i in pos_u)
    envelope_hi = max(pos_u[i] + half_u[i] for i in pos_u)
    if envelope_lo != skin_lo or envelope_hi != skin_hi:
        raise OpeningSynthesisError(
            "SKIN_NOT_FROM_END_WALLS",
            {
                "family": family,
                "end_wall_skin": [_mm_display(skin_lo), _mm_display(skin_hi)],
                "wall_set_envelope": [
                    _mm_display(envelope_lo),
                    _mm_display(envelope_hi),
                ],
                "note": (
                    "some wall pokes past the midline-bbox end wall: the "
                    "premise 'outer skin = midline bbox + each end's own "
                    "t/2' is false for this input"
                ),
            },
        )
    return _SkinEnvelope(
        lo_u=skin_lo,
        hi_u=skin_hi,
        lo_end_half_u=lo_half,
        hi_end_half_u=hi_half,
        lo_end_wall_ids=lo_ids,
        hi_end_wall_ids=hi_ids,
    )


def _require_elevation_contract(doc: dict) -> None:
    """The elevation input must BE an ``as_drawn_elevation_v0`` product --
    classified by its bytes (the registered detector), ⛔ never by file
    name or by a field's say-so."""
    decision = classify_vector_json(doc)
    if decision.contract_id != CONTRACT_AS_DRAWN_ELEVATION_V0:
        raise OpeningSynthesisError(
            "ELEVATION_CONTRACT_MISMATCH",
            {
                "detected": decision.contract_id,
                "required": CONTRACT_AS_DRAWN_ELEVATION_V0,
                "reason": decision.reason,
            },
        )


def _elevation_chain_total_mm(doc: dict, family: str) -> float:
    chain = (doc.get("calibration") or {}).get("x")
    if not isinstance(chain, dict) or not isinstance(
        chain.get("cum_mm"), list
    ) or not chain["cum_mm"]:
        raise OpeningSynthesisError(
            "ELEVATION_CHAIN_MISSING",
            {"family": family},
        )
    total = chain["cum_mm"][-1]
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        raise OpeningSynthesisError(
            "ELEVATION_CHAIN_TOTAL_NOT_NUMERIC",
            {"family": family, "cum_mm_last": total},
        )
    return total


def _elevation_openings(doc: dict) -> tuple[tuple[str, float, float, float, float], ...]:
    """(id, x_lo_m, x_hi_m, z_lo_m, z_hi_m) for every opening, validated
    loudly -- the pairing never touches an unvalidated node."""
    nodes = doc.get("openings")
    if not isinstance(nodes, list):
        raise OpeningSynthesisError(
            "ELEVATION_OPENINGS_MISSING", {"got": type(nodes).__name__}
        )
    out: list[tuple[str, float, float, float, float]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise OpeningSynthesisError(
                "ELEVATION_OPENING_NODE_MALFORMED", {"index": index}
            )
        oid = node.get("id")
        if not isinstance(oid, str) or not oid:
            raise OpeningSynthesisError(
                "ELEVATION_OPENING_WITHOUT_ID", {"index": index}
            )
        parsed = []
        for field in ("x_range_m", "z_range_m"):
            span = node.get(field)
            if (
                not isinstance(span, list)
                or len(span) != 2
                or any(
                    isinstance(v, bool) or not isinstance(v, (int, float))
                    for v in span
                )
                or not float(span[0]) < float(span[1])
            ):
                raise OpeningSynthesisError(
                    "ELEVATION_OPENING_RANGE_MALFORMED",
                    {"opening_id": oid, "field": field, "value": span},
                )
            parsed.extend(float(v) for v in span)
        out.append((oid, parsed[0], parsed[1], parsed[2], parsed[3]))
    return tuple(out)


def _require_lines(
    lines: Sequence[CutLineV1], *, kind: Literal["wall", "opening"], what: str
) -> None:
    for line in lines:
        if line.kind != kind:
            raise OpeningSynthesisError(
                "CUT_LINE_KIND_MISMATCH",
                {"what": what, "origin_id": line.origin_id,
                 "expected_kind": kind, "got_kind": line.kind},
            )


# ── the synthesis itself ────────────────────────────────────────────────────── #
def _synthesize_checked_document(
    *,
    elevation_doc: dict,
    walls: Sequence[CutLineV1],
    plan_openings: Sequence[CutLineV1],
    mirrored: bool,
    local_x_positive: str,
    evidence_debts: Sequence[EvidenceDebtV1] = (),
    elevation_source: ElevationSourceIdentity | None = None,
) -> OpeningSynthesisV1:
    """Steps 1-3 + the premise gate + the debt retirement, one facade.

    ``walls`` / ``plan_openings`` are the plan side's wall and opening cut
    lines (the projection bridge's representation -- either loader
    produces them; here they are pure data).  The CALLER decides which
    openings are candidates for THIS facade (the visibility question --
    which wall bands this facade sees across a stepped volume -- is
    ``facade_visibility``'s business, ⛔ not smuggled in here as a
    bbox-extreme shortcut).

    ``mirrored`` / ``local_x_positive`` are the caller's DECLARED drawing
    orientation, resolved fail-closed through the signed convention
    (``facade_convention``); this function ⛔ never guesses a direction,
    and an unresolved mirror flag is a loud input fault upstream.

    ⭐ ``elevation_source`` (rework 1, cross-review B-2) is the
    caller-declared identity of the ONE frozen source behind
    ``elevation_doc``; a debt is retired only if its ``affected_refs``
    name exactly that instance -- so a South run retires South's debt and
    ⛔ never East's or West's, even though all four facades mint the SAME
    obligation.  A run that declares no source retires nothing (the
    honest conservative read: no binding, no retirement).
    """
    _require_elevation_contract(elevation_doc)
    _require_lines(walls, kind="wall", what="walls")
    _require_lines(plan_openings, kind="opening", what="plan_openings")
    # (dispatch 2026-09-04e T4) fail-fast: a debt carrying an obligation
    # this registry cannot redeem is an unwritten promise -- loud BEFORE
    # any geometry runs, so the refusal cannot be mistaken for a pairing
    # failure (``redeemable_debt_ids`` enforces the same tooth again).
    assert_obligations_backed(evidence_debts)
    if elevation_source is not None and (
        elevation_source.source_contract_id != CONTRACT_AS_DRAWN_ELEVATION_V0
    ):
        # the declared identity and the checked document must at least
        # agree on WHAT KIND of source this is
        raise OpeningSynthesisError(
            "ELEVATION_SOURCE_CONTRACT_MISMATCH",
            {
                "input_id": elevation_source.input_id,
                "declared_contract": elevation_source.source_contract_id,
                "required": CONTRACT_AS_DRAWN_ELEVATION_V0,
            },
        )

    family = elevation_doc.get("facade_label")
    if family not in _FAMILIES:
        raise OpeningSynthesisError(
            "FACADE_FAMILY_UNKNOWN", {"facade_label": family}
        )
    axis = world_axis(family)
    sign = resolve_sign(
        family, mirrored=mirrored, local_x_positive=local_x_positive
    )

    # -- step 1 (T1): the anchor ------------------------------------------------
    skin = _skin_envelope(walls, across_axis=axis, family=family)
    # the chain's x=0 is the outer-skin end the SIGN points away from:
    # sign +1 => x=0 is the lo end, sign -1 => the hi end (the signed
    # convention's observer-left rule; measured on all four real facades).
    along_origin_u = skin.lo_u if sign > 0 else skin.hi_u

    # -- step 2 (T2 == T5's detector): the equality gate, THROUGH THE REGISTRY --
    chain_total_mm = _elevation_chain_total_mm(elevation_doc, family)
    # ⭐ rework 1 (cross-review B-1): the gate is NOT hard-called here.  It
    # is looked up in DEBT_REDEMPTION_REGISTRY by the product's named
    # premise and CALLED THROUGH the registry -- the registry's gate column
    # is the single source of this wiring, so it carries real weight: point
    # the premise's row at a wrong existing callable and THIS call fails
    # loudly, ⛔ never a silently accepted ornament that still retires
    # debts.  Delete the row and the premise is unwired, equally loud.
    span_key, span_row = redemption_row_for_premise(
        ELEVATION_CHAIN_SPANS_WHOLE_BUILDING
    )
    try:
        span_u = span_row.gate(
            chain_total_mm=chain_total_mm,
            skin_lo_u=skin.lo_u,
            skin_hi_u=skin.hi_u,
        )
    except OpeningSynthesisError:
        # the gate's own loud, named rejection (e.g. ELEVATION_CHAIN_SPAN_
        # MISMATCH) -- propagated untouched
        raise
    except TypeError as exc:
        # a registry gate that cannot take THE call shape the execution
        # side makes: wrong wiring made loud at the real call site (the
        # import-time teeth already bind-check this; a runtime registry
        # mutation lands here)
        raise OpeningSynthesisError(
            "DEBT_GATE_CALL_FAILED",
            {
                "obligation": span_key,
                "gate": getattr(span_row.gate, "__name__", repr(span_row.gate)),
                "call_keywords": sorted(_GATE_CALL_KEYWORDS),
                "because": str(exc),
            },
        ) from exc
    chain_total_u = span_u

    # -- step 3 (T3): pair by interval EQUALITY on the declared grid ------------
    # Both sides are bucketed by world interval FIRST, then reconciled:
    # an interval with exactly one opening on EACH side is one pairing;
    # anything else (one side empty = no counterpart, either side with
    # more than one = identity not decidable by this criterion) is
    # refused by name.  ⛔ No ordering, no proximity, no tie-breaking.
    plan_by_interval: dict[tuple[int, int], list[str]] = {}
    plan_ids_seen: set[str] = set()
    for opening in plan_openings:
        if opening.origin_id in plan_ids_seen:
            # uniqueness over the WHOLE input, ⛔ not per interval bucket:
            # the same id at two intervals is the same corruption
            raise OpeningSynthesisError(
                "PLAN_OPENING_ID_DUPLICATE",
                {"opening_id": opening.origin_id},
            )
        plan_ids_seen.add(opening.origin_id)
        lo_u = grid_units(
            opening.along_lo_m, what=f"plan opening {opening.origin_id} lo"
        )
        hi_u = grid_units(
            opening.along_hi_m, what=f"plan opening {opening.origin_id} hi"
        )
        if not lo_u < hi_u:
            raise OpeningSynthesisError(
                "PLAN_OPENING_SPAN_DEGENERATE",
                {"opening_id": opening.origin_id, "span_u": [lo_u, hi_u]},
            )
        plan_by_interval.setdefault((lo_u, hi_u), []).append(
            opening.origin_id
        )

    elevation_by_interval: dict[
        tuple[int, int], list[tuple[str, int, int]]
    ] = {}
    elevation_ids_seen: set[str] = set()
    for oid, x_lo, x_hi, z_lo, z_hi in _elevation_openings(elevation_doc):
        if oid in elevation_ids_seen:
            # whole-input uniqueness, same discipline as the plan side
            raise OpeningSynthesisError(
                "ELEVATION_OPENING_ID_DUPLICATE", {"opening_id": oid}
            )
        elevation_ids_seen.add(oid)
        lo_u = grid_units(x_lo, what=f"elevation opening {oid} x_lo")
        hi_u = grid_units(x_hi, what=f"elevation opening {oid} x_hi")
        # world = along_origin + sign * local  (facade_convention §6.3's one
        # formula, evaluated in the integer grid domain so no float ever
        # touches a comparison)
        world_lo = along_origin_u + sign * lo_u
        world_hi = along_origin_u + sign * hi_u
        interval = (min(world_lo, world_hi), max(world_lo, world_hi))
        elevation_by_interval.setdefault(interval, []).append(
            (
                oid,
                grid_units(z_lo, what=f"elevation opening {oid} z_lo"),
                grid_units(z_hi, what=f"elevation opening {oid} z_hi"),
            )
        )

    pairings: list[OpeningPairingV1] = []
    unmatched_plan: list[str] = []
    unmatched_elevation: list[str] = []
    same_interval_groups: list[tuple[str, ...]] = []
    for interval in sorted(set(plan_by_interval) | set(elevation_by_interval)):
        plans_here = plan_by_interval.get(interval, [])
        elevations_here = elevation_by_interval.get(interval, [])
        if len(plans_here) == 1 and len(elevations_here) == 1:
            oid, z_low_u, z_high_u = elevations_here[0]
            pairings.append(
                OpeningPairingV1(
                    plan_opening_id=plans_here[0],
                    elevation_opening_id=oid,
                    span_lo_u=interval[0],
                    span_hi_u=interval[1],
                    z_low_u=z_low_u,
                    z_high_u=z_high_u,
                )
            )
            continue
        # refuse the whole interval: some member has no counterpart, or
        # identity is not decidable (stacked same-interval openings) --
        # either way ⛔ never guessed onto a neighbour
        unmatched_plan.extend(plans_here)
        unmatched_elevation.extend(o for o, _, _ in elevations_here)
        if len(plans_here) + len(elevations_here) > 1:
            # only genuine ambiguity gets a group; a lone opening with no
            # counterpart is simply unmatched, not "ambiguous"
            same_interval_groups.append(
                tuple(sorted(plans_here + [o for o, _, _ in elevations_here]))
            )

    # -- T4-c: retire what THIS run actually discharged (rework 1) --------------
    # the binding evidence: the registry row whose gate ran and returned
    # above, against the ONE source instance this run checked.  With no
    # declared source the run retires nothing -- the debt stays open,
    # ⛔ never a prefix-coincidence deletion of another facade's debt.
    retired = redeemable_debt_ids(
        evidence_debts,
        executed=ExecutedRedemption(
            obligation=span_key, row=span_row, source=elevation_source
        ),
    )

    return OpeningSynthesisV1(
        schema_version=SYNTHESIS_SCHEMA_VERSION,
        facade_family=family,
        world_axis=axis,
        sign=sign,
        along_origin_u=along_origin_u,
        chain_total_u=chain_total_u,
        skin_lo_u=skin.lo_u,
        skin_hi_u=skin.hi_u,
        premise=ELEVATION_CHAIN_SPANS_WHOLE_BUILDING,
        pairings=tuple(
            sorted(pairings, key=lambda p: (p.span_lo_u, p.elevation_opening_id))
        ),
        unmatched_plan_opening_ids=tuple(sorted(unmatched_plan)),
        unmatched_elevation_opening_ids=tuple(sorted(unmatched_elevation)),
        same_interval_groups=tuple(same_interval_groups),
        retired_debt_ids=retired,
    )


def synthesize_openings(*, elevation_doc: dict, walls: Sequence[CutLineV1],
                        plan_openings: Sequence[CutLineV1], mirrored: bool,
                        local_x_positive: str, evidence_debts: Sequence[EvidenceDebtV1] = (),
                        elevation_source: ElevationSourceIdentity | None = None) -> OpeningSynthesisV1:
    """Historical low-level dict API; production callers use current tick sessions."""
    return _synthesize_checked_document(
        elevation_doc=elevation_doc, walls=walls, plan_openings=plan_openings,
        mirrored=mirrored, local_x_positive=local_x_positive,
        evidence_debts=evidence_debts, elevation_source=elevation_source)


def synthesize_current_openings(*, session, expected_batch_id, walls, plan_openings,
                                mirrored, local_x_positive, historical=False):
    """B4's production entry: current numbers and independently checked identity.

    Historical tick sessions can still be reviewed, but without a declared
    evidence artifact they redeem NO debt. Identity is never hand-assembled
    from the caller's image name or the edited preview document.
    """
    from src.agent.correction.tick_claim import TickClaimError, TickSession
    if type(session) is not TickSession:
        raise TickClaimError("TICK_SESSION_REQUIRED")
    doc = session.elevation_document(expected_batch_id)
    identity, debts = None, ()
    if not historical:
        artifact = session.evidence_artifact()
        meta = artifact.bundle.source_artifacts[0]
        identity = ElevationSourceIdentity(
            input_id=meta.input_id, source_contract_id=meta.source_contract_id,
            source_output_sha256=meta.source_output_sha256)
        debts = tuple(artifact.bundle.evidence_debts)
    return _synthesize_checked_document(
        elevation_doc=doc, walls=walls, plan_openings=plan_openings,
        mirrored=mirrored, local_x_positive=local_x_positive,
        evidence_debts=debts, elevation_source=identity)


__all__ = [
    "DECLARED_GRID_UNITS_PER_M",
    "DEBT_REDEMPTION_REGISTRY",
    "DebtRedemption",
    "ElevationSourceIdentity",
    "ExecutedRedemption",
    "OpeningPairingV1",
    "OpeningSynthesisError",
    "OpeningSynthesisV1",
    "assert_obligations_backed",
    "grid_units",
    "grid_units_from_mm",
    "redeemable_debt_ids",
    "redemption_row_for_obligation",
    "redemption_row_for_premise",
    "span_equality_gate",
    "synthesize_openings",
    "synthesize_current_openings",
]
