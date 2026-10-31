"""Read-only facts shown beside a recorded claim, for the caller to judge.

For each value: a dimension chain's total and absolute range, and every object the
value is applied to, with its floor and that floor's level-to-ceiling range, facade or
exterior/interior host, width and current z. Nothing here decides that a chain is
wrong or that objects belong to different height groups; the claim itself and its
adoption rules are unchanged.
"""
from __future__ import annotations

import math

NOTE = ("Facts only: what each chain spans and which objects each value is applied to. A chain "
        "need not span a whole floor, and different widths or facades can share a height; "
        "judge from the elevation.")


def _width(opening):
    """Width from the current geometry (span, else end points); unknown when absent."""
    if isinstance(opening.get("span"), list) and len(opening["span"]) == 2:
        return round(abs(opening["span"][1] - opening["span"][0]), 2)
    if opening.get("p1") is not None and opening.get("p2") is not None:
        return round(math.dist(opening["p1"], opening["p2"]), 2)
    return None


def claim_facts(row, proposal):
    claim = row["claim"]
    geometry = proposal["geometry"]
    # Floor level to its ceiling in the candidate, not a storey-to-storey height.
    floors = {floor["name"]: [floor["z_floor"], round(floor["z_floor"] + floor["ceiling_height"], 6)]
              for floor in geometry["floors"]}
    rooms = {cell["id"]: floor["name"] for floor in geometry["floors"] for cell in floor["cells"]}
    windows = {item["id"]: item for item in geometry.get("windows", [])}
    doors = {item["id"]: item for item in geometry.get("openings", [])}
    values = []
    for field, computation in row.get("computations", {}).items():
        targets = claim["objects"] if claim.get("value_targets") is None else claim["value_targets"][field]
        entry = {"value": field, "resolved": row.get("resolved_values", {}).get(field)}
        if computation.get("method") == "dimension_chain":
            chain = computation["chain"]
            entry.update(chain_total_m=chain["total_m"], selected_segment=computation["segment"],
                         chain_range_m=sorted([chain["origin_m"], chain["end_m"]]))
        applied = []
        for target in targets:
            if target["kind"] == "window" and target["id"] in windows:
                window = windows[target["id"]]
                applied.append(dict(id=window["id"], floor=window.get("floor"),
                                    floor_to_ceiling_m=floors.get(window.get("floor")), host=window.get("facade"),
                                    width_m=_width(window), current_z=window.get("z")))
            elif target["kind"] == "opening" and target["id"] in doors:
                door = doors[target["id"]]
                floor = rooms.get(door.get("space_id"))
                applied.append(dict(id=door["id"], floor=floor, floor_to_ceiling_m=floors.get(floor),
                                    host="exterior" if door.get("other_space_id") is None else "interior",
                                    width_m=_width(door), current_z=door.get("z")))
            else:
                applied.append(dict(id=target["id"], kind=target["kind"]))
        entry["applied_to"] = applied
        entry["distinct_floor_host_width"] = sorted(
            {(a["floor"], a["host"], a["width_m"]) for a in applied if "host" in a}, key=str)
        if "chain_range_m" in entry:
            entry["chain_and_floor_to_ceiling"] = [
                dict(floor=floor, floor_to_ceiling_m=span, chain_range_m=entry["chain_range_m"],
                     same_range=all(abs(a - b) < 0.01 for a, b in zip(span, entry["chain_range_m"])))
                for floor, span in sorted({(a["floor"], tuple(a["floor_to_ceiling_m"])) for a in applied
                                           if a.get("floor_to_ceiling_m")})]
        values.append(entry)
    return dict(note=NOTE, values=values)
