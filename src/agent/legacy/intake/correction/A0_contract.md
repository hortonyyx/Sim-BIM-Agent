# A0 — Correction contract (tolerances, evidence, audit, validation)

The shared contract every correction document (`A1`–`A4`) consumes and writes
against. A0 defines: the evidence model, the audit event taxonomy and schemas,
the tolerance registry, method profiles, the upstream input contract, and the
validation schema. A0 holds no transform rules of its own.

```
Consumes:        nothing (contract only)
Produces:        the schemas + named constants the other docs reference
Emit corrections[] when: n/a (A0 defines the schema; A1–A4 emit)
Emit conflicts[] / unsupported when: n/a
May change topology: no
```

---

## 1. Evidence model

Every primitive entering the correction layer is a typed **evidence item**, not
a bare number. Authority is resolved **per claim type**, not by one global ladder.

### 1.1 Evidence item schema

```
id               unique id
claim_type       numeric | topology_identity | semantic   (see §1.4)
grade            see §1.2
confidence       high | medium | low   (decoupled from grade)
source_kind      dimension | stroke | label | facade_window | self_check | inferred
source_ids[]     perception ids backing this item (compound for a dim chain)
floor_id
entity_id        the primitive it describes (axis / vertex / cell / window / ...)
coordinate_frame world | facade_local | plan_local
unit             m | mm | ratio | none
span             optional source bbox / extent
```

### 1.2 Evidence grades (authority, high → low)

| grade | meaning |
|---|---|
| `direct_measurement` | a value explicitly dimensioned/labelled and read directly |
| `transcribed_dimension` | derived by accumulating a dimension chain |
| `estimated_stroke` | a coordinate read off a drawn stroke, not separately dimensioned |
| `inferred_topology` | implied by enclosure/adjacency (e.g. a shared wall is one line) |
| `prior` | a commonsense/standard value from `A4`, fallback only |
| `unknown` | absent; must be completed (A3) or flagged |

Grade ranking is **claim-type-scoped** (§1.4): `inferred_topology` outranks
`estimated_stroke` for identity claims, but not for coordinate claims.

### 1.3 Confidence

`confidence ∈ {high, medium, low}`, independent of grade (a
`transcribed_dimension` can be low confidence if a chain segment was occluded).

### 1.4 Claim types and per-claim authority ladders

| claim_type | the question | authority ladder (high → low) |
|---|---|---|
| `numeric` | "what is this coordinate / length?" | `direct_measurement` > `transcribed_dimension` > `estimated_stroke` > `prior` |
| `topology_identity` | "are these the same intended wall/axis? does this cell close? is this a shared boundary?" | direct annotation / dimensioned boundary > `inferred_topology` (consistent enclosure/adjacency) > `estimated_stroke` (coordinate proximity) > `prior` |
| `semantic` | "what is this space / role?" | label / OCR > repeated layout pattern > `prior` |

Within a ladder, confidence is the tie-breaker. A genuine in-grade, in-confidence
tie is **not** silently split — it becomes a `conflict`. In particular, A2 must
**not** merge two axes on coordinate proximity alone (a `numeric` argument) when
`topology_identity` evidence says they are distinct.

Authority resolution compares structured evidence items by the tuple
`(claim_type, grade, source_kind, confidence)`, in that precedence.

---

## 2. Audit taxonomy

Four mutually exclusive event kinds. Only the last three are reportable.

| kind | definition | logged? |
|---|---|---|
| `normalization` | formatting / final-coordinate rounding within `OUTPUT_PRECISION`; no change to source value, topology, or authority | no |
| `correction` | changed a source value, closed a gap, snapped an axis, chose one evidence channel over another, or invoked a prior | **yes — `corrections[]`** |
| `conflict` | unresolved or over-threshold ambiguity | **yes — `conflicts[]`** |
| `unsupported` | cannot be safely corrected under the current regime | **yes — `unsupported[]`** |

### 2.1 Hard logging rule

If a transformation changes geometry beyond `OUTPUT_PRECISION`, changes topology,
changes evidence authority, or invokes a prior, it **must** produce a
`corrections[]` entry with `source_ids` + `rule_id`. If it cannot be logged that
way, emit `unsupported` instead of proceeding silently.

### 2.2 Audit envelope (every entry carries)

```
id
stage            A1 | A2-detect | A3-resolve | A2-apply
method_profile   room_identity | use_grouped_rooms | perimeter_core
entity_type      axis | vertex | edge | cell | surface | window | zone_input
entity_id
floor_id
parent_id        optional
coordinate_frame world | facade_local | plan_local
unit
```

### 2.3 `corrections[]` entry (envelope +)

```
rule_id
claim_type
conflict_type       one of §3, or null for a plain regularization
source_ids[]
original_value
resolved_value
value_type          scalar | point | line | polygon | ratio | area | facade_extent
tolerance_name      a name from §4
tolerance_value     the applied numeric value + unit
delta               resolved − original
evidence_grade      the grade chosen as authoritative
confidence_before
confidence_after
changes_topology    bool
prior_id            set only if a prior was invoked (else null)
note
```

### 2.4 `conflicts[]` entry (envelope +)

```
conflict_type       one of §3
claim_type
candidates[]        each: {value, source_ids[], evidence_grade, confidence}
reason_unresolved
fallback_action     what was emitted instead (kept stroke / marked unsupported / ...)
```

### 2.5 `unsupported[]` entry (envelope +)

```
reason
regime_assumption_violated   which baseline assumption broke (§6)
```

---

## 3. Conflict types

```
stroke_vs_dimension            stroke coordinate disagrees with its dimension chain (numeric)
cross_floor_axis_jitter        same intended axis differs across floors by a numeric amount (numeric)
checksum_failure               inner segments do not sum to the outer total
facade_plan_mismatch           an elevation and the plan disagree on a position/extent
semantic_size_prior            a measured size is implausible vs a prior, with a semantic label in play
unsupported_geometry           a feature outside the current regime (§6)
reference_or_identity_ambiguity "what object is this?" — frame/origin/local-to-world conflict,
                               unknown wall side or missing thickness for centerline conversion,
                               same-vs-different wall/axis where proximity and topology/semantic
                               evidence disagree (the higher-level type; cross_floor_axis_jitter
                               is its numeric subtype)
```

---

## 4. Tolerance registry

Named constants the other documents reference **by name**. Coordinates use an
**absolute grid**; areas and ratios use **relative error** — never mix. A rule
may consume a constant only if its `status` is `calibrated` or
`provisional`(with basis); a `disabled` constant forces the consuming path to
emit `unsupported`.

| name | value | unit | status | profiles | hard/warn | basis |
|---|---|---|---|---|---|---|
| `OUTPUT_PRECISION` | 10 | mm | calibrated | all | format | M/10 submodule (`GB/T 50002-2013` 3.1.2) |
| `SNAP_GRID` | 10 | mm | calibrated | all | transform | final structural regularization grid; preserves dimensioned wall-centerline truth such as 0.12 m half-thickness offsets |
| `MIN_EDGE_LENGTH` | 0.10 | m | calibrated | all | hard_fail | EP very-small-vertex warning (~0.01 m) + sliver safety gate; below → merge/re-snap/unsupported |
| `DIMCHAIN_CLOSE_TOL` | 10 | mm | calibrated | all | close / conflict | `\|Σsegments − total\|`; = M/10 |
| `GAP_CLOSE_THRESHOLD` | ≤300 | mm | calibrated | all | auto-close (connectivity) | a thermal zone needs a closed enclosure (an unclosed gap forms no EP zone); intentional sub-300mm gaps are vanishingly rare and must close for BEM anyway. The fail-to-close cost (no zone) dominates the wrong-close cost at this size, so the threshold sits well above the wall-thickness series (`GB/T 50002-2013` 4.3.2) |
| `GAP_CONFLICT_BAND` | 300–1000 | mm | calibrated | all | escalate → A3 | doorway/opening scale: the wall LINE still closes (door = sub-surface, zone boundary continuous), but whether a real opening means one merged space vs two needs judgment |
| `GAP_UNSUPPORTED` | ≥1000 | mm | calibrated | all | unsupported / A3 / zonification | open-boundary scale; likely a real open-plan edge or void — do not silently wall it; the one-zone-vs-two call is zonification, not gap-closing |
| `AXIS_JITTER_TOL` | 50 | mm | calibrated | all | same-axis only with identity evidence | clustering tolerance, intentionally coarser than `SNAP_GRID`; beyond, or if topology says distinct → `reference_or_identity_ambiguity` → A3 |
| `COVERAGE_AREA_TOL` | 0.05 | m² | calibrated | all | **BLOCK** when a floor's cells-union area differs from that floor's authoritative footprint ring by more than this; also BLOCK overlap, hole, or outside residual above it | B3 coverage gate v2. This is an area-only tolerance, independent of `MIN_EDGE_LENGTH`; preserves the prior `correction.coverage` 0.05m² threshold while making the union-vs-footprint conservation ledger explicit. Polygon area is computed from the actual orthogonal ring, never a bbox approximation. |
| `ENVELOPE_AXIS_ATTACH_TOL` | 0.010 | m | provisional | orthogonal_polygon | hard match | canonical output coordinate identity; recognizes only shared-axis attachment, never shortest edge or coverage |
| `ENVELOPE_ENDPOINT_MATCH_TOL` | 0.050 | m | provisional | orthogonal_polygon | evidence match/conflict | elevation `wing_break` endpoint to exactly one footprint shared axis |
| `ENVELOPE_CANDIDATE_AGREEMENT_TOL` | 0.050 | m | calibrated | all | evidence agreement/conflict | named replacement for the legacy envelope candidate/value agreement threshold |
| `AREA_REL_TOL` | ±5 | % | calibrated | all | warn / accept | BEM QA; `GB 50189-2015` 3.4.3 |
| `WWR_REL_TOL` | ±5% or ±0.02 | ratio | calibrated | all | warn / accept | `GB 50189-2015` 3.2.2 / 3.3.1 |
| `ENVELOPE_RECONCILE_TOL` | 0.30 | m | calibrated | all | auto-reconcile / unsupported | facade outer-envelope bounds may override a wall-centerline footprint only within wall-thickness scale; the same value is the boundary-attach tolerance for moving old-perimeter cell edges |
| `FACADE_FRAME_CROSS_CHECK_TOL` | 0.30 | m | calibrated | all | flag | gate① cross-check: deterministic `derive_facade_frame` placement from reading elevation local-x vs correction LLM window world span; wall-thickness/envelope-basis scale, never a blocking transform tolerance |
| `FACADE_VISIBILITY_DEPTH_EPSILON` | `1e-9` | m | provisional | orthogonal_polygon/v3 | INVARIANT tie/negative-depth guard | Vg (C2 §E1') same-depth-atom tie and negative-depth degeneracy guard; absorbs only IEEE-754 arithmetic noise, seven orders of magnitude below `SNAP_GRID` — not a physical resolution and never reused as one |
| `FACADE_VISIBILITY_ENDPOINT_EPSILON` | `1e-9` | m | provisional | orthogonal_polygon/v3 | INVARIANT short-edge/near-endpoint guard | Vg half-open 1D-skyline sweep's numeric topology gate: rejects degenerate short edges and near-collision along-axis events; never snaps or bridges a real gap |
| `WINDOW_SEGMENT_ENDPOINT_CLAMP_TOL` | `0.010` | m | provisional | B5/v3 | hard clamp bound | Only after unique facade-segment selection, caps physical endpoint return to that segment; never selects a segment or crosses a room seam. |
| `WINDOW_HOST_SPAN_EPSILON` | `1e-9` | m | provisional | B5/v3 | INVARIANT numeric guard | Positive-width, containment, half-open endpoint, and coverage residual arithmetic only; never a physical clamp or visibility replacement. |
| `WINDOW_HOST_PLANE_EPSILON` | `1e-9` | m | provisional | B5/v3 | INVARIANT numeric guard | Room-edge/segment/parent-wall collinearity and vertex-on-plane arithmetic only; never angle or wall-thickness tolerance. |
| `GT_DXF_NODE_JOIN_TOLERANCE` | `0.001` | m | provisional | gt-v3/C2 | hard snap-or-block | judge② tooling only; DXF endpoint numeric split clustering before polygonize, component diameter over limit blocks |
| `GT_DXF_AXIS_ALIGNMENT_TOLERANCE` | `0.001` | m | provisional | gt-v3/C2 | hard project-or-block | judge② tooling only; near-horizontal/vertical export noise projects only to a uniquely closer axis |
| `GT_DXF_TOPOLOGY_AREA_TOLERANCE` | `0.000001` | m² | provisional | gt-v3/C2 | hard topology | judge② tooling only; polygonize sliver and zone-union area residual, never geometry repair |
| `GT_OPENING_BOUNDARY_MAX_DISTANCE` | `0.400` | m | provisional | gt-v3/C2 | hard candidate gate | judge② tooling only; legal opening-boundary candidate gate, not a tie breaker |
| `GT_OPENING_ASSIGNMENT_TIE_EPSILON` | `1e-9` | m | provisional | gt-v3/C2 | INVARIANT | judge② tooling only; equal nearest legal opening-segment solutions reject |
| `GT_ELEVATION_MATCH_MAX_DISTANCE` | `0.400` | m | provisional | gt-v3/C2 | hard candidate gate | judge② tooling only; plan/elevation along-endpoint candidate gate, pending real-source calibration |
| `GT_ELEVATION_MATCH_TIE_EPSILON` | `1e-9` | m | provisional | gt-v3/C2 | INVARIANT | judge② tooling only; equal global elevation matching solutions reject |
| `PERIMETER_DEPTH` | 4.6 (range 2.4–6.1) | m | calibrated | — (downstream zoning, **not** PartA) | n/a | `ASHRAE 90.1-2019 Add. ag`; listed for reference, PartA rules must not consume it |

`WorldInterval` (Vg/Va) is always the half-open interval `[lo, hi)`; a shared
right endpoint between two segments is a touch, not an overlap, and forms no
positive-width visible span. A same-depth tie inside one along-axis atom is
always `INVARIANT` (no id/ring-order/sort tiebreak). Both `FACADE_VISIBILITY_*`
epsilons are provisional pending future cross-case numeric probes; they must
not be recalibrated ad hoc against a single case's output.

### 4.1 Va applicability contract

`FACADE_APPLICABILITY_SCHEMA_VERSION = "1"` and
`FACADE_APPLICABILITY_HELPER_VERSION = "facade_applicability_v1"` are owned by
Va. Va consumes the existing `CLAIMS_VOCAB_VERSION = "1"` and emits exactly the
seven claims in its fixed wire order. Its intervals are always half-open
`[lo, hi)` with exact endpoint algebra and no new tolerance/config input.

`FacadeVisibilityLedgerV1.facade_segments_sha256` is SHA-256 over compact,
UTF-8, `sort_keys=true` JSON of a JSON array. Each array member is the complete
`FacadeSegment.model_dump(mode="json")` object, including `id`, `floor_id`,
`facade_family`, `p1`, `p2`, `outward_normal`, `world_along_interval`, `depth`,
`visible_intervals` (the complete ordered interval list), and
`source_footprint_fingerprint`; no field is omitted or projected. Members are
sorted by `(floor_id, family_rank, world_along_interval.lo,
world_along_interval.hi, depth, id)`, where family rank is North=0, South=1,
East=2, West=3. JSON uses separators `(',', ':')` and `ensure_ascii=false`.
This is the frozen v1 preimage for accepted-correction and judge adapters.

### 4.1.1 B4b Phase A score-input registrations

The judge-only score contract is versioned independently of correction output:
`SCORER_SCHEMA="8"`, `JUDGE_SCORE_CONFIG_SCHEMA="1"`,
`JUDGE_SCORE_BINDINGS_SCHEMA="1"`, and
`JUDGE_COMPLETENESS_OVERLAY_SCHEMA="1"`.  Its only C2 tolerance profile is
`src/configs/judge_score.yaml`, canonicalized as sorted-key compact JSON,
UTF-8, `ensure_ascii=false`, then lower-case SHA-256.  The registered v1 values
are `plan_axis_alignment_tol_m=0.05`, `plan_position_tol_m=0.30`,
`plan_extent_tol_m=0.30`, `claim_complete_epsilon_m=0.05`,
`opening_match_center_tol_m=0.40`, `opening_assignment_tie_epsilon=1e-9`,
`along_claim_tol_m=0.40`, `width_claim_tol_m=0.40`, `sill_claim_tol_m=0.30`,
`head_claim_tol_m=0.30`, and `floor_line_tol_m=0.30`.  Complete epsilon is no
greater than every claim tolerance and tie epsilon is below every geometric
tolerance; no correction config, environment value, or grade config may fill a
missing score value.  The frozen v1 profile hash is
`ac2c14705bbfc285b489f7eeb593baf712cdc46de57a5457317103f36a3c4a06`.

`view_projection_binding_v1` has exactly these nine fields in its hash
preimage: `input_id`, `resolved_building_direction`,
`source_footprint_fingerprint`, `world_axis`, `sign`, `along_origin`,
`mirrored`, and `local_x_positive`, plus `schema="view_projection_binding_v1"`.
The same sorted-key compact UTF-8 SHA-256 rule applies.  Manifest identity,
resolution source, orientation hash, adapter version, scope, and GT source refs
are deliberately excluded.  B4b recomputes this preimage and the facade-segment
preimage locally; it does not import Va private hash helpers.

The following Phase-A fixture vectors are frozen byte anchors (each uses
`input_id="south"`, South, fingerprint `"a" * 64`, axis `x`, origin `0.0`):

| sign | mirrored | local_x_positive | SHA-256 |
|---:|:---:|---|---|
| 1 | false | `image_left_to_right` | `db2e25cf576ef104bb7cd39afc89026857f9860aba42bbdf2c6c52057e88dade` |
| -1 | true | `image_left_to_right` | `b2d733bed5cabbc2acdcaccfebeb177b79100425af8ad6a7a9608b436a20e970` |
| -1 | false | `image_right_to_left` | `741f28f3fa20a9231c71ea8ae0403f0e629bf7839aa67bc5d9234406021684c5` |
| 1 | true | `image_right_to_left` | `88c1c19d22be6d95750ae737bd26312f92733598d183ebc5f44a43331af81daf` |

Va is a gt-blind, in-memory adapter: plan evidence bypasses Vg visibility;
elevation evidence maps local-to-world, intersects the opening target, then
intersects the already materialized Vg-visible interval. A visible existence
fragment is `applicable`; other incomplete positive coverage is
`partially_applicable`; no positive coverage is `not_applicable/unobserved`.
Completeness never gates positive applicability: it only authorizes recorded
negative-evidence intervals. Schema/helper, claim-order, enum, aggregate, or
canonical-hash changes require a new version. Va must not read
`correction.yaml` or introduce a tolerance.

**Precedence (axis identity vs gap closing vs output).** These are distinct
operations and must not be conflated:

- **Axis identity** (are two coordinates the same intended axis/wall?) uses
  `AXIS_JITTER_TOL` **only**, gated by `topology_identity` evidence. Beyond it →
  `reference_or_identity_ambiguity` → A3.
- **Gap closing** (should two things that don't touch be made to touch?) uses the
  `GAP_CLOSE_THRESHOLD` / `GAP_CONFLICT_BAND` / `GAP_UNSUPPORTED` bands. These are
  a connectivity operation, **not** axis identity (a much bigger threshold than
  `AXIS_JITTER_TOL`, applied directionally). The ≤`GAP_CLOSE_THRESHOLD` auto-close
  of a cell edge onto a footprint boundary is executed deterministically by the
  core (internal-wall-to-exterior, the dominant case); internal-to-internal
  connectivity and the conflict/unsupported bands remain A3 judgment.
- **Output** uses `OUTPUT_PRECISION` for final formatting only. `SNAP_GRID` is a
  candidate regularization grid for low-confidence stroke-only geometry; canonical
  axis values are chosen from authoritative evidence and are **not** rounded to
  `SNAP_GRID` before dimension-chain closure.

Architectural-commonsense priors (door/window/room/height values) live in `A4`,
not here; they are advisory (`prior_score` / `warning`), never executable
tolerances.

---

## 5. Geometry schema/profile registry

`CorrectedGeometry.schema_version` is the data-shape capability declaration, not
a code release number. Missing `schema_version` defaults to
`CORRECTION_SCHEMA_V1`. Unknown versions fail gate ① as
`correction.schema_version_supported`; they must not silently downgrade to the
rectangular path.

| name | value | meaning |
|---|---|---|
| `CORRECTION_SCHEMA_V1` | `"1"` | rectangular correction contract |
| `CORRECTION_SCHEMA_V2` | `"2"` | polygon-capable correction contract |
| `CORRECTION_SCHEMA_V3` | `"3"` | strict per-floor-footprint correction wire; v1/v2 remain legacy read contracts |
| `SHAPE_RECTANGULAR` | `rectangular` | axis-aligned rectangular cell contract |
| `SHAPE_ORTHOGONAL_POLYGON` | `orthogonal_polygon` | orthogonal polygon cell fallback contract |
| `CAPABILITY_PROFILE_RECTANGULAR` | `rectangular` | allows `SHAPE_RECTANGULAR` |
| `CAPABILITY_PROFILE_ORTHOGONAL_POLYGON` | `orthogonal_polygon` | allows `SHAPE_RECTANGULAR` and `SHAPE_ORTHOGONAL_POLYGON` |

Schema-v3 registry: `Floor.id` and `Floor.footprint` are required, authoritative
floor-owned geometry; top-level `footprint_x/y` are their exact compatibility
projection. `Window.floor_id` is the primary floor reference. `FeatureStateClaimsV1`
and output-bound `FeatureStatesArtifactV1` record declared versus populated
features; `EvidenceDebtItem.debt_id` is the canonical debt primary key and a
v3 `debt_resolution` audit entry may resolve it exactly once. These are schema
registrations, not new numeric tolerances.

B5 Phase-A registration: resolver-input schema `window_resolver_inputs_v1`,
host-sidecar schema `window_hosts_v1`, resolver helper
`window_host_resolver_v1`, and current-ring direction helper
`window_direction_frame_v1` are strict v3-only wires.  Their artifact-contract
names are `correction_b5_v1` and `correction_b5_orientation_v1` (the writer
integration lands in B5 Phase D).  `manifest_floor_order_v1` means a plan
`floor_ref` is 1-based ascending `Floor.z_floor`, with no gaps; names/OCR are
not a floor identity fallback.  `window_anchor_validation` means the complete
segment + room + parent + proof-totality relation, not a window center/bbox.
For the plan branch, `window_host_resolver_v1` may filter same-floor/family
segments only by the authenticated plan source interval perpendicular to the
facade plane (North/South: `world_y_interval`; East/West: `world_x_interval`),
using `WINDOW_HOST_PLANE_EPSILON`.  This is a source-plane identity filter, not
a center/nearest-wall rule: every coplanar segment remains eligible, so a
positive-width span over two coplanar segments still blocks as
`cross_segment_boundary`.  A window-host audit `original_span` is the
resolver-entry post-snap span; verification replays deterministic snap from
the authenticated producer bytes before comparing it.

E4 orientation registry (B-O batch): `FeatureStateClaimsV1.phase_contract` is the
strict `"b2" | "e4_orientation"` literal. The orientation-enriched v3 lineage adds
helper `north_axis_orientation_v1` to the release tuple
(`floor_footprint_v1`, `facade_visibility_v1`, `north_axis_orientation_v1`) and maps
to correction release `"4"` in the central release map only — no producer may write
the literal release number. Artifact contracts `correction_e4_orientation_v1`
(orientation-enriched accepted correction) and `assembly_e4_v1` (S5 assembly with
`OutputCoordinateContract` schema `"1"` + coordinate snapshot sidecars) are the E4
wire types. `prior_fill` completion is the deterministic method
`prior_fill_default_zero_v1` under policy `c2_e4.north_axis.default_zero.v1`
(`NorthAxisEvidence(0.0, assumed)`); completion mode comes from the run config as a
content-addressed `OrientationRunConfigV1` artifact and a missing evidence-set file
is never an empty set. The building-bound coordinate object registry is version
`ep25.1-v1` (IDD/schema/converter/producer route diffs must all be empty). The EP
exit contract is `GlobalGeometryRules=Relative` + all Zone origins/Direction of
Relative North zeroed + `Building.North Axis = θ`; v1/v2 and provably standalone
legacy intake stay on the `World` path, and no code may branch coordinate mode on
θ's numeric value. These are contract registrations, not numeric tolerances (the
e2e azimuth comparison bands live with the EP e2e tests as assertion tolerances,
not here).

Profile rule: the active capability profile's allowed shapes must be a superset
of the shapes declared by the artifact schema version, else gate ① fails as
`correction.capability_profile_shapes`.

Bump rule: any newly added geometry slot that changes the data shape contract
(for example cell polygons, per-floor footprints, z spans, facade segment
tables, or future non-rectangular primitives) must introduce a new
`CorrectedGeometry.schema_version` and register its declared shapes here before
producers emit it.

Cell polygon rule: each room is exactly one cell, and a single room must never
be split into multiple cells just to keep cells rectangular. Most rooms are
rectangles and stay rectangular cells; only a room whose own shape is not a
single rectangle (e.g. an L-shaped corridor) may schema v2 give `Cell.polygon`:
a CCW, exterior-only orthogonal ring with no repeated closing vertex. Polygon
is the exception, not the default. `Cell.x` and `Cell.y` remain required and
must exactly equal the polygon bbox projection.

---

## 6. Method profiles

Correction strictness depends on the downstream zoning target. Each rule in
`A1`–`A4` states the profiles it applies under and how strict it is.
`A1`/`A2` apply under **all** profiles.

| profile | must be strict | may relax | A3 / A4 posture |
|---|---|---|---|
| `room_identity` | every internal wall (each room boundary becomes a thermal boundary); full geometry fidelity | very little | **full strength** on every internal boundary |
| `use_grouped_rooms` | room-cell closure + adjacency graph + use/schedule/load/HVAC labels + exception spaces (shafts, stairs, toilets, equipment, high-load) | exact wall thickness, tiny offsets | **semantic-grouping and closure heavy**; wall-thickness precision relaxed |
| `perimeter_core` | exterior footprint, facade orientation, floor heights, roof/ground exposure, facade window area / WWR | internal partition coordinates, except declared void/shaft/high-load exceptions and room attribution | **conservative**; envelope/facade/window first, internal arbitration only for exceptions |

---

## 7. Upstream input contract (perception)

For correction to be safe, perception input should:

- not emit estimated coordinates indistinguishable from measured strokes;
- carry structured provenance + confidence for strokes, dimension chains, labels,
  facade windows, and self-check notes;
- link estimated geometry to the dimension ids / inference rule that produced it.

Reading `Stroke.provenance` maps into this evidence model as: `seen` = visual
existence evidence and numeric `estimated_stroke` (not `direct_measurement`);
`dimension_derived` = numeric `transcribed_dimension` and requires non-empty
`dimension_refs`; `estimated` = low-confidence `estimated_stroke`;
`unknown`/missing = legacy/unknown.

### 7.1 Provenance mode and coverage

```
provenance_mode      full | partial | legacy
provenance_coverage  per evidence class {dimensions, strokes, labels, facades, windows}
```

`legacy` (no provenance) input may still run, but affected items are downgraded
to `estimated_stroke` / `unknown`, their confidence lowered, and more
`conflicts[]` emitted. Low-provenance input must not become high-confidence
output.

### 7.2 Profile-specific stop conditions

- `room_identity`: fail / mark `unsupported` when internal-wall provenance is too sparse.
- `use_grouped_rooms`: fail / mark `unsupported` when room-cell closure or labels are too sparse.
- `perimeter_core`: may continue if exterior footprint, floor height, facade orientation, and window/WWR evidence meet minimum coverage.

---

## 7. Validation schema

After correction, the layer emits a validation block (the gate after
`A2-apply`). Targets are **PartA artifacts**, not the downstream thermal-zone
artifact.

```
status                      pass | pass_with_warnings | fail   (top-level)

floor_footprint_coverage    corrected floor boundary is a valid, closed polygon
room_cell_coverage          (room_identity / use_grouped_rooms only) cells tile the footprint,
                            no overlap, no undeclared hole
facade_segment_coverage     exterior boundary attributed; every segment has an orientation
window_anchor_validation    each window ∈ its parent facade/surface; WWR attribution present
thermal_zone_coverage       RESERVED — produced by the later zonification step, not validated here
```

Additional hard checks:

```
no_invalid_polygons         no self-intersecting / zero-area polygons
inside_footprint            no geometry outside the declared footprint
min_edge_satisfied          no edge < MIN_EDGE_LENGTH
checksums_passed            all dimension chains within DIMCHAIN_CLOSE_TOL
id_uniqueness               no duplicate / missing entity ids
attribution_complete        every source item is mapped, fractionally attributed, or unsupported
z_stack_consistent          floor heights / z-stack coherent (lower ceiling z == upper floor z)
```

Soft (relative-tolerance) checks: `facade_area_residuals`, `wwr_residuals`
(within `WWR_REL_TOL`), area residuals (within `AREA_REL_TOL`),
`unsupported_count_by_severity`.

### 7.1 Fail / continue policy

- **fail**: any hard check fails (invalid polygon, overlap, undeclared hole,
  sub-`MIN_EDGE_LENGTH` edge, failed containment, outside-footprint, id clash,
  failed checksum that A3 could not resolve, broken z-stack).
- **pass_with_warnings**: hard checks pass; residuals within their relative
  tolerances; `unsupported` items present but profile stop conditions met.
- **pass**: hard checks pass and no warnings.

Anything that could not be corrected is carried as `unsupported`, never silently
normalized away.
