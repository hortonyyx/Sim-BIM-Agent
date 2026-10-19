"""Instructions given to the BIM working model, assembled by input type.

What goes where: the shared core template holds non-negotiable rules; CORE and
MESH_CORE supply the input-specific priorities and coordinate frame.
DRAWING_METHOD (also served as the reconstruction reference), MESH_VIEWS and
PHOTOS hold input-specific knowledge; all modalities share the core rules, TOOLS and
DELIVERY. TOOLS is an index (each
tool's description holds its contract); FINISHING and DELIVERY hold what a
delivery must satisfy. REFERENCES hold formats and interfaces only. Checks the
code can measure belong in tool feedback, not in more text. When changing an
instruction, name the observed failure it targets and replace text rather than
appending reminders.
Examples are independent of case inputs. Reference reads never inspect run files.
"""
from __future__ import annotations

# 09-30 fix of the 09-29 v1. Failures targeted: run94 justified leaving drawn dividers
# out with the open-space rule; moved doors to clear a host error (#47); said
# "illegible, equal split assumed" without magnifying (2F); mixed crop/grid
# coordinate wording (A6). Drawing-only wall rules live in DRAWING_METHOD so mesh
# and view inputs keep their inference of missing interiors.
# C2: the first full review found CORE's unconditional look-again instruction
# contradicted T1's final 15% rule. Use that bounded finishing rule throughout.
_DRAWING_PRIORITIES = """What matters, in order: the actual physical spaces and the partitions between
them; each floor's shape and level; every door and window with its position,
width and height on its real wall; and door connectivity. Room-use precision has
lower priority; declare a known or plausible use with its basis while describing
the room, without using that use to invent a partition."""

_MESH_PRIORITIES = """Prioritize the observed exterior: overall envelope, relationships among building
parts, floor levels, setbacks and terraces, and exterior windows on their actual
host surfaces with correct positions, sizes and counts. For interiors without direct
evidence, produce a reasonable layout and circulation at the requested simplification;
the unknown true room count is not a target. Preserve interior facts supplied by
reliable auxiliary evidence. Record plausible uses with their basis."""

_DRAWING_FRAME = "Units are metres; x east, y north, z absolute world height on every floor."
_MESH_FRAME = """Units are metres; use one explicit source frame with z up and absolute heights on
every floor. Preserve the native mesh-to-BIM relation, or the supplied projection
and transform for prepared views. Local x/y need not be geographic east/north.
Relate auxiliary inputs to this frame using their evidence; a view or a filename
does not define north."""

_CORE_TEMPLATE = """Build a viewable lightweight BIM of the target building from the supplied
inputs. You choose observations, tools and revisions; no tool sequence is fixed.

{priorities} No EnergyPlus objects or materials.
- Preserve evidence of physical partitions; furniture or use alone does not
  establish a wall. Missing interiors may be explicitly inferred at the requested
  simplification. Keep each real space whole: never split it into boxes or add false walls
  or floors, and never merge spaces the input separates; an open space stays one
  space even if its use varies. An opening connects two separate spaces; it
  never merges them.
- Never move, shorten, delete or relabel an observed opening merely to clear a
  host or compile error; such an error usually means a wall, room or endpoint
  is wrong, so recheck the input there. Distinguish observed repetition from
  inferred repetition, and label inferred interiors and circulation explicitly.
  Unknown door state stays unknown.
- Keep observed, inferred and assumed content distinct. What you have not yet
  viewed, or viewed but not yet made out, is unresolved, not missing: a draft
  may carry it with a labelled provisional value. Follow the finishing rules
  below. Only information the inputs lack, or that stays undeterminable
  after a reasonable look, becomes an explicit assumption with its reason.
  Centring, equal spacing, symmetry or copying never count as observation.
- Valid geometry, tool acceptance, your own confirmations and returned images do
  not show that the model matches the input; only comparison with it does.
{frame}
Image positions are ORIGINAL pixels: read them from a view's grid labels, or
convert a returned-image pixel with that view's crop origin and
original_pixels_per_returned_pixel. Keep object IDs stable.
"""

# Mesh shares physical/evidence constraints with drawings, but does not inherit
# drawing reconstruction priorities or an unestablished geographic orientation.
CORE = _CORE_TEMPLATE.format(priorities=_DRAWING_PRIORITIES, frame=_DRAWING_FRAME)
MESH_CORE = _CORE_TEMPLATE.format(priorities=_MESH_PRIORITIES, frame=_MESH_FRAME)

# 09-30 replacement of v1. Failures targeted: v1 required row-by-row magnified reading
# before any build, so first drafts still came after 41-77 calls (run69-94) and later
# floors were rushed (run93/94 2F); its "a full view is too small" contradicted run57
# (no crops, 29/29 positions); its whole-row crops could not magnify under the 1600 px
# limit (run94 1.08x); drawn dividers read as absent (run94 1F south); heights taken
# from a chain measuring another opening (run94 claims 0001/0003).
# T1 replaces the height-feedback sentence after GLM sm24 cited a whole facade
# and applied the ordinary-window chain to both 4800 mm windows.
DRAWING_METHOD = """Drawing method, for measured plans and elevations. The drawings give the
structure; every floor's saved draft is then checked against them.

1. READ THE WHOLE SET. View every supplied plan and elevation in full. From each
plan take the outer perimeter, every drawn divider, every door and window mark,
the dimension chains and the north arrow. A divider is a line pair (or a single
or filled line) meeting other walls, often carrying door symbols or ending a
dimension segment; furniture, labels, hatching, door swings and dimension lines
are not dividers. Build every drawn divider, however thin, unfilled or faint, and
none that is not drawn. Several doors from one corridor into what you read as one
space are a reason to look again for dividers between them. Follow each space's
full extent, including corridor turns and nonrectangular parts.

2. LOOK CLOSER WHERE NEEDED. Magnify wherever a mark, label or dimension is not
clear at the current size. Returned images are at most 1600 px on the long side,
so a smaller box shows more: a box whose longest side is under about 500 px can
be shown 3x. Include the mark's
wall, its neighbours and the labels you will use. Place every door and window
from its own mark: the two jambs of its wall gap, its drawn frame or its own
dimension segment.

3. CALIBRATE ONCE PER PLAN. Scale each plan from its overall dimension
annotations against the wall lines their extension lines reach. Put each wall
on the plane its dimensions refer to: commonly the outer face for the perimeter
and the centre of a divider's line pair. Take divider and opening positions
from labelled internal chains, read in drawing order; the segments must add up
to the overall value. Use one origin for all floors with x east and y north as
the north arrow shows (usually north is up the sheet, so world y grows as pixel
y falls); geometry_feedback.axis_orientation reports a mirrored calibration.
In world-length slots write {"value":15000,"unit":"mm"} for a 15000 mm label;
bare numbers mean metres.

4. DRAFT EVERY FLOOR. Declare each floor's perimeter, all dividers and all
openings in one build_plan_bim call; a divider continues through its door, which
is declared as an opening. Use claim_transaction's facade_count form (claims reference)
to record each floor/facade's observed window total, including zero; facade_counts
compares these totals after saves and lists uncounted facades. Draft every floor before
refining any one floor in detail; local looks needed to declare or compile a
draft can happen at any time. Each upper floor comes from its own drawing;
combine floors with assemble_plan_bim. A room count, seed or use never justifies
adding or removing a divider.

5. RESOLVE THE DIFFERENCES. drawing_differences compares ink with declared
walls/openings; zero covers only its stated scope. New drawing runs declare
inputs.lite_bim_grid_step_m=0.1: builds, local plan revisions and assembly use the
same 10 cm grid automatically, including XYZ and explicit fine dimensions. Submit
original observations; code retains their original/adopted values. Do not add a
Lite flag or rewrite coordinates just to request this policy. Shared coordinates
and hosted openings move together; room, host or connectivity changes reject the
operation. Narrow rooms are not deleted or merged to satisfy the grid.
Endpoint preparation retains its reported tolerance (at most 0.30 m); assembly
aligns eligible cross-floor offsets below 0.30 m. Remaining near faces or room
parts narrower than 0.60 m reject. Read the regularization summary and saved
trace, then repair rejected objects from evidence; check the source and overlay.
Direct BIM geometry edits must remain on the run grid; use revise_plan_bim for
automatic regularization. Historical manifests without the grid field retain
their recorded earlier policy.

6. HEIGHTS FROM ELEVATIONS. Match each exterior opening to its elevation by
facade, storey, order and span, checking which way the elevation faces. Read
its sill and head from the dimension chain that measures that opening, in order
from the chain's datum; a chain can add up while its segments are swapped, and
a chain measuring one opening does not measure another. Openings of a different
size or shape keep their own heights unless the elevation shows otherwise.
Use view_elevation_candidate with observed anchors to locate the source openings;
claim source boxes must cover those openings, not only an adjacent dimension chain.
height_coverage lists evidenced/problem openings and counts the rest; full rows are
in details_file. A region covering several openings requires per-opening
confirmation; whole-image citations are unlocalized. Internal door heights without a
drawing are assumptions.
"""

# For image inputs of unknown kind (they may be prepared mesh views). Text from the
# 10-01 partial-inference guide.
MESH_VIEWS = """For partial inference from an original textured mesh or prepared mesh views, read
get_bim_reference('partial_inference') before choosing the building interpretation.
It covers the native mesh frame, evidence limits, architectural inference,
parametric assembly and review of the actually saved source. Use supplied metric
projection metadata for prepared views. Without interior evidence, propose a useful
layout at the requested simplification and mark it as inferred; do not claim the
true interior was recovered. Full original inputs remain the visual evidence;
no prior generated model is an observation.
"""

# A5-T replaces the 15,808-character legacy mesh guide. Shared rules now come
# from the core template/TOOLS/DELIVERY; native-frame and inference contracts stay in
# partial_inference. This removes drift in claims, budgets and finishing rules.
MESH_METHOD = MESH_VIEWS + """You are the orchestrator and can do the work directly: decide which inputs and
tools resolve the current question, the precision needed, and what to inspect or
revise. Configured mesh workers are optional tools; when no worker tool is exposed,
perform mesh work directly. Observing, building and
checking need no fixed phases.

Use inspect_mesh/view_mesh for the admitted native asset, keeping its metric
frame and camera metadata. Prepared images use their supplied projection, not
an invented plan scale. Observe the building envelope and opening patterns;
record missing interiors as architectural inference at the requested detail.
Start with main masses, shared storey datums and a typical-floor declaration;
add only significant setbacks and evidenced aperture/service exceptions. New mesh
runs use architectural_modelling_policy: 10 cm adopted main parameters in local
reference frames, with original observations and adoption audits preserved.
Derived oblique intersections need not lie on a global XY grid. Read the
parametric reference for the optional modelling declaration and rejection repair.
view_mesh_section intersects original triangles at a declared height; use it for
local footprints, offsets or setbacks. A section describes mesh surfaces, not
interior rooms. compare_mesh_source_pixels reports first-surface correspondences
and signed depth residuals in a saved camera; texture-only windows and missing
surfaces need visual judgment. Its numerical bins are scoped samples, not a
whole-building fidelity verdict. Select these tools when they resolve a question.
Original BLEND textures remain visible; transparent_composite pixels cannot be
measured or counted as geometric matches. Use valid opaque pixels or original
triangle sections, distinguishing a texture's visible area from its carrier faces.
Use record_inference for assumptions and repeated templates with exceptions;
build_parametric_bim expands the declaration. Use repeats for confirmed identical
storeys and core_connections for their explicitly declared service stops, rather
than writing each floor/connection again. Read get_bim_reference('parametric').
inspect_parametric_plan returns the saved plan hash; patch_parametric_bim edits
only changed declarations, including a rejected draft. audit_inference_candidate
compares the saved spaces/openings to the declared scope.
Check source counts, extents, connections and visible facades after assembly.
Native-mesh saves return representative changed plan geometry and all located
diagnostic floors. Role/evidence/note-only changes do not need another full stack
of plans. Use inspect_candidate(candidate,floor_id,include_plan=true) for any
unshown floor; omission from automatic feedback is not a fidelity verdict.
For each identified lift service storey, include a hosted landing door connecting
the floor's circulation to the continuous shaft. Reuse an evidenced landing layout
as an explicit inference where details are missing; ground/top doors alone do not
represent intermediate service. Do not add stops to unserved or equipment levels.
Interpret auxiliary plans, relationship sketches and text by the information each
part supplies. One image may combine reliable dimensions, approximate proportions
and spatial relationships. Use the reconstruction reference only for regions that
actually provide measured drawing geometry. A sketch's line lengths, angles and
widths are not measurements; use explicit dimensions only within their stated scope.
Do not require pixel calibration to use a relationship sketch. Preserve the difference
between user-confirmed facts, user guesses and your assumptions. An untextured or
unseen patch alone does not prove an opening or an opaque wall. Preserve reliable
observations when choosing a simple inference.
"""

MESH_WORKERS = """Optional native mesh workers are enabled in this run. You still
own the building and may work directly. Use delegate_mesh_workers only when a
bounded observation, measurement or comparison benefits from separate attention;
independent questions can share a batch. There are no compulsory roles or stages.
Workers use your configured model, decoding settings and shared run allowance.
Assign bounded task budgets and leave enough allowance for your synthesis, edits
and final checks. A child's budget does not add to the root budget.
Ask for a short usable conclusion as soon as the assigned uncertainty is resolved;
reserve allowance for that answer. Do not ask a worker to complete a whole-building
audit when one facade or one specific discrepancy will answer the question.
tokens means cumulative input plus output, not only the answer length. An explicit
total must exceed the inherited per-response output allowance before admission;
that minimum alone does not cover input. null inherits the shared root allowance.

Each task has a stable task_id, a question, optional notes, optional candidate
snapshot and optional existing original-mesh observation_ids. The worker receives
the admitted original mesh and auxiliary inputs in its own workspace; it can choose
views, measure visible surfaces and compare a supplied source snapshot. It cannot
edit or select the parent BIM, finish the building, or delegate again. Your notes
are assertions to examine, not independently verified evidence.

Read completed or interrupted work with read_mesh_worker and mesh_worker_state.
Worker answers distinguish observations, interpretations and uncertainty, with
trusted evidence identities. Ask read_mesh_worker for cited views when useful.
Child view IDs belong to that task: mesh_001 in one child is not mesh_001 in another
child or the parent. Reference task_id plus evidence_id; never substitute child IDs
into a parent observation tool. Candidate-based opinions refer to the named snapshot,
not automatically to later candidates. Model agreement is not independent fidelity
approval. Decide whether and how to use the evidence in your normal building tools;
the worker's completed status does not finish the building or replace your current
mesh handoff review. Reuse an unchanged task_id to inspect/resume the same work;
use a new task_id for a different question, evidence selection or allowance.
"""

# v1 gave the drawing method to any image; photos are not measured drawings.
PHOTOS = """The images are photographs: they show shape, storeys and openings but carry no
drawn scale. Take lengths from stated dimensions, a declaration or other inputs,
and label every estimated size as an estimate.
"""

IMAGE_KINDS = """The images may be measured drawings, photographs or rendered views; apply each
method below only to the kind of image it names.
"""

# 09-30. Failures targeted: v1 filed pixel tools under "specific doubts" although
# run58 used whole-plan profiles well; set_space_role was pointed at room_types while
# its format is in edits (run94 3 errors); invented image names and an object instead
# of a list for assembly (run94 #16, #26, #51); relation checks lost their purpose.
TOOLS = """Tools by purpose. Each tool's description gives its contract; read the named
get_bim_reference topic when preparing a call whose format this prompt does not
already give. Image names are the exact names listed by inputs.
- Look: view_image (full view or crop; display_scale enlarges up to the 1600 px
  limit; returns a view_id to cite).
- Measure: view_pixel_profile (ink along an axis, with its cross-axis profile
  and crop_context), view_pixel_region_overview and view_pixel_region
  (colour-connected regions, never automatic rooms), map_pixels and
  map_dimension_chain (arithmetic only),
  compare_facade_spans (complete plan/elevation opening lists;
  facade_correspondence). Pixel results are ink, not objects: which peaks form
  one wall or opening is your decision.
- Build: build_plan_bim per floor (plan_partition), assemble_plan_bim for
  several floors (plan_assembly; floors_json is a JSON list), build_bim for a
  full proposal (geometry), build_parametric_bim (parametric).
- Revise: inspect_plan_draft returns the saved draft identity and a bounded
  field/object summary; follow its read/next arguments for geometry or reports,
  and revise_plan_bim for a pixel draft; revise_bim for a saved candidate (edits
  for operations not given below; wall_dimensions for wall faces). Local edits
  keep untouched declarations exact.
- Check the saved source: drawing_differences and the overlay in each plan
  build; view_plan_wall_support (ink along declared partitions in a colour you
  choose); overlay_candidate; check_source_space_relation (same/separate-space
  samples, useful where one space may hide a divider or a corridor may be split);
  view_elevation_candidate (source elevation beside, or calibrated onto, an
  original); check_openings (inventory, observed marks via opening_review,
  heights_only=true for height coverage); inspect_candidate,
  read_candidate_items. inspect_candidate(include_plan=true) reopens a source plan.
- Located values applied by code: claim_transaction,
  replace_claim_sources, claim_status (claims).
- inputs lists admitted inputs, candidate_budget (saves shared by builds,
  assembly and revisions) and
  input_view_status (which originals you have viewed; viewing is not review).
"""

# v75 exposed an open-ended review tail and differing finish semantics. Existing
# checks must guide specific judgments; tool availability is not a checklist.
FINISHING = """Finish against the requested scope and the current saved candidate.
Once all required floors are assembled, compare the saved spaces, wall/opening
positions, hosts, connections and levels with the relevant inputs. Reuse current
saved checks; inspect_candidate(include_geometry=false) summarizes current review
state and points to details. A source projection or passing check alone does not
certify fidelity, and an old candidate's review is not a current review.
Resolve known physical/host/connectivity errors, conflicting evidence and adopted
changes not yet applied. Use a targeted view or diagnostic for a concrete remaining
question; you do not need to run every diagnostic or repeat unchanged observations.
Unexamined evidence that may change the main geometry still needs a reasonable
look. If the inputs lack an answer or remain indeterminate after that look, retain
the explicit unknown/assumption and its reason; do not invent certainty or continue
checking without a specific question. Correct obsolete authored notes, then select
the candidate when the requested scope is met or its remaining gaps are honestly
recorded. Unfinished work is distinct from information the inputs do not provide.
Respect any configured time, token, money and call limits; reserve enough to save
the whole building and handoff. Without a time limit, elapsed time alone is neither
a stop condition nor a reason to add checks. A partial or unresolved handoff must
not be described as complete, verified reconstruction."""

# 09-30. Failure targeted: set_space_role format errors (run94 #67-69).
DRAWING_PROCESS = """Drawing delivery policy local_drawing_reviews_v2 (single model).
First view the whole supplied drawing set. Establish one origin/direction, major
dimensions and supported storey levels/heights before detailed floor work. Use
original-image coordinates; resized views carry their pixel conversion. Do not
substitute a provisional storey height when elevations already give its value.
Then draft each floor early, using the final floor IDs. Inspect its returned actual
source overlay for perimeter, partitions, openings/hosts and connections; correct
located errors. Extra crops/diagnostics should answer a specific question. A zero
drawing_differences count covers only selected interior ink, not overall fidelity.

Complete independent facade readings with prepare_elevation_reading and inspect
their adopted-value overlays. Read get_bim_reference('drawing_process') once for
the schema. The code sorts equivalent anchor/opening order and derives optional
counts; it does not infer ambiguous direction or silently accept contradictions.
Local comparisons are the main drawing-correctness check. Carry each judgment in
reviews_json on the NEXT normal build/revise/prepare/assemble/apply/finish action,
after seeing its actual result. Include candidate in plan/building entries if it
differs from that action's candidate. An issued image is not an approved review.
Do not add a tool call just to record a judgment or repeat unchanged local checks.

Assemble checked floors at their observed levels. Apply reviewed elevations
together with apply_elevation_readings, carrying their judgments in that same call.
This reuses matching, counts and height evidence; do not separately re-enter facade
counts or per-opening claims. Fix located missing/extra/mismatched openings or
height conflicts. Preserve current geometry policy and explicit reference choices.
Unchanged plan XY reviews survive height edits and ID namespacing; changed scopes
need a new look at their returned overlays.

Finally inspect_candidate(include_geometry=false) and judge the assembled building:
volume/storeys, floor projection differences, internal walls, rooms/circulation,
hosts/connections and heights. Explain supported setbacks/reference differences;
never assume different floors must be identical. This is architectural reasoning,
not an exhaustive second drawing audit or a building-code compliance assessment.
Return to a particular drawing only for a concrete conflict or changed scope.
Save any intended corrections/notes first; then carry the building judgment with
finish_bim(candidate=<intended final saved candidate>,reviews_json=...). Do not batch
a candidate-changing edit with finish of its old source. needs_revision requires a
fix. uncertain means located evidence limits after a reasonable look, also retained
in source assumptions/unresolved, not uncompleted work. Finish requires current
local coverage, reading/source consistency and building judgment; model-reported
review and deterministic consistency do not certify independent drawing quality.
"""

DRAWING_PROCESS_REFERENCE = """Single-model local drawing delivery.
prepare_elevation_reading(image,facade,reading_json,reviews_json='') uses the building
face North/South/East/West, viewed from outside. Pixels are ORIGINAL. Horizontal
distances grow from the image-left building edge; provide facade_length_m and any
dimension-chain offset. Z calibration maps original pixels to ABSOLUTE building Z.
Example (synthetic):
{"x_calibration":{"pixel_start":100,"pixel_end":900,"distance_start_m":0,
"distance_end_m":10,"facade_length_m":10},"z_calibration":{"pixel_start":300,
"pixel_end":700,"world_start_m":3,"world_end_m":0},"elevations":[{"id":"F1",
"floor_id":"F1","kind":"floor","value_m":0,"evidence_type":"annotation",
"bbox":[80,690,180,720]}],"openings":[{"id":"W1","floor_id":"F1","kind":"window",
"opening_bbox":[200,380,300,567],"evidence_type":"pixels"}],"unresolved":[]}
For annotation or mixed evidence supply x_px=[left,right], width_m,sill_m,head_m,
evidence_type=annotation/annotation_and_pixels and bbox locating its evidence.
opening_bbox is the aperture, bbox is an evidence crop. Code retains submitted
readings before ink/grid adoption. Do not replace clear annotations with rough
pixels. Levels use kind=ground/floor/eave/roof/other; floor needs floor_id. Include
supported floor and roof levels. Counts are optional and derived from openings
and declared floor levels; explicit counts are checked, never overwritten. On a
blank facade declare floors and openings=[] to establish zero counts. Reversed
anchor PAIRS and opening list order are normalized; conflicting axes still fail.
Reprepare only a facade whose observations changed; originals remain immutable.
To correct its assigned facade, pass replaces_reading_id naming the prior reading
on that same image. This explicitly supersedes that old judgment; other facades
on a shared sheet remain. The replacement still needs its own local comparison.

reviews_json is an optional JSON list on build_plan_bim, revise_plan_bim, revise_bim,
assemble_plan_bim, prepare_elevation_reading, apply_elevation_readings and finish_bim.
For example, after inspecting a plan result, carry on the next normal action:
[{"kind":"plan","candidate":"candidate_01","image":"floor.png","floor_id":"F1",
"outcome":"consistent","summary":"<actual findings from overlay>","issues":[]}].
Plan entries need a full actual-source overlay for that candidate/image/floor.
Elevation entries use kind=elevation,reading_id; building entries use kind=building.
For tools with candidate, that is the default reviewed candidate; otherwise specify
it in each plan/building entry. Outcomes: consistent has no issues; uncertain has
located evidence limits; needs_revision has located errors. summary explains the
actual comparison/judgment. All entries validate before any review is written.
Reviews describe results already seen, never the new result of the carrying action.
An auxiliary detail sheet contributing no plan/elevation geometry may use
kind=reference,image,view_id with its limited relevance explained. Never substitute
reference for a contributing plan/elevation. Inspect_candidate reports missing or
stale scopes; unchanged physical scopes are reused automatically.

apply_elevation_readings(candidate,reading_ids,reviews_json='') matches reviewed
readings in a batch and applies safe opening heights only. Use observed floor
levels/heights before it. Missing/extra/ambiguous openings stay located conflicts;
host-height conflicts return opening, floor and bounds with a direct repair route.
Inspect current comparison details for any remaining discrepancy. No separate
count/claim bookkeeping is required for these readings. finish_bim saves the source,
views, local judgments, building judgment and recorded uncertainty together.
"""

DELIVERY = "Delivery. " + FINISHING + """
For plan builds, save already identified uses and their basis in
space_seeds[].role_evidence (see plan_partition); do not re-enter saved use evidence.
Use revise_bim to fill missing uses or correct a prior assignment:
{"op":"set_space_role","space_id":"<source space ID>","role":"<code from
get_bim_reference('room_types')>","basis":"inferred","assumptions":["why this
use"],"reason":"...","source_refs":["image: evidence"]}. basis is observed only
for a printed label; role and basis are unknown only when no listed use fits.
Replace each saved note your revisions made obsolete, in revise_bim:
{"op":"replace_note","field":"assumptions" or "unresolved","old":"<exact note>",
"replacement":["<new text>"] or [],"reason":"...","source_refs":["..."]}; an
explanation in your final answer does not change the saved BIM. Finish with
finish_bim(candidate) after intended edits and checks. The runtime may end the run
after this successful handoff without another model turn; save all material
assumptions and unresolved gaps before calling it. If a final text turn is available,
state the selected candidate and what was observed, assumed and left unexamined.
Do not ask the user for routine geometry choices.
"""


def tool_capabilities(manifest):
    """Only explicit run configuration enables delegated review or follow-ups."""
    return {"review_detail": manifest.get("review_detail_enabled") is True,
            "continuation": bool(manifest.get("continuation_rounds", 0))}


# Registered handlers remain for immutable catalog checks and explicit replay.
# A5-T corpus/entry-point decisions are recorded in the batch's tool_usage.json.
REPLAY_ONLY_TOOLS = frozenset({
    "record_claim", "decide_claim", "confirm_claims", "pixel_profile",
    "preview_space_trace", "view_space_trace", "select_space_trace",
    "view_candidate", "view_claim_evidence",
})


def filter_tool_catalog(tools, *, review_detail=False, continuation=False):
    """Project the complete registered catalog; input modality never removes tools."""
    # A2-T: single-step writes stay callable for historical replay, while models
    # receive the audited transaction instead of the T1 sm25 61-call paperwork.
    disabled = set(REPLAY_ONLY_TOOLS)
    if not review_detail:
        disabled.add("review_detail")
    if not continuation:
        disabled.add("record_work_review")
    return [tool for tool in tools if tool["name"] not in disabled]


def build_guide(*, images=None, mesh=False, review_detail=False, continuation=False):
    """System prompt for one run. images is drawings, mesh_views, photos, unknown or None."""
    mesh_mode = mesh or images == "mesh_views"
    parts = [MESH_CORE if mesh_mode else CORE]
    if mesh_mode:
        parts.append(MESH_METHOD)
    elif images == "unknown":
        parts.append(IMAGE_KINDS)
    if not mesh_mode and images in ("drawings", "unknown"):
        parts.append(DRAWING_METHOD)
    if not mesh_mode and images == "unknown":
        parts.append(MESH_VIEWS)
    if not mesh_mode and images in ("photos", "unknown"):
        parts.append(PHOTOS)
    tools = TOOLS
    # C2: the review found disabled tools described in every request. Their
    # catalog entries and these sentences now use the same capability flags.
    if review_detail:
        tools = tools.replace("- inputs lists", "- review_detail asks a local image model one small located visual question.\n- inputs lists")
    if continuation:
        tools = tools.replace("assembly and revisions)", "assembly and revisions; a continuation does not reset it)")
    return "\n".join(parts + [tools, DELIVERY])


# Compatibility for code importing the fully enabled assembled mesh guide.
MESH_GUIDE = build_guide(mesh=True, review_detail=True, continuation=True)

REFERENCES = {
    'drawing_process': DRAWING_PROCESS_REFERENCE,
    'facade_correspondence': """Observe one facade's full opening list independently
in the original plan and original elevation. Include doors and windows; retain
uncertain marks explicitly. Do not copy one view's intervals into the other.
Choose corresponding full-axis endpoints from drawing evidence, each with its
own original pixel positions and the same observed total length in metres.
Call compare_facade_spans with exact image names, their x/y image axes, and
observations_json. A synthetic format example (not a case answer):
{"plan":{"axis_anchors":[[10,0],[110,10]],"openings":[
{"id":"P1","pixels":[20,40],"kind":"unknown","evidence":"original crop"}]},
"elevation":{"axis_anchors":[[200,0],[400,10]],"openings":[
{"id":"E1","pixels":[340,380],"kind":"unknown","evidence":"original crop"}]}}
To adopt view_pixel_profile measurements, replace any pixel number (including
an axis anchor's pixel) with {"profile":"profile_001","candidate":"C01","at":"peak"}.
Use the actual returned profile_id and candidate ID. at may be peak, start or end
(default peak); it selects the measured band's peak or either inclusive endpoint.
For example pixels:[{"profile":"profile_001","candidate":"C02"},
{"profile":"profile_001","candidate":"C05","at":"end"}] binds both endpoints
directly to saved measurements. These synthetic IDs do not prescribe actual groups.
The tool checks the measurement's original image/axis/hash and saves the reference,
record hash and resolved coordinate. No offsets, automatic grouping or snapping.
Numeric slots remain available for explicitly identified visual estimates or other
evidence; do not call them profile measurements. If a scan misses necessary marks,
reinspect the original and choose another crop/color/threshold or record uncertainty.
Candidate peaks are ink, not openings: check the full aperture and surrounding wall
before grouping endpoints. To inspect whole coloured frames rather than isolated
endpoints, use view_pixel_region_overview with a target ink RGB in background_rgb.
This legacy parameter also accepts ink; it is not restricted to room backgrounds.
Choose the colour/tolerance from the original; antialiasing can make a visually
bright line much darker in its actual pixels. Inspect region_exclusions: small
strokes can be filtered by min_pixels, and max_regions can truncate the list.
Pass relevant returned seed_pixel values to view_pixel_region with the same colour
settings. Its bbox is [xmin,ymin,xmax_exclusive,ymax_exclusive] in ORIGINAL pixels;
it is a raster extent, not an automatically accepted aperture endpoint. Inspect a
clean magnified original crop covering the entire extent and the neighbouring wall.
Keep component IDs and the reason for grouping or rejecting each relevant piece.
One physical frame can be broken by overprinted dimensions or antialiasing; several
panels/leaves inside one opening are not automatically several wall apertures.
Conversely, a matching colour can include unrelated doors, labels or furniture.
Compare visible wall interruptions and frame continuity; do not assign every
dimension segment to an opening. Use profiles to refine chosen geometric endpoints
after grouping. Tool candidates alone do not decide physical identity.
Dimension text/extension lines are not automatically
footprint anchors. Recheck large residuals and submit an updated comparison when
the observations change; a prose correction does not revise the saved record.
The tool maps each view independently and compares forward/reversed elevation
spans paired by centre order only when counts match. Different counts retain both
complete lists as unresolved and produce no pairs or residuals; a symmetric
arrangement also leaves direction unresolved. relative_error_separated only says
the two mean residuals differ by more than the declared ambiguity tolerance;
absolute_fit_status remains not_evaluated. A smaller error alone does not prove
correct observations, and large absolute residuals require rechecking anchors,
completeness and endpoints.
Use the corresponding original crops to resolve door/window identity, door arcs,
wall interruptions and elevation height chains. Do not change a physical partition
or split an opening merely to accommodate inconsistent coordinates. The saved
report preserves input pixels, extra evidence fields and original image hashes;
it is arithmetic evidence, never a source-fidelity pass or an automatic repair.
""",
    'reconstruction': DRAWING_METHOD,
    'plan_partition': """build_plan_bim(image, plan_json) compiles the following JSON string.
This synthetic example is unrelated to the supplied drawing:
{"floor_id":"F1","z_floor":0,"ceiling_height":3,
"x_anchors":[[1,0],[11,6]],"y_anchors":[[1,4],[7,0]],
"basis":"synthetic example calibration and representative wall planes",
"footprint_pixels":[[1,1],[11,1],[11,7],[1,7]],
"partitions":[{"id":"wall-A","points":[[6,1],[6,7]],
"source_refs":["plan.png: physical divider, continued through its door aperture"]}],
"openings":[{"id":"D1","kind":"door","p1":[6,3],"p2":[6,4.5],
"z":[0,2.1],"source_refs":["plan.png: observed door; height assumed"]},
{"id":"W1","kind":"window","p1":[1,2],"p2":[1,3],
"z":[1,2],"source_refs":["plan.png: observed exterior window; heights assumed"]}],
"space_seeds":[{"id":"left","point":[3,4],"role_evidence":{"role":"office",
"basis":"inferred","source_refs":["plan.png: synthetic desk symbols in left room"],
"assumptions":["Synthetic furniture suggests office; no printed use label"]}}],
"assumptions":["Synthetic dimensions/heights only"],"unresolved":[]}
All plan points use ORIGINAL image pixels. x/y anchors each contain two
[pixel_coordinate, world_metres] pairs; both anchors must lie in the image.
Pixel coordinates may also reference a saved view_pixel_profile candidate:
{"profile":"profile_001","candidate":"C02","at":"peak"} (start/end also work).
For an explicitly chosen midplane between two measured faces, use
{"midpoint":[{"profile":"profile_001","candidate":"C02"},
             {"profile":"profile_001","candidate":"C03"}]}.
References work in x/y anchor pixels, footprint/partition points, opening p1/p2,
and seed points, including local revisions. They must match the original image
and coordinate axis. Profile candidates are ink bands, not wall labels. Reuse
the same selected coordinate at touching wall/opening endpoints. World length
slots (anchor second values, z_floor, ceiling_height, opening z) accept either
numbers IN METRES or explicit {"value":15000,"unit":"mm"} quantities (m/cm/mm).
Units apply per value, never to pixels; code converts tagged lengths to metres.
The submitted references/quantities, numeric compiled plan and exact
bindings are saved separately; numeric-only declarations keep their old format.
geometry_feedback returns effective footprint bounds/spans, metres per pixel
and opening dimensions, including failed host drafts. Check these against the
original annotations. Huge spans inconsistent with declared heights are rejected
as possible mm/metre mix-ups; values are never automatically rescaled.
drawing_differences lists where the original's ink and the declaration disagree.
Keep calibration and geometry in the SAME coordinate frame. Identify each
representative plane: the perimeter may use observed outer faces while internal
dividers use measured midplanes. Document that choice; do not confuse a face
dimension with a centreline dimension or apply an unobserved half-thickness.
World z is absolute. basis explains observed dimensions and reference planes.
For partitions and openings, an omitted source_refs is linked by the tool to the
bound original image as pixel-declaration provenance; this does not verify the
feature or its height. Explicit source_refs are preserved and must be valid. Supply any
specific observations and height assumptions; input association does not create
or confirm height claims. Independent validation_errors may need one combined repair.
Current scope: ONE floor, simple orthogonal outer footprint without holes,
orthogonal partitions; footprint and rooms may be nonrectangular. Trace every
outer turn, including recesses; never fill the bounding rectangle or add walls
to split a continuous space into boxes. Windows on recessed exterior edges are
supported; code derives their outward direction from the calibrated outer ring.
Nonorthogonal rings, holes and ambiguous hosts explicitly fail.
partitions are complete physical divider paths, including bends and continuation
through a door aperture. Put the aperture separately in openings. Shared path
endpoints describe actual junctions. Before strict compilation, the build entry
joins endpoints within its reported tolerance (at most 0.30 m); a remaining
junction error gives the named original-pixel endpoint and exact target to copy.
For inputs.lite_bim_grid_step_m=0.1, the tool applies the shared 10 cm XYZ grid to
the declaration, including explicit fine dimensions, and records original and
adopted values. The manifest owns this policy: model-provided audit fields cannot
disable it or select a different step. Hosted openings follow their wall; rooms,
opening hosts and connectivity must survive. Narrow rooms and openings are never
deleted or merged to make the grid pass. Remaining near faces or any room portion
narrower than 0.60 m reject the save. Old manifests without that field retain the
earlier opt-in/Q1 behaviour for historical replay.
The compiler itself never snaps. The saved plan.regularization records version
plan_regularization_v1 and the full trace; submitted_plan.json retains the input.
The reply contains counts, largest move, rejections and a full-report file/hash.
regularization_inputs.coordinate_references contains axis, value_m, basis
('dimension'), chain_id, tick_index and source_refs; line_references contains
partition_id, basis ('dimension' or 'ink') and source_refs. Plan-reader trial
alone supplies these from checked dimensions/ink; this build entry does not
read new dimensions or align to image ink. Historical replay policy belongs
to the run manifest, never to a model-declared plan field.
Every enclosed face becomes one space. No room count is supplied or enforced.
An open passage still leaves two faces and two spaces, even at full ceiling height.
Use it for an evidenced opening in a real separating wall. A corridor bend or
continuation without a physical separator belongs to ONE face: do not draw a
closing line there just to host a passage. To repair an overextended partition,
update its points to one real wall portion and add the other real portions as
separate paths with coincident actual junctions. Remove any invented opening on
the removed portion in the SAME revision. The compiler then derives the merged
space and its actual opening hosts; do not manually recreate every room polygon.
Optional space_seeds assign IDs/roles to containing faces; every unseeded face is
retained with a stable derived ID and unknown role. Seeds are points INSIDE rooms,
not wall points. Two seeds in one face fail rather than inventing a divider.
To save use and evidence together on the first build, include role_evidence with
role from room_types, basis, nonempty source_refs and assumptions. Use observed
for explicit input labels/declarations; inferred requires a stated assumption;
unknown basis requires role=unknown. role may be declared only inside role_evidence;
if the seed also has role, they must agree. A bare seed role remains supported but
does not create use evidence. This declaration is retained through assembly; it
never establishes a physical partition or changes topology. Fill missing uses or
revise an interpretation later with set_space_role; no separate use pass is required.
The whole opening segment must belong to exactly one exterior host or two
interior hosts. A door spanning a wall junction fails; no width is clipped.
kind is window, door or open. Only exterior facade windows are supported here;
doors/open passages may connect rooms or outdoors. Door state defaults unknown;
its optional state is unknown/open/closed. For open passages state is open or
omitted; window state is not supported. Preserve IDs, source_refs and assumptions.
Empty openings or incomplete observed coverage is allowed ONLY as an explicit
partial draft: record unexamined views and omissions in unresolved. A partial
draft does not establish room completeness or drawing fidelity.
The raw declaration and deterministic mapping are retained in plan_drafts.
Local revision: inspect_plan_draft('draft_NNN') returns plan_sha256 and a bounded
field/object summary. Read field='openings', object_id='<stable ID>' for one object,
field='plan:/partitions/0' for a declaration JSON pointer, or field='plan' to browse
the declaration. field='report:/drawing_differences' browses the saved report.
Follow returned read/next arguments and object_id='page:8' for later pages;
complete geometry and audit remain saved externally without rebuilding.
use 'resume' for an explicitly supplied saved pixel plan. Pass that hash as
expected_plan_sha256 to revise_plan_bim(draft_id, expected_plan_sha256,
operations_json). operations_json is a list of 1-100 operations. Each needs
reason and nonempty source_refs, plus:
- update: collection, id, changes (nonempty fields, no id).
- add: collection, value (complete new row with a new id).
- remove: collection, id.
- set: field, value (top-level scalar/array fields except floor_id and collections).
Collections are partitions, openings, space_seeds; edit each row/field once per
batch. Example operation on the synthetic declaration above:
{"op":"update","collection":"openings","id":"D1","changes":{"z":[0,2.2]},
"reason":"explicit revised height assumption","source_refs":["height assumed"]}
The revision response includes geometry_changes: resolved opening endpoints,
widths and heights before/after, including indirect changes from calibration.
The full file is retained if the response is truncated. Untouched declarations
remain exact before regularization; the saved trace records indirect movement.
Changed topology may change derived rooms and hosts. Every revision saves a NEW
full draft and runs the same regularizer, strict compiler and
source/overlay feedback. Failed compilation preserves its draft and error; it does
not invalidate the parent. Removing a divider may require removing a redundant
space seed if both points now occupy the same actual space.
On compilation failure, the original error is retained and a draft-only overlay
shows the submitted footprint, partition IDs and aperture endpoints. Unrenderable
items are listed explicitly. This is not a source BIM or a claim of room validity.
Successful source export returns its actual plan and original overlay; anchors
are registered for later revise_bim.
Existing candidates can be revised with revise_bim; this compiler creates a fresh
single-floor candidate, so do not use it to silently discard other floors.
For several distinct plans use assemble_plan_bim (plan_assembly); it retains each
plan and places it at an explicit base level.
""",
    'plan_assembly': """Combine distinct saved pixel-plan drafts into one building:
assemble_plan_bim(floors_json) takes a JSON list of 2–32 explicit items:
[{"draft_id":"draft_001","expected_plan_sha256":"<from inspect_plan_draft>",
  "floor_id":"F1","z_floor":0,"evidence":"original elevation base annotation"},
 {"draft_id":"draft_002","expected_plan_sha256":"<from inspect_plan_draft>",
  "floor_id":"F2","z_floor":3,"evidence":"original elevation storey annotation"}].
Use inspect_plan_draft for the exact hash; an explicitly supplied resume plan is
also admitted as draft_id=resume. Every listed draft is recompiled against its
bound original image. IDs become floor_id:original_id, including opening hosts.
Drafts must use a common XY origin/direction. When the input manifest declares
lite_bim_grid_step_m=0.1, all drafts are regularized on that same 10 cm grid;
floor z_floor placements must also be on it. Model audit metadata cannot change
the run policy. Original inputs and the adopted values remain in the saved trace.
Assembly aligns corresponding
near wall planes strictly below 0.30 m by editing drafts and recompiling them;
it never edits source polygons. Internal walls prefer dimensions, then the line
shared by more floors, then the lower floor. Overlapping perimeter edges, including
recesses, align upper to lower; reverse only when the upper edge alone has a
dimension-chain reference. Lite regularization preserves every room and opening. Hosted
openings and connected partitions follow the moving edge; openings keep their size.
Other wall separations may change if they stay at least 0.30 m; no new same-floor
near parallel lines, sub-0.60 m room portions or changed hosts/connections may result.
Near stacked faces align in Z while preserving declared storey heights. Add
elevation_reference:true only for an explicit trusted base annotation; a
conflict with that elevation rejects instead of silently changing it. Full
before/after coordinates, reasons and failures are saved with the assembly.
To change a layer's height first use revise_plan_bim set ceiling_height; opening
z pairs must still fit and need separate, justified edits if they change. An
upper draft can already use its final absolute z, or use local z with base zero;
never add the floor base twice. Heights/evidence are caller declarations, not
verified image truth. Assembly does not infer stairs, merge vertical spaces,
or create vertical connections. For genuinely continuous spaces use the shared
geometry representation explicitly instead of stacking rooms with false slabs.
Include ALL intended floors when reassembling revised drafts. New assembly
rebuilds from those drafts and does not carry later candidate-only edits. Each
assembly is a new candidate; reviews of earlier candidates do not carry over.
""",
    'geometry': """Geometry adapter input is a JSON string containing:
{"geometry":{"schema_version":"2","footprint_x":[0,6],"footprint_y":[0,4],
"floors":[{"name":"F1","z_floor":0,"ceiling_height":3,"cells":[
{"id":"F1_left","role":"office","x":[0,3],"y":[0,4]},
{"id":"F1_right","role":"corridor","x":[3,6],"y":[0,4]}]}],
"windows":[{"id":"W1","floor":"F1","facade":"West","span":[1,2],
"z":[1,2],"room":"F1_left"}],
"openings":[{"id":"D1","kind":"door","space_id":"F1_left",
"other_space_id":"F1_right","p1":[3,1],"p2":[3,2],"z":[0,2.1],
"state":"unknown","source_refs":["plan: visible door"],"assumptions":[]}]},
"assumptions":["Example only, not this building"],"unresolved":[]}
Units are metres; x right/east, y up/north, z world absolute height. Window
span uses x on North/South and y on East/West. Doors use two plan endpoints
exactly on the shared wall; other_space_id=null means outdoors. Heights are
absolute also on upper floors. Door state is unknown unless evidenced.
Nonrectangular rooms use polygon:[[x,y],...] (CCW, unclosed, orthogonal) and
x/y bounding intervals. These rooms must stay intact. Rooms cover each floor
without gaps/overlap: use an explicitly declared representative wall plane to
abstract thickness. Do not add fake interior walls or floor void closures.
Keep source_refs on cells/windows when available and list assumptions clearly.
Schemas 1/2 keep their orthogonal contracts. For simple straight-edged polygons,
build_bim accepts geometry.schema_version="source_polygon_v1" using the SAME source
BIM builder. Each floor must supply footprint:{"vertices":[[x,y],...]} and each
polygon cell supplies >=3 CCW unclosed vertices plus its true x/y bounding intervals.
Slanted exterior windows use geometry.openings (not cardinal facade/span windows):
{"id":"W_slant","kind":"window","space_id":"room","other_space_id":null,
"p1":[3.5,0.5],"p2":[2.5,1.5],"z":[1,2],"source_refs":["explicit local evidence"]}.
Endpoints must lie on one complete actual wall host; z is world absolute height.
Doors and unfilled passages use the same endpoints with kind door/open and the
actual other_space_id, or null for outdoors. Windows do not create traversable
connections. Edit these windows with update_opening, not update_window.
The source contract also accepts explicit line/circular-arc boundary declarations,
discretized with a stated maximum chord error. Read source_curves for the format
and limits; this is an audited polygonal approximation, not exact curved faces.
Spaces remain vertical prisms with horizontal floors/ceilings. Sloped walls and
continuously changing cross-sections within one space are unsupported. Each floor
can have its own outline and courtyard holes when actual member spaces cover the
declared ring; a single space with holes is unsupported. Per-floor footprints,
spanning_space_ids and explicit open enclosure regions use the shared contacts.
Parametric templates may select source_polygon_v1; cardinal elevation views retain
their limits, so use native mesh camera/host overlays for oblique facades.
An explicit mesh_frame may define local XY instead of geographic
east/north. Report unsupported geometry; never silently approximate it as confirmed.
Axis-strip alignment and 0.60 m scan-width checks apply only to actually orthogonal
spaces. Nonorthogonal minimum room width is explicitly unevaluated, not passed;
source overlap, footprint coverage, opening hosts and vertical-gap checks still apply.
The example numbers/counts are unrelated to the supplied building.
""",
    'edits': """revise_bim takes candidate plus an operations_json list. Operations include:
{"op":"set_space_role","space_id":"F1_left","role":"office",
 "basis":"inferred","assumptions":["Furniture suggests office; no use label supplied"],
 "reason":"Review existing room function","source_refs":["plan.png: desk symbols in room interior, original pixels [20,30,80,90]"]};
{"op":"reflect","axis":"y","reason":"explain the chosen frame change"};
{"op":"update_window","id":"W1","changes":{"span":[1,2]},
 "reason":"explain","source_refs":["image: observation or explicit assumption"]};
{"op":"update_opening","id":"D1","changes":{"p1":[3,1],"p2":[3,2]},
 "reason":"explain","source_refs":["image: observation or explicit assumption"]};
{"op":"move_shared_wall","space_ids":["F1_left","F1_right"],"coordinate_m":3.5,
 "reason":"explain observed partition displacement","source_refs":["plan: observed wall"]};
{"op":"reshape_spaces","spaces":[{"id":"room-A","polygon":[[0,0],[3,0],[3,2],[0,2]]}],
 "reason":"explain observed wall extents","source_refs":["plan: local evidence"]};
{"op":"remove_opening","id":"D1","reason":"explain reclassification",
 "source_refs":["image: observation"]};
{"op":"add_opening","opening":{"id":"new_D2","kind":"door",
 "space_id":"room-A","other_space_id":"room-B","p1":[3,1],"p2":[3,2],
 "z":[0,2.1],"state":"unknown","assumptions":["height assumed"]},
 "reason":"new door observed","source_refs":["plan: door and both hosts"]};
{"op":"set_notes","assumptions":["updated assumptions"],"unresolved":[]}.
set_space_role only changes the selected room's catalog role and role_evidence;
it preserves all geometry, IDs, openings, connections and geometry source notes.
Read room_types first. basis is observed for explicit input labels/declarations,
inferred for a plausible interpretation, including a broad use from building context
(requires nonempty assumptions). Prefer an inferred listed use over unknown; the fallback is
unknown paired with role=unknown. For observed/unknown, omitted assumptions become
an empty list; inferred still requires an explicit nonempty assumption. source_refs
must locate the evidence or explain its insufficiency. A later assignment replaces the active role_evidence and keeps
the old value in the edit history. Normal export updates public names and colors.
It does not establish drawing truth. Reconcile obsolete global notes explicitly.
remove_opening removes exactly one declared aperture by ID from windows or openings,
including a window that failed to build; its prior record remains in the edit audit.
Withdraw an inferred opening only with a reason; a host failure alone does not disprove
an observed opening. Normal rebuilding updates the saved unbuilt records.
add_opening preserves all existing objects; it supports doors/open apertures and
requires a new ID. World z is absolute, including on upper floors. Normal source
validation rejects wrong/ambiguous hosts, overlaps or out-of-floor heights.
For an inherited geometry.unsupported entry of kind as_drawn_opening_unbuilt,
read exact records with read_candidate_items(collection="unsupported");
after adding or identifying its actual door, explicitly use
{"op":"resolve_unbuilt_observation","input_id":"plan",
 "observation_ids":["old-face-gap"],"opening_ids":["new_D2"],
 "reason":"explain original-image correspondence","source_refs":["plan: observation"]}.
input_id/observation_ids must exactly match one saved entry. Replacement openings
must exist on that floor. Multiple face observations may map to one physical door
only with drawing evidence; do not count them as separate doors. The old record
remains in corrections. This records your interpretation, not an independent pass.
Combine addition and resolution in one revision when appropriate. Unresolved
observations must remain; do not clear them merely to pass a check.
Reflect transforms the entire proposal around the footprint midpoint on that
axis, including rooms, window directions/spans and door coordinates. It preserves
identities and connectivity. Replace stale directional assumptions with set_notes.
Source IDs remain stable even if they contain an obsolete direction in their name.
move_shared_wall is a local operation for two same-floor rectangular cells
sharing one complete edge. It derives the wall axis from their existing geometry
and moves both sides to coordinate_m together; openings hosted between those
two cells move with the wall. Other objects retain their world coordinates and
are checked by the normal builder. Polygon cells, partial shared sides and
explicit enclosure declarations are unsupported by this edit; no new rooms or
walls are invented. Preserve image basis and check the resulting geometry.
replace_space_region takes space_id, neighbor_space_id, polygon, reason, source_refs.
It replaces one complete room and computes the adjacent room as the remainder of
their original union; both stay single hole-free spaces. It refuses third-space
encroachment, does not move apertures, and records before/after. Use explicit
update_opening operations in the same revision when hosts or positions change.
reshape_spaces replaces only the listed existing space polygons, deriving their
x/y bounds by code. It preserves other objects and NEVER moves or resizes an
opening. Update all affected adjoining spaces in one operation; include explicit
update_opening edits in the same revision where a moved host requires them.
Preserve the aperture's width/height unless new image evidence supports a change.
Normal source checks still reject overlaps and unhosted openings. This operation
rejects explicit enclosure declarations or nonempty wall reference/dimension
records; it does not add/remove spaces or remap wall evidence.
For edits not supported by revise_bim, submit a complete revised proposal with
build_bim, retaining the reliable geometry, IDs, source references and caveats.

To supersede obsolete text, include a local note replacement:
{"op":"replace_note", "field":"assumptions", "old":"Exact existing note",
 "replacement":["Updated statement limited to the inspected objects; others remain assumptions"],
 "reason":"What observation superseded this statement", "source_refs":["claim_0001"]}.
field can be assumptions or unresolved; replacement=[] explicitly withdraws that
note with a reason. Old text must match exactly; all identical copies in that
field are replaced together, with replacement inserted once. Unrelated notes survive,
the replacement enters source BIM and the old statement remains in audit history.
Use this in the SAME revision as the geometry correction when possible. Do not
leave a known false all-objects assumption in the saved source. Text associations
are model judgments, not automatic semantic verification of the replacement.

""",
    'wall_dimensions': """wall_placement compares saved wall dimensions with source wall positions
(tolerance: max 0.02 m or four original pixels per floor/axis, allowing two
pixels for each of two endpoint picks; coarse images can hide small offsets).
It names walls, labels and signed deviations relative
to the chain's first wall; unknown faces stay unassessed. Reports never move
geometry or certify the reading. Recheck mismatches against the original.

For an explicit wall position, check_wall_dimensions positions_json accepts:
[{"id":"pos-A","boundary_id":"<source wall ID>","axis":"x","lengths":[3000,2000],
"unit":"mm","origin_m":0,"direction":1,"node":1,"offset_m":0,
"basis":"annotation endpoint is the representative wall plane","source_refs":["plan.png: 3000 label"]}].
node selects cumulative endpoints (0 is origin). offset_m explicitly converts
that endpoint to the wall plane; unknown faces cannot be guessed. Results and
annotations are logged with the source hash. Repeat on revised candidates.

For wall thickness and annotation baselines, use check_wall_dimensions to list
actual source wall IDs, then calculate explicit endpoint conversions. Preserve
the representative room boundaries: offsets describe wall faces and do not move
rooms or openings. Do not assume an axis is centred or add half a wall thickness
to close a chain. Unknown offsets remain unknown even if total thickness is known.
Optional proposal fields wall_references and wall_dimensions persist these facts.
Prefer local edits of an existing record, retaining its original label/pixels and
the other records. A same-wall thickness label is a valid dimension observation,
not an invalid or redundant chain to delete merely because its sides conflict.
revise_bim operations:
{"op":"update_wall_dimension","id":"dim-A","changes":{"start":{"image":"plan.png"}},
"reason":"explain image evidence","source_refs":["plan.png: observed endpoint"]};
{"op":"update_wall_reference","id":"wall-A","changes":{"evidence_status":"inferred"},
"reason":"explain inferred scope","source_refs":["plan.png: observed versus inferred scope"]}.
Endpoint patches merge into the existing start/end, preserving untouched pixels,
wall_id and image. Other editable dimension fields are axis, direction, value and
unit; only change transcribed numbers when the original actually supports that.
Reference patches may change boundary_id, offsets_m, thickness_m, reference_basis,
thickness_scope or evidence_status. Keep derived output fields out of edits.
revise_bim also accepts {"op":"set_wall_references","wall_references":[...],
"wall_dimensions":[...],"reason":"image basis"}, replacing both whole lists.
Example wall reference (unrelated to supplied drawings):
{"id":"wall-A","boundary_id":"space/room/wall/1","offsets_m":[-0.08,0.16],
"thickness_m":0.24,"reference_basis":"declared representative axis; eccentric",
"thickness_scope":"unknown layer scope","evidence_status":"inferred",
"source_refs":["plan.png: local wall band observation or explicit assumption"]}.
offsets_m are signed along POSITIVE world x for a constant-x wall, or positive
world y for a constant-y wall, independent of which room owns the boundary.
Use offsets_m:null when unknown; thickness_m may also be null. Each reference
covers one complete straight source wall and its congruent counterpart. Partial
shared walls and explicit enclosure declarations are unsupported here. List only
local evidence you have; do not fill all walls with one guessed thickness.
Dimension example: {"id":"dim-A","axis":"x","direction":1,"value":5000,
"unit":"mm","start":{"wall_id":"wall-A","side":"positive","image":"plan.png",
"pixel":[100,200]},"end":{"wall_id":"wall-B","side":"negative",
"image":"plan.png","pixel":[500,200]},"source_refs":["plan.png: visible 5000 label"]}.
side is negative, positive, representative or unknown; axis labels count as
representative only if that correspondence is evidenced. Pixels are original
dimension extension endpoints. Pass dimensions in chain order only when related;
different wall sides are different chain endpoints. Tools preserve raw labels,
conversion terms and model residuals separately; they do not verify your reading.
Check endpoint pixels and world anchors describe the SAME face before calibrating.
Saved overlays show representative boundaries in magenta and declared wall faces
in blue, with observed/inferred/unknown status in metadata. A blue face is derived
from your claim, not independently detected. Geometry edits keep the representative
plane fixed for thickness updates; move_shared_wall carries attached face offsets
with the moved wall. Reflection with such evidence requires a full revised proposal.

Component thickness (a claims-bound edit; see claims for recording the value): inspect source wall/floor/ceiling boundary IDs via
check_wall_dimensions/include_inventory for walls, or existing source inventories.
revise_bim also accepts:
{"op":"set_component_thickness", "boundary_id":"space/room_A/wall/0",
 "thickness_m":{"claim":"claim_0002","value":"thickness"},
 "basis":"observed overall thickness; finishes included",
 "reason":"Preserve the explicit property without moving geometry"}.
The claim names this boundary. This updates optional component_attributes on a
new proposal/source; no space dimensions, opening geometry or level changes.
Shared full coincident sides receive ONE property record. Partial contacts and
open/unknown enclosure are not supported. Host identity is bound; later changing
that boundary requires explicit rebinding rather than silently reusing thickness.
""",
    'opening_review': """For a whole-floor plan review (without facade), use complete only after
inspecting ALL openings of that kind on that floor; use partial for a local check.
One mark represents one aperture/connection, not a crop with several doors.
Door swings, dimension ticks and window marks are different evidence. A note
saying one door does not remove a second modeled door. A review binds the reviewed
source; changed openings are unreviewed on the new candidate. Delivery retains current/old/unreviewed scopes;
prose cannot override those records. Do not fabricate marks to pass a checklist.

check_openings review_json example (unrelated to supplied drawings):
{"floor_id":"F1","kind":"door","image":"plan.png","coverage":"complete",
 "marks":[{"mark_id":"door-mark-1","box":[10,20,40,60],
 "opening_ids":["D1"],"space_ids":["room","hall"],"basis":"visible",
 "note":"one leaf and arc in a wall gap"}]}.
note is optional supplementary text; omitting it is equivalent to an empty string.
Use original-image pixels for box and enclose the entire observed aperture,
not only its label/arc. For registered plan images, both actual source endpoints
must fit the mark box; a mismatch is reported even if ID/kind/room all agree.
This checks location/containment, not exact width or visual truth. Without a
plan calibration, or for facade/elevation reviews, location_check says not_checked.
An exterior opening lists only its indoor
space ID in space_ids; never add an ID named 'outside' or 'outdoors'. Use no
opening_ids when an observed aperture has not been modeled. Separate paired
arcs serving different rooms into separate marks; a double-leaf door serving
one connection is one aperture. basis may be visible, inferred or uncertain.
The tool checks consistency with your observations, not their visual truth.
For an elevation review, add optional facade: North, South, East or West.
It limits coverage to exterior openings whose actual source host faces that
direction; a file name alone does not establish the physical facade. Complete
then means the WHOLE named facade for that floor/kind, not every facade on the
floor. Submit marks:[] when a fully inspected facade has no aperture of that
kind. All actual exterior directions, including zero-opening directions, need
complete reviews before facade reviews can cover a floor/kind. Interior or
unclassifiable openings stay explicitly uncovered. Without facade the original
whole-floor plan scope remains unchanged. Use partial for incomplete views.

""",
}

REFERENCES['partial_inference'] = """A method for completing a useful lightweight BIM
when the supplied exterior or mesh evidence does not reveal the whole building.
The model chooses the observations, architectural interpretation, level of detail
and revisions. Deterministic tools measure, expand repeated declarations, build
hosts, preserve IDs and report geometry; they do not choose the interpretation.
The orchestrator may perform the work itself. Only use worker delegation when an
explicitly configured tool is available; worker results are bounded evidence for
the orchestrator's judgment and do not automatically change source geometry.

AUXILIARY INPUTS. Assign a purpose and precision to each relevant piece of evidence,
including local plans, hand sketches and text. A diagram may establish adjacency,
core continuity or a terrace without supplying metric geometry. Do not infer measured
lengths, angles or wall thicknesses from hand-drawn strokes. A stated dimension
applies only to the object or region it describes; it does not calibrate unrelated
parts of the sheet. When an auxiliary region is a reliable measured drawing, use the
reconstruction reference for that region and relate its coordinates to the mesh frame.
Keep user-confirmed information, user speculation and model assumptions distinct.

NATIVE MESH EVIDENCE. When inputs contains a mesh_input, inspect_mesh, view_mesh and
measure_mesh_pixels access the admitted ORIGINAL textured GLB. Choose cameras,
targets, spans and bounds for the uncertainty at hand; there is no required set of
screenshots. Query bounds before metric close-ups and measure visible surface pixels
instead of guessing scale. Mesh local coordinates are Z-up
[GLB.x,-GLB.z,GLB.y], optionally rotated in xy by an explicit yaw_degrees. Keep one
declared frame for construction and evidence. Prepared mesh views instead use their
supplied metric projection and explicit transform; local x/y need not be geographic
east/north.

Original OPAQUE and BLEND materials retain their alpha. Visible fractional-alpha
contributions have no unique measured XYZ: point queries return
reason='transparent_composite', hit=false and background=false; correspondence
excludes them from residual summaries. Alpha-zero fragments do not occlude; a
fully opaque front surface remains measurable. Sections intersect original carrier
triangles, not alpha-cut texture shapes. Excess fragment/layer budgets reject the
view explicitly. MASK, full PBR lighting and refraction are not implemented.

inspect_mesh_directions returns area-weighted directions of selected near-vertical
triangles. These directions are surface evidence, not the yaw to apply. Parallel
directions retain quarter-turn and half-turn ambiguity; resolve orientation from
the whole asymmetric footprint, wings and identifiable faces. Pixel hits include
triangle normals and tilts. A point on a roof, slope or noisy remnant does not by
itself establish a wall edge. Compare independent surfaces, texture, local sections
and adjoining remnants before adopting a frame or missing volume.

The saved mesh-to-BIM relation is source XYZ =
rotate_xy(yaw)*original_Zup + translation_m. set_candidate_mesh_frame records it
on a new candidate; direct build_bim can declare the same mesh_frame with the mesh
hash, yaw, translation, reason and source references. A viewing camera supplies no
implicit frame. Translation cannot repair a wrong orientation. Frame correction
does not establish footprint, height, rooms or openings. revise_bim preserves the
frame, and obsolete frame notes must be explicitly replaced. overlay_mesh_candidate
projects the ACTUAL saved source edges onto a saved mesh view without fitting;
its default visibility='visible' filters by host facing and occlusion by the saved
source geometry. boundary_ids can select arbitrary wall hosts, including oblique
ones; other source geometry still occludes them. Inspect the returned host IDs and
visible edge samples: projected objects that are not visible remain in the record.
This visibility test does not compare depth with the original mesh and does not
establish alignment or observed window identity. Optional visibility='xray' also
shows hidden source edges; never count every green frame as a window visible on the
current facade. Use suitable whole-building, side, top and local views to distinguish
orientation, displacement and shape errors.

NATIVE MESH HANDOFF. After inspecting the intended final saved source and its
overlays, finish_bim can carry reviews_json as a JSON object:
{"mesh_review":{"coverage":"whole_building","summary":"Scope checked, architectural
interpretation and remaining uncertainty","overlay_ids":["overlay_001"]}}.
Use coverage="partial" for unfinished scope. Choose relevant current overlays;
there is no fixed camera count or required sequence. Code binds the declaration
to the actual source, original inputs and reviewed image bytes. This is your scope
judgment, not an independent accuracy verdict. A current whole_building declaration
plus source validation status='pass' and a saved viewer permits completion. Missing, partial or stale
declarations remain useful saved handoffs but do not finish the runtime. Drawing
floor completeness stays a separate field and may be unknown for a mesh input.

OBSERVATION AND INFERENCE. Separate what the source shows, what it constrains, what
you infer, what you deliberately simplify and what remains unresolved. Missing or
cropped mesh surfaces are missing evidence, not proof of an opening, blank wall or
absent volume. A plausible completion may combine cut remnants, adjoining face
directions, texture, repeated facade patterns, local sections and architectural
context. State why the completion is preferred and retain viable uncertainty.
When several interpretations fit the available evidence equally well, prefer the
simpler useful one. Preserve real spaces, separating walls, apertures, shafts and
major setbacks. Minor facade relief, trim or roof blades can be explicitly omitted
or represented as detail evidence at the requested scale; do not invent a narrow
room or equipment space merely to display a geometry-only solid. Component
thickness metadata does not create that solid. Record the simplification and its
scope instead of assigning a fictitious room use.

Use observed metric extents where they are reliable. Within noisy or incomplete
evidence, choose reasonable architectural dimensions and regularize consistently,
while labelling those values as inferred rather than measured. Save important
interpretations with record_inference. Its declaration contains statement, basis
(observed, inferred or simplified), reason and source_refs; optional object_refs
bind spaces, boundaries, openings or floors, and missing_information names evidence
that was unavailable. Observed records need a source reference; inferred or
simplified records need a source reference or explicit missing information.
inspect_inference reopens immutable records and their binding status. These records
keep evidence and assumptions close to affected source objects; they do not turn an
inference into an observation.

ARCHITECTURAL INTERPRETATION. First understand the relationship among the main
volume, wings, annexes, storeys, exceptional levels, roof forms and missing regions.
Exterior evidence constrains a plausible internal organization but rarely determines
one. Select the requested simplification directly: preserve actual physical rooms
and circulation that the chosen interpretation needs, and keep a continuous open
space intact when there is no proposed physical partition. Do not split rooms merely
by orientation, thermal convenience or rectangular decomposition.

Facade windows constrain room depth, bay rhythm and where partitions can meet the
exterior. Partition ends must respect observed apertures and intervening wall strips;
never clip, merge or swallow a window to make a room fit. Window groups do not
determine the number of rooms. Choose plausible room sizes and circulation from
building use, the requested simplification and available interior information;
unsupported interior subdivisions need no reconstruction. A window group is an
aperture, not each pane. Visible open space, room use, circulation, corner conditions
and explicit user information can support different layouts.
Preserve exceptional windows rather than forcing every storey or facade into one template.

Assign common room types from get_bim_reference('room_types') using building context,
furniture or circulation where available. Prefer a plausible common type over
unknown when evidence supports one, but keep classification secondary to physical
spaces and connectivity. Do not label a large upper or roof volume as office merely
because it has floor area. Distinguish plausible occupied rooms, attic, mechanical
space, stair or lift continuations, shafts, cavities and geometry-only roof parts;
mark uncertain roof use as inferred or unresolved.

Vertical circulation and service cores may be continuous spaces through several
storeys when that interpretation fits the evidence. Model the continuous volume and
its actual contacts; surrounding room polygons exclude its footprint, while each
served storey includes it through explicit spanning_space_ids membership. Do not clone
the core per storey or insert fake intermediate slabs simply to fit a repeated-floor
template. Conversely, do not invent a continuous void when separate rooms or real
floors are supported.

An identified lift service storey needs a landing door connecting its accessible
floor space or lobby to the shaft; continuous shaft membership alone supplies no
door connection. If the service pattern is established but a floor's door position
or size is unseen, reuse a compatible observed landing layout with inferred
position/size and source references, checking the actual host and floor elevation.
Do not omit intermediate landing doors merely because only ground/top plans are
available. Keep uncertain service scope explicit, and do not mechanically add
doors to machine rooms, equipment ranges or levels the lift does not serve.

Place doors from the intended circulation and the actual separating partition.
Choose a plausible entrance side before a position. Allow usable jamb or wall return,
door-leaf clearance and stair or landing clearance; repeated adjacent rooms may use
paired or consistently offset doors when appropriate. Do not mechanically select the
longest shared wall, put every door at its midpoint or use a fixed offset for every
building. A door connects already distinct spaces and must not substitute for a
missing or invented partition. Door sizes, states and positions without direct
evidence remain architectural inferences.

SCALE AND SIMPLIFICATION. Adopt main dimensions, shared reference offsets and
heights on the run's 0.1 m architectural scale. Preserve measured values as
observations; the program keeps the adopted copy and its audit. Align compatible
major roofs/storey datums and reuse one storey height for a repeated range.
Start from a simple envelope; variations below roughly 0.3 m are candidates for
omission/alignment when they change no meaningful outline, opening, gap or space
relationship. This is not an automatic merge distance. Protect genuine narrow
shafts, cabinets, entrance recesses and actual wall/gap identities. Explicitly
declare meaningful exceptions; do not chase millimetre bumps to clear a numeric
residual. Geometry and host rejection must be repaired, but unreviewed metadata
and optional fine detail do not require another full modelling cycle.

ASSEMBLY AND REVISION. build_parametric_bim can expand explicit storey/space
templates and aperture spans; read get_bim_reference('parametric') for its contract.
Repetition is a declared hypothesis, not evidence, and exceptions stay explicit.
A continuous core can be a separate tall instance referenced by the served storeys
through spanning_space_ids. build_bim remains available for geometry that does not fit the compact
template. Keep source references and stable IDs on observed and inferred objects.
When feedback exposes a host, boundary or interpretation error, revise that object
or its local declarations and preserve reliable geometry, openings and evidence.
Never shorten or delete an observed aperture merely to clear a host failure.

SOURCE REVIEW AND DELIVERY. Inspect the source that was actually saved, not only the
proposal or compact plan. Compare its overlays and views with the original evidence
for overall massing, levels, roof volumes, openings and preserved wall strips. Review
the internal result for physical partitions, usable room scale, continuous cores,
door hosts, circulation and the requested simplification. audit_inference_candidate
reports saved-source memberships, ranges, window distribution, door host/end
clearances and connectivity; it supplies no truth, code-compliance, threshold or
acceptance verdict. Use exact entity diffs when revising to verify the intended
objects changed and reliable objects did not. Geometry success, counts and a visually
plausible render do not prove fidelity. Deliver the saved source and viewable result
with observed, inferred, simplified and unresolved scope stated honestly.
"""

REFERENCES['parametric'] = """build_parametric_bim(plan_json) takes this compact JSON structure:
{
 "templates": {"typical": {
   "footprint": [[0,0],[12,0],[12,8],[0,8]],
   "spaces": [
     {"id":"office","role":"office","rect":[0,0,9,8],
      "source_refs":["explicit illustrative layout hypothesis"]},
     {"id":"hall","role":"corridor","rect":[9,0,12,8],
      "source_refs":["explicit illustrative circulation hypothesis"]}],
   "window_rows": [{"id":"northrow","facade":"North","plane":8,
      "spans":[[1,3],[4,6]],"z":[1,2.5],
      "source_refs":["actual supplied image and pixel bounds"],
      "assumptions":["example only"]}],
   "doors":[{"id":"office_door","space":"office","other_space":"hall",
      "p1":[9,3],"p2":[9,4],"z":[0,2.1],
      "source_refs":["hypothetical interior door"]}]
 }},
 "instances": [{"id":"L1","template":"typical","z":0,"height":3},
               {"id":"L2","template":"typical","z":3,"height":3}],
 "connections": [],
 "assumptions": ["Example only; unrelated to current building"], "unresolved": []
}
All x/y/plane/span values are in the ONE common building frame. Instance z is
absolute. Window row and template door z are RELATIVE to instance z. A template
may contain many rows with different heights/spans. Repetition is your explicit
inference, never automatic evidence. Instance IDs and local IDs cannot contain ':';
expanded space IDs are INSTANCE:SPACE, windows INSTANCE:ROW:1 (one-based),
doors INSTANCE:door:DOOR. Only provide listed fields; no hidden variables/expressions.
The default schema_version is "2": spaces use EITHER rect:[xmin,ymin,xmax,ymax]
OR polygon:[[x,y],...], with id, role, source_refs and optional assumptions.
Default footprints and spaces are single orthogonal rings; clockwise input is
normalized without coordinate movement. For nonorthogonal/curved source spaces,
set top-level schema_version="source_polygon_v1". Triangles and arbitrary simple
polygons are then allowed. Supply top-level boundary_curves and use boundary_path
instead of a space polygon; footprint may be an object with boundary_path or
vertices and optional holes (see source_curves). Repeated floors reuse the same
declared XY curves and tolerance. One space with holes remains unsupported.
Code derives bounds but never splits spaces. Local spaces plus explicitly declared
spanning members must cover the instance footprint exactly, without overlap. An
instance may set spanning_space_ids:["CORE:core"] using final global expanded IDs.
These spaces must cover that entire storey height; they are referenced, never cloned.
Keep the core inside the overall storey footprint and outside its local room polygons.
Different instances may have independent footprints, setbacks, heights and base levels.
Never insert fake intermediate slabs to simplify a core. Holes within
one space ring are unsupported: do not split a continuous open room just to fit.
For windows declare facade, plane, spans, relative z, id and source_refs; optional
assumptions. The code resolves each whole span to exactly ONE outward room edge
on that plane. It refuses spanning a partition, wrong plane or wrong direction;
no window clipping, wall movement or answer inference. The complete source kernel
then checks exterior status, contacts, overlaps and host heights. North/South
span along x, East/West along y; these names refer to the LOCAL frame.
Template doors identify local space and other_space (null=outdoors), p1/p2,
relative z and source_refs. kind defaults door; state defaults unknown.
With source_polygon_v1, opening_rows handles any straight wall direction:
{"id":"slant","space":"office","kind":"window","p1":[6,0],"p2":[8,4],
 "spans":[[0.5,1.5],[2,3]],"z":[1,2],"source_refs":["actual local observation"]}.
spans are metres along the directed p1-to-p2 baseline, not fractions or an axis.
Every complete span must fit one actual host; no clipping or automatic splitting.
The baseline does not move or rotate spaces. other_space defaults null; state
defaults unknown (open for kind open). Each output ID is INSTANCE:row:ROW:1.
For curved facades, explicitly locate windows on the chosen discretized chord;
do not stretch one planar opening across multiple chord hosts. Different template
instances may use different templates for exceptions without editing the repeated
base. An optional top-level mesh_frame is preserved on the expanded candidate.
Cross-instance connections may still be ordinary geometry.openings records with
GLOBAL space_id/other_space_id and ABSOLUTE z (see geometry reference).

REPEATED STOREYS. Keep exceptional or continuous volumes in instances. Add repeats:
[{"id":"office_run","template":"typical","id_prefix":"O","start":1,
  "count":20,"id_padding":2,"z":8.4,"height":3.2,
  "spanning_space_ids":["L1:shaft"],"source_refs":["declared repeat inference"]}].
This creates O01..O20 at z+i*height. Increasing count expands in code; do not
resubmit repeated geometry or enumerate each service door. Repetition is still
your architectural judgment, not newly observed evidence. Total expanded
instances must be 1..100. Optional exclude_ids names exceptional IDs; each must
be supplied as a complete explicit instance. Exclusion keeps its position in the
height sequence and removes it from that repeat's default service selection.
No expressions, interpolated geometry or automatic matching are evaluated.
Template spaces may include role_evidence using the existing role/basis/
source_refs/assumptions contract; code copies it to the repeated spaces.

CORE SERVICE CONNECTIONS. Add core_connections, for example:
[{"id":"lift1","core_space_id":"L1:shaft","space":"hall",
  "repeat_ids":["office_run"],"instance_ids":["special"],
  "p1":[9,3],"p2":[9,4],"z":[0,2.1],
  "source_refs":["observed landing reused as explicit inference"]}].
repeat_ids/instance_ids select an explicit nonempty service set; use only IDs
actually declared in this plan. The local space is INSTANCE:hall, the global
core is unchanged, and z is relative to each service floor. Generated IDs are
INSTANCE:core:lift1. The core must be a spanning member covering that entire
floor; each generated aperture must pass the ordinary source host/height checks.
Duplicate service declarations and an already explicit matching connection are
rejected. Defaults kind=door,state=unknown; open passages require kind=open,state=open.
An exceptional door layout uses a separate disjoint service selection. Equipment,
roof and non-stopping floors are never inferred as stops merely by intersection.
Unknown service is a question/assumption to resolve, not permission to stop everywhere.

ARCHITECTURAL ADOPTION. New native-mesh manifests declare
architectural_modelling_policy={"schema_version":"1","grid_m":0.1,
"simplification_tolerance_m":0.3}. The tool regularizes main supporting-line
offsets and vertical levels before expanding repeats; original plans/observations,
the adopted copy and audit remain separately bound. Historical runs without this
policy keep their recorded behavior. Do not round every world vertex yourself.
Optional plan.modelling adds explicit construction context, for example:
{"frames":[{"id":"wing","origin":[10,20],"yaw_degrees":17,
"templates":["typical"]}],"vertical":{"datum_m":0},
"space_intents":{"typical:shaft":"shaft"}}.
Template coordinates are still source XY: a frame describes the local rulers
used for adoption, not another transform to apply to the input. Omitted templates
use the source identity frame. mesh_frame stays the independent source-to-input
relation. Shared parallel supporting lines use shared offsets; oblique intersections
retain derived decimals. Existing opening endpoints follow their same host.
An acute corner whose derived movement exceeds one grid step rejects instead of
silently magnifying a small line adjustment into a large envelope change.
Curved-template XY and narrow-width checks are explicitly not_assessed, preserving
declared curves; normal source checks still apply. Thickness attributes are separate.
For a justified common roof level, vertical.merge_groups accepts
[{"id":"roof","semantic":"one simplified plant roof level","targets":[
{"kind":"instance","id":"R1","edge":"top"},
{"kind":"instance","id":"R2","edge":"top"}]}].
Kinds instance/repeat and edges base/top refer to declared ranges; repeat top is
its entire range end. Compatible uses/edge meanings and the 0.3 m cap are checked.
A repeated range keeps one grid-aligned height: if accumulated change would move
its main endpoint by over one grid step, resolve the intended main levels once;
do not hide the residual in a thin storey. Real volume contacts/gaps are preserved.
space_intents accepts room/shaft/cabinet/component. Narrow geometry-only
blade/parapet spaces require a semantic correction or an explicit omission;
component is a diagnostic intent, not a supported solid-component constructor.
Never widen/delete a genuine shaft simply to clear this diagnostic.
Generic build/revise applies the same policy to floor footprints/cells (or the
existing explicit common footprint_x/y rectangle),
retaining IDs and current edits. Local frame context follows a bound parent.
Ambiguous hosts, absent footprint declarations or stale component bindings
reject with an audit; use an explicit parametric declaration for those cases.

LOCAL PARAMETRIC REVISION. The saved compact plan and expanded source remain bound.
inspect_parametric_plan(candidate=...) returns an exact/stale status, plan hash
and summary; read pointer="/templates/typical" or full=true for details. Large
values are paged by offset/next_offset. A failed declaration is readable using
draft=the returned draft id instead of candidate. Apply add/replace/remove/test
JSON Pointer operations with patch_parametric_bim and expected_plan_sha256;
preserve reliable IDs/geometry/evidence and explain each change.
For every add/replace/remove, include reason and nonempty source_refs, for example
{"op":"replace","path":"/templates/typical/spaces/0/rect/2","value":6.2,
 "reason":"Correct measured exterior extent","source_refs":["mesh_004 span"]}.
Use inspect to find exact JSON Pointer array indices; never guess them. Never resend an
entire unchanged plan just to move one coordinate. A generic revise_bim candidate
can diverge from its ancestor's parametric plan: stale_lineage is reference only,
and patch refuses to overwrite those later source edits silently. Keep template
changes in the parametric path when they remain naturally expressible there.
Resolve all currently known actionable changes in one patch, then compare the
actual saved source. A repair to a failed draft retains its last hash-bound valid
feedback baseline. Repeated equal diagnostics can share a representative plan;
all affected IDs and complete findings remain in the saved selection record.
Return feedback is a geometric check, not input fidelity approval.
"""

REFERENCES['source_curves'] = """Explicit curves for source_polygon_v1 only; metres, one XY frame.
Declare geometry.boundary_curves as a bank keyed by stable IDs, for example:
{"arc":{"kind":"circular_arc","center":[0,0],"radius_m":4,
 "start_degrees":0,"sweep_degrees":90,"max_chord_error_m":0.02,
 "source_refs":["observed arc or declared approximation"]},
 "a":{"kind":"line","p1":[0,4],"p2":[0,0],"source_refs":["basis"]},
 "b":{"kind":"line","p1":[0,0],"p2":[4,0],"source_refs":["basis"]}}.
Use boundary_path:[{"curve_id":"arc"},{"curve_id":"a"},{"curve_id":"b"}]
on a cell instead of polygon, or on a floor footprint instead of vertices.
reverse:true traverses a bank entry backward. Paths must connect and close within
the reported numerical tolerance (1e-8 m);
code never snaps, closes a gap or chooses a curve from texture. Shared curve IDs
use the same samples in both directions, preserving coincident boundaries.
The normal source builder validates derived polygons and actual opening hosts.
Original declarations, generated points and per-segment provenance are retained.
Read saved source geometry for the actual chord endpoints, bounds and error.
An explicit opening may select one chord using
curve_segment:{"curve_id":"arc","segment_index":0,"span_fraction":[0.1,0.9]}
in place of p1/p2; its z is still absolute. This remains ONE planar opening.
Cross-chord/curved glazing is not silently split or counted as multiple windows.
Changing a curve requires an explicit revised full proposal/template, preserving
evidence and rebuilding derived endpoints. Do not retain stale polygon/p1/p2
values inconsistent with the changed declaration. Per-floor footprint holes use
explicit interior rings; actual spaces must cover the remaining floor area without
fake walls. A single room with a hole, arbitrary splines and continuously sloped
spaces are unsupported. These limits are independent of observed mesh completeness.
"""

# A2-T replaces the T1 sm25 register/adopt/confirm sequence (61/147 calls,
# step 115 whole-proposal rejection) with one audited transaction. Arithmetic,
# source location and drawing interpretation remain separate conclusions.
REFERENCES['claims'] = """Evidence transactions for drawing counts and existing-candidate parameters.

Use claim_transaction(candidate, entries_json). entries_json is a list; each
entry has claim (a new observation) OR claim_id (an existing one), action,
reason and operations where needed. Entries run in order, using the candidate
returned by the preceding successful apply. Each entry commits independently;
failures keep earlier results and any recorded claim/decision. Read each status
and audit_file. Do not blindly retry a partly completed transaction.
- action=confirm records/adopts and verifies unchanged parameters, without a new BIM.
- action=apply records/adopts and revises using the same operations as revise_bim.
- action=record records/adopts only; action=decide changes an existing decision.
  disposition is adopted (default), deferred or retracted; confirm/apply require adopted.
  An already retracted claim cannot be automatically revived by confirm/apply.
Entry reason explains YOUR evidence-based decision; if omitted it uses the claim's reason.
In confirm/apply, a claim-bound operation with omitted reason inherits that entry
reason. Explicit operation reasons remain unchanged; unrelated edits need their own reason.
No decision, applied value or confirmation certifies drawing truth.

Example entries_json (synthetic numbers, NOT a case answer):
[{"claim":{
  "objects":[{"kind":"opening","id":"door_A"}],
  "basis":"annotation_and_pixels",
  "reason":"This door's labelled chain; world origin is 3 m and direction is down.",
  "sources":[{"view_id":"view_0001"}],
  "values":{"height":{"type":"dimension_chain","lengths":[900,1800,300],
    "unit":"mm","origin_m":3.0,"direction":-1,"segment":1}},
  "observation_mode":"candidate_review","unresolved":[]},
 "action":"apply","reason":"Apply this door's observed extent",
 "operations":[{"op":"update_opening","id":"door_A",
   "changes":{"z":{"claim":"$claim","value":"height"}},
   "reason":"Apply the recorded chain"}]}]
A new claim defaults to the current candidate. $claim refers to that entry's ID;
existing IDs also work. Each apply/confirm entry references its own claim; use
separate entries for different claims. Other explicit edits may accompany an
apply; unbound parameters remain recorded as such. The example computes absolute
z=[0.3,2.1]. Transcribe actual labels and explain their datum and physical extent.

Use inspect_candidate/read_candidate_items for exact IDs. Object kinds are
window (geometry.windows), opening (geometry.openings: doors/passages), space,
and boundary (exact source boundary ID). The unified source openings list also
contains windows; their claim kind is still window. Do not alter IDs.

basis: annotation_and_pixels, pixels, visual_estimate, inference or declared.
Image bases require sources. Declared/inferred values may have none; label the
actual basis and unresolved evidence. Sources accept {"view_id":"view_0001"}
from view_image OR {"image":"elevation.png","box":[20,30,100,200]} in original
pixels. Do not combine the forms. Code binds original bytes and view metadata;
whole-image references without box remain unlocalized. View the opening, its
labels and dimension endpoints before adopting. view_image(claim_id=...,
source_index=..., display_scale=1..8) reopens saved crops; indices are zero-based.
Seeing a crop is not OCR or proof of interpretation. observation_mode is direct
or candidate_review, a caller-reported distinction, not independent verification.

Value types:
- literal: {"type":"literal","value":0.18,"unit":"m"}; a scalar or a vector/interval.
- dimension_chain: as above; zero-based segment selects its ordered metre span.
- image_axis: {"type":"image_axis","image":"elevation.png","axis":"y",
  "anchors":[[10,3.0],[210,0.0]],"pixels":[40,180]}.
  Explain which world x/y/z the image axis represents. One pixel gives a scalar;
  two give an ordered interval; reduction="midpoint" gives the two-face midpoint.
  Pixels/anchor pixels may use {"profile":"profile_001","candidate":"C01","at":"start"}
  (also peak/end) from view_pixel_profile. Code checks original image/axis/hash.
  Ink candidates are not automatically physical walls or apertures.
For p1/p2 use a literal [x,y]; an image_axis interval is not a 2D point.

Supported references replace parameter values: update_window.z/span;
update_opening.z/p1/p2; move_shared_wall.coordinate_m (name both spaces);
add_opening.opening.p1/p2/z (name existing host spaces, both for an interior door);
and scalar polygon coordinates in reshape_spaces, e.g.
{"op":"reshape_spaces","spaces":[{"id":"room_A",
 "polygon":[[0,0],[{"claim":"$claim","value":"wall_x"},0],
 [{"claim":"$claim","value":"wall_x"},5],[0,5]]}],"reason":"Measured wall"}.
Keep unaffected coordinates literal, reference every changed occurrence, and
explicitly revise affected openings as usual. Source references are added by code.
Measured endpoints and assumed heights need separate claims; a plan source must
not masquerade as observed height. New identity/kind/connectivity are declarations.
Confirmation supports window/opening, shared-wall and reshape intents; every
confirmed parameter must be bound. A reshape confirmation therefore needs every
coordinate bound; use window/opening intents for ordinary height confirmation.

Optional value_targets maps EVERY value to the subset of declared objects it
supplies, e.g. {"wall_x":[{"kind":"space","id":"room_A"}],"face_interval":[]}.
[] denotes supporting evidence, never an applicable or missing parameter.
Without value_targets, every value targets every object. Do not conflate objects
or floors just because numbers match. Claims and originals remain immutable.
An unmodified target can inherit only along the actual candidate ancestry while
its geometry, physical host, source views and recomputed values remain unchanged.
The return names any changed item; handle unchanged targets separately and
reobserve changed ones. A notes/use-only change can retain physical observations.
A moved-then-restored target is conservatively invalid. No duplicate observation
is needed merely to change the parent hash. A confirmation is still required to
establish that an unapplied value matches the current parameter.

If only a source region is wrong, replace_claim_sources with actual view_ids and
a reason saves a new claim with the same objects/values and retracts the old one.
Adoption/confirmation does not transfer; inspect the new sources and transact its
new claim_id. If the interpretation/values change, record new evidence and use
action=decide, disposition=retracted for the obsolete claim. A wider view alone
does not change a saved narrow source.

For counts before or after BIM, use an entry with action=record and claim:
{"observation_type":"facade_count","image":"elevation.png","floor_id":"L1",
 "facade":"South","window_count":4,"reason":"Synthetic whole-facade count"}.
No candidate, decision or operations is needed for counts. Replace floor_id with
floor_plan_image before building. Optional box gives a source region; door_count
is separate. Include observed zero. Counts cover the WHOLE named floor/facade,
not just a crop. Re-record to correct the same scope; different images are
compared, never summed. facade_counts reports missing/conflicting totals and
unresolved scopes on saves/checks/delivery; matching counts prove no positions.

height_coverage lists evidenced/problem openings; other missing bindings are counted.
Full rows in details_file retain z, status, claim IDs and views.
Location needs that facade's explicit elevation calibration and a source
region containing this opening. A region containing several openings is marked
needs_per_opening_confirmation, even when numbers match; narrow/replace the
source with each opening's own evidence. Internal heights remain in the source
inventory. Status is not proof that a dimension chain was read correctly.

claim_status(candidate) reports confirmed_unchanged, applied_current,
pending_application, partially_satisfied, changed_since_check and decisions,
with missing bindings and unresolved evidence. Other branches stay separate;
imported prior-run applications are inherited/not rechecked. claim_status()
returns history. A failed edit keeps its parent and audit; a failed source
candidate may remain for inspection. Unbound geometry is untracked, not thereby
wrong. finish_bim preserves execution failures and adopted-but-unapplied evidence.
"""

from src.agent.roles import room_types_reference
REFERENCES['room_types'] = room_types_reference()
REFERENCES['naming'] = """Public naming (bim_names_v2), generated by code, never manually rename IDs:
Floors F1,F2,... in ascending base elevation (a sequence, not a surveyed storey label).
Rooms Z01_F1_Office_SW: global serial, floor, catalog name token, model-XY location.
Within a floor order by centroid north to south, then west to east. Location uses
the whole-building footprint bounding box; model +X=E and +Y=N, not proven true north.
Walls Z01_W1,W2,...: from the southernmost vertex, westernmost on a tie, traverse
CCW as viewed from above. Windows Z01_W1_Win1, doors Z01_W1_Door1, empty openings
Z01_W1_Opening1: number each kind along that directed wall, then by bottom height.
Shared openings have one source ID and two room-side aliases. Floor/Ceiling/Roof
suffixes follow actual boundary kind; source ceiling does not assert exterior roof.
Viewer fragments append Part1, open/unknown regions Open1/Unknown1, edges Edge1.
Geometry/ordering edits may change public numbers; stable source IDs and all
host/connection/evidence references stay unchanged. Unknown role is never office.
"""
