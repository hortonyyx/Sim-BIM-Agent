"""Read-only facade evidence checks for T1; never approve or edit geometry.

GLM sm24 linked every opening to its facade but spread one height chain over
different window widths. A whole-image citation or missing calibration cannot
establish per-opening location. Report those limits separately from a measured
source-region miss; all remain advisory, including on delivery.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from statistics import median

from src.agent.execution.bim_height_coverage import height_coverage


def _calibrations(toolkit):
    """Latest explicit original-image/facade transform; no inference from names."""
    latest, problems = {}, []
    for path in sorted((toolkit.run / "elevation_reviews").glob("review_*.json")):
        row = json.loads(path.read_text())
        if "transform" not in row or "image" not in row:
            continue
        key = row["image"], row["facade"]
        latest[key] = row
    for key, row in list(latest.items()):
        info = toolkit.manifest.get("images", {}).get(row["image"], {})
        if info.get("sha256") != row.get("image_sha256"):
            problems.append(dict(image=key[0], facade=key[1], reason="calibration_image_changed"))
            del latest[key]
            continue
        try:
            toolkit.image_path(row["image"])
            transform = row["transform"]
            values = [transform[k] for k in ("horizontal_metres_per_pixel", "horizontal_offset_m", "z_metres_per_pixel", "z_offset_m")]
            if not all(math.isfinite(v) for v in values) or values[0] == 0 or values[2] == 0:
                raise ValueError("invalid transform")
        except (OSError, ValueError, KeyError, TypeError) as error:
            problems.append(dict(image=key[0], facade=key[1], reason=str(error)))
            del latest[key]
    return latest, problems


def _pixel_box(opening, facade, transform):
    axis = 0 if facade in {"North", "South"} else 1
    points = [((v[axis] - transform["horizontal_offset_m"]) / transform["horizontal_metres_per_pixel"],
               (v[2] - transform["z_offset_m"]) / transform["z_metres_per_pixel"])
              for v in opening["vertices"]]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def located_height_report(toolkit, candidate, current_state=None):
    """One external-opening table: current values, binding, location and limits.

    A2-T replaces the two competing reports. T1 sm24/sm25 counted every opening
    inside a shared source box as located. A box covering multiple exterior
    openings now requires individual evidence, even if all numbers match.
    """
    store = toolkit.claims()
    _, source = store.candidate(candidate)
    coverage = height_coverage(store, candidate, current_state)
    openings = {o["id"]: o for o in source.get("openings", []) if o.get("exterior")}
    items = {r["opening_id"]: r for r in coverage["openings"] if r["opening_id"] in openings}
    claims = {row["id"]: row for row in store.status()["claims"]}
    calibrations, calibration_problems = _calibrations(toolkit)
    rows, groups, binding_widths, checked = [], defaultdict(list), defaultdict(list), {}

    def evidence_current(record):
        identity = record["id"]
        if identity not in checked:
            try:
                refs = store._sources(record["claim"])
                values, computations = store._resolve_values(record["claim"], refs)
                checked[identity] = (refs == record["sources"] and values == record["resolved_values"]
                                     and computations == record["computations"])
            except (OSError, ValueError, KeyError, TypeError):
                checked[identity] = False
        return checked[identity]

    def covers(box, projected):
        return (box[0] - 2 <= projected[0] and box[1] - 2 <= projected[1]
                and box[2] + 2 >= projected[2] and box[3] + 2 >= projected[3])

    for identity, item in items.items():
        facade = item.get("facade")
        vertices = openings[identity]["vertices"]
        width = math.hypot(max(v[0] for v in vertices) - min(v[0] for v in vertices),
                           max(v[1] for v in vertices) - min(v[1] for v in vertices))
        row = dict(opening_id=identity, kind=item["kind"], facade=facade, floor_ids=item["floor_ids"],
                   absolute_z_m=item["absolute_z_m"], width_m=round(width, 3),
                   status="missing", evidence=[], issues=[])
        located_kinds, assumptions, attempts = set(), False, set()
        links = item["retained_image_evidence"] + item["retained_non_image_evidence"]
        seen = set()
        for link in links:
            key = (link["claim_id"], link["value"], link["binding_kind"])
            if key in seen:
                continue
            seen.add(key)
            record = claims[link["claim_id"]]
            entry = dict(claim_id=link["claim_id"], value=link["value"],
                         binding=link["binding_kind"], views=[])
            row["evidence"].append(entry)
            if not evidence_current(record):
                entry["issue"] = "source_view_or_value_changed"
                attempts.add(entry["issue"])
                continue
            if link["evidence_class"] == "non_image":
                assumptions = True
                entry["basis"] = record["claim"]["basis"]
                continue
            binding_widths[(link["claim_id"], link["value"], facade, tuple(item["floor_ids"]))].append((identity, width))
            verified_reader = link.get("verified_reader_evidence")
            if verified_reader is not None:
                entry["views"].append({
                    "image": verified_reader["image"],
                    "box": verified_reader["box"],
                    "reader_task_id": verified_reader["task_id"],
                    "reader_artifact_sha256": verified_reader["artifact_sha256"],
                    "artifact_opening_id": verified_reader["artifact_opening_id"],
                    "verification": verified_reader["verification"],
                    "location": "located",
                })
                located_kinds.add(link["binding_kind"])
                continue
            for index, ref in enumerate(record["sources"]):
                view = dict(image=ref["image"], source_index=index)
                if "view_id" in ref:
                    view["view_id"] = ref["view_id"]
                calibrated = calibrations.get((ref["image"], facade))
                box = ref["box"]
                if calibrated is None:
                    location = "no_elevation_calibration"
                elif box == [0, 0, *ref["size"]]:
                    location = "whole_image_only"
                elif not covers(box, _pixel_box(openings[identity], facade, calibrated["transform"])):
                    location = "outside_source_region"
                else:
                    covered = [other for other, other_item in items.items() if other_item.get("facade") == facade
                               and covers(box, _pixel_box(openings[other], facade, calibrated["transform"]))]
                    location = "located" if len(covered) == 1 else "needs_per_opening_confirmation"
                    if len(covered) > 1:
                        view["covered_opening_ids"] = sorted(covered)
                view["location"] = location
                entry["views"].append(view)
                if location == "located":
                    located_kinds.add(link["binding_kind"])
                else:
                    attempts.add(location)
        if located_kinds:
            row["status"] = "located_applied" if "application" in located_kinds else "located_confirmed"
        elif assumptions:
            row["status"] = "assumed"
        row["issues"] = sorted(attempts) if not located_kinds else []
        if not links:
            row["issues"] = ["no_current_height_binding"]
        if row["kind"] == "window" and facade:
            groups[(tuple(row["floor_ids"]), facade)].append(row)
        rows.append(row)
    shared_risks = set()
    for values in binding_widths.values():
        if len(values) < 2:
            continue
        typical = median(width for _, width in values)
        shared_risks.update(identity for identity, width in values
                            if typical > 0 and abs(width - typical) >= .5
                            and (width >= typical * 1.5 or width <= typical / 1.5))
    for grouped in groups.values():
        typical = median(row["width_m"] for row in grouped)
        for row in grouped:
            width = row["width_m"]
            if (len(grouped) > 1 and typical > 0 and abs(width - typical) >= .5
                    and (width >= typical * 1.5 or width <= typical / 1.5)):
                row["issues"].append("distinct_width_on_facade")
    for row in rows:
        if row["opening_id"] in shared_risks:
            row["issues"].append("shared_height_across_widths")
    return dict(schema_version="opening_heights_v2", candidate=candidate,
        source_model_sha256=source["source_model_sha256"], delivery_blocked=False,
        summary=dict(exterior_count=len(rows),
                     located_count=sum(row["status"].startswith("located_") for row in rows),
                     status_counts=dict(Counter(row["status"] for row in rows))),
        openings=rows, calibration_problems=calibration_problems,
        note="Current values, evidence binding and source location, not drawing correctness. A region containing several openings needs individual evidence. Report only.")


def compact_located_heights(report):
    """Compatibility helper: the unified table is already the model presentation."""
    return report


_COUNT_EXAMPLE = {"observation_type": "facade_count", "image": "elevation.png",
    "floor_id": "L1", "facade": "South", "window_count": 4,
    "reason": "Synthetic format only; replace with your own observed facade total"}


def record_facade_count(toolkit, observation):
    """GLM sm25 missed two west windows: save one image/floor/facade total.

    Counts describe the drawing, so can precede any candidate. Separate immutable
    files avoid confusing observational totals with adopted geometric parameters.
    A new record for the same image/scope supersedes its earlier count; counts
    from different images stay separate and conflicts are reported, never added.
    """
    import os
    import tempfile
    import time
    import uuid
    from scripts.tool_scripts.bim_agent_feedback import resolve_image_name

    def reject(reason):
        example = {**_COUNT_EXAMPLE, "image": next(iter(toolkit.manifest["images"]), "elevation.png")}
        raise ValueError("facade_count: " + reason + "; minimal example: " + json.dumps(example))

    allowed = {"observation_type", "image", "floor_id", "floor_plan_image", "facade",
               "window_count", "door_count", "box", "reason"}
    if set(observation) - allowed:
        reject("unknown fields " + str(sorted(set(observation) - allowed)))
    if not isinstance(observation.get("facade"), str) or observation["facade"] not in {"North", "South", "East", "West"}:
        reject("facade must be North/South/East/West")
    if ("floor_id" in observation) == ("floor_plan_image" in observation):
        reject("give exactly one of floor_id or floor_plan_image")
    scope_key = "floor_id" if "floor_id" in observation else "floor_plan_image"
    if not isinstance(observation[scope_key], str) or not observation[scope_key].strip():
        reject(scope_key + " must be a nonempty string")
    for key in ("window_count", "door_count"):
        if key == "door_count" and key not in observation:
            continue
        if type(observation.get(key)) is not int or observation[key] < 0:
            reject(key + " must be a nonnegative integer; zero is an explicit observation")
    if not isinstance(observation.get("reason"), str) or not observation["reason"].strip():
        reject("reason must describe the observed scope")
    image = resolve_image_name(toolkit.manifest["images"], observation.get("image"))
    toolkit.image_path(image)
    info = toolkit.manifest["images"][image]
    box = observation.get("box", [0, 0, *info["size"]])
    if (not isinstance(box, list) or len(box) != 4 or
            not all(type(v) in (int, float) and math.isfinite(v) for v in box) or
            not (0 <= box[0] < box[2] <= info["size"][0] and 0 <= box[1] < box[3] <= info["size"][1])):
        reject("box must be [left,top,right,bottom] within the original image")
    scope = observation[scope_key]
    if scope_key == "floor_plan_image":
        scope = resolve_image_name(toolkit.manifest["images"], scope)
        toolkit.image_path(scope)
    row = {**observation, "image": image, scope_key: scope, "box": box,
           "image_sha256": info["sha256"], "image_size": info["size"],
           "schema_version": "facade_count_observation_v1",
           "id": f"count_{time.time_ns():020d}_{uuid.uuid4().hex[:12]}",
           "drawing_fidelity": "not_evaluated",
           "note": "Observer-reported total for the entire named floor/facade, not a verified count. Re-record this image/scope to correct it; no adoption or geometry confirmation is needed."}
    if scope_key == "floor_plan_image":
        row["floor_plan_sha256"] = toolkit.manifest["images"][scope]["sha256"]
    folder = toolkit.run / "facade_counts"
    folder.mkdir(exist_ok=True)
    # Publish complete records atomically; independent wall/floor calls share no latch.
    with tempfile.NamedTemporaryFile(mode="w", dir=folder, suffix=".tmp", delete=False) as output:
        json.dump(row, output, ensure_ascii=False, indent=2)
        temporary = output.name
    os.replace(temporary, folder / (row["id"] + ".json"))
    return row


def facade_count_report(toolkit, candidate):
    from scripts.tool_scripts.bim_agent_budget import candidate_floor_images
    from src.agent.geometry.opening_review import facade_inventory

    _, source = toolkit.claims().candidate(candidate)
    inventory = facade_inventory(source)
    floor_ids = {row["id"] for row in source["floors"]}
    mapping = candidate_floor_images(toolkit.run, candidate, toolkit=toolkit)
    latest = {}
    for path in sorted((toolkit.run / "facade_counts").glob("count_*.json")):
        observation = json.loads(path.read_text())
        key = (observation["image"], observation["facade"],
               observation.get("floor_id"), observation.get("floor_plan_image"))
        latest[key] = observation
    observations, unused = defaultdict(list), []
    for observation in latest.values():
        problem = None
        images = [(observation["image"], observation["image_sha256"])]
        if observation.get("floor_plan_image"):
            images.append((observation["floor_plan_image"], observation["floor_plan_sha256"]))
        for image, expected_hash in images:
            try:
                toolkit.image_path(image)
                if toolkit.manifest["images"][image]["sha256"] != expected_hash:
                    problem = "source_image_changed"
            except (OSError, ValueError, KeyError):
                problem = "source_image_changed"
        matches = ([observation["floor_id"]] if "floor_id" in observation
                   else mapping.get(observation["floor_plan_image"], []))
        if len(matches) != 1 or matches[0] not in floor_ids:
            problem = problem or "floor_not_resolved_in_candidate"
        if problem:
            unused.append(dict(id=observation["id"], reason=problem))
            continue
        observations[(matches[0], observation["facade"])].append(observation)
    rows = []
    classifications = inventory["opening_classifications"]
    for floor in inventory["floors"]:
        for facade in floor["facades"]:
            scope = floor["floor_id"], facade["facade"]
            records = observations.pop(scope, [])
            row = dict(floor_id=scope[0], facade=scope[1], observation_ids=[r["id"] for r in records])
            for kind in ("window", "door"):
                actual = [identity for identity in facade["opening_ids"] if classifications[identity]["kind"] == kind]
                totals = sorted({record[kind + "_count"] for record in records if kind + "_count" in record})
                status = ("not_counted" if not totals else "conflicting_observations" if len(totals) > 1
                          else "matches_observed_total" if totals[0] == len(actual) else "count_mismatch")
                row[kind] = dict(status=status, built_count=len(actual), observed_counts=totals,
                    built_opening_ids=actual, missing_count=(totals[0] - len(actual)) if len(totals) == 1 else None)
            rows.append(row)
    for scope, records in observations.items():
        unused.extend(dict(id=record["id"], reason="facade_not_present_in_candidate", floor_id=scope[0], facade=scope[1])
                      for record in records)
    summary = dict(floor_facade_count=len(rows), window_status_counts=dict(Counter(r["window"]["status"] for r in rows)),
                   door_status_counts=dict(Counter(r["door"]["status"] for r in rows)), unused_observation_count=len(unused))
    return dict(schema_version="facade_count_report_v1", candidate=candidate,
        source_model_sha256=source["source_model_sha256"], delivery_blocked=False,
        summary=summary, scopes=rows, unused_observations=unused,
        unsupported_exterior_boundaries=[dict(floor_id=floor["floor_id"], boundaries=floor["unsupported_exterior_boundaries"])
            for floor in inventory["floors"] if floor["unsupported_exterior_boundaries"]],
        note="Compare observer-reported whole-floor/facade totals with actual source openings. Uncounted facades, zero totals, conflicting evidence and unresolved scopes stay explicit. Equal counts do not prove position or drawing fidelity. Report only.")
