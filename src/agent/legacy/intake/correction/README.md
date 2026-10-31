# 1_correction — Correction layer

The correction layer turns perceived geometric primitives (possibly noisy or
self-contradictory) into a clean, self-consistent, simulation-friendly set,
and records every material change. It is **stage 1 of the pipeline** (after the
reading stage produces vectors), ahead of the deterministic geometry build:

```
0_reading (perception) → [ 1_correction → deterministic core → 2_modelling + 3_split_pairing (geometry) ] → 4_mep (physics) → 5_intakeoutput → downstream → EnergyPlus
```

## Documents (read in order)

| doc | type | concern |
|---|---|---|
| `A0_contract.md` | spine | tolerances, evidence model, audit schema, validation schema, method profiles, upstream input contract — referenced by all others |
| `A1_coordinate_normalization.md` | deterministic (over typed evidence) | world frame, local→world, z-stack, centerline convention |
| `A2_regularization.md` | deterministic (over typed evidence) | canonical axis sets, snapping, dimension-chain closure, quantization, sliver prevention |
| `A3_arbitration.md` | judgment | conflict arbitration, completion, prior usage, unsupported policy |
| `A4_priors.md` | data | architectural commonsense priors (advisory) |

## Runtime (staged, with feedback)

```
A1 → A2-detect → A3-resolve (+A4) → A2-apply → validate
```

A1/A2 are deterministic only over an already-typed evidence graph; ambiguous
inputs escalate to A3. A4 is advisory data, never a self-acting corrector.

## Every document starts with a header block

```
Consumes:        <input fields this doc reads>
Produces:        <output fields this doc may write>
Emit corrections[] when: <...>
Emit conflicts[] / unsupported when: <...>
May change topology: yes | no
```
