# Optional Python BIM interfaces

Start with [SDK.md](SDK.md). These interfaces are available when the default
inspect/build/review/finish path needs an additional operation.

## Discover one interface

```python
from src.agent.sdk import BimSession, edge_segment
with BimSession(run_directory) as bim:
    print(bim.describe("view_mesh"))
    print(bim.call("view_mesh", azimuth_degrees=0, elevation_degrees=0))
    # bim.catalog() lists names and short descriptions when the tool is unknown.
```

Pass normal Python dictionaries/lists to native parameters ending in `_json`;
the SDK encodes them. `reply.data` is the full structured result, `reply.texts`
contains additional messages, `reply.images` contains paths, hashes and MIME
types. Native receipts and lossless image bytes are retained in `run/sdk/`.
`reply.presentation()` is the compact display object used by printing and CLI.

## Shared geometry helpers

```python
from shapely.geometry import box
from src.agent.modeling import region, shared_edges, shared_opening, clearance_report
hall = region(box(0, 0, 7, 2))
room = region(box(0, 2, 4, 6))
edges = shared_edges(hall, room)
door = shared_opening(hall, room, edge=0, offset=1.2, width=0.9)
print(clearance_report(hall, width=0.9))
```

Merge `hall`/`room` into normal space records and `door` into a door record with
explicit IDs, spaces, height and source references. `region(*parts, subtract=...)`
returns existing polygon/holes fields. Disconnected results and touching holes
are rejected; no small part is discarded. `shared_opening` preserves the requested
width or reports a conflict. Reconsider the space organization if it cannot fit;
do not shrink a door to fit an unsuitable boundary. `clearance_report` is a
circular passage-body probe at your supplied width, with approximation limits;
it does not certify entry-to-exit access, regulations or architectural quality.

Construct related regions/openings in a shared local frame before applying one
transform. These helpers do not weld floating-point gaps or search for a nearby
wall. Declare existing `modelling.frames` when reference-line adoption uses local
axes. `edge_segment(p1, p2, offset=..., width=...)` returns a door segment from an
actual polygon edge. Preserve the relationship to original image/mesh evidence.

## Construction check

`bim.check_plan(plan)` uses current adoption/source rules without saving a
candidate or rendering images. It returns `.source`, `.report`, `.geometry_ready`
and `.receipt_file`. Use it to measure a proposed construction or compare
alternatives. Success is not an architectural review or a later delivery promise.

## CLI

```text
python -m src.agent.sdk --run RUN describe build_parametric_bim
python -m src.agent.sdk --run RUN call build_parametric_bim --arguments args.json
python -m src.agent.sdk --run RUN call view_image --arguments-json '{"name":"plan.png"}'
```

`--arguments` expects a UTF-8 JSON **file path**; `--arguments-json` expects inline
JSON. They are mutually exclusive. Prefer a Python script for multiple operations
and geometry: it avoids shell quoting and repeated process setup. CLI exit code 1
denotes a tool error. After a process failure, inspect existing native receipts
before retrying writes. Neither CLI nor Python automatically repairs or approves.

Runs are prepared with `src.agent.bim_inputs.prepare_bim_inputs` from authorized
original inputs. `src.agent.runtime_entry.prepare_inputs` can additionally enable
process-review policy. The SDK reflects the admitted policy without changing it.
