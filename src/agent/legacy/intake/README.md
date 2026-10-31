# Legacy EnergyPlus intake resources

Documents retained for the explicitly selected historical EnergyPlus intake workflow.
This is not the current BIM harness's architecture or guidance entrypoint. Current
guidance lives in [bim_agent_guidance.py](../../../../scripts/tool_scripts/bim_agent_guidance.py)
and [runtime_roles/guidance.py](../../runtime_roles/guidance.py).

The former top-level `skills/intake_pipeline` library is relocated here. Folders
describe resource purposes, not a current BIM task's required execution sequence.

| Resource | Retained purpose |
|---|---|
| [reading](reading/guide.md) | Isolated historical image-to-vector reader |
| [correction](correction/README.md) | Vector correction and evidence contracts |
| [modelling](modelling/spec.md) | Deterministic volume construction reference |
| [split_pairing](split_pairing/spec.md) | Face cutting/pairing reference |
| [mep](mep/authoring.md) | Optional EnergyPlus physical-property authoring |
| [intakeoutput](intakeoutput/spec.md) | Legacy IntakeOutput assembly |

Consumers use [resources.py](../resources.py). Internal stage labels and persisted
historical run IDs still describe that workflow. Judge rubrics remain evaluation-only
and are excluded from isolated reader workspaces.

These resources ship with their optional consumers and are included in domain file
hashes. No current role automatically reads them. Historical instructions can be
recovered through the development repository's
[archive index](https://github.com/hortonyyx/Sim-BIM-Agent/blob/main/AI_agent/reports/archive-index.md);
new cases use its
[current run guide](https://github.com/hortonyyx/Sim-BIM-Agent/blob/main/AI_agent/workflow/run_case.md).
These development documents are not bundled in the product.
