"""B2: multi-floor assembly — derive per-storey z from the frozen floor-level
ladder (B3 evidence) and stack single-floor projections into one
``CorrectedGeometryV3`` with ``floors[]`` of length N.

⭐ The whole point of this module (dispatch 2026-09-03ai / rework 2026-09-04a /
rework-2 2026-09-04g): the storey elevations are DERIVED from frozen reading
bytes, ⛔ never hand-filled.

Rework-3 (2026-09-04w) — round 3 fell to a forged CARRIER, not a forged claim
-----------------------------------------------------------------------------
Rounds 1-2 fixed the surface (z dropped from the entry signature, then the
claim paths gated), so the round-3 reviewer stopped forging claims and forged
the CARRIER instead: ``ValidatedFloorLadder((SimpleNamespace(z_floor_m=12.34,
...),))`` — a public dataclass constructor whose ``_levels`` element annotation
Python never enforces at runtime — after which ``assemble_multifloor_geometry``
read ``level.z_floor_m`` straight off whatever it found inside.  The docstring
said "The SEALED assembly carrier" while no seal existed ([[design-doc-
described-what-code-never-implemented]], [[gate-measures-right-but-carrier-
gets-swapped]]).  Two type-level facts are enforced now:

  * **The constructor is sealed** (dispatch §一(a)): ``__init__`` compares a
    token that exists ONLY inside the ``_seal_validated_ladder`` closure — not
    a module attribute, never returned, never stored on an instance.  Every
    construction attempt from outside (a direct call, ``dataclasses.replace``,
    a subclass constructor) is a named ``LADDER_MINT_SEAL_REQUIRED`` /
    ``LADDER_SEALED_NO_SUBCLASS`` red.  ⭐ Closure-held is strictly stronger
    than a module-global ``_SEAL``: an underscore global is still reachable as
    an attribute of the module, a closure cell of a factory that has returned
    is reachable only by introspection.

  * **The carrier stores NO z-bearing state** (dispatch §一(c) moved to the
    consumption boundary — the exit check, ⛔ not a narrower entrance): its only
    field is the sealed ``CorrectionEvidenceBundleArtifactV1``.  The per-storey
    levels are RE-DERIVED on every read — ``validate_evidence_bundle`` first,
    then ``_byte_z`` resolution — so there is no stored element to swap, and a
    shell forged with ``object.__new__`` (which no ``__init__`` can stop)
    still cannot move a z: whatever artifact it ends up carrying is GATED AT
    THE READ.  Assembly's z is therefore never a value read off instance
    state; it is always re-derived from frozen bytes that re-passed the gate
    in that very call.

Why "a hand-filled z assembles" is now un-CONSTRUCTIBLE, not merely refused:
the only z that reaches assembly is computed inside ``_levels_of`` from an
artifact that must re-pass ``validate_evidence_bundle`` at the moment of the
read.  Changing the assembled number requires supplying different frozen bytes
that still pass the gate — i.e. authoring a different frozen reading product,
which is the reading trust boundary the 2026-09-04p verdict adjudicated as out
of B2's scope.  (What no Python type layer can stop: runtime introspection that
reads this module's own closure cells or rebinds its globals — that is
equivalent to editing the code, and even then the z stays gated, because the
gate runs at consumption, ⛔ not at mint time only.)

Layering: this module depends only on the evidence contract, the correction
schema, and the geometry validator.  It never imports ``pipeline``.  The
model-driven orchestration that runs the evidence chain once per plan product
lives in ``pipeline.run_multifloor_correction`` (which imports THIS module, not
the other way round).

⛔ NOT this module's job (dispatch §四): opening synthesis (B4), touching the
projection bridge's geometry algorithm, relaxing the z-stack continuity check,
or reading gt.  It also does not specialise to sm25 — a specific storey count
or storey height is a reading, not a theorem: the storey count is COUNTED from
the data and each storey height is COMPUTED from it (⛔ no sm25 elevation
constant is written into this module — the acceptance greps for exactly that).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Sequence

from src.agent.correction.evidence_contract import (
    ArtifactPointerV1,
    CorrectionEvidenceBundleArtifactV1,
    FloorLevelClaimV1,
    MIN_FLOOR_LEVELS,
    resolve_json_pointer,
    validate_evidence_bundle,
)
from src.agent.correction.geometry_validator import check_zstack
from src.agent.correction.schema import CorrectedGeometryV3, FootprintRing, FloorV3


class MultiFloorAssemblyError(RuntimeError):
    """A named, LOUD refusal from the multi-floor assembly (dispatch T4).

    Mirrors the projection bridge's ``ProjectionBridgeError`` shape: a code
    token plus a machine-readable detail dict, so a bad input is a counted,
    diagnosable red — ⛔ never a silent shrug or a fabricated floor."""

    def __init__(self, code: str, detail: dict | None = None):
        self.code = code
        self.detail = detail or {}
        super().__init__(f"{code}: {self.detail}" if self.detail else code)


def _byte_z(frozen_docs: dict[str, dict], ref: ArtifactPointerV1) -> float:
    """Resolve ONE z from the FROZEN BYTES via its ``z_ref`` json pointer.

    ⭐ This is the load-bearing "z is byte-derived" mechanism (B-2, dispatch
    §〇③(a)): the assembled z is always the byte the ref names, ⛔ never a value a
    caller set on a claim (``claim.z_m``) or baked onto a level.  A ``model_copy``
    on ``z_m`` — or a hand-forged level — therefore cannot move the z; only
    supplying different frozen bytes can, which is the reading trust boundary."""
    doc = frozen_docs.get(ref.input_id)
    if doc is None:
        raise MultiFloorAssemblyError(
            "FLOOR_LEVEL_SOURCE_UNKNOWN",
            {"input_id": ref.input_id, "pointer": ref.json_pointer},
        )
    try:
        value = resolve_json_pointer(doc, ref.json_pointer)
    except KeyError as exc:
        raise MultiFloorAssemblyError(
            "FLOOR_LEVEL_SOURCE_UNRESOLVED",
            {"input_id": ref.input_id, "pointer": ref.json_pointer,
             "because": str(exc)},
        ) from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MultiFloorAssemblyError(
            "FLOOR_LEVEL_SOURCE_NOT_NUMERIC",
            {"input_id": ref.input_id, "pointer": ref.json_pointer,
             "got": type(value).__name__},
        )
    return float(value)


def _declared_tick_m(frozen_docs: dict[str, dict], ref: ArtifactPointerV1) -> float:
    """丁 (ruling 2026-09-07x §一): the storey z is the DECLARED tick, ⛔ not the ink.

    The drawing's own ``calibration.z`` chain DECLARES the vertical dimension
    chain (``cum_mm`` — the same frozen bytes the adapter's closure recompute
    already reads); the rung's ``pos_m`` is the independently measured INK of
    the same floor line, and its scatter across the four facades (measured
    1.0–13.5 mm on sm25) is pure pixel-side product.  So the RECOGNITION
    stays the ink (which line, selected by the frozen rule — unchanged),
    while the VALUE the ladder uses is the nearest DECLARED tick.

    ⭐ The mapping must be PROVEN unique, never assumed (the dispatch's
    stop-and-report trigger, as a machine-checkable refusal — ⛔ no invented
    tolerance, ⛔ no silent fallback to the ink):

        noise_bound_mm = calibration.z.mm_per_px × max|residual_px|
            — recomputed from the per-tick residuals, ⛔ not read off the
            self-reported ``max_abs_residual_px`` (whose declared value is
            cross-checked; a drift is a named red) — the BLK-1 shape.

        unique  ⇔  |ink_mm − second_nearest_tick| > noise_bound_mm

    (That one direction IS the ruled proof — the ruling 2026-09-07x §一 and
    the dispatch define non-uniqueness as exactly "次近刻度距离 ≤ 噪声界".
    The NEAREST-side ink residual is NOT gated: measured on the real south
    facade it runs 6.5 mm against a 1.46 mm tick-fit bound — the bound
    describes the dimension-witness FIT, while the rung ink is an
    independent pixel measurement whose scatter (1.0–13.5 mm across the
    four facades) is exactly the pixel-side product the ruling moved OFF
    the critical path.  Gating it would re-block the pathway in a new
    coat; it travels as a readout instead — ``ink_snap_residual_mm``.)

    Measured margins on the four real sm25 facades: second-nearest distance
    exceeds the bound by 356× / 102× / 547× / 118× (east/north/south/west).
    """
    ink_m = _byte_z(frozen_docs, ref)
    doc = frozen_docs[ref.input_id]
    calibration = doc.get("calibration") if isinstance(doc, dict) else None
    z_chain = (
        calibration.get("z") if isinstance(calibration, dict) else None
    )
    if not isinstance(z_chain, dict):
        raise MultiFloorAssemblyError(
            "FLOOR_TICK_DECLARATION_MISSING",
            {
                "input_id": ref.input_id,
                "pointer": ref.json_pointer,
                "reason": (
                    "丁: the storey z is taken from the drawing's declared "
                    "calibration.z.cum_mm; this product declares none, and no "
                    "tolerance may be invented to bridge that (stop-and-"
                    "report shape, as a named refusal)"
                ),
            },
        )
    ticks = z_chain.get("cum_mm")
    mm_per_px = z_chain.get("mm_per_px")
    residual_px = z_chain.get("residual_px")
    declared_max = z_chain.get("max_abs_residual_px")
    malformed = (
        not isinstance(ticks, list) or len(ticks) < 2
        or any(isinstance(t, bool) or not isinstance(t, (int, float))
               for t in ticks)
        or isinstance(mm_per_px, bool) or not isinstance(mm_per_px, (int, float))
        or not isinstance(residual_px, list) or not residual_px
        or any(isinstance(r, bool) or not isinstance(r, (int, float))
               for r in residual_px)
    )
    if malformed:
        raise MultiFloorAssemblyError(
            "FLOOR_TICK_DECLARATION_MISSING",
            {
                "input_id": ref.input_id,
                "reason": "calibration.z is present but cum_mm / mm_per_px / "
                          "residual_px are malformed for the uniqueness proof",
            },
        )
    if float(mm_per_px) <= 0.0:
        raise MultiFloorAssemblyError(
            "FLOOR_TICK_DECLARATION_MISSING",
            {"input_id": ref.input_id,
             "reason": f"calibration.z.mm_per_px = {mm_per_px!r} is not positive"},
        )
    recomputed_max = max(abs(float(r)) for r in residual_px)
    if declared_max is not None and (
        isinstance(declared_max, bool)
        or not isinstance(declared_max, (int, float))
        or float(declared_max) != recomputed_max
    ):
        raise MultiFloorAssemblyError(
            "FLOOR_TICK_RESIDUAL_SUMMARY_DRIFT",
            {
                "input_id": ref.input_id,
                "declared_max_abs_residual_px": declared_max,
                "recomputed_max_abs_residual_px": recomputed_max,
            },
        )
    bound_mm = float(mm_per_px) * recomputed_max
    ink_mm = ink_m * 1000.0
    ordered = sorted((float(t) for t in ticks), key=lambda t: abs(t - ink_mm))
    nearest, second = ordered[0], ordered[1]
    d_second = abs(second - ink_mm)
    if not d_second > bound_mm:
        raise MultiFloorAssemblyError(
            "FLOOR_LINE_TICK_UNPROVEN",
            {
                "input_id": ref.input_id,
                "pointer": ref.json_pointer,
                "ink_mm": ink_mm,
                "nearest_tick_mm": nearest,
                "second_nearest_tick_mm": second,
                "dist_second_mm": d_second,
                "noise_bound_mm": bound_mm,
                "reason": "second_nearest_tick_inside_noise_bound",
            },
        )
    return nearest / 1000.0


def _ink_snap_residual_mm(
    frozen_docs: dict[str, dict], ref: ArtifactPointerV1
) -> float:
    """READOUT (丁, non-gating): how far the rung's frozen INK sits from the
    declared tick it was snapped onto, in mm.  Visible on every level so the
    pixel-side scatter stays observable after the value moved to the
    declared integers — ⛔ never a gate (see ``_declared_tick_m``)."""
    ink_m = _byte_z(frozen_docs, ref)
    ticks = frozen_docs[ref.input_id]["calibration"]["z"]["cum_mm"]
    nearest = min((float(t) for t in ticks), key=lambda t: abs(t - ink_m * 1000.0))
    return abs(nearest - ink_m * 1000.0)


@dataclass(frozen=True, eq=False)
class _DerivedFloorLevel:
    """One storey's z, BYTE-RESOLVED from a bounding pair of gate-validated
    floor-level claims (B2/T1) — ⛔ never hand-filled.

    ⭐ Type-level no-hand-fill (B-2, dispatch §二 / §〇③): this carrier holds the
    two bounding ``FloorLevelClaimV1`` (``lower`` = the rung this storey sits on,
    ``upper`` = the next rung up) and the ``frozen_docs`` map its refs resolve
    into.  Every z-shaped attribute is a READ-ONLY property that DEREFERENCES the
    claim's ``z_ref`` INTO ``frozen_docs`` — it never reads ``claim.z_m`` and
    there is no settable z field:

      * ``z_floor_m`` is the DECLARED tick the lower rung's frozen ink
        provably selects (丁, ruling 2026-09-07x §一 — see
        :func:`_declared_tick_m`; the recognition stays the ink byte, the
        VALUE is ``calibration.z.cum_mm``, uniqueness PROVEN against the
        product's own noise bound, ⛔ never the raw measured ``pos_m``);
      * ``ceiling_height_m`` is the rise ``upper-tick − lower-tick`` — a DERIVED
        difference whose BOTH operands are frozen bytes.

    There is no ``z_floor_m=`` / ``ceiling_height_m=`` constructor keyword, so the
    reviewer's ``DerivedFloorLevel(z_floor_m=12.34, ceiling_height_m=5.67)`` is a
    ``TypeError``.  And because z is byte-resolved, even a ``model_copy`` on a
    claim's ``z_m`` (the reviewer's round-2 bypass) has NO effect on the derived
    z — the byte the ref names is unchanged.  This class is PRIVATE; the only
    sanctioned minter is :func:`derive_floor_ladder`, which runs the frozen-byte
    gate first, and the sealed :class:`ValidatedFloorLadder` it returns is the
    only thing :func:`assemble_multifloor_geometry` accepts."""

    floor_index: int
    lower: FloorLevelClaimV1
    upper: FloorLevelClaimV1
    frozen_docs: dict

    @property
    def z_floor_m(self) -> float:
        return _declared_tick_m(self.frozen_docs, self.lower.z_ref)

    @property
    def ceiling_height_m(self) -> float:
        return _declared_tick_m(self.frozen_docs, self.upper.z_ref) - self.z_floor_m

    @property
    def z_floor_claim_id(self) -> str:
        return self.lower.structure_line_id

    @property
    def z_floor_ref(self) -> ArtifactPointerV1:
        return self.lower.z_ref

    @property
    def z_top_claim_id(self) -> str:
        return self.upper.structure_line_id

    @property
    def z_top_ref(self) -> ArtifactPointerV1:
        return self.upper.z_ref

    @property
    def ink_snap_residual_mm(self) -> float:
        """丁 readout: the storey's LOWER rung ink-to-tick residual (mm),
        non-gating — see :func:`_ink_snap_residual_mm`."""
        return _ink_snap_residual_mm(self.frozen_docs, self.lower.z_ref)

    @property
    def ink_snap_residual_upper_mm(self) -> float:
        """丁 readout: the UPPER rung's ink-to-tick residual (mm).  The top
        rung of the building only ever appears as an ``upper`` bound, so this
        is where its scatter stays visible."""
        return _ink_snap_residual_mm(self.frozen_docs, self.upper.z_ref)


# ── the seal (dispatch §一(a)) ──────────────────────────────────────────────── #
# ``_LADDER_SEAL`` exists ONLY inside this factory's closure: it is not a module
# attribute (⭐ unlike a ``_SEAL`` global, which stays reachable as ``m._SEAL``),
# it is never returned, and it is never stored on an instance.  The factory runs
# once at import and hands back the class plus a private minter that can present
# the token; module-external code has NO name that binds it.
def _seal_validated_ladder():
    _LADDER_SEAL = object()

    @dataclass(frozen=True, eq=False, init=False, repr=False)
    class ValidatedFloorLadder:
        """The SEALED assembly carrier (dispatch §一(a)+(c), rework-3).

        ⭐ CLAIM LEDGER — every claim below names the code that enforces it
        (rework-3 dispatch §二#3); a claim with no enforcing line gets deleted,
        ⛔ not narrated:

        1. "It cannot be populated from outside this module" — ``__init__``
           compares ``_seal`` against the closure-held ``_LADDER_SEAL``; any
           external construction attempt (direct call, ``dataclasses.replace``,
           a subclass's inherited constructor) raises the named
           ``LADDER_MINT_SEAL_REQUIRED``.  ``object.__new__`` can still yield an
           attribute-less shell — no ``__init__`` can stop that — which is why
           claim 3 exists.
        2. "It cannot be subclassed" — ``__init_subclass__`` raises the named
           ``LADDER_SEALED_NO_SUBCLASS`` at class-creation time, so an
           ``isinstance``-passing subclass with an overridden constructor
           cannot exist.
        3. "It stores NO z-bearing state, so there is nothing to swap" — its
           only field is ``_artifact``; ``__len__`` / ``__iter__`` /
           ``__getitem__`` all go through ``_levels_of_carrier``, which
           RE-DERIVES the levels (``validate_evidence_bundle`` first, then
           ``_byte_z`` resolution) on every read.  An ``object.__new__`` shell
           — or an honest carrier whose ``_artifact`` was swapped post-hoc —
           either assembles its artifact's GATED bytes or fails by name
           (``LADDER_CARRIER_CORRUPT`` / ``EvidenceContractError``); it can
           never assemble a value that was merely SET on an instance.
        4. "The only sanctioned minter gates first" — the closure-held
           ``_mint_sealed`` is module-private, and its only module-level caller
           is :func:`derive_floor_ladder`, whose first act is ``_levels_of``
           (the gate + derivation), so a bad artifact is a named red at the
           minter's door, ⛔ never inside assembly.
        """

        _artifact: CorrectionEvidenceBundleArtifactV1

        def __init__(self, _artifact=None, *, _seal=None):
            # ⭐ the parameter NAME matches the field name on purpose:
            # ``dataclasses.replace`` rebuilds init kwargs from FIELD names
            # (init=False on the decorator only suppresses GENERATING
            # ``__init__`` — the field's init flag stays True), so every
            # replace shape re-enters THIS seal check, ⛔ never a keyword
            # TypeError by accident.
            if _seal is not _LADDER_SEAL:
                raise MultiFloorAssemblyError(
                    "LADDER_MINT_SEAL_REQUIRED",
                    {
                        "got": type(self).__name__,
                        "reason": (
                            "ValidatedFloorLadder cannot be constructed "
                            "outside multifloor: it is minted only by "
                            "derive_floor_ladder, which runs the frozen-byte "
                            "gate first (dispatch §一(a))"
                        ),
                    },
                )
            if not isinstance(_artifact, CorrectionEvidenceBundleArtifactV1):
                raise MultiFloorAssemblyError(
                    "LADDER_MINT_REQUIRES_SEALED_ARTIFACT",
                    {
                        "got": (
                            type(_artifact).__name__
                            if _artifact is not None
                            else "None"
                        )
                    },
                )
            object.__setattr__(self, "_artifact", _artifact)

        def __init_subclass__(cls, **kwargs):
            raise MultiFloorAssemblyError(
                "LADDER_SEALED_NO_SUBCLASS",
                {
                    "subclass": cls.__name__,
                    "reason": (
                        "ValidatedFloorLadder is sealed; an isinstance-passing "
                        "subclass with an overridden constructor must not "
                        "exist (dispatch §一(a))"
                    ),
                },
            )

        def _levels_of_carrier(self) -> tuple[_DerivedFloorLevel, ...]:
            """The exit check (§一(c)): re-derive levels from the artifact AT
            THE READ — gate first, bytes only, ⛔ never instance-carried z."""
            artifact = getattr(self, "_artifact", None)
            if not isinstance(artifact, CorrectionEvidenceBundleArtifactV1):
                raise MultiFloorAssemblyError(
                    "LADDER_CARRIER_CORRUPT",
                    {
                        "got": (
                            type(artifact).__name__
                            if artifact is not None
                            else "None"
                        ),
                        "reason": (
                            "the carrier carries no sealed artifact — an "
                            "object.__new__ shell or a stripped instance "
                            "has no z to assemble (dispatch §一(c))"
                        ),
                    },
                )
            return _levels_of(artifact)

        def __len__(self) -> int:
            return len(self._levels_of_carrier())

        def __iter__(self):
            return iter(self._levels_of_carrier())

        def __getitem__(self, index):
            return self._levels_of_carrier()[index]

    def _mint_sealed(artifact: CorrectionEvidenceBundleArtifactV1):
        return ValidatedFloorLadder(artifact, _seal=_LADDER_SEAL)

    return ValidatedFloorLadder, _mint_sealed


ValidatedFloorLadder, _mint_sealed_ladder = _seal_validated_ladder()


def _mint_ladder(
    claims: Sequence[FloorLevelClaimV1],
    frozen_docs: dict[str, dict],
) -> tuple[_DerivedFloorLevel, ...]:
    """Build the DERIVED levels from ALREADY-GATE-VALIDATED claims + frozen docs.

    ⚠️ PRIVATE and byte-derived: the z used to order and to size each storey is
    resolved from ``frozen_docs`` (see :func:`_byte_z`), ⛔ never from
    ``claim.z_m``.  The rule (the consumer-side mirror of B3's
    ``FLOOR_LEVEL_SELECTION_RULE``): sort the rungs ascending by their frozen
    byte; N distinct rungs give N-1 storeys; storey ``i`` sits on rung ``i`` and
    rises to rung ``i+1``.  It returns the raw levels tuple — sealing them
    into a :class:`ValidatedFloorLadder` is :func:`derive_floor_ladder`'s job
    (the seal lives in the closure, ⛔ not here).

    Loud, never silent (T4):
      * fewer than ``MIN_FLOOR_LEVELS`` rungs -> ``FLOOR_LADDER_DEGENERATE``;
      * two rungs at the same byte z (the ladder does not strictly ascend,
        "标高不单调"), also exactly the degenerate zero-height case ->
        ``FLOOR_LADDER_NOT_ASCENDING``.

    ⛔ Sorting is NOT silent repair: after the sort, any adjacent pair whose
    rise is <= 0 can only be a duplicate rung, reported by name, not swallowed.

    ⭐ 丁 (ruling 2026-09-07x §一): the declared-tick mapping is validated
    EAGERLY here, per rung — the minter's own "gates first" rule.  The z
    properties on the levels are lazy (byte-resolved at read), so a proof
    that only ran at read time would be skippable by a consumer that never
    reads z; at mint time it is not.
    """
    ordered = sorted(claims, key=lambda c: _byte_z(frozen_docs, c.z_ref))
    if len(ordered) < MIN_FLOOR_LEVELS:
        raise MultiFloorAssemblyError(
            "FLOOR_LADDER_DEGENERATE",
            {"n_levels": len(ordered), "min_levels": MIN_FLOOR_LEVELS},
        )
    for claim in ordered:
        _declared_tick_m(frozen_docs, claim.z_ref)
    levels: list[_DerivedFloorLevel] = []
    for index in range(len(ordered) - 1):
        lower, upper = ordered[index], ordered[index + 1]
        rise = _byte_z(frozen_docs, upper.z_ref) - _byte_z(frozen_docs, lower.z_ref)
        if rise <= 0.0:
            raise MultiFloorAssemblyError(
                "FLOOR_LADDER_NOT_ASCENDING",
                {
                    "lower_id": lower.structure_line_id,
                    "upper_id": upper.structure_line_id,
                    "z_lower_m": _byte_z(frozen_docs, lower.z_ref),
                    "z_upper_m": _byte_z(frozen_docs, upper.z_ref),
                    "rise_m": rise,
                },
            )
        levels.append(
            _DerivedFloorLevel(
                floor_index=index, lower=lower, upper=upper, frozen_docs=frozen_docs
            )
        )
    return tuple(levels)


def _levels_of(
    elevation_evidence: CorrectionEvidenceBundleArtifactV1,
) -> tuple[_DerivedFloorLevel, ...]:
    """THE single derivation core — gate FIRST, then byte-resolve the levels.

    ⭐ Rework-3 (dispatch §一(c) at the boundary): EVERY consumer of storey z —
    :func:`derive_floor_ladder`, the carrier's own ``__len__`` / ``__iter__`` /
    ``__getitem__``, and :func:`assemble_multifloor_geometry` — gets its levels
    from THIS function, which re-runs B3's ``validate_evidence_bundle`` and
    resolves each z from the frozen bytes via :func:`_byte_z`.  There is no
    second copy of the gate and no stored z anywhere: whatever a caller did to
    an instance in between cannot survive this re-derivation.  A claim whose
    ``z_m`` drifted from the byte its ``z_ref`` names is a named
    ``FLOOR_LEVEL_VALUE_DRIFTED_FROM_SOURCE`` red HERE."""
    validate_evidence_bundle(elevation_evidence)
    frozen_docs = {
        source.artifact.input_id: json.loads(source.raw_bytes)
        for source in elevation_evidence.frozen_sources
    }
    return _mint_ladder(elevation_evidence.bundle.floor_level_claims, frozen_docs)


def derive_floor_ladder(
    elevation_evidence: CorrectionEvidenceBundleArtifactV1,
) -> ValidatedFloorLadder:
    """B2/T1: turn B3's frozen floor-level ladder into a SEALED per-storey ladder.

    ⭐ B-1/B-2 (rework-2 2026-09-04g) + the rework-3 seal: the SOLE input is the
    SEALED carrier ``elevation_evidence`` (``CorrectionEvidenceBundleArtifactV1``
    = bundle plus its frozen bytes), ⛔ NOT a detached
    ``Sequence[FloorLevelClaimV1]``.  The FIRST act is B3's existing value↔byte
    gate via :func:`_levels_of`: a claim whose ``z_m`` drifted from the byte its
    ``z_ref`` names is a named ``FLOOR_LEVEL_VALUE_DRIFTED_FROM_SOURCE`` red
    HERE, before any carrier is minted (the derivation result is deliberately
    consumed only for its errors — the carrier re-derives on every read, so it
    stores no z-bearing state).  The returned :class:`ValidatedFloorLadder` is
    the only thing :func:`assemble_multifloor_geometry` accepts, so "passed the
    frozen-byte gate" is carried by the TYPE, ⛔ not by the history of some call.
    """
    _levels_of(elevation_evidence)
    return _mint_sealed_ladder(elevation_evidence)


# ── W-1 T2-④ / BLK-1 (2026-09-07): the cross-floor footprint snap ──────────── #
#
# WHAT THIS IS: an explicit, tolerance-gated snap step between the per-floor
# chains and ``assemble_multifloor_geometry``.  Measured on sm25 (T1 probe,
# ``2026-09-07h_w1_t1_probe``): the two plan products are calibrated
# INDEPENDENTLY (each from its own dimension witnesses), so the same wall edge
# lands ~7-12 mm apart in the two floors' world coordinates, and assembly's
# zero-tolerance fingerprint comparison rejects it as a "setback".  That is a
# calibration residual, not a drawing error — per the user's 2026-09-07 domain
# ruling ("吸附/分辨率残差 = 机器直接修，真·画错才签字") it is machine-fix
# territory, ⛔ never a human-signing item.
#
# THE TOLERANCE IS FULLY DERIVED (verdict BLK-1, 2026-09-07i): ⛔ no invented
# constant, ⛔ no ``max(<derived>, <literal>)`` floor.  Both limbs of
#
#     tolerance = min(noise_bound, cap)
#
# are read from the plan products' OWN declarations:
#
#   noise_bound = hypot(bx_ref + bx_up, by_ref + by_up)
#       where b_axis = mm_per_px_axis × max|residual_px|_axis / 1000
#       — ``observations.calibration.{x,y}`` is the product's own per-axis
#       least-squares calibration; ``residual_px`` are the per-tick fit
#       residuals, and the max is RECOMPUTED from them (⛔ not read off the
#       self-reported ``max_abs_residual_px`` — a recompute, though the
#       declared summary is cross-checked and a drift is a named red).  Each
#       drawing independently calibrated ⇒ comparing a point across two
#       drawings compounds BOTH bounds per axis; the distance then combines
#       the two axes Euclideanly.
#
#   cap = min(thinnest declared wall of ref, of upper) / 2
#       from ``declarations.thickness_callouts_mm`` — the SAME semantics as
#       the same-day ladder CAP ruling (half the thinnest wall the source
#       itself declares; 2026-09-07 user ruling: ⛔ never a hard-coded
#       constant, the two cases both declare 0.06 m and baking that in would
#       freeze it).  Absorbing a displacement larger than half a wall stops
#       being "straighten the representation" and becomes "WHICH design is
#       this" — that is not this step's call, and it is exactly what trips
#       the existing loud ``PER_FLOOR_FOOTPRINT_MISMATCH`` instead.
#
# ⚠ The production as_drawn chain is floating-point metres, NOT quantised, so
# ``projection_bridge.resolution_from_units_per_metre``'s granularity (the
# fixture world's ``units_per_metre``) is 0.0 here and CANNOT be a tolerance
# source on this leg — its own docstring (N-3) requires the production caller
# to REDECLARE the granularity source, which is exactly what the calibration
# declarations above are.
#
# MATCHING SHAPE: the rings arrive here as CORNER-ONLY rings (the projection
# bridge's ``_corner_only_ring`` drops collinear subdivisions upstream, so
# both sm25 floors enter with 8 vertices each).  The snap is nonetheless
# gated on the SYMMETRIC Hausdorff distance between the two rings as point
# sets — Hausdorff is chosen precisely so this step does NOT depend on the
# two rings having equal vertex counts, a property the upstream producer is
# free to change (a future producer that keeps its collinear subdivisions,
# or whose wall-end jogs land at different along-edge positions per drawing,
# would make per-vertex correspondence structurally impossible again).
# Every vertex of either ring must lie within the tolerance of the OTHER
# ring's boundary polyline.  Within tolerance ⇒ the upper floor's ring
# is replaced VERBATIM by the reference ring (bitwise-identical ⇒ assembly's
# fingerprint comparison passes exactly, ⛔ not "approximately"); over
# tolerance ⇒ this step does NOTHING and ``assemble_multifloor_geometry``'s
# existing ``PER_FLOOR_FOOTPRINT_MISMATCH`` fires unchanged — a real setback
# is never swallowed.  ⚠ Replacing the ring does NOT touch the floor's cells:
# they keep their own drawing's coordinates (the schema enforces no
# cell-in-footprint containment), leaving them ~the same residual off the
# adopted ring, inside the same declared bound.


@dataclass(frozen=True)
class PlanCalibrationDeclaration:
    """One plan product's own tolerance-bearing declarations, as consumed by
    :func:`footprint_snap_tolerance_m`.  ``input_id`` names the product for
    error messages only."""

    input_id: str
    bound_x_m: float
    bound_y_m: float
    cap_m: float


@dataclass(frozen=True)
class DeclaredExteriorFrame:
    """One plan product's own exterior axis-frame declarations (W#6, dispatch
    2026-09-08c S-B): the overall extents and the thickness callouts the
    exterior axis frame ``[t/2, overall − t/2]`` is derived from — the same
    "the drawing declares, the code derives" shape as 丁's ladder ticks."""

    input_id: str
    overall_x_m: float
    overall_y_m: float
    thickness_callouts_mm: tuple[float, ...]


def read_declared_exterior_frame(
    doc: dict, *, input_id: str
) -> DeclaredExteriorFrame:
    """Derive one plan product's exterior-frame inputs from ITS OWN declared
    quantities (W#6).  Loud, never defaulted — a product that does not
    declare its overall extents cannot have an axis frame derived for it."""
    calibration = (doc.get("observations") or {}).get("calibration")
    if not isinstance(calibration, dict):
        raise MultiFloorAssemblyError(
            "PLAN_CALIBRATION_MISSING",
            {"input_id": input_id, "reason": "no observations.calibration declared"},
        )
    overall: dict[str, float] = {}
    for axis in ("x", "y"):
        chain = calibration.get(axis)
        if not isinstance(chain, dict):
            raise MultiFloorAssemblyError(
                "PLAN_CALIBRATION_AXIS_MISSING",
                {"input_id": input_id, "axis": axis},
            )
        value = chain.get("overall_mm")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or float(value) <= 0.0
        ):
            raise MultiFloorAssemblyError(
                "PLAN_OVERALL_EXTENT_MISSING",
                {"input_id": input_id, "axis": axis, "overall_mm": value},
            )
        overall[axis] = float(value) / 1000.0
    callouts = (doc.get("declarations") or {}).get("thickness_callouts_mm")
    if (
        not isinstance(callouts, list)
        or not callouts
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or float(v) <= 0.0
            for v in callouts
        )
    ):
        raise MultiFloorAssemblyError(
            "PLAN_THICKNESS_CALLOUTS_MISSING",
            {
                "input_id": input_id,
                "reason": "no positive declarations.thickness_callouts_mm — "
                          "the exterior axis frame has no declared source",
            },
        )
    return DeclaredExteriorFrame(
        input_id=input_id,
        overall_x_m=overall["x"],
        overall_y_m=overall["y"],
        thickness_callouts_mm=tuple(float(v) for v in callouts),
    )


def read_plan_calibration_declaration(
    doc: dict, *, input_id: str
) -> PlanCalibrationDeclaration:
    """Derive one plan product's snap inputs from ITS OWN declared quantities.

    Loud, never defaulted (BLK-1): a product that does not declare its
    calibration or its wall-thickness callouts cannot have a tolerance derived
    for it, and an invented default here would be exactly the retired
    ``max(..., 0.05)`` shape in a new coat.  (A precedent for defaulting
    exists elsewhere — ``validator/checks/as_drawn.py`` defaults a missing
    callout list to ``[240]`` — ⛔ this step deliberately does NOT follow it:
    that consumer grades, this one displaces geometry.)
    """
    calibration = (doc.get("observations") or {}).get("calibration")
    if not isinstance(calibration, dict):
        raise MultiFloorAssemblyError(
            "PLAN_CALIBRATION_MISSING",
            {"input_id": input_id, "reason": "no observations.calibration declared"},
        )
    bounds: list[float] = []
    for axis in ("x", "y"):
        chain = calibration.get(axis)
        if not isinstance(chain, dict):
            raise MultiFloorAssemblyError(
                "PLAN_CALIBRATION_AXIS_MISSING",
                {"input_id": input_id, "axis": axis},
            )
        mm_per_px = chain.get("mm_per_px")
        residual_px = chain.get("residual_px")
        if (isinstance(mm_per_px, bool) or not isinstance(mm_per_px, (int, float))
                or float(mm_per_px) <= 0.0):
            raise MultiFloorAssemblyError(
                "PLAN_CALIBRATION_SCALE_INVALID",
                {"input_id": input_id, "axis": axis, "mm_per_px": mm_per_px},
            )
        if (not isinstance(residual_px, list) or not residual_px
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       for v in residual_px)):
            raise MultiFloorAssemblyError(
                "PLAN_CALIBRATION_RESIDUAL_MALFORMED",
                {"input_id": input_id, "axis": axis},
            )
        # ⭐ recompute, ⛔ never trust the self-reported summary (the summary
        # is checked for drift right after — a tampered self-report dies here).
        recomputed_max = max(abs(float(v)) for v in residual_px)
        declared_max = chain.get("max_abs_residual_px")
        if (isinstance(declared_max, bool) or not isinstance(declared_max, (int, float))
                or abs(recomputed_max - float(declared_max)) > 0.0):
            raise MultiFloorAssemblyError(
                "PLAN_CALIBRATION_RESIDUAL_SUMMARY_DRIFT",
                {
                    "input_id": input_id,
                    "axis": axis,
                    "declared_max_abs_residual_px": declared_max,
                    "recomputed_max_abs_residual_px": recomputed_max,
                },
            )
        bounds.append(float(mm_per_px) * recomputed_max / 1000.0)
    callouts = (doc.get("declarations") or {}).get("thickness_callouts_mm")
    if (not isinstance(callouts, list) or not callouts
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or float(v) <= 0.0 for v in callouts)):
        raise MultiFloorAssemblyError(
            "PLAN_THICKNESS_CALLOUTS_MISSING",
            {
                "input_id": input_id,
                "reason": "no positive declarations.thickness_callouts_mm — "
                          "the snap CAP has no declared source and must not "
                          "be defaulted",
            },
        )
    return PlanCalibrationDeclaration(
        input_id=input_id,
        bound_x_m=bounds[0],
        bound_y_m=bounds[1],
        cap_m=(min(float(v) for v in callouts) / 2.0) / 1000.0,
    )


def footprint_snap_tolerance_m(
    reference: PlanCalibrationDeclaration,
    upper: PlanCalibrationDeclaration,
) -> float:
    """``min(noise_bound, cap)`` — both limbs derived, zero literals.

    The noise limb compounds the two drawings' independently-calibrated
    per-axis bounds (sum per axis, Euclidean across axes); the cap limb is
    half the thinnest wall EITHER compared drawing declares (the ladder-CAP
    semantics).  The ``min`` mirrors ``_axis_snap_deviation_limit``'s
    ``min(angle-envelope, CAP)``: beyond either limb this is not a
    representation fix, and the existing assembly gate owns the refusal.
    """
    noise = math.hypot(reference.bound_x_m + upper.bound_x_m,
                       reference.bound_y_m + upper.bound_y_m)
    cap = min(reference.cap_m, upper.cap_m)
    return min(noise, cap)


def _ring_points(floor: FloorV3) -> list[tuple[float, float]]:
    """The footprint ring as an OPEN vertex cycle (duplicate tail removed)."""
    pts = [(float(x), float(y)) for x, y in floor.footprint.vertices]
    if pts and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _point_to_segment_distance(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _directed_hausdorff_m(
    points: Sequence[tuple[float, float]], ring: Sequence[tuple[float, float]]
) -> float:
    """Max over ``points`` of the distance to the nearest SEGMENT of ``ring``
    (nearest-segment, ⛔ not nearest-vertex: the rings carry different
    collinear-subdivision granularity, so the nearest point of the other ring
    is usually mid-segment)."""
    segs = list(zip(ring, list(ring[1:]) + [ring[0]]))
    worst = 0.0
    for p in points:
        d = min(_point_to_segment_distance(p, a, b) for a, b in segs)
        if d > worst:
            worst = d
    return worst


@dataclass(frozen=True)
class FootprintSnapRecord:
    """One non-reference floor's snap decision, with the full derivation."""

    floor_index: int
    floor_id: str
    input_id: str
    action: str  # "identical" | "snapped" | "refused"
    hausdorff_upper_to_reference_m: float
    hausdorff_reference_to_upper_m: float
    tolerance_m: float
    noise_bound_m: float
    cap_m: float
    bbox_shift_m: dict


@dataclass(frozen=True)
class FootprintSnapAccount:
    """The wiring-side ledger payload (``footprint_snap_ledger.json``)."""

    schema: str = "footprint_snap_ledger_v1"
    applied: bool = False
    records: tuple[FootprintSnapRecord, ...] = ()
    endpoint_connections: tuple[dict, ...] = ()

    def to_payload(self) -> dict:
        return {
            "schema": self.schema,
            "applied": self.applied,
            **({"endpoint_connections": list(self.endpoint_connections)} if self.endpoint_connections else {}),
            "records": [
                {
                    "floor_index": r.floor_index,
                    "floor_id": r.floor_id,
                    "input_id": r.input_id,
                    "action": r.action,
                    "hausdorff_upper_to_reference_m": r.hausdorff_upper_to_reference_m,
                    "hausdorff_reference_to_upper_m": r.hausdorff_reference_to_upper_m,
                    "tolerance_m": r.tolerance_m,
                    "tolerance_derivation": {
                        "noise_bound_m": r.noise_bound_m,
                        "cap_m": r.cap_m,
                    },
                    "bbox_shift_m": r.bbox_shift_m,
                }
                for r in self.records
            ],
        }


def snap_footprints_to_reference(
    single_floor_geometries: Sequence[CorrectedGeometryV3],
    declarations: Sequence[PlanCalibrationDeclaration],
) -> tuple[tuple[CorrectedGeometryV3, ...], FootprintSnapAccount]:
    """Gate each upper floor's footprint against the GROUND floor's.

    Reference = ``single_floor_geometries[0]`` (``plan_runs[0]``, ground-up
    order is the caller contract).  For each upper floor, in declaration
    order (``declarations[i]`` pairs with ``single_floor_geometries[i]``):

      * already fingerprint-identical → ``identical`` (nothing consumed, no
        debt; the tolerance is still DERIVED and recorded — the derivation
        runs on every multi-floor path, so a product that stops declaring its
        calibration is caught here even on a coincidentally-matching pair);
      * symmetric Hausdorff ≤ tolerance → ``snapped``: the floor's ring is
        replaced verbatim by the reference ring and its geometry bbox
        re-stamped from the reference (the record carries the displacement
        actually absorbed);
      * over tolerance → ``refused``: returned UNTOUCHED, so
        ``assemble_multifloor_geometry``'s existing zero-tolerance comparison
        raises ``PER_FLOOR_FOOTPRINT_MISMATCH`` — the loud refusal is that
        gate's, ⛔ never swallowed here.

    Single-floor input: no cross-floor comparison exists; the account records
    the no-op (``applied=False``) and nothing is derived.
    """
    if len(single_floor_geometries) != len(declarations):
        raise MultiFloorAssemblyError(
            "SNAP_DECLARATION_COUNT_MISMATCH",
            {
                "n_geometries": len(single_floor_geometries),
                "n_declarations": len(declarations),
            },
        )
    if len(single_floor_geometries) < 2:
        return tuple(single_floor_geometries), FootprintSnapAccount()

    out: list[CorrectedGeometryV3] = [single_floor_geometries[0]]
    reference = single_floor_geometries[0]
    ref_ring = _ring_points(reference.floors[0])
    ref_decl = declarations[0]
    records: list[FootprintSnapRecord] = []
    applied = False
    for index in range(1, len(single_floor_geometries)):
        geom = single_floor_geometries[index]
        decl = declarations[index]
        floor = geom.floors[0]
        upper_ring = _ring_points(floor)
        tolerance = footprint_snap_tolerance_m(ref_decl, decl)
        noise = math.hypot(ref_decl.bound_x_m + decl.bound_x_m,
                           ref_decl.bound_y_m + decl.bound_y_m)
        cap = min(ref_decl.cap_m, decl.cap_m)
        if _footprint_fingerprint(floor) == _footprint_fingerprint(
            reference.floors[0]
        ):
            records.append(FootprintSnapRecord(
                floor_index=index, floor_id=floor.id, input_id=decl.input_id,
                action="identical", hausdorff_upper_to_reference_m=0.0,
                hausdorff_reference_to_upper_m=0.0, tolerance_m=tolerance,
                noise_bound_m=noise, cap_m=cap,
                bbox_shift_m={"x_m": 0.0, "y_m": 0.0},
            ))
            out.append(geom)
            continue
        upper_to_ref = _directed_hausdorff_m(upper_ring, ref_ring)
        ref_to_upper = _directed_hausdorff_m(ref_ring, upper_ring)
        if max(upper_to_ref, ref_to_upper) > tolerance:
            records.append(FootprintSnapRecord(
                floor_index=index, floor_id=floor.id, input_id=decl.input_id,
                action="refused", hausdorff_upper_to_reference_m=upper_to_ref,
                hausdorff_reference_to_upper_m=ref_to_upper,
                tolerance_m=tolerance, noise_bound_m=noise, cap_m=cap,
                bbox_shift_m={"x_m": 0.0, "y_m": 0.0},
            ))
            out.append(geom)
            continue
        snapped_floor = floor.model_copy(update={
            "footprint": FootprintRing(
                vertices=[[x, y] for x, y in ref_ring]
            ),
        })
        snapped_geom = geom.model_copy(update={
            "floors": [snapped_floor],
            "footprint_x": [float(v) for v in reference.footprint_x],
            "footprint_y": [float(v) for v in reference.footprint_y],
        })
        applied = True
        records.append(FootprintSnapRecord(
            floor_index=index, floor_id=floor.id, input_id=decl.input_id,
            action="snapped", hausdorff_upper_to_reference_m=upper_to_ref,
            hausdorff_reference_to_upper_m=ref_to_upper,
            tolerance_m=tolerance, noise_bound_m=noise, cap_m=cap,
            bbox_shift_m={
                "x_m": abs(float(geom.footprint_x[0]) - float(reference.footprint_x[0])),
                "y_m": abs(float(geom.footprint_y[0]) - float(reference.footprint_y[0])),
            },
        ))
        out.append(snapped_geom)
    account = FootprintSnapAccount(applied=applied, records=tuple(records))
    return tuple(out), account


# ── W#6 (wallhunt 2026-09-08b / dispatch 2026-09-08c S-B): the cut-line-level
# cross-floor reconciliation that REPLACES the geometry-level verbatim ring
# swap inside the production wiring (and the writer replay). ───────────────── #
def cut_lines_from_sidecar(payload: dict) -> tuple:
    """Rebuild the chain's filed ``CutLineV1`` tuple from the sidecar dict."""
    from src.agent.correction.projection_bridge import CutLineV1

    return tuple(
        CutLineV1(
            axis=item["axis"],
            pos_m=float(item["pos_m"]),
            along_lo_m=float(item["along_lo_m"]),
            along_hi_m=float(item["along_hi_m"]),
            half_thickness_m=float(item["half_thickness_m"]),
            kind=item["kind"],
            origin_id=item["origin_id"],
        )
        for item in payload["lines"]
    )


def reconcile_floors_to_reference(
    per_floor_cut_lines: Sequence[tuple],
    per_floor_project: Sequence[dict],
    declarations: Sequence[PlanCalibrationDeclaration],
    *, preserve_connections: bool = False,
) -> tuple[tuple[CorrectedGeometryV3, ...], FootprintSnapAccount]:
    """W#6: reconcile the upper floors onto the reference floor, ON THE CUT
    LINES, and RE-PARTITION every floor from its (possibly aligned) lines.

    WHY this replaced ``snap_footprints_to_reference`` in the wiring (the
    measured root cause of W#6): the verbatim ring swap replaced floor 2's
    footprint ring with floor 1's while LEAVING floor 2's cells from its own
    partition — coverage conservation broke by 0.3806 m² against a 0.05 gate.
    Aligning the cut lines instead and re-partitioning keeps ring and cells
    from the SAME arrangement, so conservation survives BY CONSTRUCTION
    (measured on sm25: both floors at 0.000000 while the rings become
    bit-identical — which is what assembly's zero-tolerance compare needs).

    The alignment absorbs exactly the same quantity the old snap did — the
    cross-floor calibration residual, gated by the SAME fully-derived
    tolerance (``footprint_snap_tolerance_m``: noise limb from both
    products' declared calibration residuals, cap limb half the thinnest
    declared wall) — but as WALL POSITIONS, not as a ring transplant:

      * a wall line moves only onto the reference floor's NEAREST same-axis
        wall position, only within the tolerance (openings follow their
        host, in band);
      * a wall with no counterpart within the tolerance keeps its own
        position — genuinely different layouts are never touched, and a
        still-mismatched footprint after re-partitioning is the existing
        loud ``PER_FLOOR_FOOTPRINT_MISMATCH`` in assembly, unchanged;
      * the reference floor itself is re-partitioned verbatim from its own
        filed lines (the deterministic re-run of the chain's own
        projection, byte-identical to what the chain already produced).
    """
    from src.agent.correction.projection_bridge import (
        align_wall_lines_to_reference,
        preserve_endpoint_connections,
        project_cut_lines,
    )

    if len(per_floor_cut_lines) != len(per_floor_project) \
            or len(per_floor_cut_lines) != len(declarations):
        raise MultiFloorAssemblyError(
            "RECONCILE_INPUT_COUNT_MISMATCH",
            {
                "n_cut_lines": len(per_floor_cut_lines),
                "n_project": len(per_floor_project),
                "n_declarations": len(declarations),
            },
        )
    if not per_floor_cut_lines:
        raise MultiFloorAssemblyError(
            "RECONCILE_INPUT_EMPTY", {"reason": "no floors to reconcile"}
        )
    reference_lines = per_floor_cut_lines[0]
    ref_decl = declarations[0]
    out: list[CorrectedGeometryV3] = []
    records: list[FootprintSnapRecord] = []
    applied = False
    endpoint_records = []
    for index, (lines, project_kwargs, decl) in enumerate(
        zip(per_floor_cut_lines, per_floor_project, declarations)
    ):
        tolerance = footprint_snap_tolerance_m(ref_decl, decl)
        noise = math.hypot(ref_decl.bound_x_m + decl.bound_x_m,
                           ref_decl.bound_y_m + decl.bound_y_m)
        cap = min(ref_decl.cap_m, decl.cap_m)
        aligned, _align_records = (
            (lines, ())
            if index == 0
            else align_wall_lines_to_reference(
                lines, reference_lines, tolerance_m=tolerance
            )
        )
        if preserve_connections:
            aligned, _endpoint_records = preserve_endpoint_connections(lines, aligned)
            endpoint_records.extend({"floor_id": decl.input_id, **r} for r in _endpoint_records)
        floor_id = project_kwargs.get("floor_id") or decl.input_id
        moved = bool(_align_records)
        envelope = project_cut_lines(aligned, **project_kwargs)
        geom = envelope.geometry
        if index == 0:
            upper_to_ref = 0.0
            ref_to_upper = 0.0
            reference = geom
        else:
            ref_geom = out[0]
            upper_ring = _ring_points(geom.floors[0])
            ref_ring = _ring_points(ref_geom.floors[0])
            upper_to_ref = _directed_hausdorff_m(upper_ring, ref_ring)
            ref_to_upper = _directed_hausdorff_m(ref_ring, upper_ring)
        if moved:
            applied = True
        action = (
            "identical"
            if not moved and _footprint_fingerprint(geom.floors[0])
            == _footprint_fingerprint(reference.floors[0])
            else ("snapped" if moved else "refused")
        )
        records.append(FootprintSnapRecord(
            floor_index=index, floor_id=floor_id, input_id=decl.input_id,
            action=action, hausdorff_upper_to_reference_m=upper_to_ref,
            hausdorff_reference_to_upper_m=ref_to_upper,
            tolerance_m=tolerance, noise_bound_m=noise, cap_m=cap,
            bbox_shift_m={
                "x_m": abs(float(geom.footprint_x[0]) - float(reference.footprint_x[0])),
                "y_m": abs(float(geom.footprint_y[0]) - float(reference.footprint_y[0])),
            },
        ))
        out.append(geom)
    account = FootprintSnapAccount(applied=applied, records=tuple(records),
                                   endpoint_connections=tuple(endpoint_records))
    return tuple(out), account


def _footprint_fingerprint(floor: FloorV3):
    """A rotation/reflection-invariant fingerprint of one floor's footprint.

    ⭐ B-3 (dispatch §二 / §三): a VERBATIM copy of ``schema.py:_v3_integrity``'s
    per-floor identity fingerprint, so the explicit common-footprint pre-check
    below decides "footprints differ" EXACTLY as the schema would — never a
    looser or stricter mirror that would relabel a non-footprint error or miss a
    real one."""
    pts = [(float(x), float(y)) for x, y in floor.footprint.vertices]
    if pts and pts[0] == pts[-1]:
        pts.pop()
    forward = min(tuple(pts[i:] + pts[:i]) for i in range(len(pts)))
    rev = list(reversed(pts))
    backward = min(tuple(rev[i:] + rev[:i]) for i in range(len(rev)))
    return min(forward, backward)


def assemble_multifloor_geometry(
    ladder: ValidatedFloorLadder,
    single_floor_geometries: Sequence[CorrectedGeometryV3],
) -> CorrectedGeometryV3:
    """B2/T2+T3: stack N single-floor projections into one ``floors[]``.

    ⭐ B-2 (dispatch §〇③ + rework-3 §一(c)): the SOLE z-bearing input is
    ``ladder``, a SEALED :class:`ValidatedFloorLadder` minted only by
    :func:`derive_floor_ladder` (which runs the frozen-byte gate first).  A bare
    ``Sequence[_DerivedFloorLevel]`` — or anything else — is type-refused as
    ``UNSEALED_FLOOR_LADDER``, so no low-level-helper combination re-acquires
    production assembly capability.  ⭐ And this boundary does not TRUST the
    carrier's history either: the levels it consumes are RE-DERIVED through the
    carrier's read path (``tuple(ladder)`` -> gate + ``_byte_z`` resolution),
    ⛔ never read off stored instance state — so even an ``object.__new__``
    shell or a post-hoc-swapped artifact assembles only bytes that re-pass the
    gate in THIS call.  Each output floor's ``z_floor`` / ``ceiling_height``
    are re-stamped from that derived rung's BYTE-RESOLVED z, ⛔ never from
    whatever the incoming single-floor geometry carried.
    ``single_floor_geometries`` supplies only the XY (id/name/footprint/cells),
    one per storey, ground-up (``ladder[i]`` pairs with
    ``single_floor_geometries[i]``).
    Explicit doors/passages are refused until assembly can preserve their
    positions while restamping storey elevations; they must not disappear.

    Loud, never silent (T4):
      * ``ladder`` is not a ``ValidatedFloorLadder`` -> ``UNSEALED_FLOOR_LADDER``;
      * the carrier carries no sealed artifact (an ``object.__new__`` shell or
        a stripped instance) -> ``LADDER_CARRIER_CORRUPT`` (from the read path);
      * the carrier's artifact fails the re-run gate (e.g. a drifted ``z_m``)
        -> ``EvidenceContractError`` from ``_levels_of``, RAW, ⛔ never swallowed;
      * ``len(ladder) != len(single_floor_geometries)``
        -> ``FLOOR_PLAN_COUNT_MISMATCH`` (⛔ never a truncation);
      * a derived level with ``ceiling_height_m <= 0`` ->
        ``NONPOSITIVE_CEILING_HEIGHT`` (defense-in-depth: unreachable through
        the sealed surface, because ``_mint_ladder`` refuses non-ascending
        rungs — kept for a future assembler that stamps z from elsewhere);
      * a single-floor geometry not carrying exactly one floor
        -> ``EXPECTED_SINGLE_FLOOR_GEOMETRY``;
      * two storeys sharing a floor id -> ``DUPLICATE_FLOOR_ID`` (downstream
        cell ids are ``{floor_id}-cNNN``; a duplicate would collide cells).

    ⭐ B-3 (dispatch §二 / §三 / verdict B-3): the common-footprint invariant is
    checked by an EXPLICIT pre-construction comparison of every floor's footprint
    fingerprint (:func:`_footprint_fingerprint`, the schema's own algorithm).  A
    mismatch is raised by name as ``PER_FLOOR_FOOTPRINT_MISMATCH`` HERE, before
    the schema construction runs.  ⛔ The construction's ``ValidationError`` is
    NO LONGER inspected by ``loc``/``type`` or by any substring of ``str(exc)``:
    ``PER_FLOOR_FOOTPRINT_MISMATCH`` comes ONLY from this pre-check, so ANY schema
    error at construction (empty floor id, a future windows/segments rule, …)
    propagates RAW and can never be mislabeled as a footprint mismatch
    (acceptance #4, as a RULE — ⛔ not a list of exceptions).

    Then the stacked floors MUST pass the existing z-stack continuity check
    (``geometry_validator.check_zstack``, the same rule as
    ``pipeline.correction_draw_issues`` at pipeline.py:661): a break is
    ``Z_STACK_DISCONTINUITY``.  ⛔ The check is neither bypassed nor relaxed
    (T3) — it is called, and its "not ok" is raised.  (⚠️ By construction the
    stacked ladder is continuous; this guard therefore has teeth only against a
    future assembler that stamps z from some other source.  Its passing is a
    guardrail, ⛔ not an acceptance signal — see dispatch §三①.)

    ⭐ Localised assumption (invariant #6): assembly is COMMON-FOOTPRINT only —
    the current "共底面盒子 / 每层满铺楼板" simplification.  Per-floor DIFFERENT
    footprints (setback / 退台) are explicitly NOT this module's job (dispatch
    §四); the assumption is not 烤死-silent — a violation is the named
    ``PER_FLOOR_FOOTPRINT_MISMATCH`` above.
    """
    if not isinstance(ladder, ValidatedFloorLadder):
        raise MultiFloorAssemblyError(
            "UNSEALED_FLOOR_LADDER",
            {
                "got": type(ladder).__name__,
                "reason": (
                    "assembly accepts ONLY a ValidatedFloorLadder minted by "
                    "derive_floor_ladder (which runs the frozen-byte gate); a "
                    "detached level sequence has not passed the gate"
                ),
            },
        )
    # ⭐ rework-3: the levels are RE-DERIVED here (gate + byte resolution) via
    # the carrier's read path — an instance-carried z never reaches this loop.
    levels = tuple(ladder)
    if len(levels) != len(single_floor_geometries):
        raise MultiFloorAssemblyError(
            "FLOOR_PLAN_COUNT_MISMATCH",
            {
                "n_storeys_from_ladder": len(levels),
                "n_plan_products": len(single_floor_geometries),
            },
        )

    floors: list[FloorV3] = []
    seen_ids: dict[str, int] = {}
    xs_lo: list[float] = []
    xs_hi: list[float] = []
    ys_lo: list[float] = []
    ys_hi: list[float] = []
    for level, geom in zip(levels, single_floor_geometries):
        if geom.openings:
            raise MultiFloorAssemblyError(
                "WALL_OPENING_ASSEMBLY_UNSUPPORTED",
                {
                    "floor_index": level.floor_index,
                    "opening_ids": sorted(o.id for o in geom.openings),
                    "reason": "multi-floor assembly does not yet support source doors/open passages; refusing to silently discard them",
                },
            )
        if level.ceiling_height_m <= 0.0:
            raise MultiFloorAssemblyError(
                "NONPOSITIVE_CEILING_HEIGHT",
                {
                    "floor_index": level.floor_index,
                    "ceiling_height_m": level.ceiling_height_m,
                },
            )
        if len(geom.floors) != 1:
            raise MultiFloorAssemblyError(
                "EXPECTED_SINGLE_FLOOR_GEOMETRY",
                {"floor_index": level.floor_index, "n_floors": len(geom.floors)},
            )
        src = geom.floors[0]
        if src.id in seen_ids:
            raise MultiFloorAssemblyError(
                "DUPLICATE_FLOOR_ID",
                {
                    "floor_id": src.id,
                    "first_index": seen_ids[src.id],
                    "second_index": level.floor_index,
                },
            )
        seen_ids[src.id] = level.floor_index
        # ⭐ z is re-stamped from the DERIVED level (byte-resolved) — evidence is
        # the single source of truth for storey elevation; the incoming
        # geometry's own z_floor/ceiling_height are not trusted here.
        floors.append(
            src.model_copy(
                update={
                    "z_floor": float(level.z_floor_m),
                    "ceiling_height": float(level.ceiling_height_m),
                }
            )
        )
        xs_lo.append(float(geom.footprint_x[0]))
        xs_hi.append(float(geom.footprint_x[1]))
        ys_lo.append(float(geom.footprint_y[0]))
        ys_hi.append(float(geom.footprint_y[1]))

    # ⭐ B-3: EXPLICIT common-footprint pre-check (the schema's own fingerprint).
    # PER_FLOOR_FOOTPRINT_MISMATCH is raised ONLY here — so no schema
    # ValidationError at construction below can ever be mislabeled as footprint.
    if len({_footprint_fingerprint(floor) for floor in floors}) != 1:
        raise MultiFloorAssemblyError(
            "PER_FLOOR_FOOTPRINT_MISMATCH",
            {
                "floor_ids": [f.id for f in floors],
                "reason": (
                    "assembly is common-footprint only (invariant #6); "
                    "per-floor different footprints (setback) are not B2's "
                    "job — see dispatch §四"
                ),
            },
        )

    # ⛔ No try/except around the construction: any ValidationError (empty floor
    # id, etc.) propagates RAW — it is NEVER relabeled as footprint (B-3).
    assembled = CorrectedGeometryV3(
        schema_version="3",
        footprint_x=[min(xs_lo), max(xs_hi)],
        footprint_y=[min(ys_lo), max(ys_hi)],
        floors=floors,
        windows=[],
        facade_segments=[],
    )

    zstack = check_zstack(assembled)
    if not zstack.ok:
        raise MultiFloorAssemblyError(
            "Z_STACK_DISCONTINUITY", dict(zstack.evidence or {})
        )
    return assembled


__all__ = [
    "MultiFloorAssemblyError",
    "ValidatedFloorLadder",
    "FootprintSnapAccount",
    "FootprintSnapRecord",
    "PlanCalibrationDeclaration",
    "assemble_multifloor_geometry",
    "derive_floor_ladder",
    "footprint_snap_tolerance_m",
    "read_plan_calibration_declaration",
    "snap_footprints_to_reference",
]
