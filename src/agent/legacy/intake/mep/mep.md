# MEP priors — DRAFT seed

> **Status: DRAFT seed, not a knowledge base yet.** This holds the default
> MEP load / schedule / HVAC values the 4_mep stage assigns when the input carries no
> such data — which is the current situation (the drawings + testdata give
> geometry, not loads or schedules). It was extracted out of `rules.md` Step 7 so
> the modeling rules stay geometry-focused and these defaults live in one place.
>
> **Scope now = de-mixing, not expansion.** The project focus is geometry-modeling
> correctness; this is left intentionally thin. Expand into a proper prior library
> (typed by space type, tagged `national_code` vs `convention`, sourced like
> [`../1_correction/A4_priors.md`](../correction/A4_priors.md)) **after
> geometry stabilizes**. Geometry priors currently live in `A4_priors.md`; the two
> are expected to consolidate into this `priors/` directory later.
>
> Any value here is a **default only** — explicit input data always overrides it.

---

## Office (seeded building type)

Source profile: ASHRAE 90.1 Office default (same as the original Step 7 text).

**Loads**
- people: 10 m²/person
- lights: 10 W/m²

**Schedules** (weekday occupied 9–18, weekend off)
- `Office_Workday` / `Office_Weekend` style `Schedule:Compact` profiles

**HVAC** (2026-08-14: code-generated, not authored from this doc — see
`authoring.md` "What you output". Listed here only because `run_mep`
(`src/agent/pipeline.py`) hardcodes these same Office-default values, so this
prior and the code stay the documented source of the same numbers.)
- `HVACTemplate:Zone:IdealLoadsAirSystem`
- cooling setpoint 24 °C / heating setpoint 20 °C

---

## To expand later (placeholders, do not invent values now)

When the input starts carrying real MEP intent and geometry is stable, grow this
into the MEP half of the shared prior library:

- loads by space type (office / corridor / meeting / WC / server) — occupancy
  density, LPD, plug/equipment load
- occupancy / lighting / equipment / HVAC-availability schedule profiles
- HVAC: outdoor-air rates, setpoint/ setback schedules
- envelope thermal: window U-Factor / SHGC (real, via `SimpleGlazingSystem`),
  opaque U-values (currently `Default_*` placeholders) — by climate zone
- WWR design targets by orientation (the WWR *upper bound* is a code limit and
  already lives in `A4_priors.md` `wwr_upper`; cross-link when expanding)

Tag each value `national_code` (GB 50189-2015 mandatory caps / GB 50736 HVAC) vs
`convention` (ASHRAE / typical), and type it by space — mirror `A4_priors.md`.
