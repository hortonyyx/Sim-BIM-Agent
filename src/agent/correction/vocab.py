"""Mechanically-derived correction schema vocabulary — the SINGLE source both
the correction system prompt (F-4b) and the inner-retry corrective message
(F-4a) read from.

Why this module exists: the correction LLM draw is single-shot, and its only
feedback on a schema rejection used to be a *blind* re-issue of the same
prompt — ``_call_json_llm`` wrote the validation error to disk and retried
verbatim, so a systematic schema misunderstanding (e.g. an unknown
``north_axis`` key, or a non-vocabulary window ``provenance`` key) burned the
whole retry budget on the same mistake and then crashed the flow. The fix tells
the model *which field* broke and *what the legal tokens are* — but the legal
tokens MUST come from the schema itself (``WINDOW_CLAIMS`` /
``NorthAxisEvidence.model_fields``), never from a hand-copied second list. A
second list is a second ruler, and this project has been bitten by that before.

These helpers derive the vocabulary mechanically, so a schema change propagates
to both the prompt and the retry message with no manual sync step to forget.
They carry ONLY format / word-list information — never geometry, upstream
reading content, gt, or numeric values from a rejected draw.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args


def window_provenance_vocabulary() -> list[str]:
    """Legal keys for ``WindowV3.provenance`` — the opening-claim vocabulary.

    Derived from ``WINDOW_CLAIMS`` (the schema's authoritative word list in
    ``claims.py``), not hand-copied."""
    from src.agent.correction.claims import WINDOW_CLAIMS

    return sorted(WINDOW_CLAIMS)


def provenance_kind_vocabulary() -> list[str]:
    """Legal VALUES for a claim's ``provenance`` field — observed / derived /
    assumed (the VALUE enum, distinct from :func:`window_provenance_vocabulary`
    which is the claim-name KEY set).

    Derived mechanically from ``FieldProvenance.provenance``'s own ``Literal``
    annotation (the schema's authoritative enum), not hand-copied, and asserted
    element-equal to ``NorthAxisEvidence.provenance`` so the two Literal
    declarations cannot drift. A schema change (a fourth kind added to either)
    propagates to both the system prompt and the retry message automatically."""
    from src.agent.correction.schema import FieldProvenance, NorthAxisEvidence

    field_kinds = get_args(FieldProvenance.model_fields["provenance"].annotation)
    north_kinds = get_args(NorthAxisEvidence.model_fields["provenance"].annotation)
    if set(field_kinds) != set(north_kinds):
        raise AssertionError(
            "FieldProvenance.provenance and NorthAxisEvidence.provenance enums "
            f"drifted: {field_kinds} vs {north_kinds}"
        )
    return sorted(field_kinds)


def north_axis_allowed_fields() -> list[str]:
    """Legal top-level fields on ``NorthAxisEvidence`` (``extra='forbid'``).

    Derived from the pydantic model's own field declarations, not hand-copied."""
    from src.agent.correction.schema import NorthAxisEvidence

    return sorted(NorthAxisEvidence.model_fields)


def _is_v3_target(target: Any) -> bool:
    schema_model = getattr(target, "schema_model", None)
    try:
        from src.agent.correction.schema import CorrectedGeometryV3

        return isinstance(schema_model, type) and issubclass(schema_model, CorrectedGeometryV3)
    except Exception:  # noqa: BLE001 — a malformed target is simply "not v3"
        return False


def _provenance_value_break(loc: tuple) -> bool:
    """True when a ``ValidationError`` loc targets a claim's ``provenance`` VALUE
    (the observed/derived/assumed ``Literal`` on ``FieldProvenance`` /
    ``NorthAxisEvidence``) — as opposed to the window ``provenance`` dict KEY set.

    Both shapes end in ``'provenance'``: a window claim's value error is
    ``('windows', i, 'provenance', <claim>, 'provenance')`` and north_axis's is
    ``('north_axis', 'provenance')``, whereas the window key-set rejection is
    exactly ``('windows', i, 'provenance')`` (the dict field itself). The key-set
    case is the one shape to exclude so it is guided to the KEY vocabulary, not
    the value enum."""
    if not loc or str(loc[-1]) != "provenance":
        return False
    if str(loc[0]) == "windows" and len(loc) == 3:
        return False
    return True


def correction_schema_vocabulary(target: Any) -> dict[str, list[str]]:
    """Map of stable label -> sorted legal tokens for the target schema.

    Only v3 (``CorrectedGeometryV3``) carries ``WindowV3.provenance`` and
    ``north_axis``; v1 (rectangular) has neither, so its vocabulary map is
    empty and the prompt/retry path adds nothing — legacy behaviour unchanged."""
    if not _is_v3_target(target):
        return {}
    return {
        "window_provenance_keys": window_provenance_vocabulary(),
        "provenance_kinds": provenance_kind_vocabulary(),
        "north_axis_fields": north_axis_allowed_fields(),
    }


def producer_facing_json_schema(schema_model: Any) -> dict:
    """The JSON Schema shown to the correction LLM draw prompt (F-15,
    2026-08-07).

    Structurally excludes any field the schema itself marks
    ``schema.CORRECTION_DRAW_FORBIDDEN`` (e.g. ``CorrectedGeometryV3.
    facade_segments`` / ``WindowV3.facade_segment_id``) — the deterministic
    core's OWN audit trail, computed downstream from a correction draw, never
    legal input to one. Also excludes ``schema.CORRECTION_DRAW_DERIVED``
    fields (e.g. ``WindowV3.floor``, F-16 2026-08-08 §6 摊一 Step 2) — a
    different reason (the schema derives them from a sibling field the model
    DOES draw, rather than a later stage populating them) but the same
    "the model must never see this as fillable" outcome.

    Before this fix the FULL, unmodified ``model_json_schema()`` was dumped
    verbatim into the prompt (F-15 A1/A5): the schema offered these fields as
    ordinary optional fields worth filling, and nothing in the prompt or
    schema said otherwise. The model filled them — including fabricating a
    64-hex placeholder for ``source_footprint_fingerprint`` — and every one
    of its 3 blind inner-retry attempts was rejected by the (unweakened)
    ``_producer_preflight`` door in ``window_sources.py`` with the exact same
    error, because nothing told it to stop (see also
    ``retry_guidance_for_correction`` below, which now also translates that
    specific rejection into guidance — F-15 B2).

    The exclusion is marker-driven, not a hand-copied field-name list: a
    future core-only field is excluded automatically as long as it carries
    the marker in its own ``Field(json_schema_extra=...)`` declaration — same
    discipline as the rest of this module (single source of truth, no second
    list to forget). Validation of an actual draw against the full
    ``CorrectedGeometryV3`` model is completely unaffected by this function —
    it only changes what the model is TOLD, never what the core accepts.
    """
    import copy

    from src.agent.correction.schema import CORRECTION_DRAW_DERIVED, CORRECTION_DRAW_FORBIDDEN

    schema = copy.deepcopy(schema_model.model_json_schema())

    def _strip(node: dict) -> None:
        properties = node.get("properties")
        if not isinstance(properties, dict):
            return
        # F-16 (2026-08-08, §6 摊一 Step 2): a CORRECTION_DRAW_DERIVED field
        # (e.g. WindowV3.floor) gets the exact same "the model must never see
        # this as fillable" treatment as a CORRECTION_DRAW_FORBIDDEN one —
        # the two markers differ in what happens AFTER a value slips through
        # (derived-and-overwritten vs. stays-forbidden-forever), not in
        # whether the prompt schema should offer the field at all.
        forbidden = [
            name
            for name, prop in properties.items()
            if isinstance(prop, dict)
            and (prop.get(CORRECTION_DRAW_FORBIDDEN) is True or prop.get(CORRECTION_DRAW_DERIVED) is True)
        ]
        for name in forbidden:
            del properties[name]
            required = node.get("required")
            if isinstance(required, list) and name in required:
                required.remove(name)

    _strip(schema)
    for definition in schema.get("$defs", {}).values():
        if isinstance(definition, dict):
            _strip(definition)

    # A stripped property can leave its own type definition (e.g.
    # `FacadeSegment`, the type of the now-removed `facade_segments`) as an
    # ORPHANED `$defs` entry — unreferenced by any `$ref`, but still visually
    # present in the dumped JSON, which would re-offer the model the exact
    # shape it should never fill. Prune anything no longer reachable by
    # `$ref` from the (already-stripped) root.
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        reachable = _reachable_def_names({k: v for k, v in schema.items() if k != "$defs"}, defs)
        schema["$defs"] = {name: value for name, value in defs.items() if name in reachable}
    return schema


def _collect_ref_names(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            out.add(ref[len("#/$defs/"):])
        for value in node.values():
            _collect_ref_names(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_ref_names(item, out)


def _reachable_def_names(root: object, defs: dict) -> set[str]:
    """BFS closure of `$defs` entries reachable by `$ref` from `root`."""
    seen: set[str] = set()
    frontier: set[str] = set()
    _collect_ref_names(root, frontier)
    while frontier:
        name = frontier.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        more: set[str] = set()
        _collect_ref_names(defs[name], more)
        frontier |= more - seen
    return seen


def format_correction_system_vocabulary(target: Any) -> str:
    """The allowed-vocabulary block for the correction system prompt (F-4b).

    Empty string for targets that carry no constrained vocabulary (v1), so the
    v1 prompt is byte-identical to before the change."""
    vocab = correction_schema_vocabulary(target)
    if not vocab:
        return ""
    lines = [
        "===== BEGIN ALLOWED VOCABULARY (mechanically derived from the schema; "
        "if the schema changes this block changes with it — never invent tokens "
        "outside it) =====",
    ]
    if "window_provenance_keys" in vocab:
        lines.append(
            "window `provenance` keys — the opening-claim vocabulary (use ONLY "
            "these; any other key is rejected): "
            + ", ".join(vocab["window_provenance_keys"])
        )
    if "provenance_kinds" in vocab:
        lines.append(
            "each claim's `provenance` VALUE — observed/derived/assumed (the value "
            "of every opening-claim AND north_axis; any other string is rejected): "
            + ", ".join(vocab["provenance_kinds"])
        )
    if "north_axis_fields" in vocab:
        lines.append(
            "`north_axis` allowed fields (the schema rejects any extra key): "
            + ", ".join(vocab["north_axis_fields"])
        )
    lines.append("===== END ALLOWED VOCABULARY =====")
    return "\n".join(lines) + "\n\n"


# F-15 B2 (2026-08-07): `_producer_preflight` / `parse.py`'s early-exit raise
# `WindowResolverInputError` with `category="model_draw_error"` for exactly
# two named draw mistakes (deterministic-core-only fields prefilled by the
# model). That exception is a plain `ValueError` subclass, NOT a pydantic
# `ValidationError` — before this fix `_guide` below only recognised
# `ValidationError`, so this specific, already-named, already-stable
# rejection code fell through to `return None` (blind retry) every time,
# despite the retry-guidance channel being fully wired into the loop that
# raises it (`_make_correction_validator` -> `parse_correction_draw` IS the
# `validate` callback `_call_json_llm` retries with). A real run
# (F-15 A1) burned all 3 attempts on the identical
# `producer_segment_ref_prefilled` error because nothing ever told the model
# what to remove. This mapping is the fix: format-only, code-keyed guidance,
# same discipline as the ValidationError branch below (never echoes
# geometry/coordinates/upstream content).
_MODEL_DRAW_ERROR_GUIDANCE: dict[str, str] = {
    "producer_segment_ref_prefilled": (
        "Your previous output was rejected: `facade_segments` and/or a "
        "window's `facade_segment_id` were filled in. These are "
        "deterministic-core-only fields — the core computes them from your "
        "draw AFTER you submit it; they are not legal input from this draw "
        "(they were also removed from the schema shown to you above, so do "
        "not re-add them). Remove the top-level `facade_segments` array "
        "entirely and leave every window's `facade_segment_id` unset (or "
        "`null`). Do NOT change any other coordinate, numeric value, "
        "room/window placement, or upstream reading content that already "
        "passed."
    ),
    "producer_resolver_audit_prefilled": (
        "Your previous output was rejected: one of `corrections` / "
        "`conflicts` / `unsupported` contains a row with `\"kind\": "
        "\"window_host_resolution\"`. That audit trail is produced "
        "downstream by the deterministic core, never by this draw. Remove "
        "any such row. Do NOT change any other coordinate, numeric value, "
        "room/window placement, or upstream reading content that already "
        "passed."
    ),
    # F-15 follow-up (2026-08-07, orchestrator A3): the b2 draw-contract gate
    # in parse.py now raises this SAME exception class/category (previously a
    # plain ValueError, which this guidance channel could never recognise —
    # a real run burned 2 of its 3 attempts blind on this exact rejection
    # before this fix landed).
    "producer_b2_forbidden_field_populated": (
        "Your previous output was rejected: it populated one or more "
        "deterministic-core-only fields (`facade_segments` and/or "
        "`north_axis`). Both are computed downstream by the deterministic "
        "core AFTER you submit your draw — they are not legal input from "
        "this draw (they were also removed from the schema shown to you "
        "above, so do not re-add them). Remove the top-level "
        "`facade_segments` array and leave `north_axis` unset (or `null`). "
        "Do NOT change any other coordinate, numeric value, room/window "
        "placement, or upstream reading content that already passed."
    ),
    # F-16 (2026-08-08, orchestrator interface sweep §6 摊一 Step 2): a
    # window's `floor` name string used to be required, independent input —
    # the SAME floor fact declared twice (once as `floor_id`, once as this
    # name), and the two could disagree (the actual F-16 crash:
    # `floors[0].id="F1"` / `name="Level 1"`, window wrote `floor="F1"`
    # against the id instead of the name). `floor` is now derived from
    # `floor_id` by the core; a draw must leave it unset. Distinct code (and
    # message) from `producer_segment_ref_prefilled`/
    # `producer_b2_forbidden_field_populated` on purpose — reusing either of
    # those would name the WRONG field in the guidance text shown back to
    # the model.
    "producer_window_floor_populated": (
        "Your previous output was rejected: one or more windows had `floor` "
        "filled in. `floor` is derived automatically from `floor_id` (it is "
        "no longer legal input from this draw — it was also removed from "
        "the schema shown to you above, so do not re-add it). Leave every "
        "window's `floor` unset (or `null`); keep `floor_id` pointing at "
        "the correct floor's `id`. Do NOT change any other coordinate, "
        "numeric value, room/window placement, or upstream reading content "
        "that already passed."
    ),
}


def retry_guidance_for_correction(target: Any) -> Callable[[BaseException], str | None]:
    """Build the inner-retry corrective-message callable for ``_call_json_llm`` (F-4a).

    Returns a machine-generated, FORMAT-ONLY correction when the failure is a
    Pydantic schema ``ValidationError``: each error's field path + the
    validator's own reason + the schema's mechanically-derived legal vocabulary
    / field-set for that field. Also returns FORMAT-ONLY, code-keyed guidance
    for the two named ``model_draw_error`` codes raised by
    ``_producer_preflight`` / ``parse.py`` when the draw prefills a
    deterministic-core-only field (F-15 B2, see ``_MODEL_DRAW_ERROR_GUIDANCE``
    above). Returns ``None`` for any other failure (transport, JSON syntax, a
    semantic ``ValueError`` from ``correction_draw_issues``, or an
    ``input_integrity_error``-category ``WindowResolverInputError`` — an
    upstream/environment fault that resampling cannot fix) so those retry
    blind — the inner retry owns ONLY schema/format robustness, never
    geometry, upstream content, gt, or numeric values (run_stage.py:320
    "Inner retry handles ONLY schema/format robustness").

    The message never echoes the rejected draw's coordinates or numbers: only
    field paths, the validator's reason text, and the schema's legal tokens.
    """
    from pydantic import ValidationError

    # Vocab is fixed at build time; the schema model is immutable for a run, so
    # this is a cache, not a staleness risk.
    vocab = correction_schema_vocabulary(target)

    def _guide(exc: BaseException) -> str | None:
        from src.agent.correction.window_sources import WindowResolverInputError

        if isinstance(exc, WindowResolverInputError):
            if exc.category == "model_draw_error":
                return _MODEL_DRAW_ERROR_GUIDANCE.get(exc.code)
            return None
        if not isinstance(exc, ValidationError):
            return None
        lines = [
            "Your previous output was rejected by the schema validator. This is a "
            "FORMAT error only — correct the fields named below and re-emit the "
            "SAME geometry. Do NOT change any coordinate or numeric value, any "
            "room/window placement, or any upstream reading content that already "
            "passed; fix only the schema-format mistakes.",
        ]
        for err in exc.errors():
            loc = err.get("loc", ())
            path = ".".join(str(part) for part in loc)
            reason = err.get("msg", "")
            lines.append(f"- field path: {path}  reason: {reason}")
            if _provenance_value_break(loc) and "provenance_kinds" in vocab:
                lines.append(
                    "  legal `provenance` value (observed/derived/assumed): "
                    + ", ".join(vocab["provenance_kinds"])
                )
            elif "windows" in loc and "provenance" in loc and "window_provenance_keys" in vocab:
                lines.append(
                    "  legal window `provenance` keys (opening-claim vocabulary): "
                    + ", ".join(vocab["window_provenance_keys"])
                )
            elif loc and str(loc[0]) == "north_axis" and "north_axis_fields" in vocab:
                lines.append(
                    "  legal `north_axis` fields: " + ", ".join(vocab["north_axis_fields"])
                )
        return "\n".join(lines)

    return _guide
