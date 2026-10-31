"""A-6a: one-image dimension decisions, frozen batches and explicit reconsideration.

The public decision API accepts candidate IDs, never model coordinates. Reading
witnesses nominate candidates, not facts. A session owns the current batch;
consumers must supply its independently retained expected batch ID. Persistence
is immutable JSON bytes, not a mutable tree of pointers. This is ordinary API
encapsulation, not a defence against Python reflection or modified source code.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from src.agent.correction.config import load_core_tolerances
from src.agent.reading.as_drawn.schema import SCHEMA as PLAN_V2_SCHEMA

# Historical-only branch: the ONLY v0 plan producer is the 2026-08-23
# experiment, never production reading. Keep its bytes readable, not canonical.
HISTORICAL_PLAN_V0_PRODUCER = (
    "AI_agent/logs/experiments/2026-08-23_as_drawn_reading_prototype/tools/as_drawn.py"
)


class TickClaimError(ValueError):
    def __init__(self, code: str, detail: object = None):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def freeze(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def units(mm: object) -> int:
    if isinstance(mm, bool) or not isinstance(mm, (int, float, str, Decimal)):
        raise TickClaimError("VALUE_OFF_DECLARED_GRID", mm)
    number = Decimal(str(mm)) * 10
    if not number.is_finite() or number != number.to_integral_value():
        raise TickClaimError("VALUE_OFF_DECLARED_GRID", mm)
    return int(number)


def require_chain(chain: dict) -> tuple[int, ...]:
    """Positive segment lengths, zero-relative prefix nodes; origin is separate."""
    try:
        values = tuple(units(v) for v in chain["values_mm"])
        nodes = tuple(units(v) for v in chain["cum_mm"])
        overall = units(chain["overall_mm"])
    except (KeyError, TypeError) as exc:
        raise TickClaimError("CHAIN_RECORD_INVALID") from exc
    if not values or len(nodes) != len(values) + 1 or nodes[0] != 0:
        raise TickClaimError("CHAIN_DOMAIN_INVALID")
    if any(v <= 0 for v in values):
        raise TickClaimError("CHAIN_SEGMENT_NOT_POSITIVE")
    if sum(values) != overall or nodes[-1] != overall:
        raise TickClaimError("CHAIN_TOTAL_NOT_CLOSED")
    if any(nodes[i + 1] != nodes[i] + v for i, v in enumerate(values)):
        raise TickClaimError("CALIBRATION_CHAIN_NODE_NOT_PREFIX_SUM")
    return nodes


@dataclass(frozen=True)
class OperandRef:
    """Indices address either nodes or segments; they are never interchangeable."""
    source_sha: str
    chain_id: str
    domain: Literal["node", "segment", "declaration"]
    index: int


@dataclass(frozen=True)
class Expression:
    operation: Literal["node", "anchored_sum", "anchored_diff", "axis_half_wall", "axis_half_span"]
    anchor: OperandRef
    operands: tuple[OperandRef, ...] = ()
    direction: Literal["positive", "negative"] = "positive"
    thickness_kind: Literal["full", "half"] | None = None

    def __post_init__(self):
        # Own the sequence even when ordinary callers supply a mutable list.
        object.__setattr__(self, "operands", tuple(self.operands))


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    expression: Expression
    value_u: int


@dataclass(frozen=True)
class Edge:
    edge_id: str
    axis: str
    raw_u: int
    pointer: str
    witness: bytes
    candidates: tuple[Candidate, ...]
    missing_chains: tuple[str, ...]


class TickChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    edge_id: str
    action: Literal["select", "pixel", "pixel_pending_evidence", "reperceive"]
    candidate_id: str | None = None
    reason: str

    @model_validator(mode="after")
    def choice_shape(self):
        if (self.action == "select") != (self.candidate_id is not None):
            raise ValueError("CHOICE_CANDIDATE_SHAPE")
        if not self.reason.strip():
            raise ValueError("CHOICE_REASON_REQUIRED")
        return self


class TickResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    packet_id: str
    choices: tuple[TickChoice, ...]


@dataclass(frozen=True)
class TickPacket:
    packet_id: str
    image_id: str
    source_sha: str
    generation: int
    edges: tuple[Edge, ...]
    source_bytes: bytes
    supplement_bytes: bytes | None
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class TickBatch:
    batch_id: str
    record: bytes


@dataclass(frozen=True)
class TickFact:
    edge_id: str
    axis: str
    value_u: int
    tier: str
    candidate_id: str | None
    source_sha: str
    batch_id: str
    debt_id: str | None


def require_v2_plan(raw: bytes, *, image_id: str) -> dict:
    """Validate the native source, including the selection this consumer needs.

    The classifier's public decisions stay unchanged. In particular an honest
    empty model selection is a legal reading product, but cannot drive this
    opening workflow. No candidate graph is ever used to invent a selection.
    """
    from src.agent.reading.vector_contract import (
        CONTRACT_AS_DRAWN_PLAN, classify_vector_json,
    )
    from src.agent.correction.evidence_adapters import adapt_as_drawn_plan

    doc = json.loads(raw)
    decision = classify_vector_json(doc)
    if decision.contract_id != CONTRACT_AS_DRAWN_PLAN:
        raise TickClaimError("TICK_PLAN_MALFORMED_DECLARED_CONTRACT", decision.reason)
    hyp = doc["hypotheses"]
    if not hyp.get("pairs") or hyp.get("pairs_status") != "SELECTED":
        raise TickClaimError("TICK_PLAN_MODEL_SELECTION_REQUIRED", hyp.get("pairs_status"))
    # Reuse the existing full graph/reference/five-way disposition gate.
    adapt_as_drawn_plan(raw, input_id=image_id, floor_ref=image_id)
    faces = {f["id"]: f for f in doc["observations"]["face_lines"]}
    candidates = {(p["face_a"], p["face_b"]): p for p in hyp["pair_candidates"]}
    for pair in hyp["pairs"]:
        if ({k: v for k, v in pair.items() if k != "source"}
                != candidates.get((pair["face_a"], pair["face_b"]))):
            raise TickClaimError("TICK_PLAN_SELECTED_PAIR_DRIFT")
    seen = set()
    for opening in hyp["opening_candidates"]:
        key = opening["face_line"], opening["gap_index"]
        gap = faces[key[0]]["gaps"][key[1]]
        if key in seen:
            raise TickClaimError("TICK_PLAN_GAP_DUPLICATED", opening["id"])
        seen.add(key)
        if any(opening[k] != gap[k] for k in ("span_m", "len_m", "len_px", "ink_by_family")):
            raise TickClaimError("TICK_PLAN_OPENING_GAP_DRIFT", opening["id"])
        if not opening["span_m"][0] < opening["span_m"][1]:
            raise TickClaimError("TICK_PLAN_INTERVAL_NOT_ORDERED", opening["id"])
    expected = {(f["id"], i) for f in faces.values() for i in range(len(f["gaps"]))}
    if seen != expected:
        raise TickClaimError("TICK_PLAN_GAP_COVERAGE_MISMATCH")
    if not set(hyp.get("opening_types") or {}) <= {o["id"] for o in hyp["opening_candidates"]}:
        raise TickClaimError("TICK_PLAN_OPENING_TYPE_UNKNOWN_ID")
    return doc


def _native_chains(doc: dict) -> dict[str, dict]:
    """Arithmetic operands from v2's declarations, retaining each chain ID.

    This computes prefix sums, not a reading-format conversion. Neither wall
    pairing nor gap identity participates in this arithmetic.
    """
    try:
        declarations = doc["declarations"]["chains"]
        if not isinstance(declarations, dict) or not declarations:
            raise TickClaimError("TICK_PLAN_DIMENSION_CHAINS_MISSING")
        records = {}
        for cid, ch in declarations.items():
            if (ch["axis"] not in ("row", "col") or type(ch["direction"]) is not int
                    or ch["direction"] not in (-1, 1)):
                raise TickClaimError("CHAIN_FRAME_INVALID", cid)
            vals = ch["values_mm"]
            if not isinstance(vals, list) or any(type(v) not in (int, float) for v in vals):
                raise TickClaimError("CHAIN_RECORD_INVALID", cid)
            nodes = [0]
            for value in vals:
                nodes.append(nodes[-1] + units(value))
            records[cid] = dict(axis="x" if ch["axis"] == "row" else "y",
                                values_mm=vals, cum_mm=[v / 10 for v in nodes],
                                overall_mm=nodes[-1] / 10, origin_mm=ch["world_start_mm"],
                                direction=ch["direction"], qualification="drawing_dimension")
            require_chain(records[cid])
            units(ch["world_start_mm"])
        return records
    except (KeyError, TypeError) as exc:
        raise TickClaimError("TICK_PLAN_DIMENSION_CHAINS_MISSING") from exc


def _chain_records(raw: bytes, supplement: bytes | None) -> dict[str, dict]:
    doc = json.loads(raw)
    native = doc.get("schema") == PLAN_V2_SCHEMA
    records = _native_chains(doc) if native else {}
    calibration = doc["observations"].get("calibration") if native else doc.get("calibration", {})
    if not isinstance(calibration, dict):
        raise TickClaimError("CALIBRATION_MISSING")
    # Check every calibration chain even when a supplemental declaration is used.
    for ch in calibration.values():
        if isinstance(ch, dict) and "values_mm" in ch:
            require_chain(ch)
    if supplement is None:
        return records
    cfg = json.loads(supplement)
    # The reading supplement is explicitly bound to these source bytes and image.
    if (cfg.get("source_sha") != digest(raw) or cfg.get("image") != doc.get("image")
            or cfg.get("schema") != "tick_reading_supplement_v1"):
        raise TickClaimError("SUPPLEMENT_SOURCE_MISMATCH")
    additional = cfg["chains"]
    for cid, ch in additional.items():
        require_chain(ch)
        if ch.get("axis") not in ("x", "y", "z") or ch.get("direction") not in (-1, 1):
            raise TickClaimError("CHAIN_FRAME_INVALID", cid)
        units(ch["origin_mm"])
        if ch.get("qualification") != "drawing_dimension":
            raise TickClaimError("OPERAND_NOT_DECLARED", cid)
        if cid in records and records[cid] != ch:
            raise TickClaimError("SUPPLEMENT_DECLARATION_CONFLICT", cid)
    records = {**records, **additional}
    for axis, cid in cfg.get("primary", {}).items():
        if cid not in records or axis not in calibration:
            raise TickClaimError("PRIMARY_CHAIN_IDENTITY_MISSING", cid)
        original = calibration[axis]
        if (records[cid]["axis"] != axis or
                require_chain(records[cid]) != require_chain(original)):
            raise TickClaimError("PRIMARY_CHAIN_SOURCE_MISMATCH", cid)
    return records


def freeze_prototype_supplement(raw: bytes, config_bytes: bytes) -> bytes:
    """Reading handoff for the existing prototype's explicit dimension config.

    Does not edit either original. Image/facade and primary arrays must agree.
    The config is an additional declared input, not reconstructed from the lossy
    pixel map. Persist its original bytes in the supplement for review.
    """
    doc, cfg = json.loads(raw), json.loads(config_bytes)
    if cfg.get("image") != doc.get("image") or (
        doc.get("facade_label") is not None and cfg.get("view_facade") != doc["facade_label"]
    ):
        raise TickClaimError("SUPPLEMENT_IMAGE_MISMATCH")
    if "chains" not in cfg:
        raise TickClaimError("SUPPLEMENT_CONFIG_MISSING_CHAINS")
    chains = {}
    vertical = "z" if doc["schema"] == "as_drawn_elevation_v0" else "y"
    for cid, ch in cfg["chains"].items():
        vals = ch["values_mm"]
        cumulative = [0]
        for v in vals:
            cumulative.append(cumulative[-1] + units(v))
        chains[cid] = dict(axis="x" if ch["axis"] == "row" else vertical,
                           values_mm=vals, cum_mm=[v / 10 for v in cumulative],
                           overall_mm=cumulative[-1] / 10,
                           origin_mm=ch["world_start_mm"], direction=ch["direction"],
                           qualification="drawing_dimension")
    result = freeze(dict(schema="tick_reading_supplement_v1", source_sha=digest(raw),
                         image=doc["image"], config=json.loads(config_bytes),
                         chains=chains, declarations=[],
                         primary={a: cfg[f"primary_{a}_chain"] for a in ("x", vertical)}))
    _chain_records(raw, result)
    return result


def evaluate(expression: Expression, *, raw: bytes, supplement: bytes | None, axis: str) -> int:
    """All results are positions in the source image's declared local axis frame.

    Sum/diff first produce displacement, then explicitly add/subtract at anchor.
    The sign never comes from an opening edge's low/high role.
    """
    return _evaluate(expression, axis=axis, context=_expression_context(raw, supplement))


def _expression_context(raw: bytes, supplement: bytes | None) -> tuple:
    return (_chain_records(raw, supplement),
            json.loads(supplement) if supplement is not None else {},
            json.loads(raw), digest(raw))


def _evaluate(expression: Expression, *, axis: str, context: tuple) -> int:
    chains, cfg, doc, source_sha = context
    if type(expression) is not Expression or expression.direction not in ("positive", "negative"):
        raise TickClaimError("OPERATION_SIGNATURE_INVALID")

    def resolve(ref: OperandRef, domain: str) -> tuple[int, dict]:
        if type(ref) is not OperandRef or ref.source_sha != source_sha:
            raise TickClaimError("OPERAND_CROSS_IMAGE")
        if ref.domain != domain or type(ref.index) is not int or ref.index < 0:
            raise TickClaimError("OPERAND_REF_DOMAIN", domain)
        if domain == "declaration":
            if ref.chain_id == "/declarations/thickness_callouts_mm":
                if doc.get("schema") != PLAN_V2_SCHEMA:
                    raise TickClaimError("OPERAND_REF_DOMAIN")
                try:
                    value = doc["declarations"]["thickness_callouts_mm"][ref.index]
                except (KeyError, IndexError, TypeError) as exc:
                    raise TickClaimError("OPERAND_REF_MISSING") from exc
                return units(value), dict(kind="full", quantity="wall_thickness")
            if ref.chain_id != "declarations":
                raise TickClaimError("OPERAND_REF_DOMAIN")
            try:
                decl = cfg["declarations"][ref.index]
            except (IndexError, KeyError) as exc:
                raise TickClaimError("OPERAND_REF_MISSING") from exc
            if (decl.get("qualification") != "drawing_dimension" or decl.get("axis") != axis
                    or not decl.get("callout_id")):
                raise TickClaimError("OPERAND_NOT_DECLARED")
            return units(decl["value_mm"]), decl
        try:
            chain = chains[ref.chain_id]
            if chain["axis"] != axis:
                raise TickClaimError("OPERAND_FRAME_MISMATCH")
            values = require_chain(chain) if domain == "node" else tuple(units(v) for v in chain["values_mm"])
            value = values[ref.index]
        except (KeyError, IndexError) as exc:
            raise TickClaimError("OPERAND_REF_MISSING") from exc
        if domain == "node":
            value = units(chain["origin_mm"]) + chain["direction"] * value
        return value, chain

    anchor, _ = resolve(expression.anchor, "node")
    op, operands = expression.operation, expression.operands
    sign = 1 if expression.direction == "positive" else -1
    if op == "node":
        if operands or expression.thickness_kind is not None or sign != 1:
            raise TickClaimError("OPERATION_SIGNATURE_INVALID")
        return anchor
    if op == "axis_half_wall":
        if len(operands) != 1 or expression.thickness_kind not in ("full", "half"):
            raise TickClaimError("OPERATION_SIGNATURE_INVALID")
        value, decl = resolve(operands[0], "declaration")
        if (decl.get("kind") != expression.thickness_kind or value <= 0
                or decl.get("quantity") != "wall_thickness"):
            raise TickClaimError("THICKNESS_DECLARATION_MISMATCH")
        if expression.thickness_kind == "full":
            if value % 2:
                raise TickClaimError("WALL_THICKNESS_HALF_UNGRID")
            value //= 2
        return anchor + sign * value
    if expression.thickness_kind is not None:
        raise TickClaimError("OPERATION_SIGNATURE_INVALID")
    if op == "anchored_sum":
        if not operands:
            raise TickClaimError("OPERATION_SIGNATURE_INVALID")
        if (len({r.chain_id for r in operands}) != 1 or
                [r.index for r in operands] != list(range(operands[0].index, operands[0].index + len(operands)))):
            raise TickClaimError("SEGMENTS_NOT_CONTIGUOUS")
        displacement = sum(resolve(r, "segment")[0] for r in operands)
    elif op in ("anchored_diff", "axis_half_span"):
        if len(operands) != 2 or operands[0].chain_id != operands[1].chain_id:
            raise TickClaimError("OPERATION_SIGNATURE_INVALID")
        lo, _ = resolve(operands[0], "node")
        hi, _ = resolve(operands[1], "node")
        displacement = hi - lo
        if op == "axis_half_span":
            if displacement % 2:
                raise TickClaimError("CHAIN_HALF_SPAN_UNGRID")
            displacement //= 2
    else:
        raise TickClaimError("OPERATION_SIGNATURE_INVALID")
    return anchor + sign * displacement


def _raw_edges(doc: dict):
    """The frozen source, not a caller-supplied list, defines the entire scope."""
    if doc.get("schema") == "as_drawn_elevation_v0":
        seen = set()
        for i, opening in enumerate(doc["openings"]):
            oid = opening["id"]
            if oid in seen:
                raise TickClaimError("OPENING_ID_DUPLICATE", oid)
            seen.add(oid)
            for axis, names in (("x", ("x0", "x1")), ("z", ("z_low", "z_high"))):
                for j, name in enumerate(names):
                    yield f"{oid}:{name}", axis, opening[f"{axis}_range_m"][j], f"/openings/{i}/{axis}_range_m/{j}", opening.get("edge_witnesses", {}).get(name, {})
    elif doc.get("schema") == PLAN_V2_SCHEMA:
        faces = {f["id"]: f for f in doc["observations"]["face_lines"]}
        for i, opening in enumerate(doc["hypotheses"]["opening_candidates"]):
            face = faces[opening["face_line"]]
            axis = "y" if face["constant_world_axis"] == "x" else "x"
            for j, role in enumerate(("lo", "hi")):
                witness = dict(face_line=opening["face_line"], gap_index=opening["gap_index"],
                               ink_by_family=opening["ink_by_family"],
                               dimension_witnesses=doc["observations"].get("dimension_witnesses"))
                yield (f"{opening['id']}:{role}", axis, opening["span_m"][j],
                       f"/hypotheses/opening_candidates/{i}/span_m/{j}", witness)
    elif doc.get("schema") == "as_drawn_plan_v0":
        # Historical-only producer registered by HISTORICAL_PLAN_V0_PRODUCER.
        seen = set()
        for i, band in enumerate(doc["wall_bands"]):
            if band["id"] in seen:
                raise TickClaimError("OPENING_ID_DUPLICATE", band["id"])
            seen.add(band["id"])
            axis = "y" if band["constant_world_axis"] == "x" else "x"
            for k, opening in enumerate(band["opening_runs"]):
                for j, role in enumerate(("lo", "hi")):
                    yield f"{band['id']}:run{k}:{role}", axis, opening["run_m"][j], f"/wall_bands/{i}/opening_runs/{k}/run_m/{j}", opening.get("edge_witnesses", {}).get(role, {})
    else:
        raise TickClaimError("TICK_SOURCE_CONTRACT_UNSUPPORTED")


def build_packet(raw: bytes, *, image_id: str, generation: int,
                 supplement: bytes | None = None,
                 expressions: tuple[tuple[str, Expression], ...] = ()) -> TickPacket:
    raw = bytes(raw)
    supplement = bytes(supplement) if supplement is not None else None
    if json.loads(raw).get("schema") == PLAN_V2_SCHEMA:
        require_v2_plan(raw, image_id=image_id)
    chains = _chain_records(raw, supplement)
    source_sha = digest(raw)
    context = (chains, json.loads(supplement) if supplement is not None else {},
               json.loads(raw), source_sha)
    edges = []
    extra = {}
    for eid, expr in expressions:
        extra.setdefault(eid, []).append(expr)
    for eid, axis, measured_m, pointer, witness in _raw_edges(json.loads(raw)):
        candidates = []
        refs = witness.get("dimension_refs", [])
        named = {}
        for ref in refs:
            match = re.fullmatch(r"(.+)_s([1-9][0-9]*)", ref)
            if not match:
                raise TickClaimError("WITNESS_REFERENCE_INVALID", ref)
            named.setdefault(match[1], set()).add(int(match[2]))
        missing = tuple(sorted(cid for cid in named if cid not in chains))
        # Preserve chain identity. No use of dimension_witnesses' lossy value map.
        proposed = []
        for cid, segments in sorted(named.items()):
            if cid not in chains:
                continue
            chain = chains[cid]
            if chain["axis"] != axis:
                raise TickClaimError("WITNESS_CHAIN_FRAME_MISMATCH", eid)
            n = len(chain["values_mm"])
            if any(s > n for s in segments):
                raise TickClaimError("WITNESS_SEGMENT_MISSING", eid)
            indices = {s for s in segments if s + 1 in segments}
            if segments == {1}:
                indices.add(0)  # candidate only; NOT an automatic origin claim
            if segments == {n}:
                indices.add(n)
            # The nominated boundary is only a hint. Let the model choose any
            # node of that same declared chain, including a corrected identity.
            indices.update(range(n + 1))
            for index in sorted(indices):
                proposed.append(Expression("node", OperandRef(source_sha, cid, "node", index)))
        # Source without witnesses still offers its declared nodes for model review.
        if not named:
            for cid, chain in sorted(chains.items()):
                if chain["axis"] == axis:
                    proposed.extend(Expression("node", OperandRef(source_sha, cid, "node", i))
                                    for i in range(len(chain["cum_mm"])))
        proposed.extend(extra.pop(eid, []))
        unique = {}
        for expr in proposed:
            value = _evaluate(expr, axis=axis, context=context)
            cid = digest(freeze({"edge": eid, "expression": asdict(expr)}))
            unique[cid] = Candidate(cid, expr, value)
        candidates = tuple(unique[k] for k in sorted(unique))
        edges.append(Edge(eid, axis, units(Decimal(str(measured_m)) * 1000), pointer,
                          freeze(witness), candidates, missing))
    if extra:
        raise TickClaimError("EXPRESSION_EDGE_UNKNOWN", sorted(extra))
    if len({e.edge_id for e in edges}) != len(edges):
        raise TickClaimError("EDGE_ID_DUPLICATE")
    # Include source, config, full edge universe and all proposed expressions.
    identity = dict(image_id=image_id, generation=generation, source_sha=source_sha,
                    supplement_sha=digest(supplement) if supplement else None,
                    edges=[dict(edge=e.edge_id, candidates=[c.candidate_id for c in e.candidates]) for e in edges])
    return TickPacket(digest(freeze(identity)), image_id, source_sha, generation,
                      tuple(edges), raw, supplement, ("SAME_IMAGE_MODEL_REQUIRED",) if edges else ())


def _require_ordered_intervals(rows: list[dict]) -> None:
    by_id = {r["edge_id"]: r for r in rows}
    for eid, row in by_id.items():
        for low, high in ((":x0", ":x1"), (":z_low", ":z_high"), (":lo", ":hi")):
            if eid.endswith(low):
                if row["value_u"] >= by_id[eid[:-len(low)] + high]["value_u"]:
                    raise TickClaimError("TICK_INTERVAL_NOT_ORDERED", eid)
                break


class TickSession:
    """Owner of the current per-image generation; all cross-image reads pass here."""
    def __init__(self, raw: bytes, *, image_id: str, supplement: bytes | None = None,
                 expressions: tuple[tuple[str, Expression], ...] = (), max_rounds: int = 3):
        if max_rounds < 1:
            raise TickClaimError("ROUND_BUDGET_INVALID")
        self._generation = 0
        self._max_rounds = max_rounds
        self._packet = build_packet(raw, image_id=image_id, generation=0,
                                    supplement=supplement, expressions=expressions)
        self._current: TickBatch | None = None
        self._history: list[bytes] = []
        self._previous_debts: dict[str, tuple[str, str]] = {}
        self._evidence_bytes: bytes | None = None
        self._blocked: str | None = None
        self._precision_u = units(Decimal(str(load_core_tolerances().output_precision_m)) * 1000)
        if self._precision_u <= 0:
            raise TickClaimError("OUTPUT_PRECISION_INVALID")

    @classmethod
    def from_artifact(cls, artifact, *, supplement: bytes | None = None,
                      expressions: tuple[tuple[str, Expression], ...] = (), max_rounds: int = 3):
        """Production identity comes from the checked bundle, never a hand echo."""
        from src.agent.correction.evidence_contract import validate_evidence_bundle
        validate_evidence_bundle(artifact)
        if len(artifact.bundle.source_artifacts) != 1 or len(artifact.frozen_sources) != 1:
            raise TickClaimError("TICK_SINGLE_IMAGE_ARTIFACT_REQUIRED")
        meta = artifact.bundle.source_artifacts[0]
        source = artifact.frozen_sources[0]
        if source.artifact != meta:
            raise TickClaimError("TICK_ARTIFACT_SOURCE_MISMATCH")
        session = cls(source.raw_bytes, image_id=meta.input_id, supplement=supplement,
                      expressions=expressions, max_rounds=max_rounds)
        session._evidence_bytes = artifact.model_dump_json().encode()
        return session

    def evidence_artifact(self):
        """Return a fresh validated snapshot, not caller-owned mutable lists."""
        from src.agent.correction.evidence_contract import (
            CorrectionEvidenceBundleArtifactV1, validate_evidence_bundle,
        )
        if self._evidence_bytes is None:
            raise TickClaimError("TICK_SOURCE_ARTIFACT_REQUIRED")
        artifact = CorrectionEvidenceBundleArtifactV1.model_validate_json(self._evidence_bytes)
        validate_evidence_bundle(artifact)
        meta = artifact.bundle.source_artifacts[0]
        if meta.input_id != self.packet.image_id or meta.source_output_sha256 != digest(self.packet.source_bytes):
            raise TickClaimError("TICK_ARTIFACT_SOURCE_MISMATCH")
        return artifact

    def persist(self, directory: Path, expected_batch_id: str) -> None:
        """Immutable audit archive; it grants no current-session authority.

        Both original byte channels and the exact batch record travel together.
        A missing supplement is explicitly declared, never replaced with a
        manufactured reading. Write the manifest last so a partial write is loud.
        """
        self.consume(expected_batch_id)
        packet = self.packet
        files = {
            "source.bin": packet.source_bytes,
            "batch.json": self._current.record,
            "packet.json": freeze(dict(
                packet_id=packet.packet_id, image_id=packet.image_id, generation=packet.generation,
                expressions=[(e.edge_id, asdict(c.expression)) for e in packet.edges for c in e.candidates])),
            "history.json": freeze([r.decode() for r in self.history]),
        }
        if packet.supplement_bytes is not None:
            files["supplement.bin"] = packet.supplement_bytes
        manifest = freeze(dict(schema="tick_archive_v1", batch_id=expected_batch_id,
                               supplement_present=packet.supplement_bytes is not None,
                               files={name: digest(raw) for name, raw in files.items()}))
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, raw in {**files, "manifest.json": manifest}.items():
            path = directory / name
            if path.exists():
                if path.read_bytes() != raw:
                    raise TickClaimError("TICK_ARCHIVE_IMMUTABLE", name)
            else:
                with path.open("xb") as stream:
                    stream.write(raw)
        verify_tick_archive(directory, expected_batch_id=expected_batch_id)

    def elevation_document(self, expected_batch_id: str) -> dict:
        facts = {f.edge_id: f for f in self.consume(expected_batch_id)}
        doc = json.loads(self.packet.source_bytes)
        if doc["schema"] != "as_drawn_elevation_v0":
            raise TickClaimError("ELEVATION_TICK_SOURCE_REQUIRED")
        for opening in doc["openings"]:
            for axis, names in (("x", ("x0", "x1")), ("z", ("z_low", "z_high"))):
                values = [facts[f"{opening['id']}:{n}"].value_u for n in names]
                # A-6-d1: the same consumer-side lo<hi guard as the plan side.
                if values[0] >= values[1]:
                    raise TickClaimError("TICK_ELEVATION_INTERVAL_NOT_ORDERED", opening["id"])
                opening[f"{axis}_range_m"] = [v / 10000 for v in values]
        return doc

    @property
    def packet(self) -> TickPacket:
        return self._packet

    @property
    def history(self) -> tuple[bytes, ...]:
        return tuple(self._history)

    def _checked_choices(self, response: TickResponse) -> dict[str, TickChoice]:
        """Same decision validation on submission and consumption; no state writes."""
        if type(response) is not TickResponse or response.packet_id != self._packet.packet_id:
            raise TickClaimError("STALE_TICK_RESPONSE")
        response = TickResponse.model_validate(response.model_dump(mode="python"))
        choices = {c.edge_id: c for c in response.choices}
        if (len(choices) != len(response.choices) or
                set(choices) != {e.edge_id for e in self._packet.edges}):
            raise TickClaimError("TICK_DECISION_COVERAGE_MISMATCH")
        if any(c.action == "reperceive" for c in choices.values()):
            raise TickClaimError("RETURN_TO_READING")
        return choices

    def _decision_rows(self, choices: dict[str, TickChoice],
                       previous_debts: dict[str, tuple[str, str]]) -> list[dict]:
        """Rebuild all row fields from the packet and validated model choices."""
        rows = []
        context = _expression_context(self._packet.source_bytes, self._packet.supplement_bytes)
        for edge in self._packet.edges:
            choice = choices[edge.edge_id]
            candidates = {c.candidate_id: c for c in edge.candidates}
            candidate = candidates.get(choice.candidate_id)
            debt = None
            if choice.action == "select":
                if candidate is None:
                    raise TickClaimError("UNKNOWN_TICK_CANDIDATE", edge.edge_id)
                value = _evaluate(candidate.expression, axis=edge.axis, context=context)
                tier = "chain_backed"
            else:
                if choice.action == "pixel_pending_evidence":
                    if not edge.missing_chains:
                        raise TickClaimError("EVIDENCE_DEBT_WITHOUT_MISSING_SOURCE")
                    debt = digest(freeze(dict(image=self._packet.image_id, edge=edge.edge_id,
                                             missing=edge.missing_chains)))
                value = int((Decimal(edge.raw_u) / self._precision_u).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * self._precision_u
                tier = "pixel_only"
            rows.append(dict(edge_id=edge.edge_id, axis=edge.axis, value_u=value, tier=tier,
                             pointer=edge.pointer, witness=json.loads(edge.witness),
                             candidate=asdict(candidate) if candidate else None,
                             choice=choice.model_dump(), debt_id=debt,
                             retired_debt_id=previous_debts[edge.edge_id][0]
                             if (tier == "chain_backed" and not edge.missing_chains
                                 and edge.edge_id in previous_debts
                                 and previous_debts[edge.edge_id][1] == self._packet.source_sha) else None))
        return rows

    def _retirement_context(self, current_record: bytes) -> dict[str, tuple[str, str]]:
        """Read the ledger before this commit; do not retire a debt a second time.

        submit removes retired entries from _previous_debts. Replaying that
        mutable post-commit map would wrongly reject a legitimate retirement.
        Only return events make a prior batch's debts available for retirement.
        """
        debts, last = {}, None
        for raw in self._history:
            if raw == current_record:
                break
            entry = json.loads(raw)
            if entry.get("schema") == "tick_batch_v1":
                last = entry
                for row in entry["rows"]:
                    if row["retired_debt_id"]:
                        debts.pop(row["edge_id"], None)
            elif (entry.get("event") == "RETURN_TO_STEP_ONE" and last is not None
                  and entry.get("invalidated") == digest(freeze(last))):
                debts.update({r["edge_id"]: (r["debt_id"], last["source_sha"])
                              for r in last["rows"] if r["debt_id"]})
        return debts

    def submit(self, response: TickResponse) -> TickBatch:
        if self._blocked:
            raise TickClaimError(self._blocked)
        if type(response) is not TickResponse or response.packet_id != self._packet.packet_id:
            raise TickClaimError("STALE_TICK_RESPONSE")
        if self._current is not None:
            raise TickClaimError("BATCH_ALREADY_DECIDED_USE_RECONSIDER")
        rows = self._decision_rows(self._checked_choices(response), self._previous_debts)
        try:
            _require_ordered_intervals(rows)
        except TickClaimError as exc:
            self.reconsider("INTERVAL_NOT_ORDERED")
            raise TickClaimError("RETURN_TO_STEP_ONE_INTERVAL", exc.detail) from exc
        record = freeze(dict(schema="tick_batch_v1", packet_id=response.packet_id,
                             source_sha=self._packet.source_sha, image_id=self._packet.image_id,
                             generation=self._generation, response=response.model_dump(),
                             output_precision=dict(config_field="output_precision_m", units=self._precision_u),
                             rows=rows))
        self._current = TickBatch(digest(record), record)
        self._history.append(record)
        for row in rows:
            if row["retired_debt_id"]:
                self._previous_debts.pop(row["edge_id"], None)
        return self._current

    def consume(self, expected_batch_id: str, batch: TickBatch | None = None) -> tuple[TickFact, ...]:
        current = self._current
        if current is None or current.batch_id != expected_batch_id:
            raise TickClaimError("TICK_BATCH_INVALIDATED")
        supplied = current if batch is None else batch
        if type(supplied) is not TickBatch or supplied.batch_id != expected_batch_id or supplied.record != current.record or digest(supplied.record) != expected_batch_id:
            raise TickClaimError("TICK_BATCH_NOT_CURRENT_DECISION")
        if self._blocked:
            raise TickClaimError(self._blocked)
        try:
            record = json.loads(supplied.record)
            rows = record["rows"]
            row_ids = [r["edge_id"] for r in rows]
            if type(rows) is not list or any(type(eid) is not str for eid in row_ids):
                raise TypeError("rows must be a list of identified records")
        except (ValueError, KeyError, TypeError) as exc:
            raise TickClaimError("TICK_BATCH_RECORD_INVALID") from exc
        if record.get("packet_id") != self._packet.packet_id or record.get("source_sha") != digest(self._packet.source_bytes):
            raise TickClaimError("TICK_BATCH_SOURCE_MISMATCH")
        metadata = dict(schema="tick_batch_v1", image_id=self._packet.image_id,
                        generation=self._packet.generation,
                        output_precision=dict(config_field="output_precision_m", units=self._precision_u))
        if freeze({k: record.get(k) for k in metadata}) != freeze(metadata):
            raise TickClaimError("TICK_BATCH_METADATA_MISMATCH")
        edges = {e.edge_id: e for e in self._packet.edges}
        if len(rows) != len(edges) or set(row_ids) != set(edges):
            raise TickClaimError("TICK_DECISION_COVERAGE_MISMATCH")
        try:
            row_response = TickResponse.model_validate_json(freeze(dict(
                packet_id=record["packet_id"], choices=[r["choice"] for r in rows])))
            response = TickResponse.model_validate_json(freeze(record["response"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise TickClaimError("TICK_BATCH_RESPONSE_INVALID") from exc
        row_choices = self._checked_choices(row_response)
        rebuilt = self._decision_rows(row_choices, self._retirement_context(supplied.record))
        by_id = {r["edge_id"]: r for r in rows}
        for row in rebuilt:
            actual = by_id[row["edge_id"]]
            if freeze(actual.get("value_u")) != freeze(row["value_u"]):
                raise TickClaimError("TICK_VALUE_RECOMPUTE_MISMATCH")
            if freeze(actual) != freeze(row):
                raise TickClaimError("TICK_ROW_RECOMPUTE_MISMATCH", row["edge_id"])
        _require_ordered_intervals(rebuilt)
        choices = self._checked_choices(response)
        if freeze({k: v.model_dump() for k, v in choices.items()}) != freeze(
                {k: v.model_dump() for k, v in row_choices.items()}):
            raise TickClaimError("TICK_BATCH_RESPONSE_MISMATCH")
        if set(record) != {"packet_id", "source_sha", "response", "rows", *metadata}:
            raise TickClaimError("TICK_BATCH_RECORD_INVALID")
        return tuple(TickFact(r["edge_id"], r["axis"], r["value_u"], r["tier"],
                              r["choice"]["candidate_id"], record["source_sha"], expected_batch_id,
                              r["debt_id"]) for r in rebuilt)

    def reconsider(self, reason: str, *, raw: bytes | None = None,
                   supplement: bytes | None = None,
                   expressions: tuple[tuple[str, Expression], ...] = ()) -> TickPacket:
        """Named return to step one, owned by correction; old consumers go stale.

        Reading supplies evidence; correction calls this and asks its same-image
        model again. Debts retire only after the new matching edge is chain-backed.
        """
        if not reason.strip():
            raise TickClaimError("RECONSIDERATION_REASON_REQUIRED")
        if self._current:
            self._previous_debts.update({r["edge_id"]: (r["debt_id"], self._packet.source_sha) for r in json.loads(self._current.record)["rows"] if r["debt_id"]})
        self._history.append(freeze(dict(event="RETURN_TO_STEP_ONE", reason=reason,
                                        invalidated=self._current.batch_id if self._current else None)))
        self._current = None  # invalidate BEFORE any possible retry failure
        self._blocked = "REGISTER_PENDING_READING_INPUT"
        self._generation += 1
        if self._generation >= self._max_rounds:
            self._blocked = "REGISTER_PENDING_ROUND_LIMIT"
            raise TickClaimError("REGISTER_PENDING_ROUND_LIMIT")
        old = self._packet
        new_raw = old.source_bytes if raw is None else raw
        if json.loads(new_raw).get("image") != json.loads(old.source_bytes).get("image"):
            raise TickClaimError("SUPPLEMENT_IMAGE_MISMATCH")
        new_supplement = old.supplement_bytes if supplement is None else supplement
        if not expressions and new_raw == old.source_bytes:
            expressions = tuple((e.edge_id, c.expression) for e in old.edges for c in e.candidates)
        self._packet = build_packet(new_raw, image_id=old.image_id, generation=self._generation,
                                    supplement=new_supplement, expressions=expressions)
        self._blocked = None
        return self._packet


def verify_tick_archive(directory: Path, *, expected_batch_id: str) -> TickBatch:
    """Rebuild packet and decision bytes from disk, returning audit data ONLY.

    No loader returns a live TickSession: yesterday's self-consistent archive
    cannot make an invalidated batch current in a geometry assembly.
    """
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        required = {"source.bin", "batch.json", "packet.json", "history.json"}
        if manifest.get("supplement_present") is True:
            required.add("supplement.bin")
        elif manifest.get("supplement_present") is not False:
            raise TickClaimError("TICK_ARCHIVE_MANIFEST_INVALID")
        if (manifest.get("schema") != "tick_archive_v1" or manifest.get("batch_id") != expected_batch_id
                or set(manifest["files"]) != required):
            raise TickClaimError("TICK_ARCHIVE_MANIFEST_INVALID")
        files = {name: (directory / name).read_bytes() for name in required}
        if any(digest(raw) != manifest["files"][name] for name, raw in files.items()):
            raise TickClaimError("TICK_ARCHIVE_BYTES_MISMATCH")
        packet = json.loads(files["packet.json"])
        expressions = tuple((eid, Expression(
            operation=e["operation"], anchor=OperandRef(**e["anchor"]),
            operands=tuple(OperandRef(**r) for r in e["operands"]),
            direction=e["direction"], thickness_kind=e["thickness_kind"]))
            for eid, e in packet["expressions"])
        session = TickSession(files["source.bin"], image_id=packet["image_id"],
                              supplement=files.get("supplement.bin"), expressions=expressions)
        session._generation = packet["generation"]
        session._packet = build_packet(files["source.bin"], image_id=packet["image_id"],
                                       generation=packet["generation"],
                                       supplement=files.get("supplement.bin"), expressions=expressions)
        if session.packet.packet_id != packet["packet_id"]:
            raise TickClaimError("TICK_ARCHIVE_PACKET_MISMATCH")
        session._current = TickBatch(digest(files["batch.json"]), files["batch.json"])
        session._history = [r.encode() for r in json.loads(files["history.json"])]
        session.consume(expected_batch_id)
        return session._current
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TickClaimError("TICK_ARCHIVE_INCOMPLETE") from exc
