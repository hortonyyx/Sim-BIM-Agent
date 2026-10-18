"""Role-specific drawing guidance derived from the unchanged single-model text."""

from __future__ import annotations

import hashlib
import json

from scripts.tool_scripts.bim_agent_guidance import build_guide
from src.agent.runtime_roles.readers import READER_TOOL_NAMES
from src.agent.roles import room_types_reference
from .plan_format import READER_PLAN_EXAMPLE

_SHARED_READER_SCOPE = """You receive exactly one original image and its original-pixel grid.
Use that image and its returned views. Deliver this drawing's reading; assembly and
cross-drawing decisions belong to the orchestrator. Boxes use [left,top,right,bottom]
ORIGINAL pixels. The assigned drawing type is known; report a type conflict only with
specific visible evidence, rather than repeatedly reclassifying the task."""

_PLAN_CORE = """You are the PLAN reader for task.target (one floor).
Preserve actual rooms, walls, openings and connections; use stable IDs. The assigned datum
is fixed: world x East, y North, z Up. A whole-building bounding-box origin can lie outside
the footprint; do not replace it with a physical corner. Points and first anchor values
are ORIGINAL pixels; second anchor values and thickness are metres. Set reading_type="plan".
Omit unknown heights: this task produces a 2D reading, not source BIM."""

_PLAN_FIRST_DRAFT = """Read the supplied image once for the complete footprint, partitions, room uses, openings,
main dimension chains and visible thickness exceptions. Enlarge unreadable details only;
inputs, whole-image rereads, view_plan_blocks and line-by-line profiles are not required.
Submit a complete first trial as soon as the structure is represented. Example:
{example}
Replace synthetic values with observations. Use partitions[].points, openings[].p1/p2 and
space_seeds[].point. One seed labels one connected room, including a continuous corridor;
labels never create boundaries. Do not duplicate the footprint as partitions.

Trace exterior OUTER faces, including recesses, and interior centerlines. One physical wall
band has one representative line. Reuse coordinates at shared wall junctions and put each
opening wholly on its host segment, without crossing a T-junction. Exterior openings lie
on footprint, interior doors/passages on partitions that continue through the aperture.
One aperture includes both jambs: a double-leaf door is one opening. Wall piers, swing arcs
and nearby junctions are outside its span. Only exterior windows are supported.

Supply main dimension_chains with id, axis=x/y, segments_mm, ordered tick_pixels and
source_refs. Ticks are original axis numbers or [x,y] points at the actual extension lines;
keep different wall faces/axes distinct. total_mm/start_world_m are optional when unknown.
Prefer clearly bracketed aperture dimensions over rough estimates; add a single-segment
chain for an uncertain or conflicting span, not for every clear opening. Code accumulates
shared nodes and adopts 0.1 m coordinates while retaining readings. Do not hand-round or
remeasure merely to remove sub-grid tails.

Every wall needs positive thickness at 1 cm resolution. Use wall_metadata defaults for
footprint:* and partition:*, then only visible exceptions at footprint:<edge>,
partition:<id> or partition:<id>:<segment>. Rows need thickness_m, evidence_status,
short basis and source_refs. Batch defaults are inferred; observed requires direct evidence
for that exact segment. A thickness annotation must bracket the wall across its width,
not a nearby pier or door. Otherwise choose a plausible common thickness and mark inferred;
no separate per-wall measurement pass. Common cm candidates:
6,7,8,9,10,12,13,14,15,16,18,19,20,22,24,25,26,28,29,30,35,36,37,40,45,49,50,55,60,62,
65,70,75,80,85,90,95,100,110,120,130,140,150,160,180,200; other positive 1 cm values allowed.
Default expansion is exterior inward / interior half each side: omit side fields.
For a face-traced exception give expand_direction=up/down/left/right toward WALL MATERIAL
in the original image; code converts it, so do not also give reference_side.
Thickness-aware merging selects an existing anchor within 2 * larger thickness, then the
10 cm grid, never an average; openings follow hosts. Cross-floor matching is assembly work.

Object source_refs may be omitted (image and ID are recorded); keep explicit annotation
references when useful. Use room roles from the table; unclear use is unknown. Optional
inferred role_evidence needs assumptions. Record each material uncertainty once; omitted
heights and absent plan north arrows do not need repetitive unresolved entries.""".format(
    example=json.dumps(READER_PLAN_EXAMPLE, ensure_ascii=False, separators=(",", ":")))

_PLAN_REVIEW = """Inspect the returned clean plan and gray-original/color-adopted overlay together.
The clean plan shows thickness and a dashed reference line; the overlay exposes placement.
Check missing/extra walls, room connectivity, opening sequence, both jambs and clear widths,
plus thickness/direction exceptions. drawing_differences and topology_issues locate possible
conflicts; no hint or matching counts do not prove correct identity. Accept audited grid and
thickness-normal shifts for correctly read features; do not polish small permitted offsets.
Use a targeted crop/profile only for a specific conflict unclear in these views.

Batch visible corrections in one trial using operations against the latest editable draft
(including failed revisions). When moving a shared wall, update incident endpoints and
openings together. Never remove a real object or alter room topology just to pass.
Each topology issue needs an implemented retain_opening/continuous_space decision with
issue_id, basis and bbox; for a false separator remove it, its opening and redundant seed
while retaining real wall portions. No issue means no topology_decisions.
read_plan_draft without arguments locates saved fields/IDs; field plus object_id reads one
object. Follow read/next pointers for more, rather than rereading the entire plan.
Operation schema is in trial_plan_bim; get_bim_reference(plan_editing) gives detailed repair
and note rules if needed. Full replacement plans remain accepted."""

_ARTIFACT_SCHEMA = """After checking the latest passed trial's actual views, submit_plan_reading with
trial_id="latest" and local_review (text or {summary:text}). Use receipt counts if reporting
totals; do not resend plan/evidence or duplicate wall_metadata as wall_reference.
A geometry/source change needs its new views; a verified metadata-only revision can reuse
unchanged views. Omit empty optional fields. Notes may annotate plan fields/object IDs or
trial fields, never change geometry or invent observations. Keep unresolved delivery
questions. Mirrored axes require original north_arrow evidence matching axis_orientation;
normal orientation needs none. Acceptance ends the task automatically."""

_PLAN_EDITING = """Edit the latest editable declaration, including failed revisions. Each operation needs
reason, source_refs, original-pixel bbox and: update(collection,id,changes without id),
add(collection,value complete row), remove(collection,id), or set(field,value).
Collections: partitions/openings/space_seeds. Set can change other fields except floor_id.
Use 1-100 operations with each row/field once; moving a wall also needs its incident endpoints
and openings updated. Ambiguous IDs need complete replacement. Cross-task rework preserves
unpointed objects. Notes change annotations only, not geometry or room-use permissions.
read_plan_draft(field='plan') browses bounded pages; plan:/ and report:/ JSON pointers select
nested fields; object_id='page:8' continues. trial_id='reference' selects an inherited draft.
Optional submission notes={item,kind,basis}, kind=assumption/inferred/unresolved. item uses
plan.<field>, an exact object path/ID, or trial.<field> actually in the receipt. Short aliases
must be unique. Omitted 2D height fields are addressable but notes do not populate them."""

PLAN_READER_REFERENCE = "\n\n".join((_PLAN_CORE, _PLAN_FIRST_DRAFT, _ARTIFACT_SCHEMA))
PLAN_READER_GUIDANCE = "\n\n".join((
    _PLAN_CORE, _SHARED_READER_SCOPE, _PLAN_FIRST_DRAFT, _PLAN_REVIEW, _ARTIFACT_SCHEMA,
    room_types_reference(),
))


ELEVATION_EXAMPLE = {
    "orientation": "North",
    "view_direction": "South",
    "x_calibration": {
        "pixel_start": 100,
        "pixel_end": 900,
        "world_start_m": 10,
        "world_end_m": 0,
        "world_axis": "x",
    },
    "elevations": [
        {"id": "ground", "kind": "ground", "value_m": 0,
         "evidence_type": "annotation", "bbox": [80, 700, 180, 760]},
    ],
    "openings": [
        {"id": "W1", "floor_id": "F1", "kind": "window", "x_px": [200, 300],
         "width_m": 2.5, "sill_m": 1.0, "head_m": 2.4,
         "evidence_type": "annotation_and_pixels", "bbox": [190, 300, 310, 650]},
    ],
    "counts": [{"floor_id": "F1", "window_count": 1, "door_count": 0}],
    "unresolved": [],
}

COMPACT_ELEVATION_EXAMPLE = {key: value for key, value in ELEVATION_EXAMPLE.items()
                             if key not in {"orientation", "view_direction", "x_calibration"}}
COMPACT_ELEVATION_EXAMPLE["x_calibration"] = {
    "pixel_start": 100, "pixel_end": 900, "distance_start_m": 0, "distance_end_m": 10,
    "facade_length_m": 10}
COMPACT_ELEVATION_EXAMPLE["z_calibration"] = {
    "pixel_start": 300, "pixel_end": 700, "world_start_m": 3, "world_end_m": 0}
COMPACT_ELEVATION_EXAMPLE["openings"] = [
    {"id": "W1", "floor_id": "F1", "kind": "window", "opening_bbox": [200, 380, 300, 567],
     "evidence_type": "pixels"}]

_ELEVATION_SCHEMA = """The first request already contains this admitted image, its original size and a pixel grid.
Read it directly; use extra crops only where details are unreadable.
Read the whole facade first: batch its absolute level marks, overall horizontal dimension
and every opening in image order. Call trial_elevation_reading with one rough box per opening
and two independent image calibrations:
{example}
Replace the example with original observations. x_calibration distances increase from the
image-left building edge; retain any partial-chain offset and give total facade_length_m.
The task fixes facade and world direction. z_calibration pairs top/bottom pixels with
absolute Z metres, decreasing down the image. Locate the supporting level marks in
elevations (ground/floor/eave/roof/other; floor needs floor_id). Where the corresponding
horizontal line can be located, include line_y_px; the text bbox locates the label, not the
line. Check actual extension/level lines; algebraic agreement alone cannot validate calibration.
Include facade_outline={{points_px:[[x,y],...],closed:true/false,evidence_type:"pixels",basis:"..."}}
for the visible exterior/roof outline. Use closed:true only for a complete observed perimeter;
a cropped or hidden boundary stays open, with its missing extent in unresolved. The clean
elevation must show both building outline and apertures. No supplied outline means incomplete coverage.
opening_bbox is the approximate aperture [left,top,right,bottom] in ORIGINAL pixels;
bbox, if supplied, is a separate evidence crop. Use evidence_type=pixels only for pixel
estimates without annotation support; rough boxes suffice. For annotation, mixed or other
bases, supply width_m/sill_m/head_m and x_px or opening_bbox: these readings are retained;
code can take x_px from the supplied box. The tool checks ink
within 0.30m and changes only pure pixels with a unique continuous line. It preserves
floor-contact sills and records numeric/ink disagreements for review. It then adopts all
absolute level Z values and opening width/sill/head values on the configured Lite BIM grid
(currently 0.1 m), retaining each pre-grid reading and shift in regularization metadata.
Do not hand-round values or repeatedly measure four profiles to remove finer tails.
Floor levels must be supported by floor/level evidence; a raised door sill or threshold
alone is not a floor level. Keep openings in image order within each floor and count every window/door. Keep unclear
marks and mirrored/nonstandard views in unresolved; never borrow plan positions.
Correct a named rejection and resubmit. In the returned overlay, gray is original ink and
color is adopted geometry. Review it together with the clean elevation, then call
submit_elevation_reading with that trial_id and local_review.summary. Batch already visible
corrections into one trial; retain uncertain evidence in unresolved. Acceptance ends the task.""".format(
    example=json.dumps(COMPACT_ELEVATION_EXAMPLE, ensure_ascii=False, separators=(",", ":")))

ELEVATION_READER_GUIDANCE = "\n\n".join((
    "You are the ELEVATION reader for task.target (the assigned facade and floors).",
    _SHARED_READER_SCOPE,
    "Read only visible facts. Keep observed values distinct from assumptions, and use absolute building Z for every level, sill and head.",
    _ELEVATION_SCHEMA,
    "Do not copy plan positions or apply heights to a building draft.",
))


def reader_references(role_id):
    """Only the delegated reader's methods; parent/single-model references are untouched."""
    if role_id == "plan_reader":
        return {"plan_partition": PLAN_READER_REFERENCE, "plan_editing": _PLAN_EDITING,
                "room_types": room_types_reference()}
    if role_id == "elevation_reader":
        return {"elevation_reading": _ELEVATION_SCHEMA}
    raise ValueError(f"unknown reader role: {role_id}")


COORDINATOR_GUIDANCE = """Inventory inputs; delegate_readers for plans AND elevations together with a common
origin. Define the datum unambiguously in task.origin: use a visible common survey datum,
or the west/south extents of the WHOLE building envelope, including setbacks and projections.
The bounding-box southwest point may be outside an L-shaped footprint; do not call it a
physical corner or let each floor choose its own extents. Preserve explicit per-view offsets.
Targets: floor ID or North/South/East/West[/F1,F2]. First dispatch fills missing
readers and waits for all. Instructions contain drawing facts/questions only.
Read results with role_state and read_role_artifact. The reader owns local drawing fidelity:
its delivery includes numeric data, rendered views, a checked original overlay and unresolved
items. Reuse these checks; do not remeasure every original feature. You own shared axes/origin,
floor IDs, absolute Z, cross-drawing conflicts and architectural reasonableness. Do not move
walls manually merely to match another floor. Assembly performs thickness-aware matching
and records adopted anchors; inspect genuine cross-view conflicts using that audit.

assemble_from_readers builds accepted floors, resolves levels, carries uses and located
safe heights. Audited regularization needs no confirmation. Inspect the assembled building:
storeys, adjacent-floor footprints, room layout, corridors/stairs/voids, opening hosts and
vertical plausibility. Explain actual conflicts; rework only their affected reader/object.
Check openings/result, then finish_bim with architectural_review.summary. This records your building judgment and ends the run; no extra acknowledgement is required. Reassemble new reader deliveries; unchanged inputs reuse receipts.
level_overrides cite {task_id,elevation_id}: absolute z_floor or ceiling_height=top Z-floor Z.

Target <10cm. Compare independent opening endpoints/width after identity matching.
10-30cm automatically retain plan with a note. Every >30cm difference must be explicitly
decided before finish_bim. role_state(position_details=true) lists readings;
role_state(position_decision_id=...) returns both original crops and view_ids.
Optional edit_bim position_decision: decision_id, choice=keep_plan/use_elevation/
reread_plan/reread_elevation, reason, view_ids. Above 30cm inspect both original crops;
choosing a side requires both saved views, while reread remains delivery-blocking until a
new reader delivery is assembled and decided. use_elevation stays on its host; conflicting numeric width needs keep_plan or
rereading, never pixel-width replacement. Never average positions. One-sided or unsafe
height matches stay unresolved.

Rework substantive room/opening/host/connectivity errors. Re-dispatch with new task_id and
previous_task_id; the runtime carries the prior task's located failure handoff automatically,
and issues may add drawing-specific facts. Plan rework_targets: plan.partitions:<id>,
plan.space_seeds:<id>, plan.openings:<id>,plan.openings or a plan field. Elevation targets:
elevation.openings:<id>[.<field>] or elevation.elevations:<id>[.<field>]. The server
preserves all unpointed objects and their original-reading audit. Actual reader-geometry
changes need review_role_assembly with reasons; height/use/note edits retain confirmation.
Missing-floor decisions accept partial delivery; stale/unverified reader lineage needs repair.

edit_bim heights go separately: id,sill_m,head_m,reader_evidence={task_id,sha256,opening_id}
reuse the immutable reader's original box without new views. Independent evidence uses
image,bbox. use(id,role,image) defaults to inferred; position(id,along_start_m,along_end_m,
image) needs geometry review; note(text) needs none. Do not draft walls or reread heights
when a reader can. Preserve unresolved evidence; check_openings reuses unchanged checks."""


ROLE_GUIDANCE = {
    "coordinator": COORDINATOR_GUIDANCE,
    "plan_reader": PLAN_READER_GUIDANCE,
    "elevation_reader": ELEVATION_READER_GUIDANCE,
}


def get_role_guide(role_id: str) -> str:
    try:
        return ROLE_GUIDANCE[role_id]
    except KeyError as error:
        raise ValueError(f"unknown role guide {role_id!r}; choose {sorted(ROLE_GUIDANCE)}") from error


def get_role_tool_names(role_id: str) -> tuple[str, ...]:
    if role_id in READER_TOOL_NAMES:
        return READER_TOOL_NAMES[role_id]
    if role_id == "coordinator":
        return (
            "delegate_readers", "read_role_artifact", "assemble_from_readers", "role_state",
            "review_role_assembly", "edit_bim",
        )
    raise ValueError(f"unknown role catalog {role_id!r}")


def guidance_catalog() -> dict[str, object]:
    single = build_guide(images="drawings", mesh=False)
    roles = {
        role_id: {
            "character_count": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "tools": list(get_role_tool_names(role_id)),
        }
        for role_id, text in ROLE_GUIDANCE.items()
    }
    return {
        "single_model": {
            "character_count": len(single),
            "sha256": hashlib.sha256(single.encode("utf-8")).hexdigest(),
            "unchanged_source": "scripts/tool_scripts/bim_agent_guidance.py:build_guide",
        },
        "roles": roles,
    }
