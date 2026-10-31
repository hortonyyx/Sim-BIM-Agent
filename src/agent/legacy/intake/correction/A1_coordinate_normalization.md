# A1 — Coordinate normalization

Deterministic transforms over the typed evidence graph defined in `A0`: bring
every primitive into one world frame, express wall axes at centerline, and
reconcile the z-stack. Ambiguous inputs escalate to `A3`; A1 never guesses.

```
Consumes:        A0 evidence items (numeric coordinate + topology_identity);
                 perception frames (plan_local, facade_local); known wall thickness
Produces:        all geometry in one world frame; centerline wall axes; reconciled z-stack
Emit corrections[] when: a coordinate's frame is converted, a face coordinate is
                 moved to its wall centerline, or a z value is reconciled
Emit conflicts[] / unsupported when: frame/origin ambiguous, wall side / thickness
                 basis unknown, or facade–plan orientation conflicts
                 → reference_or_identity_ambiguity / facade_plan_mismatch → A3
May change topology: no  (frame / centerline / z only; A1 does not merge or split entities)
```

Applies under **all** method profiles.

---

## 1. World coordinate system

- A single world frame. Origin = the SW inner corner of the overall projected
  maximum boundary. **No per-floor local origins.**
- All output coordinates in meters, world frame.

## 2. Local → world transforms

### 2.1 Plan
Apply the plan's origin offset / rotation to bring `plan_local` coordinates into
the world frame.

### 2.2 Facade (elevation)
The reading stage gives each elevation an **image-local** `facade` block only
(`view_facade`, `local_x_positive`, `mirrored`); world axis / sign / base are
derived **here**, never asserted by reading.

- **Horizontal axis + sign** → map the elevation's local-x to the facade's world
  axis using `view_facade` + the standard external-elevation convention below
  (default orthogonal building; `local_x_positive=image_left_to_right`, not
  mirrored). Flip the sign if `mirrored=true` or `local_x_positive=image_right_to_left`.

  | view_facade | local-x maps to | increasing local-x → |
  |---|---|---|
  | South | world **x** | east (**+x**) |
  | North | world **x** | west (**−x**) |
  | East  | world **y** | north (**+y**) |
  | West  | world **y** | south (**−y**) |

  This per-facade table is the orthogonal default. For an oblique / polygon facade
  whose plane is not axis-aligned, the world direction is the in-plan direction of
  the matched perimeter wall, not a fixed axis — derive it from the plan footprint
  (future: a first-class `facade_local → world` transform artifact); if the facade
  cannot be matched to a plan edge, emit `facade_plan_mismatch` → A3.
- `y_local` (height) → world z **through the `facade_local → world` transform**.
  If the transform establishes that `y_local` is building-absolute, no per-floor
  offset is added. If the vertical origin is ambiguous (e.g. a cropped elevation
  with a local vertical datum), emit `facade_plan_mismatch` /
  `reference_or_identity_ambiguity` → A3 rather than assuming a convention.
- Facade orientation is fixed by the facade's world plane. If the declared
  orientation conflicts with the plan position, emit `facade_plan_mismatch`
  (claim_type `topology_identity`) → A3; do not silently re-orient.

## 3. Z-stack and floor-height reconciliation

- Per floor: `z_top = z_floor + ceiling_height`; the next floor's `z_floor`
  equals this floor's `z_top` (contiguous stack).
- On a shared horizontal boundary, lower-zone ceiling z **==** upper-zone floor z;
  carry the single shared value.
- Derive heights from an elevation's total + segment chains. If two derivations
  disagree beyond `DIMCHAIN_CLOSE_TOL`, emit `checksum_failure` → A3 — do not
  split the difference.

## 4. Centerline convention

All wall axes are expressed at the wall **centerline** in the world frame.

- Drawings mix conventions (interior walls often traced at the inner face,
  exterior walls at centerline). Convert a face coordinate to centerline using
  the wall's **measured** thickness.
- The coordinate shift is a `correction` with `claim_type = numeric`; the
  "which side / what thickness" decision rests on `topology_identity` evidence.
- A1 may consume a wall thickness only if it is **measured** or already
  **resolved by A3**. A1 does **not** invoke `A4` itself: if thickness is absent
  and only an `A4` prior could fill it, A1 emits `reference_or_identity_ambiguity`
  and stops that conversion until A3 resolves it (priors are applied in A3, never
  inside A1).
- Escalation: unknown wall side, no thickness basis, or conflicting origin →
  `reference_or_identity_ambiguity` → A3. Do not guess a centerline.

## 5. Out of scope for A1

A1 does **not** cluster/merge axes across floors, close gaps, quantize, or change
topology — those are `A2`.
