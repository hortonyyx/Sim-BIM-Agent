# A4 — Architectural priors (P0)

Advisory commonsense values, consumed **only by A3** under the hard gating in
`A3 §3`. A4 holds no rules and never self-acts. Values are typed by space type;
office is the seeded building type. Authority tags: `national_code` (a real
lower/upper bound) vs `convention` (score / warning only).

```
Consumes:        nothing (data)
Produces:        prior_score / warning candidates for A3
Emit corrections[]: never (A3 logs the prior use with prior_id)
May change topology: no
```

---

## 1. Usage contract (restated)

- A4 emits `prior_score` / `warning`, never a correction.
- A prior is applied (by A3) only when evidence is missing / contradictory /
  low-confidence, and is logged with `prior_id`.
- A prior never overrides consistent measured evidence.
- A high-confidence semantic label beats a size prior: a labelled small
  service / shaft / WC / storage room is kept, not normalized to a "plausible"
  office size.

## 2. Hard bounds (`national_code` — usable as plausibility limits)

| prior_id | item | bound | source |
|---|---|---|---|
| `office_door_w` | office-room door opening width | ≥ 1.00 m | JGJ/T 67-2019 4.1.7 |
| `office_door_h` | office-room door opening height | ≥ 2.10 m | JGJ/T 67-2019 4.1.7 |
| `evac_door_w` | public/evacuation door net width | ≥ 0.90 m | GB 50016-2014 5.5.18 |
| `window_sill_safety` | exterior window sill height (no guard) | ≥ 0.80 m | GB 50352-2019 6.11.6 |
| `office_corridor_w` | office corridor net width | 1.30 / 1.50 (≤40 m single/double); 1.50 / 1.80 (>40 m) | JGJ/T 67-2019 4.1.9 |
| `office_net_h` | office net height | ≥ 2.50–2.90 (by AC/ceiling) | JGJ/T 67-2019 4.1.11 |
| `corridor_net_h` | office corridor net height | ≥ 2.20 m | JGJ/T 67-2019 4.1.11 |
| `office_area_pp` | ordinary office area per person | ≥ 6.0 m²/person | JGJ/T 67-2019 4.2.3 |
| `single_office_area` | single office room area | ≥ 10 m² | JGJ/T 67-2019 4.2.3 |
| `stair_flight_w` | stair flight net width | ≥ 1.10–1.20 m | GB 50352-2019 6.8.3 |
| `wwr_upper` | single-facade WWR advisory upper | ≤ 0.60 (severe cold) / ≤ 0.70 (other) | GB 50189-2015 3.2.2 |

Use these to detect impossible cells (e.g. a 1.2 m-wide "office") — flag a
`semantic_size_prior` conflict, do not silently resize.

## 3. Score-only priors (`convention` — warning / score, never a bound)

| prior_id | item | typical | range | note |
|---|---|---|---|---|
| `window_w` | single window width | 1.80 m | 0.9–2.4 (modular) | reject ghost-room widths, not resize measured |
| `window_h` | window height | 1.50 m | 1.2–1.8 | fallback only |
| `window_head_h` | window head height | 2.40 m | 1.8–2.4 | fallback when elevation chain missing |
| `floor_to_floor` | office floor-to-floor | 3.60 m | 3.3–4.5 | only if elevation height chain missing |
| `int_wall_thk` | interior / shaft wall thickness | 0.10 m | 0.05–0.15 | A1 centerline prior (A3-applied) |
| `ext_wall_thk` | exterior wall thickness | 0.20 m | 0.15–0.30 | A1 centerline prior (A3-applied) |
| `col_grid` | office column grid | 8.1 m | 7.2–8.4 | score only |
| `wc_min_area` | WC room area | 3.0 m² | 2–8 | never erase if labelled WC |
| `storage_area` | storage room area | 4.0 m² | 2–10 | keep labelled small storage |
| `lift_cell` | lift shaft / core cell | 2.0 m | 1.6–2.8 | confirm by symbol/label |

## 4. Modular grids (`national_code`, GB/T 50002-2013)

- 1M = 100 mm; horizontal expanded 2M/3M/6M/9M/12M (300/600 mm common);
  vertical nM; submodules M/2 = 50 mm, M/10 = 10 mm.
- Use modular values to score plausibility of widths/heights; never to override
  measurement.

> P0 seed. Extend by building type / space type as new cases arrive; keep every
> value tagged `national_code` vs `convention` and typed by space.
