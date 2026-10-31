"""The as-drawn plan **producer's own types** (②-2 module 1, 2026-08-30).

Why this file exists
--------------------
``vector_contract.py`` recognised this contract by a KEY LIST:

    ``_is_declared(raw, "as_drawn_plan_v2") and _has_keys(raw, "observations",
    "declarations", "hypotheses")``

⇒ a file that declared the right string and carried the three top-level keys was
"recognised" **no matter what was inside them**.  Measured on the real
``out/sm25_2f_v2.json``: **15** separate element-level corruptions -- a bucket
value turned into a dict or into a number, a bucket turned into a list, an
invented sibling bucket, a face line without an ``id``, ``runs_px`` turned into
a string, an image axis replaced by a world axis, a pair without ``face_b``, a
spacing arriving as text, ``pairs`` turned into a dict, an opening candidate
without ``span_m``, ``opening_types`` values turned into dicts, an ink profile
without ``span_ratio``, an interval carrying three numbers, an interval carrying
one -- were **all** classified ``as_drawn_plan / KNOWN_NOT_CONSUMED``, i.e.
accepted as well-formed products.  ⭐ That premise is
not folklore: ``tests/test_o22m1_as_drawn_producer_types.py`` re-states the old
rule and asserts it said yes to each of the 15 before asserting the new rule
says no.  That is ⭐ ``vector_contract``'s own
discipline #3 -- "every contract is recognized by its producer's own TYPE, never
by a key list induced from existing artifacts" -- unimplemented for the one
contract whose producer lives in this package.

⛔ This module adds **no** geometry and **no** judgement.  ``assemble()`` builds
exactly the dict it built before and this type only reads it; the three tracked
products re-build byte-for-byte identical (sha256 recorded in the execution
report).

Scope of this version (⭐ deliberately partial, and the partiality is declared)
------------------------------------------------------------------------------
The cross-family verdict trimmed module 1 to the **wall + opening families**:
``face_lines`` / ``pairs`` / ``pair_candidates`` / the face-disposition buckets /
``opening_candidates`` / ``opening_types``.  Everything else (``ledger`` /
``roles`` / calibration / palette / declarations) is **deferred, not silently
allowed** -- every deferred channel is an explicitly declared field, and
``DEFERRED_CHANNELS`` below is the machine-readable roster of them.  A test locks
the roster against the model, so "what is not checked yet" can never drift into
prose.

⭐ How many face-disposition buckets are there?  **Four.**
------------------------------------------------------------------------------
The verdict said "五桶" without enumerating them.  Counted three ways in the
tree on 2026-08-30, the answer is four:

  1. ``as_drawn_v2.select_pairs`` (the producer of the completeness invariant)
     reads exactly four declared buckets -- ``non_wall_face_lines``,
     ``unpaired_wall_faces``, ``solid_band_walls``, ``ambiguous_face_lines``.
  2. ``validator/checks/as_drawn.py:576-583`` names the same four, and no other.
     ⛔⛔ The repo-root prefix is missing ON PURPOSE and ⛔ must not be added
     back.  With it, this one sentence became the ONLY dependency edge into that
     module -- ``affected_tests.build_edges`` reads EVERY string constant,
     docstrings included, and builds a real edge from any repo-relative path it
     finds inside one -- so 166 test files "reached" a module no test exercises
     and its honest ``uncovered_allowlist`` entry turned into a lie.  The
     authority suite caught it (2026-08-30, ②-2 module 1).
     ⚠️ Note that this warning cannot spell the string it is warning about
     without re-creating the very edge -- which is why the rule is stated as
     "⛔ never prefix a cited production path here", ⛔ not as an example.
  3. All three tracked products (``sm25_1f`` / ``sm25_2f`` / ``sm24_1f``) carry
     exactly those four keys under ``hypotheses``.

The FIFTH accounting slot is real but is **not a bucket**: a face line may also
be accounted for by being half of a selected ``pairs`` entry.  ``select_pairs``
unions it with the four (``accounted = {faces of pairs} | non_wall | lone | band
| ambiguous``), and ``judge/as_drawn/reading_grade.py:121`` does the same when it
splits face lines into claimed/abstained.  So the disposition space has five
members, four of which are ``dict[face_id, reason]`` buckets and the fifth of
which is ``pairs`` -- ⭐ and the verdict's own trim list already names ``pairs``
separately, so counting it again as a "bucket" would double-count.  ⛔ There is
no fifth dict, and one is not invented here to reach five.

⚠️ A known gap this version does NOT close (N-1, cross-family, 2026-08-30)
------------------------------------------------------------------------------
Every bucket value is **prose** (``dict[face_id, str]``) and is typed as prose
here on purpose: the verdict's instruction is to receive today's product
faithfully and ⛔ not rewrite history while modelling it.  The consequence is
visible in a real product: ``sm25_2f`` ``unpaired_wall_faces.L012`` says, in
prose, *"The ink is there -- column 655 carries 170 px over rows 1080-1249 --
and the reader dropped it"*.  That is the sixth ``counterface_state``
(``ink_present_unpromoted``): counterface ink present, never promoted to a face
line.  Structurally it is indistinguishable here from any other lone-face
reason, because the only carrier is a sentence.  ⛔ This module does not invent a
structured slot for it -- an unexercised union member with zero real instances is
a place for a defect to hide, and the state belongs to module 2's
``EvidenceContract``.  What this module does instead is **pin the instance**
(``tests/test_o22m1_as_drawn_producer_types.py``) so the day a producer starts
emitting structure there, this type has to be changed on purpose rather than
absorbing it silently.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.reading.as_drawn._plan_ink import Axis

Interval = Annotated[list[float], Field(min_length=2, max_length=2)]
"""A ``[lo, hi]`` pair in metres. ⭐ The length is part of the shape: a
three-element "interval" is a malformed element, ⛔ not a longer interval.
⚠️ This is the LENGTH only -- that ``hi > lo``, that a run's coverage list is as
long as its run list, and every other relation between the numbers is the job of
the eleven gates in ``validator/checks/as_drawn.py``, ⛔ not of a type."""

PixelInterval = Annotated[list[int], Field(min_length=2, max_length=2)]
"""The same pair in pixel coordinates."""

SCHEMA = "as_drawn_plan_v2"
"""⭐ The ONE definition of the declared contract value.

``as_drawn_v2.SCHEMA`` re-exports this name, and ``vector_contract`` imports it
from there; a literal in either place would be a second definition that stops
matching the day this one changes.
"""

DEFERRED_CHANNELS: tuple[str, ...] = (
    "image",
    "image_label",
    "observations.calibration",
    "observations.ink_palette",
    "observations.components_by_family",
    "observations.dimension_witnesses",
    "declarations",
    "hypotheses.family_roles",
    "hypotheses.opening_candidates_basis",
    "hypotheses.pair_candidates_basis",
    "hypotheses.perception_source",
    "hypotheses.note",
    "ledger",
)
"""⭐ Every channel this version declares but does NOT check, by dotted path.

⛔ Not an ``extra="allow"`` shrug: each of these is a real, named field on the
model below, typed ``Any`` with the reason in its ``description``.  The roster
and the model are cross-checked by a test, so the two cannot drift apart, and
"what is still unvalidated" is answerable by reading one tuple.

Why each is deferred: the cross-family trim for module 1 is the wall + opening
families only.  The verdict directs ``ledger`` / ``roles`` to plan.md as later
work (⚠️ as of this commit that plan.md entry is not written yet -- this tuple
is the only place the deferral is recorded, which is exactly why it is a
constant and not a sentence); ``declarations`` is transcription (compared
verbatim by text, never by meaning); calibration / palette / components /
witnesses are the measurement substrate the eleven
``validator/checks/as_drawn.py`` gates already recompute.
"""


class _Node(BaseModel):
    """Base for every in-scope node: unknown keys and coercions are LOUD.

    ⭐ ``extra="forbid"`` is half the teeth.  Without it a bucket element could
    grow a field nobody typed and travel downstream unnoticed, which is the
    shape of every defect this module was written to make visible.

    ⭐ ``strict=True`` is the other half, and it was added after measuring the
    hole: in pydantic's default lax mode a pair whose ``spacing_m`` had been
    turned into the STRING ``"0.24"`` was still accepted -- coerced back to a
    float and waved through, which is precisely "the gate measured the right
    thing, but the carrier got swapped".  A number that arrives as text is a
    producer defect, ⛔ not a formatting preference.  Measured cost of strict on
    real data: zero -- all three tracked products and every honest historical
    variant validate identically under both modes; only forged/mutated products
    change verdict, and every one of those changes in the direction of refusal.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


# ── observations: the measured layer ──────────────────────────────────────── #
class InkProfileV2(_Node):
    """One ink family's distance profile across one blank stretch of a line.

    ``nearest_px`` is ``None`` when that family has no ink within the widest bin
    -- ⭐ a measured absence, and the one field here allowed to be null.
    """

    on_line: int
    by_distance_px: dict[str, int]
    """Ring counts keyed by the bin distance as a STRING (JSON object keys).
    Empty for a zero-length stretch; ⛔ the bin set itself is the producer's
    ``PROFILE_BINS_PX`` and is not re-declared here."""
    span_ratio: float
    nearest_px: int | None


class GapV2(_Node):
    """A maximal blank stretch between two runs of one face line.

    ⛔ Carries measurements only.  There is deliberately no ``class`` field:
    naming a gap is 1_correction's job, and v1's threshold-in-the-scorer is the
    defect this schema was built to prevent.
    """

    lo_px: int
    hi_px: int
    len_px: int
    ink_by_family: dict[str, InkProfileV2]
    span_m: Interval
    len_m: float


class FaceLineV2(_Node):
    """One traced face line: where it is, what is inked, what is blank."""

    id: str
    axis: Axis
    """Image axis the line runs along, ⭐ imported from ``_plan_ink`` rather than
    re-spelled here -- a second copy of the two words is a second definition
    that stops matching the day one of them grows a third value.
    ⚠️ NOT a world axis -- see
    ``constant_world_axis`` for that ([[observation-named-as-fact-travels-as-fact]]:
    a row-axis line's ``pos_m`` is a *y*, and reading it as an *x* has already
    cost this project one wrong diagnosis)."""
    constant_world_axis: Literal["x", "y"]
    pos_px: float
    pos_m: float
    support_cols_px: PixelInterval
    edges_m: Interval
    support_width_m: float
    runs_px: list[PixelInterval]
    runs_m: list[Interval]
    gaps: list[GapV2]
    ink_coverage_per_run: list[float]
    covered_px: int
    support_px: int


class ObservationsV2(_Node):
    """⭐ The only scorable layer: what a ruler measured on pixels.

    Only ``face_lines`` is in module 1's trim.  The other four members are the
    measurement substrate and are declared-but-unchecked (``DEFERRED_CHANNELS``).
    """

    face_lines: list[FaceLineV2]
    """⭐ REQUIRED, no default (NF-1, 2026-09-01).  The key must be present; an
    empty list is still legal (a honest reading of a blank image produces it).
    ⛔ The producer's ``assemble()`` writes ``face_lines`` unconditionally
    (``as_drawn_v2.py``), so a product that omits the key entirely could not have
    come from the producer -- it is hand-made or corrupt, and this schema makes
    that a loud unknown instead of a recognised ``as_drawn_plan``.  ⛔ Do NOT put
    a ``default`` back: "量到零" (measured zero) is scored by grade/zero-wall, ⛔
    not by pretending the key was there."""
    calibration: Any = Field(default=None, description="deferred: module 1 trim")
    ink_palette: Any = Field(default=None, description="deferred: module 1 trim")
    components_by_family: Any = Field(default=None, description="deferred: module 1 trim")
    dimension_witnesses: Any = Field(default=None, description="deferred: module 1 trim")


# ── hypotheses: the derived layer ─────────────────────────────────────────── #
class PairCandidateV2(_Node):
    """Every admissible same-axis partner for a face line.

    ⛔ No spacing threshold decides membership; ``matched_declared_mm`` is a
    LABEL taken from the drawing's callouts, never a measurement.
    """

    face_a: str
    face_b: str
    spacing_px: float
    spacing_m: float
    matched_declared_mm: list[int]
    overlap_px: int


class PairV2(PairCandidateV2):
    """A candidate that perception SELECTED. ⭐ The only added field is
    ``source`` -- the pair itself is the candidate verbatim, which is what makes
    "this pair was invented, not chosen" checkable at all."""

    source: str


class OpeningCandidateV2(_Node):
    """One blank stretch offered to perception, with every family's ink on it.

    ⛔ No classification and ⛔ no threshold: whether this is an opening is the
    model's call, and the evidence to disagree travels with it.
    """

    id: str
    face_line: str
    gap_index: int
    span_m: Interval
    len_m: float
    len_px: int
    ink_by_family: dict[str, InkProfileV2]


class HypothesesV2(_Node):
    """⛔ Not scored, droppable in one piece.

    ⭐ The four ``*_face_lines`` / ``*_wall_faces`` / ``*_walls`` members are the
    face-disposition buckets (see module docstring: there are **four**, and
    ``pairs`` is the fifth accounting slot rather than a fifth bucket).  Each is
    ``dict[face_id, reason]`` where the reason is free prose supplied by
    perception -- ⛔ typed as prose on purpose; see the N-1 note above.
    """

    pairs: list[PairV2] | None = None
    """``None`` when perception supplied no selection at all
    (``pairs_status == "ABSENT_NO_MODEL_SELECTION"``) -- ⭐ distinct from an
    empty list, which would claim a selection was made and chose nothing."""
    pair_candidates: list[PairCandidateV2] = Field(default_factory=list)
    opening_candidates: list[OpeningCandidateV2] = Field(default_factory=list)
    opening_types: dict[str, str] | None = None
    opening_types_source: str | None = None
    pairs_status: str | None = None
    pairs_note: str | None = None

    non_wall_face_lines: dict[str, str] = Field(default_factory=dict)
    unpaired_wall_faces: dict[str, str] = Field(default_factory=dict)
    solid_band_walls: dict[str, str] = Field(default_factory=dict)
    ambiguous_face_lines: dict[str, str] = Field(default_factory=dict)

    family_roles: Any = Field(default=None, description="deferred: module 1 trim")
    perception_source: Any = Field(default=None, description="deferred: module 1 trim")
    opening_candidates_basis: Any = Field(default=None, description="deferred: module 1 trim")
    pair_candidates_basis: Any = Field(default=None, description="deferred: module 1 trim")
    note: Any = Field(default=None, description="deferred: module 1 trim")


FACE_DISPOSITION_BUCKETS: tuple[str, ...] = (
    "non_wall_face_lines",
    "unpaired_wall_faces",
    "solid_band_walls",
    "ambiguous_face_lines",
)
"""The four buckets, in the order ``select_pairs`` unions them. ⭐ Consumers that
need the full disposition space must add ``pairs`` themselves -- it is named
separately because it is a different shape, not because it is optional."""


class AsDrawnPlanV2(BaseModel):
    """The whole product. ⭐ Deliberately ``extra="allow"`` at THIS level only.

    ⛔ Not a shrug -- forbidding extras here would be a live regression.
    ``vector_contract`` reports a file that declares this contract AND satisfies
    it AND also matches legacy ``ReadingView`` structure as **AMBIGUOUS**
    (``test_r5_complete_declaration_plus_legacy_is_still_ambiguous``).  Such a
    hybrid carries a stray ``strokes`` key; if this model rejected it, the file
    would drop to a single legacy match and be **consumed** -- F-97 reopened.
    So the envelope stays open and every node below it is ``extra="forbid"``.

    The three layers are REQUIRED (a file with only ``observations`` is a
    malformed declaration, which ``vector_contract``'s BLK-A rule turns into a
    loud unknown).  ⭐ NF-1 (2026-09-01): ``observations.face_lines`` is now also
    required (no default), so the historical "declared skeleton"
    ``{observations: {}, declarations: {}, hypotheses: {}}`` **no longer
    validates** -- the producer emits ``face_lines`` unconditionally, so an empty
    skeleton can only be hand-made or corrupt and is turned into a loud unknown.
    ⚠️ That teeth is on the PRESENCE of the ``face_lines`` key, ⛔ not on
    emptiness: ``face_lines: []`` (a honest reading of a blank image) still
    validates and still routes to ``as_drawn_plan``; whether zero measured walls
    is right is grade/zero-wall's job, ⛔ never a structural refusal here.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, strict=True)

    declared_schema: str = Field(alias="schema")
    observations: ObservationsV2
    declarations: Any = Field(description="deferred: transcription, compared verbatim")
    hypotheses: HypothesesV2

    image: Any = Field(default=None, description="deferred: module 1 trim")
    image_label: Any = Field(default=None, description="deferred: module 1 trim")
    ledger: Any = Field(default=None,
                        description="deferred: verdict directs it to plan.md")

    @field_validator("declared_schema")
    @classmethod
    def _is_this_contract(cls, value: str) -> str:
        if value != SCHEMA:
            raise ValueError(f"declares schema={value!r}, not {SCHEMA!r}")
        return value


def validate_as_drawn_plan(doc: dict) -> dict:
    """Validate ``doc`` against this package's own type and return **it**.

    ⭐ Returns the SAME object, ⛔ never ``model_dump()``.  A dump round-trip
    would re-order keys, coerce numerics and drop the extras the envelope
    deliberately allows -- i.e. it would quietly rewrite the product while
    claiming to check it.  Byte-identity of the three tracked artifacts is an
    acceptance condition of this dispatch, and returning the original is the
    only way to make it structurally true rather than tested-and-hoped.

    Raises ``pydantic.ValidationError`` naming the offending path.
    """
    AsDrawnPlanV2.model_validate(doc)
    return doc
