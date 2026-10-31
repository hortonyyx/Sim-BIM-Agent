"""Legacy EnergyPlus intake: reading-stage vector JSON -> IntakeOutput.

This is an explicitly selected historical workflow. Current Sim BIM Agent
entrypoints use runtime_entry/runtime_roles and do not execute this sequence.
Resource documents live under src.agent.legacy; persisted stage IDs stay stable.

The reading stage (0_reading: image -> semantic vector JSON) is image-bound and
half-manual; this module runs everything after it, image-blind, as ordered
stages each independently swappable and with a verifiable / diffable intermediate:

  1_correction      CORRECTION (LLM)   vectors + correction docs -> CorrectedGeometry
  core              DETERMINISTIC      canonical axis snap + sliver guard (code)
  2_modelling +     GEOMETRY (code)    CorrectedGeometry -> BuildingGeometry, then
  3_split_pairing                      serialized to zone/surface/fenestration specs
  4_mep             PHYSICS (LLM)      the 8 non-geometry IntakeOutput fields
  5_intakeoutput    ASSEMBLY (code)    stitch geometry + MEP specs -> IntakeOutput

The CorrectedGeometry checkpoint is materialized (correction_geometry*.json) so the
correction error budget is separable, and the deterministic core guarantees no
cross-floor sliver reaches EnergyPlus. Geometry is fully deterministic (the kernel
builds + serializes it); the LLM does only correction judgment (1_correction) and
physics semantics (4_mep).

This module is the single implementation. It is called from:
  - `intake_node` (the optional EnergyPlus graph), and
  - `scripts/run_pipeline_deepseek.py` (thin standalone CLI wrapper).

Per-stage model config: `intake_correction` / `intake_mep` in llm.yaml if present,
else fall back to `intake_correction` (so "model switching has one home" still
holds). Why a raw OpenAI client instead of langchain: single-shot calls with
DeepSeek thinking on; langchain's structured-output burns tokens on
reasoning_content and never emits the tool call, so we ask for JSON-only content
and parse it ourselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from openai import OpenAI

from omegaconf import OmegaConf

from src.agent._share import ensure_schema_initialized
from src.agent.correction import CorrectedGeometry, apply_deterministic_core
from src.agent.correction.config import load_core_tolerances
from src.agent.correction.envelope import extract_authoritative_envelope
from src.agent.correction.vocab import (
    format_correction_system_vocabulary,
    producer_facing_json_schema,
    retry_guidance_for_correction,
)
from src.agent.execution.evidence_preflight import (
    WINDOW_EVIDENCE_CHANNEL_SPLIT_DEBT_ID,
    EvidenceDebt,
    compute_evidence_debt_from_vector_dir,
    compute_reading_report_from_vector_dir,
    project_evidence_debt,
    window_evidence_channel_split_debt,
    write_evidence_debt,
)
from src.agent.execution.subscription_json import call_subscription_json
from src.agent.llm import load_llm_section, resolve_llm_config_path
from src.agent.reading.vector_contract import classify_vector_dir
from src.agent.state import IntakeOutput
from src.validator.checks.schema import Disposition, RunProfile

if TYPE_CHECKING:
    from src.agent.correction.decision_executor import DecisionLoopOutcomeV1
    from src.agent.correction.decision_schema import (
        CorrectionDecisionPacketV1,
        CorrectionDecisionResponseV1,
    )
    from src.agent.correction.evidence_contract import (
        CorrectionEvidenceBundleArtifactV1,
    )
    from src.agent.geometry.modelling import BuildingGeometry
    from src.agent.output_coordinates import IntakeArtifactBundle

from src.agent.legacy.resources import INTAKE_RESOURCES

_CORRECTION_DIR = INTAKE_RESOURCES / "correction"
_CORRECTION_DOCS = [
    "README.md",
    "A0_contract.md",
    "A1_coordinate_normalization.md",
    "A2_regularization.md",
    "A3_arbitration.md",
    "A4_priors.md",
]

_PLAN_RE = re.compile(r"^(\d+)f_view\.json$", flags=re.IGNORECASE)
_JS_CONCAT_RE = re.compile(r'"\s*\+\s*\n\s*"', flags=re.MULTILINE)


def discover_vector_files(vector_dir: Path) -> list[str]:
    """Scan a reading-stage vector dir for ALL vector JSONs, in a stable order.

    Order: numeric floor plans (by floor number) -> facade elevations ->
    supplementary / section / other vector JSONs. `reading_summary.md` is read
    separately.
    """
    names = sorted(p.name for p in vector_dir.glob("*.json"))
    if not names:
        raise FileNotFoundError(f"no *.json found under {vector_dir}")
    plans = sorted(
        (n for n in names if _PLAN_RE.match(n)),
        key=lambda n: (int(_PLAN_RE.match(n).group(1)), n),
    )
    elevations = [
        n for n in names if not _PLAN_RE.match(n) and n.lower().endswith("_view.json")
    ]
    others = [
        n
        for n in names
        if not _PLAN_RE.match(n) and not n.lower().endswith("_view.json")
    ]
    return plans + elevations + others


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8").strip()


def _fix_js_concat(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = _JS_CONCAT_RE.sub("", s)
    return s


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return _fix_js_concat(text)


def _load_correction_docs() -> str:
    return "\n\n".join(
        f"----- 1_correction/{name} -----\n{_read(_CORRECTION_DIR / name)}"
        for name in _CORRECTION_DOCS
    )


def _section(stage: str) -> dict:
    """Load `intake_<stage>` if present, else fall back to `intake_correction`.

    Only the "section absent from the config file" case triggers fallback.
    Any other error (YAML syntax, env-var interpolation failure, missing
    `default` and missing section) propagates immediately so a misconfigured
    `intake_mep` block never silently masquerades as `intake_correction`.
    """
    section_name = f"intake_{stage}"
    # Read the raw (unresolved) config keys without triggering interpolation —
    # this lets us distinguish "key absent → safe to fall back" from "key
    # present but value is broken → must surface the error".
    raw_keys = set(OmegaConf.load(resolve_llm_config_path()).keys())
    if section_name in raw_keys:
        # Section exists: load and resolve it; any error here is a real config
        # problem and must not be swallowed.
        return load_llm_section(section_name)
    # Section absent: fall back to intake_correction — but ONLY if it is itself
    # present. We must NOT delegate the absence to load_llm_section, which would
    # silently resolve to the downstream `default` ReAct config (wrong model /
    # thinking / temperature). A missing intake_correction is a hard config error.
    if "intake_correction" not in raw_keys:
        raise RuntimeError(
            f"llm.yaml has no 'intake_correction' section (required as the "
            f"fallback for '{section_name}'); refusing to silently use 'default'. "
            f"Add an 'intake_correction' section to the config."
        )
    return load_llm_section("intake_correction")


def _load_decision_beat_section(section_name: str) -> tuple[dict, str]:
    """Load the decision beat's llm.yaml section BY ITS REAL NAME (v3, B-1).

    ⭐ This is deliberately NOT ``_section``: that helper serves the staged-
    intake legs and prefixes ``intake_`` onto its argument, so asking it for
    ``correction_decision`` silently resolved to ``intake_correction`` — the
    v2 cross-review's B-1: the configured model seat was never loaded, and
    the route record only echoed the REQUESTED name, so nothing on disk
    could prove which model actually adjudicated.  This loader resolves the
    name verbatim; an absent section is a LOUD config error, because falling
    back to ``default`` would seat the ReAct downstream config (thinking
    disabled, temperature 0.7) in the adjudication beat, and falling back to
    ``intake_correction`` would collapse two config points that exist so
    they can be pointed at different models.

    Returns ``(section, loaded_name)`` so every downstream record (route /
    ``response_source``) can carry what was RESOLVED — with the model name
    read out of the loaded dict — never the request's echo.
    """
    raw_keys = set(OmegaConf.load(resolve_llm_config_path()).keys())
    if section_name not in raw_keys:
        raise RuntimeError(
            f"llm.yaml has no '{section_name}' section (the decision beat's "
            f"model seat); refusing to silently fall back to 'default' or "
            f"'intake_correction'. Add that section to the active config, "
            f"or pass llm_section_name explicitly."
        )
    return load_llm_section(section_name), section_name


def _call_json_llm(
    section: dict,
    system_prompt: str,
    human: str,
    *,
    out_dir: Path | None,
    prefix: str,
    attempts: int = 1,
    validate: Callable[[dict], None] | None = None,
    retry_guidance: Callable[[BaseException], str | None] | None = None,
) -> dict:
    """JSON-only LLM call with retry; return the parsed (and optionally validated) dict.

    The staged-intake LLM (DeepSeek) is single-shot per call and occasionally
    returns invalid JSON or a structurally-valid-but-incomplete object (e.g. a
    CorrectedGeometry that dropped all facade windows — the sm21 0-window class).
    With `attempts > 1` a parse/`validate` failure re-issues the call (the model's
    sampling differs each draw), turning an intermittent bad draw from a hard
    pipeline failure into a retry. `validate(parsed)` may raise to reject a draw
    that parses but fails a semantic check. Saves raw/thinking artifacts (last
    attempt). Raises after the final attempt still fails.

    `retry_guidance` (F-4a): when a retry happens AND the failure got far enough
    to receive a model response (i.e. it failed parsing/validation, not the
    network), this callable translates the exception into a FORMAT-ONLY
    corrective message that is appended to the next attempt's messages — so a
    systematic schema misunderstanding (unknown field, off-vocabulary key) is
    corrected instead of repeated verbatim until the budget is gone. Transport
    errors (the `create()` call raised before any response) retry blind, and
    `retry_guidance` returning ``None`` (e.g. for a semantic ValueError) also
    retries blind: the inner retry owns ONLY schema/format robustness, never
    geometry/upstream/numeric values. None/omitted ⇒ fully blind retry (legacy)."""
    if section.get("provider") == "claude_subscription":
        return call_subscription_json(
            section,
            system_prompt,
            human,
            out_dir=out_dir,
            prefix=prefix,
            attempts=attempts,
            validate=validate,
            retry_guidance=retry_guidance,
            extract_json=_extract_json,
        )
    api_key = section.get("api_key")
    base_url = section.get("base_url")
    model_name = section["model_name"]
    max_tokens = section.get("max_tokens", 64000)
    temperature = section.get("temperature", 0.3)
    reasoning_effort = section.get("reasoning_effort")
    extra_body = section.get("extra_body") or {"thinking": {"type": "enabled"}}
    if not api_key:
        raise RuntimeError(f"{prefix}: no api_key (set DEEPSEEK_API_KEY in .env).")
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "{}: model={} prompt sizes system={} human={} chars",
        prefix,
        model_name,
        len(system_prompt),
        len(human),
    )
    optional: dict = {}
    if reasoning_effort is not None:
        optional["reasoning_effort"] = reasoning_effort

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0, max_retries=2)
    last_err: Exception | None = None
    # F-4a: corrective message appended to the NEXT attempt's messages. Stays
    # "" (blind retry) unless the previous attempt received a model response
    # that then failed schema validation AND retry_guidance translated that
    # failure into a format-only correction. Transport errors never set this.
    guidance_text = ""
    for attempt in range(1, attempts + 1):
        # Reset per-attempt state so the except branch can always reference
        # `content` even when a transport error fires before the response is
        # unpacked (L2: transport exceptions now consume an attempt and retry).
        content: str = ""
        finish_reason: str | None = None
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human},
        ]
        if guidance_text:
            messages.append({"role": "user", "content": guidance_text})
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
                **optional,
            )
            msg = resp.choices[0].message
            finish_reason = resp.choices[0].finish_reason
            content = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None)
            usage = resp.usage
            logger.info(
                "{}: attempt {}/{} finish_reason={} usage={}",
                prefix,
                attempt,
                attempts,
                finish_reason,
                {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                },
            )
            if out_dir is not None:
                if reasoning:
                    (out_dir / f"{prefix}_thinking.txt").write_text(
                        reasoning, encoding="utf-8"
                    )
                (out_dir / f"{prefix}_raw.txt").write_text(content, encoding="utf-8")
            if finish_reason == "length":
                logger.warning("{}: hit max_tokens — response likely truncated", prefix)

            if not content.strip():
                raise ValueError(f"empty content (finish_reason={finish_reason})")
            parsed = json.loads(_extract_json(content))
            if validate is not None:
                validate(parsed)
            return parsed
        except Exception as e:  # noqa: BLE001 — transport errors and bad draws both retry
            last_err = e
            if out_dir is not None:
                # `content` is always a str here (initialised "" above); for
                # transport failures it stays "" and the preview reflects that.
                preview = _extract_json(content)[:500] if content.strip() else "(empty)"
                (out_dir / f"{prefix}_parse_error.txt").write_text(
                    f"attempt {attempt}/{attempts}: {type(e).__name__}: {e}\n"
                    f"first 500 chars:\n{preview}",
                    encoding="utf-8",
                )
            if attempt < attempts:
                # F-4a: earn corrective feedback ONLY when we got a model
                # response back that then failed parsing/validation. `content`
                # is non-empty iff create() returned (a transport error raises
                # inside create() before `content` is assigned, so it stays ""
                # and we retry blind). retry_guidance then decides whether the
                # exception is a schema ValidationError worth translating;
                # returning None (semantic ValueError / JSON syntax / etc.)
                # also retries blind. Guidance carries field paths + the
                # schema's legal tokens only, never geometry/upstream/numbers.
                if retry_guidance is not None and content.strip():
                    try:
                        guidance_text = retry_guidance(e) or ""
                    except Exception:  # noqa: BLE001 — guidance must never crash the retry
                        guidance_text = ""
                else:
                    guidance_text = ""
                logger.warning(
                    "{}: attempt {}/{} rejected ({}); retrying",
                    prefix,
                    attempt,
                    attempts,
                    e,
                )
                continue
            raise RuntimeError(
                f"{prefix}: failed after {attempts} attempt(s): {last_err}"
            ) from last_err
    raise RuntimeError(f"{prefix}: no attempts ran")  # unreachable when attempts >= 1


# --------------------------------------------------------------------------- #
# 1_correction — correction (LLM) -> CorrectedGeometry
# --------------------------------------------------------------------------- #
def _build_correction_messages(
    vector_dir: Path,
    testdata_text: str,
    *,
    feedback: str | None = None,
    evidence_debt: EvidenceDebt | None = None,
    target=None,
    observation_reference_catalog: str | None = None,
) -> tuple[str, str]:
    correction_docs = _load_correction_docs()
    reading_guide = _read(INTAKE_RESOURCES / "reading" / "guide.md")
    reading_pens = _read(INTAKE_RESOURCES / "reading" / "pen_library.md")
    # F-2b: the reading summary is a hard dependency (it carries the
    # cross-image narrative correction assembles its prompt around). A missing
    # summary is a named, localizable failure pointing at the reading stage's
    # kickoff contract — not a bare OSError two layers down in _read.
    summary_path = vector_dir / "reading_summary.md"
    if not summary_path.is_file():
        raise FileNotFoundError(
            f"1_correction requires the 0_reading summary at {summary_path}, but "
            "it is missing — the reading stage must produce reading_summary.md "
            "(kickoff contract); correction cannot assemble its prompt without it "
            "(F-2b)"
        )
    summary = _read(summary_path)
    target = target or __import__("src.agent.correction.parse", fromlist=["correction_target"]).correction_target("rectangular")
    # F-15 (2026-08-07): the PROMPT sees a schema stripped of
    # deterministic-core-only fields (facade_segments / facade_segment_id) —
    # validation elsewhere still uses the full target.schema_model unchanged.
    geom_schema = json.dumps(producer_facing_json_schema(target.schema_model), indent=2, ensure_ascii=False)

    system_prompt = (
        "You are running the CORRECTION stage (1_correction) of a staged "
        "EnergyPlus intake pipeline. The reading stage (image -> semantic vector "
        "JSON) is done; you DO NOT see drawings. Apply the correction layer to the "
        "reading-stage primitives and emit a single CorrectedGeometry JSON object: "
        "world-frame, wall-centerline, self-consistent room cells + windows + "
        "per-floor z, plus an audit.\n\n"
        "Do the correction (A1 -> A2 -> A3, priors via A4 under A0/A3 gating): put "
        "every coordinate in one world frame at wall CENTERLINE; reconcile the "
        "z-stack; ARBITRATE stroke-vs-dimension conflicts (trust dimensions; make "
        "each elevation window fall entirely within one room); complete missing "
        "values; log every material change in `corrections`, unresolved ambiguity "
        "in `conflicts`, and anything unsafe to fix in `unsupported`.\n\n"
        "Each room is exactly one cell; never split a single room into several "
        "cells just to keep them rectangular. A rectangular room is a "
        "rectangular cell {id, role, x:[min,max], y:[min,max]}. Only when a "
        "room's own shape is not a single rectangle (e.g. an L-shaped corridor), "
        f"emit the selected schema_version {target.schema_version!r} and give that cell a CCW orthogonal `polygon` "
        "(exterior ring, first vertex not repeated); keep `x`/`y` as the exact "
        "polygon bbox projection. Polygon is the exception, not the default. "
        "For `role`, prefer the image-observed role from room_labels; fall back "
        "to layout priors ONLY when no observation covers a room. "
        "Each floor gives z_floor + ceiling_height. Each window gives facade, "
        "along-facade span [min,max], z [sill,head] (absolute world z), and the "
        "room id it belongs to. Do not output zones/surfaces — only the corrected "
        "geometry primitives.\n\n"
        "OUTPUT FORMAT (strict): ONLY the CorrectedGeometry JSON object, starting "
        "with `{` and ending with `}`. No markdown, no prose.\n\n"
        "===== BEGIN CorrectedGeometry JSON SCHEMA =====\n"
        f"{geom_schema}\n"
        "===== END CorrectedGeometry JSON SCHEMA =====\n\n"
        + (
            "Note: `corrections`/`conflicts`/`unsupported` are free-form audit "
            "rows for THIS draw's own material changes / ambiguities / unsafe "
            "fixes. Never add a row with `\"kind\": \"window_host_resolution\"` "
            "to any of them — that is a separate audit trail the deterministic "
            "core produces downstream from your draw, not something for you to "
            "pre-fill.\n\n"
            if target.schema_version == "3"
            else ""
        )
        + f"{format_correction_system_vocabulary(target)}"
        "===== BEGIN RULE DOCUMENT: 1_correction =====\n"
        f"{correction_docs}\n"
        "===== END RULE DOCUMENT: 1_correction =====\n\n"
        "===== BEGIN REFERENCE: 0_reading/guide.md =====\n"
        f"{reading_guide}\n"
        "===== END REFERENCE: 0_reading/guide.md =====\n\n"
        "===== BEGIN REFERENCE: 0_reading/pen_library.md =====\n"
        f"{reading_pens}\n"
        "===== END REFERENCE: 0_reading/pen_library.md =====\n\n"
        "===== BEGIN REFERENCE: reading_summary.md =====\n"
        f"{summary}\n"
        "===== END REFERENCE: reading_summary.md =====\n"
    )

    chunks = [
        "Project metadata (testdata_prompt.json):\n```json\n" + testdata_text + "\n```\n"
    ]
    # F-97: `discover_vector_files` only ORDERS the directory; it does not say
    # what any file IS. Every name it returned used to be pasted into the prompt
    # verbatim, so a JSON of any contract reached the model as untyped text
    # having passed no reading gate. Classify first: consumable files survive in
    # their original order, a declared non-input (check-report sidecar) is
    # dropped but named in the ledger, and anything else raises.
    vector_decision = classify_vector_dir(
        vector_dir, discover_vector_files(vector_dir)
    )
    # ⭐ Module 7 (2026-09-02): a recognized as-drawn product now has a WIRE
    # (ADAPT → evidence chain). Pasting it into this prompt was never legal,
    # and SILENTLY DROPPING it now would be worse: the run would look green
    # while its entire as-drawn evidence stayed outside the model. The
    # pasted-JSON leg refuses the directory instead — loudly, naming the
    # files and the switch. (Mirror of "the evidence chain must never fall
    # back to this leg": neither leg may quietly take the other's inputs.)
    if vector_decision.adapted:
        from src.agent.reading.vector_contract import UnconsumableVectorFile

        raise UnconsumableVectorFile(
            "1_correction pasted-JSON leg refuses to run over "
            f"{Path(vector_dir)}: {len(vector_decision.adapted)} file(s) are "
            "wired to the correction evidence adapter (module 7) and must "
            "NOT be silently dropped from this run's evidence:\n  - "
            + "\n  - ".join(vector_decision.adapted)
            + "\nOpen the evidence chain (run_correction(..., "
            "evidence_chain=True)) or move these products out of this "
            "run's 0_reading directory."
        )
    vector_files = vector_decision.consumed
    room_label_inputs = _reading_room_label_inputs(vector_dir, vector_files)
    if room_label_inputs:
        chunks.append(
            "\nExplicit room role observations from reading `room_labels` "
            "(image-local anchors):\n```json\n"
            + json.dumps(room_label_inputs, indent=2, ensure_ascii=False)
            + "\n```\n"
        )
    if evidence_debt is not None and evidence_debt.debts:
        chunks.append(
            "\nReading evidence debt from deterministic 0_reading preflight "
            "(A8 handoff; current run_profile disposition already applied):\n"
            "```json\n"
            + evidence_debt.model_dump_json(indent=2)
            + "\n```\n"
            "Correction discipline for evidence debt: do not invent coordinates, "
            "dimensions, or source links for debt items. If the reading artifact "
            "does not contain independent evidence to resolve an item, record the "
            "unresolved item in `conflicts` with the debt check_id/view/offender ids. "
            "Do not write these reading evidence-debt items to `unsupported`; "
            "`unsupported` is reserved for deterministic kernel findings.\n"
        )
    for fname in vector_files:
        chunks.append(f"\n[reading vector] {fname}:\n```json\n{_read(vector_dir / fname)}\n```\n")
    if observation_reference_catalog:
        # F-7 (2026-08-05): source_ids cites what the draw can actually see —
        # a reading-vector observation, not a locator hash it cannot compute
        # (that hash embeds reading-artifact bytes this stage never receives).
        chunks.append(
            "\n===== BEGIN WINDOW SOURCE OBSERVATIONS =====\n"
            "Each window's `provenance.<claim>.source_ids` must cite the "
            "specific reading-stage observations that support that claim, "
            "using the form `<view_file_stem>/<observation_id>` — the "
            "reading vector JSON's filename above (without `.json`) "
            "followed by `/` and the `id` of a `window`-pen stroke from that "
            "file's `strokes` array (e.g. `1f_view/S11`). Cite ONLY ids "
            "listed in the catalog below; never invent an id, and never "
            "write a `src:`-prefixed hash — you cannot compute one and must "
            "not try. Every window's `existence` claim requires at least one "
            "such reference; other claims (`host`/`along`/`width`/`sill`/"
            "`head`/`appearance`) may only cite an observation whose entry "
            "below lists that claim as allowed. For an elevation observation, "
            "when the entry below shows an 'approx world-along interval', that "
            "number is already translated out of image-local into the SAME "
            "world frame as a window's own `span` — pick the elevation "
            "observation whose interval overlaps the window you are citing "
            "for, not the one whose raw on-image position looks similar; do "
            "NOT re-derive or re-flip it yourself (it is informational, not a "
            "substitute for A1 §2.2, and is independently re-verified after "
            "your draw regardless).\n\n"
            "Legal observation references for this run "
            "(view/observation: allowed claims):\n"
            f"{observation_reference_catalog}\n"
            "===== END WINDOW SOURCE OBSERVATIONS =====\n"
        )
    if feedback:
        chunks.append(
            "\n\n=== Geometry feedback from a previous attempt — fix these ===\n"
            f"{feedback}\nAddress every item while following the correction rules.\n"
        )
    chunks.append(
        "\nApply the correction now and output ONLY the CorrectedGeometry JSON "
        "object. Derive each window's world `span` from its elevation's image-local "
        "`facade` block using the per-facade world-axis convention in 1_correction "
        "A1 §2.2 (mind the North/West sign flip); reading does not assert world axes. "
        "Enumerate every room cell and window explicitly."
    )
    return system_prompt, "".join(chunks)


def _reading_room_label_inputs(vector_dir: Path, vector_files: list[str]) -> list[dict]:
    observations = []
    for fname in vector_files:
        try:
            data = json.loads(_read(vector_dir / fname))
        except (json.JSONDecodeError, OSError):
            continue
        room_labels = data.get("room_labels") or []
        if not room_labels:
            continue
        observations.append(
            {
                "view_file": fname,
                "image_label": data.get("image_label", ""),
                "image_kind": data.get("image_kind", ""),
                "room_labels": room_labels,
            }
        )
    return observations


def _reading_window_stroke_count(vector_dir: Path) -> int:
    """Total `window`-pen strokes across all reading-stage vector JSONs.

    A proxy for "this building has windows in the drawings". Used only as a
    presence signal (>0 vs 0), not a 1:1 count — a plan window stroke and its
    elevation counterpart both count, so this over-counts window entities."""
    total = 0
    for p in sorted(vector_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        total += sum(1 for s in d.get("strokes", []) if s.get("pen") == "window")
    return total


def correction_draw_issues(
    geom: "CorrectedGeometry", expected_window_strokes: int
) -> list[str]:
    """Semantic draw-quality issues on a parsed (pre-core) CorrectedGeometry — the
    content checks that decide whether a structurally-valid draw is good enough:

    - Window completeness: reading has windows but correction emitted zero (the
      sm21 0-window instability). Conservative: only the all-or-nothing case (window
      strokes ≠ window entities, so no fuzzy "too few" threshold).
    - Duplicate cell ids across floors: three downstream dicts key on cell id;
      duplicates cause silent last-wins corruption.
    - z-stack discontinuity: adjacent floors whose gap exceeds gap_close_threshold_m
      (gaps ≤ the threshold are auto-closed by the deterministic core, so they are
      NOT issues here — run this on the PRE-core draw).

    Returns a list of messages (empty = clean). It does NOT raise — so the stepwise
    orchestrator can fold these into the stage gate① report (each semantic-bad draw
    then becomes a counted, append-only attempt + a blind resample), instead of
    being silently retried inside the LLM call and bypassing the per-stage budget.
    """
    from src.agent.correction.cell_geometry import (
        cell_has_polygon,
        validate_cell_polygon,
    )
    from src.agent.correction.config import load_core_tolerances
    from src.agent.geometry.capability import FEATURE_CELL_POLYGON, schema_supports

    tol = load_core_tolerances()
    issues: list[str] = []
    if expected_window_strokes > 0 and not geom.windows:
        issues.append(
            f"correction emitted 0 windows but the reading stage has "
            f"{expected_window_strokes} window stroke(s) — facade windows were "
            f"dropped (a known 1_correction instability, not a kernel issue)"
        )

    seen: dict[str, str] = {}  # cell_id -> floor_name
    for floor in geom.floors:
        for cell in floor.cells:
            if cell.id in seen:
                issues.append(
                    f"duplicate cell id '{cell.id}' found in floors "
                    f"'{seen[cell.id]}' and '{floor.name}' — downstream geometry "
                    f"dicts key on cell id, so duplicates cause last-wins corruption"
                )
            else:
                seen[cell.id] = floor.name
            # Polygon crimes are LLM draw defects, not code defects: mirror the
            # deterministic core's polygon raises here (winding-tolerant — the
            # core canonicalizes CW→CCW) so a bad polygon blocks THIS draw and
            # blind-resamples instead of crashing the flow inside the core.
            if cell_has_polygon(cell):
                if not schema_supports(geom, FEATURE_CELL_POLYGON):
                    issues.append(
                        f"cell '{cell.id}' has a polygon but schema_version is "
                        f"not '2' — polygon cells require the v2 contract"
                    )
                    continue
                try:
                    validate_cell_polygon(
                        cell,
                        min_edge_length_m=tol.min_edge_length_m,
                        allow_cw=True,
                        allow_closed=True,
                    )
                except ValueError as exc:
                    issues.append(f"invalid cell polygon in this draw: {exc}")
    gap_limit = tol.gap_close_threshold_m
    sorted_floors = sorted(geom.floors, key=lambda f: f.z_floor)
    for prev, cur in zip(sorted_floors, sorted_floors[1:]):
        expected_z = prev.z_floor + prev.ceiling_height
        gap = abs(cur.z_floor - expected_z)
        if gap > gap_limit:
            issues.append(
                f"z-stack discontinuity between floor '{prev.name}' "
                f"(top={expected_z:.3f}) and '{cur.name}' (z_floor={cur.z_floor}): "
                f"gap={gap:.3f} m > gap_close_threshold={gap_limit} m"
            )
    return issues


def _schema_only_correction_validator(parsed: dict, target=None) -> None:
    """Inner-retry validator for stepwise mode: accept any draw that parses into a
    valid CorrectedGeometry (schema/format robustness only). Semantic draw quality
    is judged by the OUTER gate① (correction_draw_issues), so a content-bad draw is
    counted + filed, not silently re-drawn inside `_call_json_llm`."""
    from src.agent.correction.parse import correction_target, parse_correction_draw
    parse_correction_draw(parsed, target or correction_target("rectangular"))


def _make_correction_validator(
    expected_window_strokes: int,
    target=None,
) -> Callable[[dict], None]:
    """Full inner-retry validator (legacy ``run_pipeline`` path): reject a draw on
    Pydantic schema violation OR any semantic draw issue (0-window / duplicate cell
    id / z-stack discontinuity). Raises ValueError on the first problem, which makes
    `_call_json_llm` log + retry (up to `attempts` draws).

    Stepwise mode uses ``_schema_only_correction_validator`` instead and routes the
    semantic checks through gate① — see ``correction_draw_issues``.
    """
    from src.agent.correction.parse import correction_target
    target = target or correction_target("rectangular")
    def _validate(parsed: dict) -> None:
        from src.agent.correction.parse import parse_correction_draw
        geom = parse_correction_draw(parsed, target)
        issues = correction_draw_issues(geom, expected_window_strokes)
        if issues:
            raise ValueError(issues[0])

    return _validate


_VECTOR_CONTRACT_LEDGER_NAME = "reading_vector_contract_ledger.json"


def _write_vector_contract_ledger(vector_dir: Path, stage_out_dir: Path) -> Path | None:
    """F-97 (F-c): record which contract each 0_reading JSON was consumed as.

    ⭐ This is what turns extensibility from a promise into a reading: a shape
    nobody declared stops being a silent paste and becomes a named row.
    Lands in the run's existing `_run/` metadata dir (``stage_out_dir`` is the
    1_correction stage dir, so its parent is the run dir).

    ⭐ Never raises (BLK-C). Two separate ways it used to: `ledger_for` died on
    an unreadable/undecodable member file (fixed in `vector_contract`), and the
    write itself dies when the run dir is hostile (`_run` already exists as a
    FILE => `FileExistsError`). Both destroyed F-b along with F-c: the caller
    never got to raise the NAMED refusal, it got a bare filesystem error
    instead. Losing the ledger to an unwritable disk is unavoidable; losing the
    named refusal too is a strict regression, so the write failure is logged and
    swallowed while classification carries on to its loud verdict.
    """
    from src.agent.execution.run_meta import run_meta_path
    from src.agent.reading.vector_contract import ledger_for

    try:
        names = discover_vector_files(vector_dir)
    except FileNotFoundError:
        return None
    ledger = ledger_for(vector_dir, names)
    ledger["vector_dir"] = str(vector_dir)
    try:
        path = run_meta_path(
            Path(stage_out_dir).parent, _VECTOR_CONTRACT_LEDGER_NAME, for_write=True
        )
        path.write_text(
            json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        # ⛔ Not a silent skip: the run dir is unusable, say so. The named
        # refusal from `classify_vector_dir` still follows.
        logger.warning(
            "F-97: could not file the reading contract ledger under {}: {}: {}",
            Path(stage_out_dir).parent,
            type(exc).__name__,
            exc,
        )
        return None
    return path


def _preflight_vector_contracts(vector_dir: Path, out_dir: Path | None) -> None:
    """F-97: classify the reading vector dir, file the ledger, then fail loudly.

    ⭐ Runs before ANY consumer that parses `*_view.json` -- in `run_correction`
    AND in the composite `run_pipeline_artifacts` entry. The reading evidence
    preflight further down assumes every view is an object and dies on a
    non-object with a bare ``AttributeError: 'list' object has no attribute
    'get'`` -- which is neither F-97's named refusal nor F-c's ledger. Ordering
    is the fix: the ledger is on disk and the offending file is named before
    anything else touches these files.

    ⚠️ BLK-B: "before any consumer" was only true INSIDE `run_correction`. The
    composite entry parsed `*_view.json` three times (reading report, view load,
    v3 catalog) about forty lines upstream of it, so the whole promise evaporated
    for the entry production actually calls. The docstring was the load-bearing
    claim and it was false -- entry LEVEL is part of "any consumer", not a detail
    below it. `run_pipeline_artifacts` now calls this first; `run_correction`
    still calls it too, so it remains a real entry point on its own and the
    second call is a cheap idempotent rewrite of the same ledger.
    """
    if out_dir is not None:
        _write_vector_contract_ledger(vector_dir, out_dir)
    try:
        names = discover_vector_files(vector_dir)
    except FileNotFoundError:
        return  # "no *.json at all" is an existing, separately-reported failure
    classify_vector_dir(vector_dir, names)


_UNSET_VALIDATOR = object()


# --------------------------------------------------------------------------- #
# 1_correction evidence chain (module 7 wiring, 2026-09-02)
#
#   frozen reading bytes → adapt_* → CorrectionEvidenceBundleArtifactV1
#     → compile_wall_ir → WallCompilationV1
#     → build_decision_packet → CorrectionDecisionPacketV1
#     → [the model beat] → CorrectionDecisionResponseV1
#     → run_decision_loop → DecisionLoopOutcomeV1   ← this module's terminus
#
# ⭐ The switch is EXPLICIT and the default is OFF: every existing caller keeps
# the pasted-JSON leg byte-for-byte.  When the chain is on, a failure at ANY
# link propagates to the caller UNCAUGHT and the run dir gets a record naming
# the link — ⛔ this leg NEVER falls back to the pasted-JSON prompt (guide
# §十之二 #4), and (mirror rule, enforced in _build_correction_messages) the
# pasted-JSON leg never silently drops a file this chain owns.
# --------------------------------------------------------------------------- #
_EVIDENCE_CHAIN_ROUTE_NAME = "evidence_chain_route.json"
_EVIDENCE_CHAIN_FAILURE_NAME = "evidence_chain_failure.json"
_EVIDENCE_CHAIN_OUTCOME_NAME = "decision_loop_outcome.json"
#: W-1 rework BLK-E (2026-09-07l): per-floor record of the product bytes this
#: chain run froze and consumed (``{"schema", "product_filename",
#: "source_bytes_sha256"}``), filed INSIDE ``out_dir`` so per-floor chain runs
#: do NOT overwrite each other (the route record above is run-level
#: last-writer-wins by ``run_meta_path``'s design).  The multifloor wiring
#: reconciles its own post-chain re-read against THIS hash before deriving
#: the snap tolerance from it.
_EVIDENCE_CHAIN_SOURCE_RECORD_NAME = "chain_source_record.json"
#: B1 wiring: the projection bridge's whole product, filed next to the
#: outcome.  ⛔ The bare geometry never travels as the product without
#: this envelope (design §四: hash 对不上 / envelope 丢失 ⇒ 投影失败).
_EVIDENCE_CHAIN_PROJECTION_NAME = "projection_envelope.json"
#: W#3 (wallhunt 2026-09-08b): the FINAL wall compilation the decision loop
#: settled on, filed next to the outcome/projection envelope.  This is the
#: chain's model product — the byte the writer's as_drawn replay re-drives
#: the geometry from (embedded into the candidate's chain provenance by the
#: flow wiring).  Its ``content_sha256`` equals the outcome's
#: ``final_provisional_sha256`` and the envelope's ``source_resolved_sha256``
#: — three artifacts, one binding.
_EVIDENCE_CHAIN_COMPILATION_NAME = "evidence_chain_compilation.json"
#: W#6 (wallhunt 2026-09-08b): the chain's cut lines AS PROJECTED (i.e. after
#: the exterior-frame declaration snap), plus the exact ``project_cut_lines``
#: arguments the chain used — filed so the multifloor wiring can realign the
#: upper floors' wall positions onto the reference floor and RE-PARTITION
#: from the same lines (conservation survives the alignment by construction),
#: and so the writer-side replay re-drives the identical reconciliation.
_EVIDENCE_CHAIN_CUT_LINES_NAME = "cut_lines.json"
#: The llm.yaml section the model beat reads, BY ITS REAL NAME (v3, B-1 —
#: ⛔ never through the `intake_`-prefixing `_section()`, which silently
#: resolved this to `intake_correction`).  An absent section is a LOUD
#: config error (`_load_decision_beat_section`), never a fallback to
#: `default` or `intake_correction`; the beat never hardcodes a provider.
DECISION_BEAT_LLM_SECTION = "correction_decision"


class EvidenceChainProjection(NamedTuple):
    """B1 wiring: the caller's DECLARED floor meta for the projection.

    ``z_floor_m`` / ``ceiling_height_m`` are REQUIRED with no defaults and
    no invention: the bridge refuses to mint a z (B2 owns sourcing them),
    so a caller without a sourced value cannot construct a product — pass
    nothing and ``run_correction`` fails loudly before the chain runs.
    ``floor_id`` / ``floor_name`` default to the chain's derived
    ``floor_ref`` when omitted.
    """

    z_floor_m: float
    ceiling_height_m: float
    floor_id: str | None = None
    floor_name: str | None = None


class EvidenceChainTerminal(RuntimeError):
    """The evidence chain terminated WITHOUT a product to hand back.

    B1 landed (2026-09-03): the projection bridge exists, and a SUCCESSFUL
    chain outcome is projected into a ``CorrectedGeometryV3`` whose
    envelope (``projection_envelope.json``, ``footprint_provenance=
    "derived_from_walls"``) is filed next to the outcome — that is what
    ``run_correction(evidence_chain=True)`` returns.

    This exception now fires exactly when the loop did NOT succeed: the
    final provisional compilation is then an audit-only product
    (``success=False`` on the same outcome says so), and ⛔ the provisional
    must never travel as — or be projected into — a finished
    ``CorrectedGeometry``.  Carries the outcome path, ``success`` and
    ``exit_reason`` verbatim.
    """


def _evidence_chain_run_meta(out_dir: Path | None, name: str) -> Path | None:
    """Best-effort path into the run's ``_run/`` metadata dir, or None.

    ⭐ Like `_write_vector_contract_ledger`, never raises: a hostile run dir
    must not turn a named chain failure into a bare filesystem error that
    hides the original exception.
    """
    if out_dir is None:
        return None
    try:
        from src.agent.execution.run_meta import run_meta_path

        return run_meta_path(Path(out_dir).parent, name, for_write=True)
    except OSError:
        return None


def _record_evidence_chain_failure(out_dir: Path | None, stage: str, exc: Exception) -> None:
    """File ONE run-level record naming the link that failed, then return
    (the caller re-raises the original exception — ⛔ never caught here)."""
    path = _evidence_chain_run_meta(out_dir, _EVIDENCE_CHAIN_FAILURE_NAME)
    if path is None:
        return
    try:
        path.write_text(
            json.dumps(
                {
                    "route": "evidence_chain",
                    "failed_stage": stage,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as write_exc:
        logger.warning(
            "evidence chain: could not file the failure record under {}: {}",
            path.parent,
            type(write_exc).__name__,
            write_exc,
        )


def _decision_beat_messages(packet_json: str, response_schema: str) -> tuple[str, str]:
    """The model beat's prompt pair: adjudicate THIS packet, answer as the
    no-coordinate response schema."""
    system_prompt = (
        "You are the adjudication beat of the 1_correction stage in a staged "
        "EnergyPlus intake pipeline. The deterministic compiler has turned the "
        "frozen reading evidence into a provisional wall model plus a packet "
        "of OPEN ITEMS (each with symbolic candidates). Your job: decide every "
        "open item, and give one whole-building review.\n\n"
        "Rules:\n"
        "1. Echo the packet's `packet_hash` verbatim in your response.\n"
        "2. `item_id` values must be exactly the packet's open-item ids; "
        "`candidate_id` values must be exactly the candidates listed for that "
        "item. Unknown ids are rejected.\n"
        "3. `action` is one of select_candidate / reject_all / "
        "request_reperception; `candidate_id` rides ONLY with "
        "select_candidate.\n"
        "4. `reason_code`, `finding_id`, `kind` and `rationale` are CODE "
        "TOKENS: SCREAMING_SNAKE_CASE over the ASCII letters A-Z and the "
        "underscore ONLY — no digits, no spaces, no lowercase, no "
        "punctuation, 1 to 96 characters, starting with a letter (e.g. "
        "DECLARED_THICKNESS_MATCH, SPACING_EXCEEDS_DECLARED_BAND). Cite "
        "quantities by name, never by numeral. Findings must cite entities "
        "the packet actually indexes.\n"
        "5. ⛔ Your response carries NO coordinates anywhere — no numbers at "
        "all, and no coordinate pairs inside strings. Coordinates are computed "
        "by code from the frozen evidence; a response containing them is "
        "rejected before it reaches the executor.\n"
        "6. `whole_building_review.verdict` is `accept` (no findings) or "
        "`findings` (at least one finding). Acceptance is only meaningful "
        "against THIS packet's provisional geometry.\n\n"
        "OUTPUT FORMAT (strict): ONLY the response JSON object, `{`..`}`, no "
        "markdown, no prose.\n\n"
        "===== BEGIN CorrectionDecisionResponseV1 JSON SCHEMA =====\n"
        f"{response_schema}\n"
        "===== END CorrectionDecisionResponseV1 JSON SCHEMA =====\n"
    )
    human = (
        "Adjudicate this decision packet now. Decide EVERY open item and give "
        "the whole-building review.\n\n"
        "===== BEGIN DECISION PACKET =====\n"
        f"{packet_json}\n"
        "===== END DECISION PACKET =====\n"
    )
    return system_prompt, human


def _make_decision_response_provider(
    *,
    section: dict,
    out_dir: Path | None,
) -> "Callable[[CorrectionDecisionPacketV1], CorrectionDecisionResponseV1]":
    """Build the packet→response callable that seats the MODEL in the loop.

    ``section`` is the ALREADY-RESOLVED llm.yaml section dict (v3, B-1):
    the chain resolves it once via ``_load_decision_beat_section`` so the
    route record can carry what was actually loaded — the provider holds
    that one dict for its whole life, so a run cannot change models
    mid-loop.

    Every response is checked TWICE before the executor sees it: the
    no-coordinate payload guard (numbers / coordinate pairs in strings,
    naming the JSON path) and the strict type construction — both feed the
    call's own format-retry, so a fixable draw is retried with guidance
    instead of dying as a bare schema error.
    """
    from src.agent.correction.decision_schema import (
        CorrectionDecisionResponseV1,
        CoordinateSmuggledInResponse,
        assert_response_payload_carries_no_coordinates,
    )

    def _validate_against(packet: object, parsed: dict) -> None:
        assert_response_payload_carries_no_coordinates(parsed)
        if parsed.get("packet_hash") != packet.packet_hash:
            raise ValueError(
                f"packet_hash mismatch: echo {packet.packet_hash!r} verbatim, "
                f"got {parsed.get('packet_hash')!r}"
            )
        # ⭐ model_validate_json, NOT model_validate: the response type is
        # strict, and a JSON array (item_decisions: [...]) is a tuple in the
        # type's own terms only when validated in JSON mode — the python-mode
        # path would refuse every real model draw on `tuple_type`.
        CorrectionDecisionResponseV1.model_validate_json(json.dumps(parsed))

    def _retry_guidance(exc: BaseException) -> str | None:
        if isinstance(exc, CoordinateSmuggledInResponse):
            return (
                "FORMAT CORRECTION: your previous response carried a "
                "coordinate. Resend it with every coordinate removed — "
                f"({exc})"
            )
        if isinstance(exc, ValueError):
            return f"FORMAT CORRECTION: {exc}. Resend the full response JSON."
        return None

    def provider(packet: "CorrectionDecisionPacketV1") -> "CorrectionDecisionResponseV1":
        system_prompt, human = _decision_beat_messages(
            packet.model_dump_json(indent=2),
            json.dumps(
                CorrectionDecisionResponseV1.model_json_schema(),
                indent=2,
                ensure_ascii=False,
            ),
        )
        parsed = _call_json_llm(
            section,
            system_prompt,
            human,
            out_dir=out_dir,
            # ⭐ v3 NF-1: PER-ROUND raw/thinking filenames.  One shared name
            # meant each round overwrote the last, so round 0's decision
            # hash could not be recomputed from the archive by a third
            # party -- "the model really ran" travels through the archive,
            # and an overwritten archive forces the next reviewer to either
            # re-run (costly) or trust the report.
            prefix=f"correction_decision_r{packet.round_index}",
            attempts=2,
            validate=lambda p, _packet=packet: _validate_against(_packet, p),
            retry_guidance=_retry_guidance,
        )
        # Construction cannot fail here (validate already built it once) —
        # this line is the type-layer boundary the executor relies on.
        # JSON mode for the same strict-mode reason as in _validate_against.
        return CorrectionDecisionResponseV1.model_validate_json(json.dumps(parsed))

    return provider


def run_correction_evidence_chain(
    vector_dir: Path,
    product_filename: str,
    *,
    out_dir: Path | None = None,
    floor_ref: str | None = None,
    view_type: str | None = None,
    profile: str = "exploratory",
    round_budget: int = 3,
    fixed_responses: "Sequence[CorrectionDecisionResponseV1] | None" = None,
    llm_section_name: str = DECISION_BEAT_LLM_SECTION,
    projection: EvidenceChainProjection | None = None,
) -> "DecisionLoopOutcomeV1":
    """Drive ONE frozen reading product through the evidence chain to a
    ``DecisionLoopOutcomeV1`` on disk (module 7's terminus).

    Route selection is the classifier's verdict, never a file name: an
    ``as_drawn_plan`` product takes the as-drawn adapter (plan); an
    ``as_drawn_elevation_v0`` product takes the as-drawn adapter (elevation,
    B3); a legacy reading view takes the legacy adapter (its ``image_kind``
    decides plan/elevation).  Anything else — a v0 prototype, an unknown or
    damaged shape — is a LOUD refusal at the adapt link: the chain has no
    silent fallback to the pasted-JSON leg and none to legacy recognition.

    ``fixed_responses`` is the sanctioned escape hatch (dispatch §四): when
    given, the model beat is NOT called and the fixed responses drive the
    loop — for proving the wiring when the provider cannot run.  The route
    record says which source was used; a fixture result is never reported
    as a model result.

    ``projection`` (B1 wiring): when given AND the outcome is a success,
    the chain also drives the projection bridge over the final wall
    compilation and files ``projection_envelope.json`` next to the outcome
    (the return type is unchanged — the envelope is the on-disk product).
    A non-success outcome is never projected: the provisional is
    audit-only, and refusing it is ``run_correction``'s job.  Requires
    ``out_dir`` (the envelope IS the product; ⛔ a bare geometry).

    Failure contract (acceptance 3): a failure at any link propagates to
    the caller UNCAUGHT, and ``_run/evidence_chain_failure.json`` names the
    link (source_read / adapt / compile / model / loop / project).
    """
    from src.agent.correction.decision_executor import run_decision_loop
    from src.agent.correction.evidence_adapters import (
        adapt_as_drawn_elevation,
        adapt_as_drawn_plan,
        adapt_legacy_reading_view,
    )
    from src.agent.correction.evidence_contract import EvidenceContractError
    from src.agent.reading.vector_contract import (
        CONTRACT_AS_DRAWN_ELEVATION_V0,
        CONTRACT_AS_DRAWN_PLAN,
        CONTRACT_READING_VIEW_LEGACY,
        classify_vector_json,
    )

    # W#4: this file describes the current invocation, not an earlier draw.
    # Clear it before source_read; a new exception below files its own link.
    # A normal (including non-success terminal) outcome has its own route and
    # outcome records and must not inherit a stale exception from a prior run.
    failure_path = _evidence_chain_run_meta(out_dir, _EVIDENCE_CHAIN_FAILURE_NAME)
    if failure_path is not None:
        failure_path.unlink(missing_ok=True)

    # -- source_read: freeze the bytes -------------------------------------- #
    try:
        raw = (Path(vector_dir) / product_filename).read_bytes()
    except OSError as exc:
        _record_evidence_chain_failure(out_dir, "source_read", exc)
        raise
    # W-1 rework BLK-E (2026-09-07l): the hash of THE bytes this chain run
    # froze and consumed, filed on the route so a downstream re-reader (the
    # multifloor wiring's snap-tolerance derivation) can reconcile its own
    # read against the chain's input — ⛔ the second read is authorized by
    # THIS hash or it is a named red, never an unverified derivation source.
    source_bytes_sha256 = hashlib.sha256(raw).hexdigest()
    if out_dir is not None:
        # per-floor sidecar (the route record is run-level last-writer-wins,
        # so it cannot anchor a multi-floor reconciliation alone)
        record_path = Path(out_dir) / _EVIDENCE_CHAIN_SOURCE_RECORD_NAME
        try:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(
                    {
                        "schema": "chain_source_record_v1",
                        "product_filename": product_filename,
                        "source_bytes_sha256": source_bytes_sha256,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            # best-effort like the route record: a hostile run dir must not
            # turn a named chain failure into a bare filesystem error.  The
            # WIRING's reconciliation owns the loud missing-record red.
            logger.warning(
                "evidence chain: could not file the source record under %s",
                Path(out_dir),
            )

    # -- adapt: parse, classify, and let the classifier's verdict pick the
    #    adapter (never the file name) --------------------------------------- #
    try:
        doc = json.loads(raw.decode("utf-8"))
        decision = classify_vector_json(doc)
        if decision.contract_id == CONTRACT_AS_DRAWN_PLAN:
            adapter_name = "adapt_as_drawn_plan"
            if floor_ref is None:
                m = re.search(r"(\d+)\s*f", Path(product_filename).stem, re.I)
                floor_ref = m.group(0).lower() if m else Path(product_filename).stem
            artifact = adapt_as_drawn_plan(
                raw,
                input_id=Path(product_filename).stem,
                floor_ref=floor_ref,
                view_type="plan",
            )
        elif decision.contract_id == CONTRACT_AS_DRAWN_ELEVATION_V0:
            # ⭐ B3 (2026-09-03): the elevation branch.  An elevation has no
            # floor, so the semantic slot's second coordinate is the FACADE,
            # taken from what the product itself declares (``facade_label``),
            # ⛔ never guessed from the file name unless the product declares
            # nothing.  ``view_type`` is fixed: an elevation product is an
            # elevation view BY CONTRACT -- there is no second possibility
            # to infer, so none is inferred.
            adapter_name = "adapt_as_drawn_elevation"
            if floor_ref is None:
                facade_label = doc.get("facade_label")
                floor_ref = (
                    facade_label
                    if isinstance(facade_label, str) and facade_label
                    else Path(product_filename).stem
                )
            artifact = adapt_as_drawn_elevation(
                raw,
                input_id=Path(product_filename).stem,
                facade_ref=floor_ref,
                view_type="elevation",
            )
        elif decision.contract_id == CONTRACT_READING_VIEW_LEGACY:
            adapter_name = "adapt_legacy_reading_view"
            kind = doc.get("image_kind") or "plan"
            if view_type is None:
                view_type = "elevation" if kind == "elevation" else "plan"
            if floor_ref is None:
                floor_ref = doc.get("image_label") or Path(product_filename).stem
            artifact = adapt_legacy_reading_view(
                raw,
                input_id=Path(product_filename).stem,
                floor_ref=floor_ref,
                view_type=view_type,
            )
        else:
            raise EvidenceContractError(
                "EVIDENCE_CHAIN_SOURCE_CONTRACT_UNWIRED",
                {
                    "file": product_filename,
                    "contract": decision.contract_id,
                    "reason": decision.reason,
                    "wired": [
                        CONTRACT_AS_DRAWN_PLAN,
                        CONTRACT_AS_DRAWN_ELEVATION_V0,
                        CONTRACT_READING_VIEW_LEGACY,
                    ],
                },
            )
    except Exception as exc:  # ⛔ recorded, then re-raised untouched
        _record_evidence_chain_failure(out_dir, "adapt", exc)
        raise

    # -- the loop (compile + packet + model + executor) ---------------------- #
    if fixed_responses is not None:
        provider = None
        response_source = f"fixed_responses({len(fixed_responses)}; model NOT called)"
        responses = tuple(fixed_responses)
        llm_section_requested = None
        llm_section_resolved = None
        llm_model_resolved = None
    else:
        # v3, B-1: resolve the seat EAGERLY and BY ITS REAL NAME, before the
        # loop seats the model — a missing/misnamed section is a config
        # failure of the MODEL link (recorded as such), and everything the
        # route later records is read from THIS resolution result (the model
        # name out of the loaded dict), never echoed from the request string.
        try:
            section, llm_section_resolved = _load_decision_beat_section(
                llm_section_name
            )
        except Exception as exc:
            _record_evidence_chain_failure(out_dir, "model", exc)
            raise
        llm_section_requested = llm_section_name
        llm_model_resolved = section.get("model_name")
        provider = _make_decision_response_provider(
            section=section, out_dir=out_dir
        )
        response_source = f"model:{llm_section_resolved}"
        responses = ()
    try:
        final_compilation: list = []
        outcome = run_decision_loop(
            artifact,
            profile=profile,
            responses=responses,
            response_provider=provider,
            round_budget=round_budget,
            compilation_sink=(
                final_compilation.append if projection is not None else None
            ),
        )
    except Exception as exc:
        # Name the LINK, not just the call: compile and packet construction
        # live inside the loop, so the exception family is the honest ruler —
        # WallCompilerError / an evidence-contract raise ⇒ compile, a
        # DecisionLoopError ⇒ the executor, anything else in provider mode
        # ⇒ the model beat (transport / retry exhaustion / guard refusals
        # the beat's own retry budget could not absorb).
        from src.agent.correction.decision_executor import DecisionLoopError
        from src.agent.correction.evidence_contract import EvidenceContractError
        from src.agent.correction.wall_compiler import WallCompilerError

        if isinstance(exc, (WallCompilerError, EvidenceContractError)):
            stage = "compile"
        elif isinstance(exc, DecisionLoopError):
            stage = "loop"
        else:
            stage = "model" if provider is not None else "loop"
        _record_evidence_chain_failure(out_dir, stage, exc)
        raise

    # -- terminus: the outcome + the route record, both as-measured ---------- #
    if out_dir is not None:
        (Path(out_dir) / _EVIDENCE_CHAIN_OUTCOME_NAME).write_text(
            outcome.model_dump_json(indent=2), encoding="utf-8"
        )

    # -- B1 wiring: project a SUCCESSFUL outcome's walls into the envelope -- #
    projection_record: dict | None = None
    if projection is not None:
        if not outcome.success:
            # ⛔ never project the audit-only provisional — the refusal is
            # run_correction's (EvidenceChainTerminal); the route just
            # records that no projection happened, and why.
            projection_record = {
                "requested": True,
                "projected": False,
                "reason": "outcome_success=False",
            }
        else:
            if out_dir is None:
                raise ValueError(
                    "evidence chain projection requires out_dir: the "
                    "projection envelope IS the product (⛔ a bare geometry "
                    "never travels without it)"
                )
            if not final_compilation:
                raise RuntimeError(
                    "evidence chain: the compilation sink never fired "
                    "although the loop returned an outcome"
                )
            try:
                from src.agent.correction.projection_bridge import (
                    cut_lines_from_wall_compilation,
                    opening_spans_from_artifact,
                    project_cut_lines,
                )

                spans = opening_spans_from_artifact(artifact)
                lines, _ = cut_lines_from_wall_compilation(
                    final_compilation[-1].walls, spans
                )
                # W#6 (dispatch 2026-09-08c S-B): take the FOUR exterior
                # edges' axis positions from the drawing's OWN declarations
                # (overall extents + the matched thickness callout) BEFORE
                # the cut — every floor whose declared chains agree then
                # projects onto the IDENTICAL axis frame by construction
                # (assembly's zero-tolerance compare needs no snap), and
                # each floor's ring/cells conservation survives because the
                # partition runs on the SAME snapped lines.  Interior walls
                # keep their ink-side positions (measured: their axes sit
                # 121.6-2358.7 mm off every declared tick — snapping them
                # would displace geometry, not fix representation).
                from src.agent.correction.multifloor import (
                    read_declared_exterior_frame,
                )
                from src.agent.correction.projection_bridge import (
                    snap_exterior_walls_to_declared_frame,
                    preserve_endpoint_connections,
                )

                frame = read_declared_exterior_frame(
                    doc, input_id=Path(product_filename).stem
                )
                before_frame = lines
                lines, frame_records = snap_exterior_walls_to_declared_frame(
                    lines,
                    overall_x_m=frame.overall_x_m,
                    overall_y_m=frame.overall_y_m,
                    thickness_callouts_mm=frame.thickness_callouts_mm,
                    input_id=Path(product_filename).stem,
                )
                lines, endpoint_records = preserve_endpoint_connections(before_frame, lines)
                envelope = project_cut_lines(
                    lines,
                    # N-3, redeclared HERE for the production chain: the
                    # as-drawn *_m fields are floating-point metres with no
                    # declared quantisation — ⛔ the fixture world's 1-unit
                    # number is NOT carried over.  (The source's calibration
                    # does declare m_per_px; absorbing that would blur real
                    # centimetre-scale mismatches, so it is NOT used.)
                    resolution_m=0.0,
                    resolution_source=(
                        "production evidence chain: as-drawn *_m fields, "
                        "floating-point metres, no declared quantisation "
                        "(N-3 redeclared at the wiring)"
                    ),
                    source_resolved_sha256=final_compilation[
                        -1
                    ].content_sha256,
                    floor_id=projection.floor_id or floor_ref,
                    floor_name=projection.floor_name or floor_ref,
                    z_floor_m=projection.z_floor_m,
                    ceiling_height_m=projection.ceiling_height_m,
                    view_id=Path(product_filename).stem,
                    floor_ref=floor_ref,
                    origin_label=Path(product_filename).stem,
                )
                envelope_path = Path(out_dir) / _EVIDENCE_CHAIN_PROJECTION_NAME
                envelope_path.write_text(
                    envelope.model_dump_json(indent=2), encoding="utf-8"
                )
                # W#3: file the FINAL compilation (the chain's model product)
                # alongside the envelope, so the flow wiring can freeze those
                # bytes into the candidate's chain provenance for the
                # writer-side replay.  Canonical bytes — the same body whose
                # ``content_sha256`` the compilation itself carries.
                (Path(out_dir) / _EVIDENCE_CHAIN_COMPILATION_NAME).write_bytes(
                    final_compilation[-1].model_dump_json(indent=2).encode("utf-8")
                )
                # W#6: file the projected cut lines + the exact projection
                # arguments, for the cross-floor reconciliation downstream.
                (Path(out_dir) / _EVIDENCE_CHAIN_CUT_LINES_NAME).write_text(
                    json.dumps(
                        {
                            "schema": "cut_lines_v1",
                            "endpoint_connection_policy": "preserve_endpoint_connections_v1",
                            "endpoint_connections": list(endpoint_records),
                            "lines": [
                                {
                                    "axis": line.axis,
                                    "pos_m": line.pos_m,
                                    "along_lo_m": line.along_lo_m,
                                    "along_hi_m": line.along_hi_m,
                                    "half_thickness_m": line.half_thickness_m,
                                    "kind": line.kind,
                                    "origin_id": line.origin_id,
                                }
                                for line in lines
                            ],
                            "project": {
                                "resolution_m": 0.0,
                                "resolution_source": (
                                    "production evidence chain: as-drawn "
                                    "*_m fields, floating-point metres, no "
                                    "declared quantisation (N-3 redeclared "
                                    "at the wiring)"
                                ),
                                "source_resolved_sha256": final_compilation[
                                    -1
                                ].content_sha256,
                                "floor_id": projection.floor_id or floor_ref,
                                "floor_name": projection.floor_name or floor_ref,
                                "z_floor_m": projection.z_floor_m,
                                "ceiling_height_m": projection.ceiling_height_m,
                                "view_id": Path(product_filename).stem,
                                "floor_ref": floor_ref,
                                "origin_label": Path(product_filename).stem,
                            },
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                projection_record = {
                    "requested": True,
                    "projected": True,
                    "envelope_path": str(envelope_path),
                    "face_count": envelope.face_count,
                    "completion": envelope.completion,
                    "extension_count": envelope.extension_count,
                    "n_dangling_end_debts": len(envelope.dangling_end_debts),
                    "n_opening_spans": len(spans),
                    # W#6: the exterior-frame snap's own account — what each
                    # exterior edge's ink said, what the declaration says,
                    # and the displacement absorbed.
                    "endpoint_connections": list(endpoint_records),
                    "exterior_frame_snaps": [
                        {
                            "axis": r.axis,
                            "side": r.side,
                            "wall_origin_id": r.wall_origin_id,
                            "pix_pos_m": r.pix_pos_m,
                            "declared_pos_m": r.declared_pos_m,
                            "displaced_m": r.displaced_m,
                            "matched_callout_mm": r.matched_callout_mm,
                            "followed_opening_ids": list(
                                r.followed_opening_ids
                            ),
                        }
                        for r in frame_records
                    ],
                }
            except Exception as exc:  # recorded, then re-raised untouched
                _record_evidence_chain_failure(out_dir, "project", exc)
                raise
    route = {
        "route": "evidence_chain",
        "source_file": product_filename,
        # W-1 rework BLK-E: sha256 of the frozen bytes the chain CONSUMED —
        # the reconciliation anchor for any downstream re-reader of the same
        # product (see the snap-tolerance derivation in
        # run_multifloor_correction).  ⚠ deliberately NOT named like the
        # envelope's ``source_resolved_sha256`` (that one is the wall
        # compilation's content hash, a different object).
        "source_bytes_sha256": source_bytes_sha256,
        "contract": decision.contract_id,
        "adapter": adapter_name,
        "profile": profile,
        "round_budget": round_budget,
        "response_source": response_source,
        # v3, B-1: the RESOLVED model seat, read from the section dict the
        # provider actually holds — ⛔ never an echo of the requested name
        # (None in fixed_responses mode: no model was seated at all).
        "llm_section_requested": llm_section_requested,
        "llm_section_resolved": llm_section_resolved,
        "llm_model_resolved": llm_model_resolved,
        "outcome_success": outcome.success,
        "exit_reason": outcome.exit_reason,
        "outcome_path": (
            str(Path(out_dir) / _EVIDENCE_CHAIN_OUTCOME_NAME)
            if out_dir is not None
            else None
        ),
        # B1 wiring: as-measured projection readout (None = not requested)
        "projection": projection_record,
    }
    route_path = _evidence_chain_run_meta(out_dir, _EVIDENCE_CHAIN_ROUTE_NAME)
    if route_path is not None:
        try:
            route_path.write_text(
                json.dumps(route, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(
                "evidence chain: could not file the route record under {}: {}",
                route_path.parent,
                type(exc).__name__,
                exc,
            )
    return outcome


def run_opening_adjudication(geometry, *, review, expected_result_id: str,
                             out_dir: Path, profile: str = "strict"):
    """Assemble CURRENT reviewed openings and persist their complete byte chain.

    This is also the final opening link of run_correction's evidence route.
    Historical JSON is audit data, not current-session authority. Only a strict
    result can leave this entry for the judge.
    """
    from src.agent.correction.opening_adjudication import OpeningReview
    from src.agent.correction.tick_claim import TickClaimError
    if type(review) is not OpeningReview:
        raise TickClaimError("CURRENT_OPENING_REVIEW_REQUIRED")
    if profile != "strict":
        raise TickClaimError("OPENING_JUDGE_STRICT_REQUIRED")
    # assemble_geometry calls consume/scoreable_openings, including each live
    # TickSession's independent expected batch ID, before adding any WindowV3.
    assembled = review.assemble_geometry(geometry, expected_result_id)
    archive = Path(out_dir) / "opening_batches" / expected_result_id
    review.persist(archive, expected_result_id)
    (Path(out_dir) / "opening_geometry.json").write_text(
        assembled.model_dump_json(indent=2), encoding="utf-8")
    return assembled


def run_correction(
    vector_dir: Path,
    testdata_text: str,
    *,
    out_dir: Path | None = None,
    feedback: str | None = None,
    draw_validate: "Callable[[dict], None] | None | object" = _UNSET_VALIDATOR,
    run_profile: RunProfile = "exploratory",
    capability_profile: str = "rectangular",
    evidence_debt: EvidenceDebt | None = None,
    dimensioned_views: set[str] | None = None,
    fail_closed_evidence_debt: bool = False,
    target=None,
    observation_reference_catalog: str | None = None,
    evidence_chain: bool = False,
    evidence_chain_product: str | None = None,
    evidence_chain_profile: str = "exploratory",
    evidence_chain_round_budget: int = 3,
    evidence_chain_fixed_responses: "Sequence[CorrectionDecisionResponseV1] | None" = None,
    evidence_chain_level: "object | None" = None,
    opening_review=None,
    expected_opening_result_id: str | None = None,
) -> CorrectedGeometry:
    """1_correction LLM stage → CorrectedGeometry (pre-core).

    ``draw_validate`` is the inner-retry validator (a failure re-issues the LLM call
    up to ``attempts`` times). Default = the full ``_make_correction_validator``
    (legacy ``run_pipeline`` path: schema + semantic). Pass
    ``_schema_only_correction_validator`` (stepwise mode) so the inner retry handles
    only schema/format, leaving semantic draw quality to gate① (so a content-bad
    draw is counted + filed as an attempt, not silently re-drawn). Pass ``None`` to
    disable the inner validator entirely.

    ⭐ Module 7 (2026-09-02): ``evidence_chain=True`` switches this stage onto
    the evidence chain (frozen bytes → adapter → compiler → decision packet →
    model beat → decision loop).  The switch is EXPLICIT and one-way: the
    pasted-JSON prompt below is not built at all in that mode.

    ⭐ B1 wiring (2026-09-03): a SUCCESSFUL chain outcome is projected by the
    projection bridge into the returned ``CorrectedGeometry`` (subclass
    ``CorrectedGeometryV3``), with the whole product — ``footprint_provenance=
    "derived_from_walls"`` and the binding hash — filed as
    ``projection_envelope.json`` under ``out_dir`` (both required in this
    mode).  ⭐ B2 rework-2 (2026-09-04g): the storey z is supplied by
    ``evidence_chain_level`` — a byte-validated ``_DerivedFloorLevel`` minted by
    ``multifloor.derive_floor_ladder`` over the SEALED evidence carrier — ⛔ NO
    LONGER two bare ``float`` params a caller could hand-fill.  The bridge still
    never mints a z (B2 owns sourcing); the difference is that the only z it can
    now receive is byte-resolved and gate-validated.  When the loop does NOT
    succeed there is still no product:
    ``EvidenceChainTerminal`` fires — the final provisional is audit-only
    and ⛔ must never travel as one.  A successful but ``degraded`` envelope
    is returned only for the exploratory evidence-chain profile; the strict
    profile refuses it here, before geometry can reach the judge."""
    from src.agent.correction.parse import correction_target, parse_correction_draw
    ensure_schema_initialized()  # safe for standalone stage calls (idempotent)
    target = target or correction_target(capability_profile)
    if (opening_review is None) != (expected_opening_result_id is None):
        raise ValueError("opening review and expected result ID must travel together")
    if opening_review is not None and not evidence_chain:
        raise ValueError("opening review requires the evidence-chain route")
    if evidence_chain:
        if evidence_chain_product is None:
            raise ValueError(
                "evidence_chain=True needs evidence_chain_product: the ONE "
                "frozen reading product this chain run consumes (module 7 "
                "wires one source per chain run)"
            )
        # ⭐ B2 rework-2 (2026-09-04g): the raw-z production face is MIGRATED —
        # this stage no longer accepts a bare hand-filled z.  The only z-bearing
        # input is a byte-validated _DerivedFloorLevel minted by
        # derive_floor_ladder over the SEALED carrier (which ran the frozen-byte
        # gate).  A bare float, or any other object, is refused here.
        from src.agent.correction.multifloor import _DerivedFloorLevel
        if evidence_chain_level is None:
            raise ValueError(
                "evidence_chain=True needs evidence_chain_level: a byte-"
                "validated storey level from multifloor.derive_floor_ladder over "
                "the sealed evidence carrier (design §四 — B2 owns sourcing floor "
                "elevation; the projection bridge never mints a z). A bare "
                "hand-filled z is no longer accepted."
            )
        if not isinstance(evidence_chain_level, _DerivedFloorLevel):
            raise TypeError(
                "evidence_chain_level must be a _DerivedFloorLevel minted by "
                "multifloor.derive_floor_ladder (the frozen-byte-validated "
                "carrier); a hand-built z-bearing object is refused (B2 rework-2)"
            )
        if out_dir is None:
            raise ValueError(
                "evidence_chain=True needs out_dir: the projection envelope "
                "IS the chain's product (⛔ the bare geometry never travels "
                "without it)"
            )
        outcome = run_correction_evidence_chain(
            vector_dir,
            evidence_chain_product,
            out_dir=out_dir,
            profile=evidence_chain_profile,
            round_budget=evidence_chain_round_budget,
            fixed_responses=evidence_chain_fixed_responses,
            projection=EvidenceChainProjection(
                # ⭐ byte-resolved from the frozen ladder, ⛔ never hand-filled
                z_floor_m=evidence_chain_level.z_floor_m,
                ceiling_height_m=evidence_chain_level.ceiling_height_m,
            ),
        )
        outcome_path = Path(out_dir) / _EVIDENCE_CHAIN_OUTCOME_NAME
        if not outcome.success:
            # The one surviving terminus: the loop did NOT succeed, so
            # there is no product to hand back — the final provisional is
            # audit-only (success=False says so on the same outcome) and
            # ⛔ must never travel as, or be projected into, a finished
            # CorrectedGeometry.
            raise EvidenceChainTerminal(
                "1_correction evidence chain terminated WITHOUT a product: "
                f"DecisionLoopOutcomeV1 on disk at {outcome_path} says "
                f"success={outcome.success}, exit_reason="
                f"{outcome.exit_reason!r}. The final provisional travels "
                "for audit only and is not projected."
            )
        from src.agent.correction.projection_bridge import (
            CorrectedGeometryProjectionEnvelopeV1,
        )

        envelope = CorrectedGeometryProjectionEnvelopeV1.model_validate_json(
            (Path(out_dir) / _EVIDENCE_CHAIN_PROJECTION_NAME).read_text(
                encoding="utf-8"
            )
        )
        # consumer-side binding check (design §四): hash mismatch = a
        # projection failure, never a shrug
        if envelope.source_resolved_sha256 != outcome.final_provisional_sha256:
            raise RuntimeError(
                "evidence chain projection envelope does not bind the "
                "outcome's final provisional: envelope has "
                f"{envelope.source_resolved_sha256}, outcome has "
                f"{outcome.final_provisional_sha256} — treat as a "
                "projection failure"
            )
        # W#3: reconcile the FILED final compilation against the same binding
        # — its canonical content hash must equal the outcome's final
        # provisional hash, so the bytes the flow wiring will freeze into the
        # candidate's chain provenance are exactly the bytes the loop settled
        # on (and the writer replay will re-verify the same equation again).
        from src.agent.correction.wall_compiler import WallCompilationV1
        from src.agent.correction.window_sources import canonical_sha256

        filed_compilation = WallCompilationV1.model_validate_json(
            (Path(out_dir) / _EVIDENCE_CHAIN_COMPILATION_NAME).read_bytes()
        )
        _content = filed_compilation.model_dump(mode="python")
        _declared = _content.pop("content_sha256", None)
        if (
            canonical_sha256(_content) != _declared
            or _declared != outcome.final_provisional_sha256
        ):
            raise RuntimeError(
                "evidence chain compilation does not bind the outcome's "
                "final provisional: filed compilation has "
                f"{_declared}, outcome has "
                f"{outcome.final_provisional_sha256} — treat as a "
                "projection failure"
            )
        # Design §四 hard rule, enforced at B1's consumer boundary: this is
        # the last point where the envelope and the evidence-chain profile
        # are both available before bare geometry could travel to the judge.
        # Exploratory runs may inspect degraded geometry, but strict runs
        # must stop with the filed envelope still available for audit.
        if (
            evidence_chain_profile == "strict"
            and envelope.completion == "degraded"
        ):
            raise RuntimeError(
                "strict evidence-chain profile refuses a degraded "
                "projection before the judge: envelope on disk at "
                f"{Path(out_dir) / _EVIDENCE_CHAIN_PROJECTION_NAME} has "
                f"{len(envelope.dangling_end_debts)} dangling-end debt(s)"
            )
        if opening_review is not None:
            return run_opening_adjudication(
                envelope.geometry, review=opening_review, expected_result_id=expected_opening_result_id,
                out_dir=out_dir, profile=evidence_chain_profile)
        return envelope.geometry
    # F-97 (F-c, B-03): classify and FILE THE LEDGER FIRST -- before the reading
    # evidence preflight below, which parses `*_view.json` and dies on a
    # non-object with a bare `AttributeError: 'list' object has no attribute
    # 'get'`. Filed later, a run that failed classification left neither F-97's
    # named exception nor F-c's promised ledger. `_write_vector_contract_ledger`
    # never raises; `_build_correction_messages` still owns the loud failure.
    _preflight_vector_contracts(vector_dir, out_dir)
    if evidence_debt is None:
        from src.agent.execution.case_metadata import (
            dimensioned_states_from_data,
            parse_testdata_text,
        )
        # R1-3 (派工单 §1.3): parse the structured 4-state declaration from
        # testdata (declared_true/declared_false survive). A caller-provided
        # legacy bool set is promoted to declared_true for pre-R1-3 parity.
        if dimensioned_views:
            dim_states = {stem: "declared_true" for stem in dimensioned_views}
        else:
            dim_states = dimensioned_states_from_data(parse_testdata_text(testdata_text) or {})
        evidence_debt = compute_evidence_debt_from_vector_dir(
            vector_dir,
            run_profile=run_profile,
            capability_profile=capability_profile,
            dimensioned_states=dim_states,
        )
    if out_dir is not None:
        write_evidence_debt(out_dir / "evidence_debt.json", evidence_debt)
    if fail_closed_evidence_debt and evidence_debt.blocking:
        checks = ", ".join(item.check_id for item in evidence_debt.blocking)
        raise RuntimeError(
            "1_correction preflight blocked by reading evidence debt "
            f"under run_profile={run_profile}: {checks}"
        )
    system_prompt, human = _build_correction_messages(
        vector_dir, testdata_text, feedback=feedback, evidence_debt=evidence_debt, target=target,
        observation_reference_catalog=observation_reference_catalog,
    )
    validator = (
        _make_correction_validator(_reading_window_stroke_count(vector_dir), target)
        if draw_validate is _UNSET_VALIDATOR
        else None if draw_validate is None
        else (lambda parsed: draw_validate(parsed, target))
    )
    parsed = _call_json_llm(
        _section("correction"),
        system_prompt,
        human,
        out_dir=out_dir,
        prefix="correction",
        attempts=3,
        validate=validator,
        retry_guidance=retry_guidance_for_correction(target),
    )
    try:
        geom = parse_correction_draw(parsed, target)
    except Exception as e:
        if out_dir is not None:
            (out_dir / "correction_geometry_unvalidated.json").write_text(
                json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        raise RuntimeError(f"1_correction: CorrectedGeometry validation error: {e}") from e
    if out_dir is not None:
        (out_dir / "correction_geometry.json").write_text(
            geom.model_dump_json(indent=2), encoding="utf-8"
        )
    return geom


# --------------------------------------------------------------------------- #
# B2 multi-floor assembly (2026-09-03): derive the storey ladder from the
# frozen elevation evidence and stack one evidence-chain projection per plan
# product into a single multi-floor CorrectedGeometryV3.
#
# ⭐ This is the wiring the dispatch asks for: the storey z that used to be a
# hand-filled parameter is now DERIVED from B3's ``floor_level_claims`` and fed
# in as a byte-validated level — and THIS entry point exposes no z of its own,
# so the production multi-floor path cannot hand-fill a z (dispatch T5).
#
# ⭐ B-1/B-2 rework-2 (2026-09-04g): the entry consumes the SEALED evidence
# carrier (``CorrectionEvidenceBundleArtifactV1`` = bundle + frozen bytes) via
# ``derive_floor_ladder``, which runs B3's value↔byte gate
# (``validate_evidence_bundle``) as its FIRST act, then mints a SEALED
# ``ValidatedFloorLadder`` whose per-storey z is RESOLVED FROM THE FROZEN BYTES.
# A claim whose ``z_m`` drifted from the frozen byte its ``z_ref`` names is a
# named ``FLOOR_LEVEL_VALUE_DRIFTED_FROM_SOURCE`` red before any per-floor chain
# runs — the SAME B3 gate, reused, ⛔ not a second copy.  The per-floor
# ``run_correction`` call now receives that byte-validated level (not two bare z
# floats), and assembly accepts only the sealed ladder — the hand-fill z path
# does not exist at the type layer.
# --------------------------------------------------------------------------- #
class MultiFloorPlanRun(NamedTuple):
    """One storey's plan-side inputs for :func:`run_multifloor_correction`.

    ⛔ Carries NO z: the storey elevation is DERIVED from the elevation ladder
    (B2/T5), never declared here.  The list order is the storey order,
    ground-up: ``plan_runs[i]`` is projected onto derived rung ``i``.
    ``fixed_responses`` is the sanctioned model-free escape hatch (as in the
    single-view chain); production leaves it None to seat the real model beat.
    """

    vector_dir: Path
    product_filename: str
    out_dir: Path
    fixed_responses: "Sequence[CorrectionDecisionResponseV1] | None" = None
    profile: str = "exploratory"
    round_budget: int = 3


def _as_drawn_plan_window_openings(
    plan_runs: "Sequence[MultiFloorPlanRun]",
) -> int:
    """这条腿的平面产物**自己声明**的 window 洞口数（链条跑之前就可测）。

    这是 channel-split 债的退休条件（2026-09-08h）：> 0 ⇒ 窗证据有自己的
    台账载体（平面 opening 目录 + claim links），「on chain NOT on ledger」
    不成立。只数 ``hypotheses.opening_types`` 判为 window 的候选 —— 与
    ``window_sources._as_drawn_plan_rows`` 进目录的判据同源。产品缺文件/
    解析失败按 0 计（保守：债照落，链条稍后自会对坏输入响亮拒绝）。
    """
    total = 0
    for run in plan_runs:
        try:
            doc = json.loads(
                (Path(run.vector_dir) / run.product_filename).read_text("utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        hypotheses = doc.get("hypotheses") or {}
        types = hypotheses.get("opening_types") or {}
        candidates = {c.get("id") for c in hypotheses.get("opening_candidates") or []}
        total += sum(1 for cid in candidates if types.get(cid) == "window")
    return total


def _unified_chain_profile(
    plan_runs: "Sequence[MultiFloorPlanRun]",
) -> str:
    """The ONE evidence-chain profile all ``plan_runs`` agree on.

    A mixed-profile ``plan_runs`` (one floor exploratory, another strict) is a
    loud ``PLAN_RUN_PROFILE_MIXED`` refusal: the per-floor chains would run
    under different debt policies and no single filed account could describe
    the leg.  An empty ``plan_runs`` yields the axis default (the ladder-count
    gate owns that refusal).
    """
    from src.agent.correction.multifloor import MultiFloorAssemblyError

    profiles = sorted({run.profile for run in plan_runs})
    if len(profiles) > 1:
        raise MultiFloorAssemblyError(
            "PLAN_RUN_PROFILE_MIXED", {"profiles": profiles}
        )
    return profiles[0] if profiles else "exploratory"


def run_multifloor_correction(
    elevation_evidence: "CorrectionEvidenceBundleArtifactV1",
    plan_runs: "Sequence[MultiFloorPlanRun]",
    *,
    snap_ledger_path: "Path | None" = None,
    evidence_debt_path: "Path | None" = None,
) -> "CorrectedGeometryV3":
    """B2 wiring: derive the storey ladder from the frozen elevation evidence,
    run the 1_correction evidence chain once per plan product with the DERIVED
    z, and assemble the results into one multi-floor ``CorrectedGeometryV3``.

    ⭐ B-1/B-2 (rework-2 2026-09-04g): the ONLY input carrying the storey levels
    is the SEALED carrier ``elevation_evidence``
    (``CorrectionEvidenceBundleArtifactV1`` = the bundle plus its frozen bytes),
    handed to ``derive_floor_ladder``, whose FIRST act is B3's value↔byte gate
    (``validate_evidence_bundle``): a claim whose ``z_m`` drifted from the frozen
    byte its ``z_ref`` names is a named ``FLOOR_LEVEL_VALUE_DRIFTED_FROM_SOURCE``
    red BEFORE any per-floor chain runs — the same B3 gate, reused, ⛔ never a
    second copy.  It returns a SEALED ``ValidatedFloorLadder`` whose per-storey z
    is RESOLVED FROM THE FROZEN BYTES.

    ⭐ z sourcing (T1/T5): the ONLY source of each storey's z is that byte-
    validated ladder.  Each derived level is fed to ``run_correction`` via
    ``evidence_chain_level`` (a ``_DerivedFloorLevel`` — ⛔ not two bare z
    floats); this function exposes NO z parameter, so the hand-fill path is
    unreachable from here.  Neuter the derivation and this call fails loudly
    (``FLOOR_PLAN_COUNT_MISMATCH``) — ⛔ it never falls back to a caller-declared
    z.

    ⭐ W-1 rework BLK-A (2026-09-07l): walking this leg FILES the window
    channel-split debt (``WINDOW_EVIDENCE_ON_CHAIN_NOT_ON_LEDGER``) — the
    acceptance premise of T2-⑤ is a fact on disk, ⛔ not a docstring.  It is
    registered BEFORE any chain runs (a strict refusal must cost no chain
    budget) and filed BEFORE the strict refusal raises, so the block itself
    is auditable.  ``evidence_debt_path`` (production wiring always passes
    it) is where the ledger lands — the SAME filename the legacy leg files,
    so downstream readers (flow, report assembly, validation) see one shape.

    ``plan_runs`` is ground-up: ``plan_runs[i]`` is projected onto derived rung
    ``i``.  A plan-product count that disagrees with the derived storey count is
    a loud ``FLOOR_PLAN_COUNT_MISMATCH`` (T4), raised BEFORE any chain runs.

    ⭐ W-1 T2-④ (2026-09-07, verdict 2026-09-07i BLK-1): between the chains and
    assembly, the per-floor footprints are snapped to the ground floor's ring
    through ``multifloor.snap_footprints_to_reference`` — a tolerance FULLY
    derived from the plan products' own calibration/thickness declarations
    (⛔ zero invented constants; see that function for the derivation).  An
    over-tolerance floor passes through UNTOUCHED so assembly's existing
    ``PER_FLOOR_FOOTPRINT_MISMATCH`` fires unchanged.  ``snap_ledger_path``
    (production wiring always passes it) is where the account lands — written
    UNCONDITIONALLY, ``applied`` true or false, so the ledger is a positive
    record, ⛔ not an absence that conflates "no snap needed" with "the snap
    never ran".
    """
    from src.agent.correction.multifloor import (
        MultiFloorAssemblyError,
        assemble_multifloor_geometry,
        derive_floor_ladder,
        read_plan_calibration_declaration,
        snap_footprints_to_reference,
    )

    # W-1 rework BLK-A (2026-09-07l): the window-evidence channel split is a
    # FILED fact on this leg, ⛔ never a silent emptiness.  Filed FIRST (before
    # the ladder and every chain) so the debt exists on disk whatever happens
    # next; a mixed-profile plan_runs is a loud refusal here — no single
    # account could describe a leg whose floors ran under different policies.
    #
    # ⭐ 2026-09-08h 退休（实测条件化，⛔ 不是删机制）：补窗落地后，这条腿的
    # 窗证据真走上了台账（平面 opening 目录 + claim links，且有
    # `window_position_evidence_shadow` 这道有牙的独立门在行使）⇒ 债的标题
    # 「evidence on chain NOT on ledger」不再为真，**不该再落**。退休条件
    # = 平面产物自己声明的 window 洞口数 > 0（在链条跑之前就可测）；条件
    # 不满足（平面零 window 洞）时 T2-⑤ 的原状成立，照旧落债 + strict 拒绝。
    # 退休本身也要【记账 ⛔ 不是静默】：写一份**空债账**（debts=[]），让
    # 下游能区分「测过、不欠」与「根本没跑」。
    chain_profile = _unified_chain_profile(plan_runs)
    plan_window_openings = _as_drawn_plan_window_openings(plan_runs)
    if plan_window_openings > 0:
        if evidence_debt_path is not None:
            write_evidence_debt(
                Path(evidence_debt_path),
                EvidenceDebt(
                    run_profile=(
                        "exploratory" if chain_profile == "exploratory"
                        else "regression"
                    ),
                    source_stage="1_correction",
                    debts=[],
                ),
            )
    else:
        channel_split_debt = window_evidence_channel_split_debt(
            chain_profile=chain_profile
        )
        if evidence_debt_path is not None:
            write_evidence_debt(Path(evidence_debt_path), channel_split_debt)
        if channel_split_debt.blocking:
            raise MultiFloorAssemblyError(
                WINDOW_EVIDENCE_CHANNEL_SPLIT_DEBT_ID,
                {
                    "evidence_chain_profile": chain_profile,
                    "filed_at": (
                        str(evidence_debt_path)
                        if evidence_debt_path is not None
                        else None
                    ),
                    "reason": "strict evidence-chain profile blocks the window "
                    "channel split (legacy ledger empty by ratified T2-⑤); the "
                    "debt is filed and the wiring fails closed",
                },
            )

    # B-1/B-2: derive_floor_ladder consumes the SEALED carrier and runs B3's
    # value↔byte gate (validate_evidence_bundle) as its FIRST act, BEFORE any
    # storey is minted or any per-floor chain runs.  A drifted z_m (ref kept,
    # value changed) is a named EvidenceContractError here, ⛔ not a silent
    # accept.  The returned ValidatedFloorLadder is the sealed carrier — the only
    # thing assemble_multifloor_geometry accepts.
    ladder = derive_floor_ladder(elevation_evidence)
    if len(plan_runs) != len(ladder):
        raise MultiFloorAssemblyError(
            "FLOOR_PLAN_COUNT_MISMATCH",
            {
                "n_storeys_from_ladder": len(ladder),
                "n_plan_products": len(plan_runs),
            },
        )
    geometries: list = []
    for level, run in zip(ladder, plan_runs):
        geom = run_correction(
            run.vector_dir,
            "{}",
            out_dir=run.out_dir,
            evidence_chain=True,
            evidence_chain_product=run.product_filename,
            evidence_chain_fixed_responses=run.fixed_responses,
            evidence_chain_profile=run.profile,
            evidence_chain_round_budget=run.round_budget,
            # ⭐ the byte-validated DERIVED level, ⛔ never a hand-filled z: the
            # migrated run_correction face accepts only this carrier.
            evidence_chain_level=level,
        )
        geometries.append(geom)
    # W-1 T2-④ + rework BLK-E (2026-09-07l): the snap tolerance is derived
    # from the SAME plan products the chains just consumed (each one's OWN
    # declared calibration residuals and wall-thickness callouts), re-read
    # here read-only.  ⭐ The re-read is AUTHORIZED per floor by the chain's
    # own filed record (``chain_source_record.json`` carries the sha256 of
    # the bytes that chain froze): a byte that does not match the record the
    # chain left is a named ``PLAN_PRODUCT_BYTES_DRIFTED_FROM_CHAIN`` red,
    # ⛔ never an unverified derivation source.  (Why a hash-reconciled
    # re-read and not a chain-returned doc: run_correction's return type is
    # the geometry — threading a second return channel through it would
    # touch every caller, and the envelope/outcome schemas are versioned
    # contracts; the per-floor sidecar adds the anchor with ZERO contract
    # churn.  ⚠ note the envelope's ``source_resolved_sha256`` is the WALL
    # COMPILATION's content hash, a different object — it cannot anchor
    # product bytes.)
    plan_docs: list = []
    for run in plan_runs:
        record_path = Path(run.out_dir) / _EVIDENCE_CHAIN_SOURCE_RECORD_NAME
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MultiFloorAssemblyError(
                "CHAIN_SOURCE_RECORD_MISSING",
                {
                    "run": run.product_filename,
                    "record_path": str(record_path),
                    "reason": "the per-floor chain run must file the record of "
                    "the bytes it consumed (wiring rework BLK-E)",
                },
            ) from exc
        try:
            raw = (Path(run.vector_dir) / run.product_filename).read_bytes()
        except OSError as exc:
            raise MultiFloorAssemblyError(
                "PLAN_PRODUCT_BYTES_DRIFTED_FROM_CHAIN",
                {
                    "run": run.product_filename,
                    "reason": "product file unreadable at reconciliation time",
                },
            ) from exc
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != record.get("source_bytes_sha256"):
            raise MultiFloorAssemblyError(
                "PLAN_PRODUCT_BYTES_DRIFTED_FROM_CHAIN",
                {
                    "run": run.product_filename,
                    "declared_by_chain": record.get("source_bytes_sha256"),
                    "reconciled_read": actual_sha,
                    "reason": "the snap tolerance must derive from the bytes "
                    "the chain actually consumed — these are not them",
                },
            )
        plan_docs.append(json.loads(raw.decode("utf-8")))
    declarations = [
        read_plan_calibration_declaration(
            doc, input_id=Path(run.product_filename).stem
        )
        for doc, run in zip(plan_docs, plan_runs)
    ]
    # W#6: reconcile ON THE CUT LINES and re-partition, ⛔ never the verbatim
    # ring swap (which left the upper floor's cells from its own partition
    # and broke its coverage conservation by 0.3806 m² on sm25).  The lines
    # and the exact projection arguments are the chain's own filed sidecar,
    # hash-anchored to the same compilation the envelope binds to below.
    from src.agent.correction.multifloor import (
        cut_lines_from_sidecar,
        reconcile_floors_to_reference,
    )

    per_floor_cut_lines: list = []
    per_floor_project: list = []
    for run, geom in zip(plan_runs, geometries):
        sidecar_path = Path(run.out_dir) / _EVIDENCE_CHAIN_CUT_LINES_NAME
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MultiFloorAssemblyError(
                "CUT_LINES_SIDECAR_MISSING",
                {
                    "run": run.product_filename,
                    "sidecar_path": str(sidecar_path),
                    "reason": "the cross-floor reconciliation consumes the "
                    "chain's own filed cut lines (W#6)",
                },
            ) from exc
        if sidecar.get("schema") != "cut_lines_v1":
            raise MultiFloorAssemblyError(
                "CUT_LINES_SIDECAR_SCHEMA_UNKNOWN",
                {"run": run.product_filename, "schema": sidecar.get("schema")},
            )
        filed_compilation = json.loads(
            (
                Path(run.out_dir) / _EVIDENCE_CHAIN_COMPILATION_NAME
            ).read_text(encoding="utf-8")
        )
        if (
            sidecar["project"].get("source_resolved_sha256")
            != filed_compilation.get("content_sha256")
        ):
            raise MultiFloorAssemblyError(
                "CUT_LINES_SIDECAR_UNBOUND",
                {
                    "run": run.product_filename,
                    "reason": "the sidecar's projection anchor is not the "
                    "chain's filed final compilation",
                },
            )
        per_floor_cut_lines.append(cut_lines_from_sidecar(sidecar))
        per_floor_project.append(sidecar["project"])
    geometries, snap_account = reconcile_floors_to_reference(
        per_floor_cut_lines, per_floor_project, declarations, preserve_connections=True
    )
    if snap_ledger_path is not None:
        snap_path = Path(snap_ledger_path)
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(snap_account.to_payload(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return assemble_multifloor_geometry(ladder, geometries)


# --------------------------------------------------------------------------- #
# 4_mep — physical-information authoring (LLM): non-geometry specs only
# --------------------------------------------------------------------------- #
def _build_mep_messages(
    zone_specs: str,
    used_constructions: set[str],
    testdata_text: str,
    *,
    feedback: str | None = None,
):
    from src.agent.intakeoutput import MepOutput

    authoring = _read(INTAKE_RESOURCES / "mep" / "authoring.md")
    mep = _read(INTAKE_RESOURCES / "mep" / "mep.md")
    mep_schema = json.dumps(
        MepOutput.model_json_schema(), indent=2, ensure_ascii=False
    )
    cons_list = "\n".join(f"- {c}" for c in sorted(used_constructions))

    system_prompt = (
        "You are running the 4_MEP stage of a staged EnergyPlus intake pipeline. "
        "The geometry is ALREADY built and serialized deterministically — you "
        "author ONLY the non-geometry specs and attach them by name. Do NOT "
        "produce zone_specs / surface_specs / fenestration_specs.\n\n"
        "hvac_specs is generated deterministically by CODE from the zone list, "
        "not by you (2026-08-14: a hand-authored HVAC section is where the "
        "model has historically misplaced an IDD field and silently shifted "
        "every field after it — the fix is that this section is no longer "
        "hand-authored). Set hvac_specs to an empty string \"\" in your output. "
        "Do NOT author ZoneControl:Thermostat / ThermostatSetpoint:* / "
        "ZoneHVAC:IdealLoadsAirSystem / HVACTemplate:* objects — anything you "
        "write there is discarded. Also do NOT define a thermostat "
        "heating/cooling setpoint schedule or an HVAC availability schedule in "
        "schedule_specs — code owns and injects those 3 schedules "
        "(Sch_HVACDet_HeatingSetpoint / Sch_HVACDet_CoolingSetpoint / "
        "Sch_HVACDet_Availability); defining your own is harmless but wasted "
        "effort since nothing will reference it.\n\n"
        "Output a single MepOutput JSON object with exactly these keys: building, "
        "site_location, material_specs, construction_specs, schedule_specs, "
        "hvac_specs, people_specs, lights_specs.\n\n"
        "OUTPUT FORMAT (strict): ONLY the MepOutput JSON object, `{`..`}`, no "
        "markdown, no prose.\n\n"
        "===== BEGIN MepOutput JSON SCHEMA =====\n"
        f"{mep_schema}\n"
        "===== END MepOutput JSON SCHEMA =====\n\n"
        "===== BEGIN RULE DOCUMENT: 4_mep/authoring.md =====\n"
        f"{authoring}\n"
        "===== END RULE DOCUMENT: 4_mep/authoring.md =====\n\n"
        "===== BEGIN REFERENCE: 4_mep/mep.md (default values) =====\n"
        f"{mep}\n"
        "===== END REFERENCE: 4_mep/mep.md =====\n"
    )

    chunks = [
        "Project metadata (testdata_prompt.json):\n```json\n" + testdata_text + "\n```\n",
        "\nZONE LIST (author per-zone people/lights against these exact "
        "names; do not re-author geometry; hvac is generated by code, not "
        "from this list):\n```\n" + zone_specs + "\n```\n",
        "\nREQUIRED CONSTRUCTIONS — the geometry references these; you MUST define "
        "every one in construction_specs (with its materials in material_specs):\n"
        + cons_list + "\n",
    ]
    if feedback:
        chunks.append(
            "\n\n=== Feedback from a previous attempt — fix these ===\n"
            f"{feedback}\n"
        )
    chunks.append(
        "\nProduce the MepOutput JSON now. Define every required construction, "
        "keep schedule_specs complete (people number-of-people + activity-level "
        "+ lights schedules — NOT hvac setpoint/availability schedules, those "
        "are code-owned), set hvac_specs to \"\", and follow the naming rules. "
        "Output ONLY the JSON object."
    )
    return system_prompt, "".join(chunks)


# --------------------------------------------------------------------------- #
# 4_mep — deterministic hvac_specs render (2026-08-14 dispatch)
#
# The model no longer authors hvac_specs. F-28 (08-13 accept_C, quarantined at
# retry budget): the LLM wrote ZoneControl:Thermostat with 4 fields instead of
# the required 5 (Control 1 Name missing) — every EnergyPlus \required-field
# after the gap silently shifted one slot, and mep.hvac_schedule_refs (which
# reads IDD-fixed field positions) read a bogus schedule name out of what was
# actually the object-type token of the *next* object. Same defect: left
# blank it passes (blank_reference_policy: pass), filled-with-something it
# blocks the gate and burns the run's resample budget — reliability, not
# silent physics error (the *_specs text is never pasted into the final IDF;
# downstream's hvac_agent only ever creates HVACTemplate:Thermostat /
# HVACTemplate:Zone:IdealLoadsAirSystem via named-parameter MCP tools, so a
# field shift here could never reach EnergyPlus as a wrong number — it can
# only ever reach the gate as an unresolvable name and block the run).
# Rendering these two object types in code makes the defect structurally
# impossible: there is no field position for the model to get wrong in a
# section it never writes.
_HVAC_DET_THERMOSTAT_NAME = "HVACDet_Thermostat"
_HVAC_DET_HEATING_SCHEDULE = "Sch_HVACDet_HeatingSetpoint"
_HVAC_DET_COOLING_SCHEDULE = "Sch_HVACDet_CoolingSetpoint"
_HVAC_DET_AVAILABILITY_SCHEDULE = "Sch_HVACDet_Availability"
# The names code EXCLUSIVELY owns (2026-08-14 terra cross-review, A's
# blocking finding): any Schedule:Compact the model authors under one of
# these — including a bare-case variant, since EnergyPlus name lookups are
# case-insensitive — is excluded before the code's own canonical definition
# is appended. See _merge_hvac_det_schedules.
_HVAC_DET_RESERVED_SCHEDULE_NAMES = (
    _HVAC_DET_HEATING_SCHEDULE,
    _HVAC_DET_COOLING_SCHEDULE,
    _HVAC_DET_AVAILABILITY_SCHEDULE,
)
# mep.md Office prior: "cooling setpoint 24 C / heating setpoint 20 C".
_HVAC_DET_HEATING_SETPOINT_C = 20.0
_HVAC_DET_COOLING_SETPOINT_C = 24.0
# ScheduleTypeLimits the 3 reserved schedules need.
#
# 2026-08-14 terra 2nd re-review (A's blocking finding on the 2nd pass): the
# first fix kept these bound to the GENERIC names Temperature / OnOff
# ("only add if the model hasn't already defined that name") specifically
# to avoid tampering with unrelated model schedules that might share those
# names — a correct risk call on its own. But it left the 3 "code-owned"
# canonical schedules referencing two names the MODEL still fully
# controls: a model response can narrow Temperature to e.g. `30, 40`
# (excluding the code's own 20/24 values) or spell it `temperature`
# (lower-case; EnergyPlus names are case-insensitive but this repo's
# mep.schedule_type_refs check is a case-SENSITIVE set comparison, so the
# merge's case-insensitive "don't add if already present" logic then
# leaves the canonical schedules referencing an "undefined" type limit) —
# and check_mep does not catch either shape (terra reproduced both; see
# 2026-08-14_seatA_rework_rereview_terra.md).
#
# Fixed by giving the 3 reserved schedules their OWN exclusive type-limit
# names instead — not generic categories, so nothing legitimately model-
# authored can ever collide with them, and they get EXACTLY the same
# code-ownership treatment as the 3 reserved Schedule:Compact names
# (case-insensitive exclusion of any same-named model definition, then the
# code's own canonical definition is appended unconditionally). The
# model's own generic Temperature / OnOff (if it defines them, e.g. for a
# people/lights schedule that needs one) are now completely untouched by
# this module — there is no name for them to collide with.
_HVAC_DET_TEMPERATURE_TYPE_LIMIT = "HVACDet_Temperature"
_HVAC_DET_ONOFF_TYPE_LIMIT = "HVACDet_OnOff"
_HVAC_DET_RESERVED_TYPE_LIMIT_NAMES = (
    _HVAC_DET_TEMPERATURE_TYPE_LIMIT,
    _HVAC_DET_ONOFF_TYPE_LIMIT,
)
_HVAC_DET_TYPE_LIMITS = {
    _HVAC_DET_TEMPERATURE_TYPE_LIMIT: (
        f"ScheduleTypeLimits, {_HVAC_DET_TEMPERATURE_TYPE_LIMIT}, -60, 200, Continuous;"
    ),
    _HVAC_DET_ONOFF_TYPE_LIMIT: (
        f"ScheduleTypeLimits, {_HVAC_DET_ONOFF_TYPE_LIMIT}, 0, 1, Discrete;"
    ),
}


def _render_hvac_specs(zone_names: Sequence[str]) -> str:
    """Deterministically render the hvac_specs fragment: one shared
    HVACTemplate:Thermostat + one HVACTemplate:Zone:IdealLoadsAirSystem per
    zone, in ``zone_names`` order. These are the ONLY two object types
    downstream's hvac_agent MCP tools create (create_thermostat /
    create_ideal_loads_system — src/agent/nodes/hvac.py), and
    checks/mep.py:_HVAC_SCHEDULE_REF_FIELDS already has entries for both
    (2026-08-14 dispatch §2.2). References the 3 reserved schedules this
    module also injects into schedule_specs — see ``_merge_hvac_det_schedules``.
    A pure function of ``zone_names``: same input -> byte-identical output,
    which is what makes the flow and run_stage call sites agree (A3) as long
    as both pass the same ordered zone list.

    Raises ``ValueError`` if ``zone_names`` is empty (2026-08-14 terra
    cross-review pre-acceptance item #5): a zero-zone building has no HVAC
    to render, and silently emitting a Thermostat with zero
    IdealLoadsAirSystem objects attached to it would be a meaningless,
    self-orphaning output that could mask an upstream bug (a real building
    always has >=1 zone by construction; an empty zone_names here means
    either the geometry kernel produced a zero-zone building — a kernel bug,
    not something to paper over — or a caller passed the wrong sequence).
    """
    if not zone_names:
        raise ValueError(
            "_render_hvac_specs: zone_names is empty — a zero-zone building "
            "has no HVAC to render (there is one thermostat but it would "
            "reference zero HVACTemplate:Zone:IdealLoadsAirSystem objects, "
            "i.e. nothing). This is a hard reject, not a degenerate-but-"
            "valid output: check what produced an empty zone list upstream."
        )
    lines = [
        "HVACTemplate:Thermostat,",
        f"  {_HVAC_DET_THERMOSTAT_NAME},",
        f"  {_HVAC_DET_HEATING_SCHEDULE},",
        "  ,",
        f"  {_HVAC_DET_COOLING_SCHEDULE};",
    ]
    for zone in zone_names:
        lines += [
            "",
            "HVACTemplate:Zone:IdealLoadsAirSystem,",
            f"  {zone},",
            f"  {_HVAC_DET_THERMOSTAT_NAME},",
            f"  {_HVAC_DET_AVAILABILITY_SCHEDULE};",
        ]
    return "\n".join(lines)


def _hvac_det_schedule_block() -> str:
    """The 3 reserved Schedule:Compact objects _render_hvac_specs references,
    typed against the 2 reserved (not generic) ScheduleTypeLimits names —
    see the constants block above for why these are exclusive names, not
    "Temperature"/"OnOff". Each covers all day-types with a single
    ``For: AllDays`` block (the only other legal form is a trailing
    ``For: AllOtherDays`` catch-all — see authoring.md's schedule_specs
    section); an incomplete Schedule:Compact segfaults EnergyPlus 25.1.0
    with no message, so this is a hard shape."""
    return "\n".join([
        "Schedule:Compact,",
        f"  {_HVAC_DET_HEATING_SCHEDULE},",
        f"  {_HVAC_DET_TEMPERATURE_TYPE_LIMIT},",
        "  Through: 12/31,",
        "  For: AllDays,",
        f"  Until: 24:00, {_HVAC_DET_HEATING_SETPOINT_C};",
        "",
        "Schedule:Compact,",
        f"  {_HVAC_DET_COOLING_SCHEDULE},",
        f"  {_HVAC_DET_TEMPERATURE_TYPE_LIMIT},",
        "  Through: 12/31,",
        "  For: AllDays,",
        f"  Until: 24:00, {_HVAC_DET_COOLING_SETPOINT_C};",
        "",
        "Schedule:Compact,",
        f"  {_HVAC_DET_AVAILABILITY_SCHEDULE},",
        f"  {_HVAC_DET_ONOFF_TYPE_LIMIT},",
        "  Through: 12/31,",
        "  For: AllDays,",
        "  Until: 24:00, 1.0;",
    ])


def _merge_hvac_det_schedules(schedule_specs: str) -> str:
    """Append the 3 reserved HVAC schedules (2026-08-14 dispatch §2.3 option
    (a): code owns these 3 names outright rather than heuristically guessing
    which of the model's schedules is "the" heating setpoint) — plus their
    2 reserved ScheduleTypeLimits — to the LLM-authored schedule_specs text.

    2026-08-14 terra cross-review, 1st pass finding: the prompt tells the
    model not to author these 3 schedules, but the prompt is not a
    constraint (this repo's own model-visible-but-not-its-business lesson)
    — a model response can still define a Schedule:Compact under one of the
    3 reserved names. Fixed by TRUE code ownership: any Schedule:Compact in
    ``schedule_specs`` whose name matches one of the 3 reserved names —
    compared case-INSENSITIVELY, since EnergyPlus object-name lookups are
    case-insensitive — is excluded (via eppy ``removeidfobject`` +
    ``idfstr()`` re-serialization, not string surgery) before the code's
    canonical definition is appended.

    2026-08-14 terra cross-review, 2nd pass finding: the 1st-pass fix left
    the 3 canonical schedules referencing the GENERIC ScheduleTypeLimits
    names Temperature / OnOff, only adding the code's definition when the
    model hadn't already defined that name — a deliberate choice to avoid
    tampering with unrelated model schedules that might share those names.
    But it meant the 3 "code-owned" schedules still depended on something
    the MODEL fully controlled: a model response narrowing Temperature to
    e.g. `30, 40` silently put the code's own 20/24 values out of range
    (check_mep does not catch this — terra reproduced it, all of
    mep.idf_parse / mep.schedule_type_refs / mep.schedule_completeness /
    mep.hvac_schedule_refs PASS, check_mep.passed=True), and a model
    response spelling it lower-case `temperature` made the merge's
    case-insensitive "already defined" check skip adding `Temperature`
    while the canonical schedules still spelled it exact-case — and this
    repo's mep.schedule_type_refs is a case-SENSITIVE set comparison, so
    that combination made mep.schedule_type_refs FAIL for both canonical
    schedules (terra reproduced this too; a regression THIS rework
    introduced, not a pre-existing gap).

    Fixed by giving the 3 reserved schedules their own 2 EXCLUSIVE
    ScheduleTypeLimits names (HVACDet_Temperature / HVACDet_OnOff — see the
    constants block above) instead of the generic ones, and applying
    EXACTLY the same code-ownership treatment used for the 3 schedule names
    (case-insensitive exclusion of any same-named model definition, then
    the code's own canonical definition is appended unconditionally — no
    "only if the model hasn't defined it" branch anymore, because nothing
    legitimate can ever define these reserved names). The model's own
    generic Temperature / OnOff — if it defines them for something else,
    e.g. a people/lights schedule — are now completely untouched by this
    function: there is no longer any name for them to collide with, so the
    original motive (don't tamper with unrelated model schedules) is
    preserved without the code's own values depending on the model's.

    Only reformats (re-serializes via idfstr()) when a collision actually
    exists — the common case (no collision on either the 3 schedule names
    or the 2 type-limit names) leaves ``schedule_specs`` byte-identical
    apart from the appended addendum.

    Unconditional otherwise: this runs even if ``schedule_specs`` fails to
    parse — nothing can be reliably excluded from unparseable text, but the
    concatenated MEP bundle will still fail mep.idf_parse (fail-closed, same
    as before this change), so this is not a silent gap.

    The model may separately still define ITS OWN heating/cooling/hvac
    schedules under names other than the 3 reserved ones (authoring.md and
    the system prompt now tell it not to, but that is a token-saving
    instruction, not an enforced constraint) — those become orphaned
    (defined, unreferenced). No check in this repo flags orphaned schedules
    (grep for unused/orphan/unreferenced in checks/mep.py and
    validator/schedules.py is empty), so this is silent by omission, not
    silently wrong — recorded here and in the dispatch report, not hidden."""
    from src.validator.idf_fragments import parse_idf_text

    idx = parse_idf_text(schedule_specs)
    base_text = schedule_specs
    if idx.ok:
        reserved_schedule_lower = {
            name.lower() for name in _HVAC_DET_RESERVED_SCHEDULE_NAMES
        }
        reserved_type_limit_lower = {
            name.lower() for name in _HVAC_DET_RESERVED_TYPE_LIMIT_NAMES
        }
        collisions = [
            obj for obj in idx.of_type("SCHEDULE:COMPACT")
            if obj.name.lower() in reserved_schedule_lower
        ] + [
            obj for obj in idx.of_type("SCHEDULETYPELIMITS")
            if obj.name.lower() in reserved_type_limit_lower
        ]
        if collisions:
            for obj in collisions:
                idx.idf.removeidfobject(obj.raw)
            base_text = idx.idf.idfstr()

    addendum = list(_HVAC_DET_TYPE_LIMITS.values())
    addendum.append(_hvac_det_schedule_block())
    merged = base_text.strip()
    if merged:
        merged += "\n\n"
    merged += "\n".join(addendum)
    return merged


def run_mep(
    zone_specs: str,
    used_constructions: set[str],
    testdata_text: str,
    *,
    zone_names: Sequence[str],
    out_dir: Path | None = None,
    feedback: str | None = None,
):
    """4_MEP LLM stage: author the 8 non-geometry IntakeOutput fields.

    ``hvac_specs`` is NOT one of the fields the model actually controls
    despite being part of the LLM JSON schema: whatever the model returns for
    it is discarded unconditionally and replaced by a deterministic render
    from ``zone_names`` (plus 3 reserved schedules folded into
    ``schedule_specs`` — see ``_render_hvac_specs`` / ``_merge_hvac_det_schedules``
    above). ``zone_names`` MUST be the same ordered sequence both call sites
    use for ``zone_specs`` (both currently compute
    ``list(dict.fromkeys(bg.zones))`` off the same ``BuildingGeometry`` —
    2026-08-14 dispatch §2.1); passing an unordered or differently-ordered
    sequence makes the flow and run_stage renders diverge, which defeats the
    entire point of "deterministic".
    """
    from src.agent.intakeoutput import MepOutput

    ensure_schema_initialized()
    system_prompt, human = _build_mep_messages(
        zone_specs, used_constructions, testdata_text, feedback=feedback
    )
    parsed = _call_json_llm(
        _section("mep"), system_prompt, human, out_dir=out_dir, prefix="mep", attempts=3
    )
    try:
        result = MepOutput.model_validate(parsed)
    except Exception as e:
        if out_dir is not None:
            (out_dir / "mep_output_unvalidated.json").write_text(
                json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        raise RuntimeError(f"4_mep: MepOutput validation error: {e}") from e

    # Deterministic override — unconditional, regardless of what the model
    # returned for hvac_specs (2026-08-14 dispatch; see module comment above
    # run_mep for why: F-28 field-position defect).
    result.hvac_specs = _render_hvac_specs(zone_names)
    result.schedule_specs = _merge_hvac_det_schedules(result.schedule_specs)

    if out_dir is not None:
        (out_dir / "mep_output.json").write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
    return result


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def materialize_kernel_geometry(
    geom: CorrectedGeometry,
    out_dir: Path | None,
    *,
    capability_profile: str = "rectangular",
    window_host_proof=None,
) -> tuple["BuildingGeometry | None", list[str]]:
    """Run the deterministic geometry kernel (2_modelling -> 3_split_pairing) on
    the snapped CorrectedGeometry and materialize the result + a gate report.

    Returns `(building_geometry, gate_issues)`. The geometry is authoritative —
    `run_pipeline` reuses this object (it does NOT build a second time). On a hard
    kernel error returns `(None, [err])` so the caller can fail loudly. The
    InterZone gate issues are advisory here (the downstream gate re-checks the
    assembled IDF). Source realization failures return None after saving the
    candidate geometry and source report, so exploratory runs also stop. Never
    raises for build/validation failures.
    """
    # Lazy imports: keep the kernel (shapely/eppy) off the hot path for callers
    # that never reach here, mirroring the run_pipeline import discipline.
    try:
        from src.agent.geometry import build_geometry
        from src.agent.geometry.to_idf import building_to_idf
        from src.validator.interzone import validate_interzone_surface_pairs

        # build_geometry is read-only on `geom` (it constructs new ZoneVolumes /
        # polygons), so the same `geom` is safe to hand to later stages.
        bg = build_geometry(
            geom,
            capability_profile=capability_profile,
            window_host_proof=window_host_proof,
        )
        from src.agent.geometry.source_model import materialize_source_model

        source_model = materialize_source_model(geom, bg)
        issues = validate_interzone_surface_pairs(building_to_idf(bg, boundary_validation_only=True))
        issues.extend(
            f"{finding['code']}: {json.dumps(finding, ensure_ascii=False)}"
            for finding in source_model["validation"]["findings"]
        )
    except Exception as e:  # noqa: BLE001 — advisory build, never fatal here
        logger.warning("kernel: geometry build/gate failed: {}", e)
        err = [f"kernel-error: {type(e).__name__}: {e}"]
        if out_dir is not None:
            (out_dir / "kernel_gate_report.json").write_text(
                json.dumps({"gate_issues": err, "build_notes": []}, indent=2),
                encoding="utf-8",
            )
        return None, err

    logger.info(
        "kernel: {} zones, {} surfaces, {} windows; gate issues={} notes={}",
        len(dict.fromkeys(bg.zones)),
        len(bg.surfaces),
        len(bg.windows),
        len(issues),
        len(bg.notes),
    )
    if out_dir is not None:
        from src.agent.geometry.specs import building_geometry_json

        (out_dir / "building_geometry.json").write_text(
            building_geometry_json(bg), encoding="utf-8"
        )
        (out_dir / "source_model.json").write_text(
            json.dumps(source_model, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out_dir / "kernel_gate_report.json").write_text(
            json.dumps(
                {"gate_issues": issues, "build_notes": bg.notes},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    # Source realization failures must never become downstream physical geometry,
    # including exploratory runs. Keep the candidate files for diagnosis first.
    if source_model["validation"]["status"] == "severe":
        return None, issues
    return bg, issues


def _gate_self_check_report(
    *,
    stage_name: str,
    report,
    stage_dir: Path | None,
    filename: str,
    run_profile: RunProfile,
) -> None:
    if stage_dir is not None:
        (stage_dir / filename).write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
    blocking = report.blocking()
    if not blocking:
        return
    blocking_ids = [result.check_id for result in blocking]
    if run_profile in {"golden", "regression"}:
        raise RuntimeError(
            f"{stage_name} self-check blocked under run_profile={run_profile}: "
            + "; ".join(
                f"{result.check_id}: {result.message}" for result in blocking
            )
        )
    logger.warning(
        "{} self-check reported {} blocking check(s) under run_profile={}; "
        "continuing: {}",
        stage_name,
        len(blocking),
        run_profile,
        ", ".join(blocking_ids),
    )


def run_pipeline(
    vector_dir: Path,
    testdata_text: str,
    *,
    out_dir: Path | None = None,
    feedback: str | None = None,
    run_profile: RunProfile = "exploratory",
    capability_profile: str = "rectangular",
) -> IntakeOutput:
    """Compatibility wrapper (E4-output-contract spec v2 §3.5): identical to
    the historical signature/return, but implemented on top of
    :func:`run_pipeline_artifacts` so a legacy caller and the bundle-aware
    `intake_node` mainline can never drift onto two assembly code paths."""
    intake, _bundle = run_pipeline_artifacts(
        vector_dir, testdata_text, out_dir=out_dir, feedback=feedback,
        run_profile=run_profile, capability_profile=capability_profile,
    )
    return intake


def run_pipeline_artifacts(
    vector_dir: Path,
    testdata_text: str,
    *,
    out_dir: Path | None = None,
    feedback: str | None = None,
    run_profile: RunProfile = "exploratory",
    capability_profile: str = "rectangular",
) -> "tuple[IntakeOutput, IntakeArtifactBundle | None]":
    """Staged intake: 1_correction -> deterministic core -> deterministic geometry
    kernel (2_modelling + 3_split_pairing) -> 4_mep (LLM) -> 5_intakeoutput assembly.

    Geometry is deterministic: the kernel builds + the serializer emits
    zone/surface/fenestration specs; 4_mep authors only the 8 non-geometry fields;
    assembly stitches them and runs a contract check.

    Returns ``(intake, bundle)``. ``bundle`` is the E4
    :class:`~src.agent.output_coordinates.IntakeArtifactBundle` (IntakeOutput +
    OutputCoordinateContract + coordinate snapshot + validation context) and is
    present whenever gate① accepted the correction; it is ``None`` only for
    the exploratory-profile edge case where a blocking correction result was
    warned-through (no accepted-correction identity exists to bind a contract
    to — assembly then behaves exactly as pre-E4).

    When `out_dir` is given, artifacts are filed into stage-numbered subdirs that
    mirror the 0–5 pipeline:
      out_dir/1_correction/    correction_geometry.json (pre-snap) +
                               correction_geometry_snapped.json (post core) +
                               corrections.json + correction raw/thinking
      out_dir/2_modelling/     building_geometry.json + kernel_gate_report.json +
                               kernel_checks.json
      out_dir/3_split_pairing/ geometry_specs.md (serialized cut+paired specs)
      out_dir/4_mep/           mep_output.json + mep raw/thinking
      out_dir/5_intakeoutput/  intake_output.json (final) + contract_issues.json
                               + output_coordinate_contract.json/_snapshot.json
                               sidecars when a contract was derived
    ``run_profile`` defaults to exploratory for backward compatibility.
    ``feedback`` is routed to both 1_correction (geometry) and 4_mep (physics)
    repair.
    """
    ensure_schema_initialized()

    def _stage(name: str) -> Path | None:
        if out_dir is None:
            return None
        d = out_dir / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # F-97 (F-b + F-c, BLK-B): FIRST, before anything in this function parses a
    # `*_view.json`. `run_correction` ~45 lines below runs the same preflight,
    # but by then this entry has already loaded every view three times over (the
    # reading report, the view list, and the v3 observation catalog), and a file
    # this stage cannot honour blew up inside one of those with a bare
    # `AttributeError` / `UnicodeDecodeError` / `IsADirectoryError` -- no named
    # refusal, no ledger. Measured on the composite entry, not on `run_correction`
    # in isolation: entry LEVEL is part of "which entries does the promise cover".
    # ⚠️ ⛔ NOT `_stage("1_correction")`: that would mkdir the stage dir before
    # the refusal, and "1_correction does not exist yet" is a live assertion in
    # this repo (tests/test_run_stage_flow.py). The ledger writer only reads
    # `.parent` off this path, so the directory itself is never needed.
    _preflight_vector_contracts(
        vector_dir, None if out_dir is None else out_dir / "1_correction"
    )

    logger.info("1_correction: correcting from {}", vector_dir)
    from src.agent.execution.case_metadata import (
        dimensioned_states_from_data,
        expected_zone_total_from_testdata,
        parse_testdata_text,
    )

    parsed_testdata = parse_testdata_text(testdata_text)
    # R1-3 (派工单 §1.3): per-view 4-state applicability from the structured
    # declaration, not a bool set — declared_true/declared_false survive to
    # checks.json instead of collapsing to legacy_default.
    dimensioned_states = dimensioned_states_from_data(parsed_testdata or {})
    reading_report = compute_reading_report_from_vector_dir(
        vector_dir,
        run_profile=run_profile,
        capability_profile=capability_profile,
        dimensioned_states=dimensioned_states,
    )
    from src.agent.reading import load_reading_view

    reading_views = [
        load_reading_view(path)
        for path in sorted(Path(vector_dir).glob("*_view.json"))
    ]
    s0 = _stage("0_reading")
    if s0 is not None:
        (s0 / "reading_checks.json").write_text(
            reading_report.model_dump_json(indent=2), encoding="utf-8"
        )
    evidence_debt = project_evidence_debt(
        reading_report,
        run_profile=run_profile,
    )
    s1 = _stage("1_correction")
    if s1 is not None:
        write_evidence_debt(s1 / "evidence_debt.json", evidence_debt)
    if evidence_debt.blocking:
        checks = ", ".join(item.check_id for item in evidence_debt.blocking)
        raise RuntimeError(
            "1_correction preflight blocked by reading evidence debt "
            f"under run_profile={run_profile}: {checks}"
        )
    _gate_self_check_report(
        stage_name="0_reading",
        report=reading_report,
        stage_dir=None,
        filename="reading_checks.json",
        run_profile=run_profile,
    )
    observation_reference_catalog = None
    if out_dir is not None:
        from src.agent.correction.parse import correction_target as _correction_target
        from src.agent.correction.window_sources import build_observation_reference_catalog_from_run

        if _correction_target(capability_profile).schema_version == "3":
            observation_reference_catalog = build_observation_reference_catalog_from_run(
                run_dir=out_dir, reading_dir=vector_dir, required_for_v3=True,
            )
    geom = run_correction(
        vector_dir,
        testdata_text,
        out_dir=s1,
        feedback=feedback,
        run_profile=run_profile,
        capability_profile=capability_profile,
        evidence_debt=evidence_debt,
        observation_reference_catalog=observation_reference_catalog,
    )

    from src.validator.checks.correction import check_evidence_debt_coverage

    coverage_report = check_evidence_debt_coverage(
        geom,
        evidence_debt,
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    if coverage_report.results and s1 is not None:
        (s1 / "evidence_debt_coverage_checks.json").write_text(
            coverage_report.model_dump_json(indent=2), encoding="utf-8"
        )
    blocking_coverage = [
        result
        for result, disp in coverage_report.dispositions()
        if disp == Disposition.BLOCK
    ]
    if blocking_coverage:
        raise RuntimeError(
            "1_correction evidence debt coverage blocked under "
            f"run_profile={run_profile}: "
            + "; ".join(f"{r.check_id}: {r.message}" for r in blocking_coverage)
        )

    from src.agent.correction.finalize import finalize_correction_draw
    from src.agent.correction.parse import correction_target
    verified_window_inputs = None
    if geom.schema_version == "3" and out_dir is not None:
        from src.agent.correction.window_sources import build_verified_window_inputs_from_run

        verified_window_inputs = build_verified_window_inputs_from_run(
            producer_draw=geom,
            run_dir=out_dir,
            reading_dir=vector_dir,
        )
    n_corr_before = len(geom.corrections)
    finalized = finalize_correction_draw(
        geom,
        vector_dir=vector_dir,
        target=correction_target(capability_profile),
        verified_window_inputs=verified_window_inputs,
    )
    geom = finalized.geom
    logger.info(
        "core: deterministic snap added {} correction(s), {} unsupported",
        len(geom.corrections) - n_corr_before,
        len(geom.unsupported),
    )
    if s1 is not None:
        (s1 / "correction_geometry_snapped.json").write_text(
            geom.model_dump_json(indent=2), encoding="utf-8"
        )
        (s1 / "corrections.json").write_text(
            json.dumps(
                {
                    "corrections": geom.corrections,
                    "conflicts": geom.conflicts,
                    "unsupported": geom.unsupported,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # 2026-08-12 (摊 C): pure "annotation basis" observation sidecar --
        # same convenience-copy pattern as corrections.json above, mirrored
        # from stage_runner.py's writer so the run_pipeline_artifacts path
        # (this function) and the flow/run_stage.py path both expose it.
        # Empty for legacy v1/v2 or a v3 draw with no accepted envelope axis.
        if finalized.annotation_basis:
            from src.agent.correction.config import load_core_tolerances
            from src.agent.correction.envelope_transform import annotation_basis_report

            (s1 / "annotation_basis.json").write_text(
                json.dumps(
                    annotation_basis_report(finalized.annotation_basis, load_core_tolerances()),
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    from src.validator.checks.correction import check_correction

    correction_report = check_correction(
        geom,
        window_host_proof=finalized.window_host_claims,
        window_evidence=finalized.window_evidence_ledger,
        expected_zone_total=(
            expected_zone_total_from_testdata(parsed_testdata)
            if parsed_testdata is not None
            else None
        ),
        raw_geom=None,
        relied_on_testdata=bool(parsed_testdata),
        capability_profile=capability_profile,
        run_profile=run_profile,
        evidence_debt=evidence_debt,
        reading_views=reading_views,
        # F-9 route② S2 (2026-08-12): non-blocking cross-check shadow of the
        # same live model-authored `window.span` path, using the SAME
        # `verified_window_inputs` this function already built above
        # (:1093-1101). See `check_correction`'s
        # `_window_position_evidence_shadow` for the full contract (never
        # overrides span, never blocks — CheckLayer.CROSS_CHECK only).
        verified_window_inputs=verified_window_inputs,
    )
    _gate_self_check_report(
        stage_name="1_correction",
        report=correction_report,
        stage_dir=s1,
        filename="correction_checks.json",
        run_profile=run_profile,
    )

    # ----------------------------------------------------------------- #
    # E4 (C2 E4-output-contract spec v2 §3.2bis/§11 steps 1-4): build the
    # verified accepted-correction identity, and for a v3/orthogonal_polygon
    # draw, run orientation resolution unconditionally (this batch's only
    # producer is the trusted-empty evidence-set artifact — B-O does not add
    # any total-plan/north-arrow evidence collector; see spec §0.3). v1/v2
    # rectangular corrections have no orientation concept at all and are
    # verified straight through (they derive `world_legacy` at S5).
    #
    # BO-CR2 (review-ask #1 REJECT): a blocking gate① correction leaves this
    # run with NO accepted identity to bind a contract to — a new E4 run
    # without a contract is a HARD failure under every profile; there is no
    # fall-back to pre-E4 contract-less assembly.
    # ----------------------------------------------------------------- #
    if correction_report.blocking():
        raise RuntimeError(
            "1_correction gate① reported blocking result(s) — no accepted correction "
            "identity exists to bind the E4 output-coordinate contract, and a new run "
            "may not assemble without one (E4-oc v2 §3.4): "
            + "; ".join(f"{r.check_id}: {r.message}" for r in correction_report.blocking())
        )

    from src.agent.correction.feature_state import (
        FeatureStatesArtifactV1,
        derive_feature_state_claims,
    )
    from src.agent.correction.artifact_serialization import serialize_correction_output
    from src.agent.output_coordinates import (
        sha256_bytes,
        verify_integrated_gate1_correction,
    )

    if geom.schema_version == "3":
        from src.agent.correction.orientation import resolve_orientation_from_run_dir
        from src.agent.execution.run_config import load_run_config

        pre_raw = serialize_correction_output(geom)
        pre_hash = sha256_bytes(pre_raw)
        pre_claims = derive_feature_state_claims(correction_target(capability_profile), geom)
        pre_resolver_bytes = None
        pre_hosts_bytes = None
        if finalized.verified_window_resolver_inputs is not None:
            from src.agent.correction.window_host import build_window_hosts_artifact
            from src.agent.correction.window_sources import serialize_window_resolver_inputs_artifact

            assert finalized.prepared_candidate_identity is not None
            assert finalized.window_host_claims is not None
            assert finalized.window_evidence_ledger is not None
            pre_resolver_bytes = serialize_window_resolver_inputs_artifact(
                finalized.verified_window_resolver_inputs
            )
            pre_hosts_bytes = build_window_hosts_artifact(
                output_sha256=finalized.prepared_candidate_identity.output_sha256,
                claims=finalized.window_host_claims,
                evidence=finalized.window_evidence_ledger,
            ).model_dump_json(indent=2).encode("utf-8")
        pre_verified = verify_integrated_gate1_correction(
            raw_output_bytes=pre_raw,
            correction_report=correction_report,
            feature_states=FeatureStatesArtifactV1(output_sha256=pre_hash, claims=pre_claims),
            raw_window_resolver_inputs_bytes=pre_resolver_bytes,
            raw_window_hosts_bytes=pre_hosts_bytes,
        )

        # BO-CR3: completion mode comes from the run's REAL configuration
        # (run_config.yaml `orientation.completion_mode`, default prior_fill),
        # and the evidence set is a content-addressed on-disk artifact — the
        # canonical empty set is explicitly materialized, then re-loaded and
        # hash-verified before resolution. Nothing here is an in-memory
        # pipeline literal.
        if s1 is not None:
            correction_dir = s1
        else:
            import tempfile

            correction_dir = Path(tempfile.mkdtemp(prefix="e4_orientation_"))
        run_config_dir = out_dir if out_dir is not None else correction_dir
        run_cfg = load_run_config(run_config_dir)
        orientation_result, _orientation_artifacts = resolve_orientation_from_run_dir(
            correction_dir=correction_dir,
            base=pre_verified,
            completion_mode=run_cfg.orientation_completion_mode,
            capability_profile=capability_profile,
        )
        geom = orientation_result.geom
        logger.info(
            "1_correction (E4): orientation resolved via {} -> north_axis={}deg "
            "(completion_mode={})",
            orientation_result.audit_payload["orientation"]["resolution_kind"],
            geom.north_axis.value_deg,
            run_cfg.orientation_completion_mode,
        )
        if s1 is not None:
            (s1 / "correction_geometry_snapped.json").write_text(
                geom.model_dump_json(indent=2), encoding="utf-8",
            )
            (s1 / "corrections.json").write_text(
                json.dumps(orientation_result.audit_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            (s1 / "orientation_audit.json").write_text(
                json.dumps(orientation_result.audit_payload["orientation"], indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        enriched_raw = serialize_correction_output(geom)
        enriched_hash = sha256_bytes(enriched_raw)
        enriched_resolver_bytes = None
        enriched_hosts_bytes = None
        if orientation_result.verified_window_resolver_inputs is not None:
            from src.agent.correction.window_host import build_window_hosts_artifact

            assert orientation_result.prepared_candidate_identity is not None
            assert orientation_result.window_host_claims is not None
            assert orientation_result.window_evidence_ledger is not None
            enriched_resolver_bytes = orientation_result.verified_window_resolver_inputs.raw_inputs_bytes
            enriched_hosts_bytes = build_window_hosts_artifact(
                output_sha256=orientation_result.prepared_candidate_identity.output_sha256,
                claims=orientation_result.window_host_claims,
                evidence=orientation_result.window_evidence_ledger,
            ).model_dump_json(indent=2).encode("utf-8")
        verified_correction = verify_integrated_gate1_correction(
            raw_output_bytes=enriched_raw,
            correction_report=correction_report,
            feature_states=FeatureStatesArtifactV1(
                output_sha256=enriched_hash, claims=orientation_result.feature_state_claims,
            ),
            raw_window_resolver_inputs_bytes=enriched_resolver_bytes,
            raw_window_hosts_bytes=enriched_hosts_bytes,
        )
    else:
        raw_bytes = serialize_correction_output(geom)
        claims = derive_feature_state_claims(correction_target(capability_profile), geom)
        verified_correction = verify_integrated_gate1_correction(
            raw_output_bytes=raw_bytes, correction_report=correction_report,
            feature_states=FeatureStatesArtifactV1(output_sha256=sha256_bytes(raw_bytes), claims=claims),
        )

    # Deterministic geometry kernel (2_modelling -> 3_split_pairing). This geometry
    # is authoritative; we serialize it into the geometry specs.
    s2 = _stage("2_modelling")
    s3 = _stage("3_split_pairing")
    bg, kernel_issues = materialize_kernel_geometry(
        geom,
        s2,
        capability_profile=capability_profile,
        window_host_proof=verified_correction.window_host_proof,
    )
    if kernel_issues:
        hint = "" if s2 is None else "; see 2_modelling/kernel_gate_report.json"
        logger.warning(
            "kernel: {} InterZone gate issue(s) on the deterministic build "
            "(advisory — the downstream gate re-checks the assembled IDF{})",
            len(kernel_issues),
            hint,
        )

    if bg is None:
        # Hard kernel error: the geometry is required and deterministic, so a
        # build failure is a bug to fix, not something to paper over with an LLM.
        where = " (see 2_modelling/kernel_gate_report.json)" if s2 is not None else ""
        raise RuntimeError(
            f"geometry kernel build failed{where}: " + "; ".join(kernel_issues)
        )

    from src.validator.checks.kernel import check_kernel

    kernel_report = check_kernel(
        bg,
        window_host_proof=verified_correction.window_host_proof,
        capability_profile=capability_profile,
        interzone_issues=kernel_issues,
        run_profile=run_profile,
    )
    _gate_self_check_report(
        stage_name="2_modelling",
        report=kernel_report,
        stage_dir=s2,
        filename="kernel_checks.json",
        run_profile=run_profile,
    )

    # 3_split_pairing (serialization): kernel geometry -> specs text.
    from src.agent.geometry.specs import geometry_specs_markdown, serialize_geometry
    from src.agent.intakeoutput import assemble_intake_output

    zone_specs, surface_specs, fenestration_specs, used_constructions = serialize_geometry(
        bg, frame_label="building_axis" if geom.schema_version == "3" else "world",
    )
    if s3 is not None:
        (s3 / "geometry_specs.md").write_text(
            geometry_specs_markdown(
                zone_specs,
                surface_specs,
                fenestration_specs,
                geometry_contract=bg.geometry_contract,
            ),
            encoding="utf-8",
        )

    # 4_mep (LLM): author the 8 non-geometry fields against the zone list +
    # required construction set.
    logger.info(
        "4_mep: authoring non-geometry specs ({} zones, {} constructions)",
        len(dict.fromkeys(bg.zones)),
        len(used_constructions),
    )
    s4 = _stage("4_mep")
    mep = run_mep(
        zone_specs, used_constructions, testdata_text,
        zone_names=list(dict.fromkeys(bg.zones)), out_dir=s4, feedback=feedback,
    )

    from src.validator.checks.mep import check_mep

    mep_report = check_mep(
        json.loads(mep.model_dump_json()),
        used_constructions=used_constructions or None,
        zone_names=set(dict.fromkeys(bg.zones)) or None,
        testdata=parsed_testdata,
        capability_profile=capability_profile,
    )
    _gate_self_check_report(
        stage_name="4_mep",
        report=mep_report,
        stage_dir=s4,
        filename="mep_checks.json",
        run_profile=run_profile,
    )

    # 5_intakeoutput (assembly): stitch + deterministic contract check.
    #
    # E4 (spec §3.5/§11 step 6): when this run has a verified accepted
    # correction (gate① clean — see the E4 block above), assemble through
    # `assemble_intake_artifacts` so the final Building.North Axis is the
    # unconditional S5 override from the derived OutputCoordinateContract
    # (0.0/world_legacy for v1/v2, theta/relative_north_axis for v3).
    # `verified_correction is None` (a blocking-gate① exploratory edge case)
    # falls through to the exact pre-E4 call — no output_coordinates at all.
    # BO-CR2: every integrated run reaching S5 has a verified correction (the
    # gate above raised otherwise) — there is no contract-less assembly path.
    from src.agent.output_coordinates import (
        assemble_intake_artifacts,
        build_assembly_coordinate_audit,
        build_output_coordinate_snapshot,
        canonical_json_bytes,
    )

    coordinate_snapshot = build_output_coordinate_snapshot(bg)
    bundle = assemble_intake_artifacts(
        zone_specs=zone_specs,
        surface_specs=surface_specs,
        fenestration_specs=fenestration_specs,
        mep=mep,
        correction=verified_correction,
        coordinate_snapshot=coordinate_snapshot,
    )
    intake = bundle.intake
    output_coordinate_contract = bundle.output_coordinates
    from src.validator.checks.assembly import check_assembly

    s5 = _stage("5_intakeoutput")
    assembly_report = check_assembly(
        intake,
        used_constructions,
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    if s5 is not None:
        (s5 / "assembly_checks.json").write_text(
            assembly_report.model_dump_json(indent=2), encoding="utf-8"
        )
    contract_issues = []
    for result in assembly_report.results:
        if result.check_id != "assembly.contract_backstop":
            continue
        issues = result.evidence.get("issues")
        if isinstance(issues, list):
            contract_issues.extend(issues)
    if contract_issues:
        if s5 is not None:
            (s5 / "contract_issues.json").write_text(
                json.dumps(contract_issues, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        raise RuntimeError(
            "5_intakeoutput contract check failed (4_mep omitted geometry-"
            "referenced definitions):\n- " + "\n- ".join(contract_issues)
        )
    _gate_self_check_report(
        stage_name="5_intakeoutput",
        report=assembly_report,
        stage_dir=None,
        filename="assembly_checks.json",
        run_profile=run_profile,
    )

    if s5 is not None:
        (s5 / "intake_output.json").write_text(
            intake.model_dump_json(indent=2, by_alias=False), encoding="utf-8"
        )
        logger.success("5_intakeoutput: wrote {}", s5 / "intake_output.json")
        (s5 / "output_coordinate_contract.json").write_text(
            output_coordinate_contract.model_dump_json(indent=2), encoding="utf-8",
        )
        snapshot_bytes = canonical_json_bytes(coordinate_snapshot)
        (s5 / "output_coordinate_snapshot.json").write_bytes(snapshot_bytes)
        # BO-CR7: integrated S5 also files the strict assembly coordinate
        # audit (stepwise files it as the accepted attempt's audit.json).
        assembly_audit = build_assembly_coordinate_audit(
            verified=verified_correction,
            contract=output_coordinate_contract,
            snapshot_bytes=snapshot_bytes,
            mep_placeholder_north_axis=float(mep.building.north_axis),
            final_building_north_axis=float(intake.building.north_axis),
        )
        (s5 / "assembly_coordinate_audit.json").write_text(
            assembly_audit.model_dump_json(indent=2), encoding="utf-8",
        )
    return intake, bundle
