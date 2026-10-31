"""Per-FILE contract discriminator for the 0_reading vector directory (F-97).

``src/agent/reading/contract.py`` recognizes the reading-product **envelope**
(``{"views": {...}}``).  This module answers a different question, one file at a
time: *given one parsed JSON found in a run's ``0_reading/``, which declared
contract is it, and may 1_correction consume it?*

Why this exists
---------------
``discover_vector_files`` sorts ``0_reading/*.json`` into plans / elevations /
**others**, and the correction prompt pasted every one of them in verbatim.  A
JSON of any shape whatsoever therefore reached the model as untyped text without
passing a single reading gate (F-97).  ⛔ The filename regex is an ORDERING key,
never a contract: ``1f_view_checks.json`` is a check report, not a reading view.

Discipline this module follows
------------------------------
1. ⭐ **The as-drawn schema value is imported from its producer**, never copied.
   ``as_drawn_v2.SCHEMA`` is the one place that string is defined; a literal here
   would be a second definition that silently stops matching the day the
   producer's value changes.  Same lesson as ``judge/as_drawn/denominator.py:14``
   ("⛔ never from a second re-implementation ... a check whose recomputation
   does not mirror the producer's definition measures the difference between two
   opinions").
2. ⭐ **A contract is a (declared schema value × required key set) PAIR**, not a
   schema value alone.  ``as_drawn_plan_v2`` is emitted by two different
   producers with two different shapes (the reading product, and the as-drawn
   check report from ``validator/checks/as_drawn.py``), so keying on the value
   alone would name one of them the other.
3. ⭐ **Every contract is recognized by its producer's own TYPE**, never by a key
   list induced from existing artifacts.  Legacy views use
   ``reading/schema.py:ReadingView`` plus the field correction actually eats,
   ``strokes`` (the prompt cites stroke ids and counts ``pen == "window"``);
   check-report sidecars use ``validator/checks/schema.py:CheckReport``.
   ⚠️ Both models default every field, so they validate ``{}`` — "parses"
   alone would recognize everything, hence the explicit-key conjuncts.
4. ⭐ **Every detector is evaluated; first-match-wins is forbidden.**  Two
   matches is an ambiguity to report, not a race for the detector that ran first.
5. ⭐ **Structural fallback is only for the undeclared.**  Legacy recognition
   exists for artifacts written before ``schema`` did.  A file that declares a
   ``schema`` **at all** is unknown unless the declaration lands on a registered
   contract *including that contract's key set* — even when the file still looks
   like a reading view.  Otherwise any future contract that kept a ``strokes``
   list would be silently consumed as a 2026-06 view, which is F-97 reopened in
   a new shape.
   ⚠️ "Declares something this module cannot honour" has **three** shapes and
   all three are unknown: an unregistered value, a *registered* value whose
   required keys are absent (BLK-A), and a non-string value (BLK-C).  A file
   declaring a registered value **and** satisfying that contract's key set
   **and** matching legacy structure is a genuine double match and still goes to
   #4's ambiguity path — ⛔ collapsing that one into a single verdict is the
   failure mode this very fix nearly caused, twice.
6. ⭐ **Classifying a file is never allowed to raise.**  Every entry point here
   is upstream of the consumption ledger, so an exception escaping this module
   destroys the record that was supposed to name the offender — the ledger is
   then missing precisely for the runs it exists to explain.  ⛔ The defence is a
   boundary, not an enumeration of exception types (an enumeration can never be
   finished): unreadable path, undecodable bytes and unparsable text each get a
   specific named row, and anything else at all is caught by a last-resort net
   that still produces a named ``unknown/error`` row plus an offender.  Loud and
   on the books either way.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from src.agent.reading.as_drawn.as_drawn_v2 import SCHEMA as AS_DRAWN_PLAN_SCHEMA
from src.agent.reading.schema import ReadingView

# Historical as-drawn prototype values.  ⚠️ Registered as LITERALS on purpose:
# their only producer is the 2026-08-23 prototype under `AI_agent/logs/
# experiments/`, which is not importable production code.  ⛔ Do not "fix" this
# by importing experiment tooling.  Anything with a live producer imports its
# constant instead — see AS_DRAWN_PLAN_SCHEMA above.
AS_DRAWN_PLAN_V0_SCHEMA = "as_drawn_plan_v0"
AS_DRAWN_ELEVATION_V0_SCHEMA = "as_drawn_elevation_v0"

CONTRACT_READING_VIEW_LEGACY = "reading_view_legacy"
CONTRACT_AS_DRAWN_PLAN = "as_drawn_plan"
CONTRACT_AS_DRAWN_PLAN_V0 = "as_drawn_plan_v0"
CONTRACT_AS_DRAWN_ELEVATION_V0 = "as_drawn_elevation_v0"
CONTRACT_STAGE_CHECK_REPORT = "stage_check_report"
CONTRACT_UNKNOWN = "unknown"


class Disposition(str, Enum):
    """What 1_correction does with a file of this contract."""

    CONSUME = "consume"
    """Pasted into the correction prompt (today: legacy reading views only)."""

    ADAPT = "adapt"
    """Wired to a correction evidence adapter (module 7): the FROZEN BYTES
    travel through adapt_* → bundle → compiler → decision loop, ⛔ never
    into the pasted-JSON prompt.  ⭐ The four-value set is a deliberate
    transition state: ``CONSUME`` dies with the legacy pasted-JSON leg
    (plan.md ④), at which point the target three-value set
    ``ADAPT / KNOWN_NOT_ADAPTED / EXCLUDE`` of the approved design is
    reached by RENAMING, not by this module growing a fifth behaviour."""

    KNOWN_NOT_CONSUMED = "known_not_consumed"
    """Recognized contract that this stage has no wire for ⇒ loud failure.
    ⭐ Deliberately distinct from ``unknown``: when the reading/correction
    unification lands, "not wired yet" and "never heard of it" must stay
    tellable apart."""

    EXCLUDE = "exclude"
    """Declared non-input that lives in the same directory ⇒ dropped from the
    prompt and NAMED in the consumption ledger.  ⛔ Not a silent skip (F-64 is
    "nobody can tell it happened"); the ledger names every excluded file."""


@dataclass(frozen=True)
class ContractSpec:
    contract_id: str
    disposition: Disposition
    detect: Callable[[dict], bool]
    describe: str


def _has_keys(raw: dict, *keys: str) -> bool:
    return all(k in raw for k in keys)


def _is_declared(raw: dict, schema_value: str) -> bool:
    return raw.get("schema") == schema_value


# Every schema value this module registers. A file declaring a value OUTSIDE
# this set is unknown by construction -- ⛔ it must never fall back to structural
# legacy recognition (B-01).
DECLARED_SCHEMA_VALUES: frozenset[str] = frozenset(
    {AS_DRAWN_PLAN_SCHEMA, AS_DRAWN_PLAN_V0_SCHEMA, AS_DRAWN_ELEVATION_V0_SCHEMA}
)


def _declares_unregistered_schema(raw: dict) -> bool:
    """True when the file declares a ``schema`` no registered contract answers to.

    ⭐ BLK-C: the ``isinstance`` guard is load-bearing, ⛔ not defensive noise.
    JSON allows ``"schema": []`` / ``{}``; those are unhashable, so the plain
    ``... not in DECLARED_SCHEMA_VALUES`` raised ``TypeError: unhashable type``
    from inside the ledger writer — the one function whose whole job is to
    record the offender was itself the thing that died on it.

    A non-string declaration is a *malformed declaration*, and that is two facts
    at once: the file **did** declare (⛔ so no structural legacy fallback,
    discipline #5) and the declaration matches nothing (so: unknown).
    """
    if "schema" not in raw:
        return False
    declared = raw.get("schema")
    if not isinstance(declared, str):
        return True
    return declared not in DECLARED_SCHEMA_VALUES


def _detect_legacy_reading_view(raw: dict) -> bool:
    """Legacy views are the ONE contract that predates explicit declaration.

    ⭐ B-01: a file declaring an UNREGISTERED top-level `schema` is never legacy
    by structure. Legacy recognition is the fallback for artifacts written
    before the field existed; letting a declared-but-unregistered contract fall
    back here just because it still carries `strokes` would re-open F-97's
    silent channel in a new shape -- any future contract keeping a `strokes`
    list would be consumed as if it were a 2026-06 reading view.

    ⚠️ Only UNREGISTERED declarations are rejected here. A file declaring a
    registered contract AND matching legacy structure must still reach the
    two-match path so it is reported as AMBIGUOUS -- collapsing it to a single
    verdict here would silently pick a winner, which is what ⛔ first-match-wins
    forbids.
    """
    if _declares_unregistered_schema(raw):
        return False
    # ⚠️ Both remaining conjuncts are load-bearing; see module docstring #3.
    if not isinstance(raw.get("strokes"), list):
        return False
    try:
        ReadingView.model_validate(raw)
    except Exception:
        return False
    return True


def _detect_as_drawn_plan(raw: dict) -> bool:
    """⭐ 2026-08-30: discipline #3, finally applied to this contract.

    This detector used to be ``_is_declared(...) and _has_keys(raw,
    "observations", "declarations", "hypotheses")`` -- exactly the "key list
    induced from existing artifacts" the module docstring forbids.  Measured
    before the change: **15** element-level corruptions of the real
    ``sm25_2f_v2.json`` all came back ``as_drawn_plan / KNOWN_NOT_CONSUMED``,
    i.e. a malformed product was filed as a well-formed one.  ⭐ That premise is
    re-stated and asserted, not remembered, in
    ``tests/test_o22m1_as_drawn_producer_types.py``.

    ⚠️ The explicit-key conjunct is kept even though ``AsDrawnPlanV2`` requires
    the same three: it is the module's stated pattern (see #3), and a test locks
    that the TYPE rejects a missing layer on its own, so the conjunct is a belt
    and never the only thing holding.

    ⛔ Tightening this cannot silently reclassify anything into CONSUME: a file
    that declares a registered value and then fails its contract falls to the
    BLK-A rule in ``classify_vector_json`` and comes out UNKNOWN, never legacy.

    ⭐ 2026-09-02 (module 7 wiring): the disposition moved to ``ADAPT`` -- the
    bytes of a recognized product now have a wire (``adapt_as_drawn_plan``).
    The ledger names ADAPT files without offender status: a run that has them
    but did not take the evidence chain is refused by the caller, not here.
    """
    if not _is_declared(raw, AS_DRAWN_PLAN_SCHEMA):
        return False
    if not _has_keys(raw, "observations", "declarations", "hypotheses"):
        return False
    from src.agent.reading.as_drawn.schema import AsDrawnPlanV2

    try:
        AsDrawnPlanV2.model_validate(raw)
    except Exception:
        return False
    return True


def _detect_stage_check_report(raw: dict) -> bool:
    """B-02: the three key names are a proxy; the producer's TYPE is the thing.

    ⭐ Keying on `stage`/`results`/`report_schema_version` merely being present
    let a malformed report (e.g. `results` a string) be silently EXCLUDEd --
    neither loud nor consumed, filed in the ledger as a legitimate exclusion.
    `CheckReport` is the type its producer actually writes, and all 43 real
    sidecars in the tree parse under it, so the stricter path costs no
    compatibility. Explicit presence is still required: `CheckReport` defaults
    every field, so "parses" alone would swallow `{}`.
    """
    if "schema" in raw:
        return False
    if not _has_keys(raw, "stage", "results", "report_schema_version"):
        return False
    from src.validator.checks.schema import CheckReport

    try:
        CheckReport.model_validate(raw)
    except Exception:
        return False
    return True


CONTRACTS: tuple[ContractSpec, ...] = (
    ContractSpec(
        CONTRACT_READING_VIEW_LEGACY,
        Disposition.CONSUME,
        _detect_legacy_reading_view,
        "parses as reading/schema.py:ReadingView and declares a `strokes` list",
    ),
    ContractSpec(
        CONTRACT_AS_DRAWN_PLAN,
        Disposition.ADAPT,
        _detect_as_drawn_plan,
        f"schema=={AS_DRAWN_PLAN_SCHEMA!r} (imported from its producer) "
        "with observations/declarations/hypotheses AND parses as "
        "reading/as_drawn/schema.py:AsDrawnPlanV2",
    ),
    ContractSpec(
        CONTRACT_AS_DRAWN_PLAN_V0,
        Disposition.KNOWN_NOT_CONSUMED,
        lambda raw: _is_declared(raw, AS_DRAWN_PLAN_V0_SCHEMA)
        and _has_keys(raw, "wall_bands", "dimension_witnesses"),
        f"schema=={AS_DRAWN_PLAN_V0_SCHEMA!r} with wall_bands/dimension_witnesses",
    ),
    ContractSpec(
        # ⭐ 2026-09-03 (B3, the as-drawn elevation leg): the disposition moved
        # from KNOWN_NOT_CONSUMED to ADAPT -- the bytes of a recognized
        # elevation product now have a wire (``adapt_as_drawn_elevation``),
        # exactly as the plan contract did at module-7 wiring.  The ledger
        # names them in ``adapted`` ⛔ never as offenders; a run that carries
        # them without taking the evidence chain is the caller's refusal.
        CONTRACT_AS_DRAWN_ELEVATION_V0,
        Disposition.ADAPT,
        lambda raw: _is_declared(raw, AS_DRAWN_ELEVATION_V0_SCHEMA)
        and _has_keys(raw, "openings", "structure_lines"),
        f"schema=={AS_DRAWN_ELEVATION_V0_SCHEMA!r} with openings/structure_lines",
    ),
    ContractSpec(
        CONTRACT_STAGE_CHECK_REPORT,
        Disposition.EXCLUDE,
        _detect_stage_check_report,
        "undeclared sidecar: declares stage/results/report_schema_version AND "
        "parses as validator/checks/schema.py:CheckReport",
    ),
)


@dataclass(frozen=True)
class ContractDecision:
    contract_id: str
    disposition: Disposition | None
    reason: str | None
    """Why the file is NOT a single known contract (``None`` when it is one)."""


def classify_vector_json(raw: Any) -> ContractDecision:
    """Name the declared contract of ONE parsed reading-stage JSON.

    Evaluates every detector — ⛔ never first-match-wins — so that a file
    satisfying two contracts is reported as an ambiguity rather than silently
    resolved by declaration order.
    """
    if not isinstance(raw, dict):
        return ContractDecision(
            CONTRACT_UNKNOWN,
            None,
            f"top-level JSON is {type(raw).__name__}, not an object",
        )
    matches = [spec for spec in CONTRACTS if spec.detect(raw)]
    if len(matches) == 1:
        only = matches[0]
        # ⭐ BLK-A: `_detect_legacy_reading_view` rejects UNREGISTERED
        # declarations, so a lone legacy match can still mean "declared a
        # registered value, then failed that contract's key set" — which the
        # first fix let collapse into a silent CONSUME. Discipline #5 says
        # structural fallback belongs to files that declare NOTHING, so the
        # presence of the key alone disqualifies the fallback.
        # ⚠️ Guarded by `len(matches) == 1` on purpose: a genuine double match
        # (registered value + its key set + legacy structure) never reaches
        # here and stays AMBIGUOUS. ⛔ Rejecting on `"schema" in raw` *before*
        # counting matches is the exact regression this fix already caused once.
        if only.contract_id == CONTRACT_READING_VIEW_LEGACY and "schema" in raw:
            return ContractDecision(
                CONTRACT_UNKNOWN,
                None,
                f"declares schema={raw.get('schema')!r} but matches no "
                "registered contract's key set, so it is a malformed "
                "declaration, not an undeclared legacy view; structural legacy "
                "fallback is reserved for files that declare nothing",
            )
        return ContractDecision(only.contract_id, only.disposition, None)
    if len(matches) > 1:
        named = ", ".join(f"{m.contract_id} ({m.describe})" for m in matches)
        return ContractDecision(
            CONTRACT_UNKNOWN,
            None,
            f"AMBIGUOUS: matches {len(matches)} declared contracts at once: {named}",
        )
    keys = sorted(k for k in raw if isinstance(k, str))[:12]
    declared = raw.get("schema")
    if isinstance(declared, str):
        declared_txt = (
            f"declares schema={declared!r} but no registered contract has that "
            "value with a matching key set"
        )
    elif "schema" in raw:
        # ⚠️ `schema: null` used to be reported as "declares no `schema` field",
        # which names an observation as a different fact than the one measured
        # ([[observation-named-as-fact-travels-as-fact]]): the file DID declare,
        # it declared something that is not a contract name.
        declared_txt = (
            f"declares a non-string schema={declared!r} "
            f"({type(declared).__name__}), which names no registered contract"
        )
    else:
        declared_txt = "declares no `schema` field and matches no structural contract"
    return ContractDecision(
        CONTRACT_UNKNOWN,
        None,
        f"{declared_txt}; top-level keys={keys}",
    )


@dataclass(frozen=True)
class LedgerRow:
    filename: str
    contract_id: str
    disposition: str
    reason: str | None

    def as_dict(self) -> dict:
        return {
            "file": self.filename,
            "contract": self.contract_id,
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VectorDirDecision:
    consumed: list[str]
    """Filenames 1_correction may paste, in the caller's original order."""
    adapted: list[str]
    """Filenames wired to a correction evidence adapter (module 7).

    ⭐ Point-named in the ledger, ⛔ never offenders and ⛔ never in
    ``consumed``: "adapted" and "pasted into the prompt" are disjoint
    wires, and a run that carries ADAPT files without taking the
    evidence chain is refused by the CALLER (the classifier does not know
    which leg the run is on)."""
    rows: list[LedgerRow]

    def as_ledger(self) -> dict:
        counts: dict[str, int] = {}
        for row in self.rows:
            counts[row.disposition] = counts.get(row.disposition, 0) + 1
        return {
            "ledger_version": "reading_vector_contract_ledger_v1",
            "consumed": list(self.consumed),
            "adapted": list(self.adapted),
            "counts": counts,
            "files": [row.as_dict() for row in self.rows],
        }


class UnconsumableVectorFile(RuntimeError):
    """A 0_reading JSON that 1_correction must not paste into its prompt."""


UNEXPECTED_FAILURE_PREFIX = "unexpected classifier failure"
"""⭐ Marks a row produced by the last-resort net rather than by a named path.

A row carrying this prefix means the file was refused *because something blew
up*, not because the discriminator understood it and said no.  ⛔ Tests must
assert its ABSENCE when they mean to lock a specific named path — otherwise the
net silently stands in for the mechanism under test and the neuter of that
mechanism stays green ([[neuter-proves-wiring-not-discriminating-power]])."""


def _unintelligible(name: str, reason: str) -> tuple[ContractDecision, str]:
    """A file that never became a parsed JSON value: decision + offender line.

    ⚠️ Deliberately phrased differently from "unknown contract": "I could not
    read this" and "I read it and recognize no contract" are different findings
    and the ledger must not blur them into one sentence.
    """
    return ContractDecision(CONTRACT_UNKNOWN, None, reason), f"{name}: {reason}"


def _classify_one(
    vector_dir: Path, name: str
) -> tuple[ContractDecision, str | None]:
    """Read, decode, parse and classify ONE file. ⭐ Never raises (discipline #6).

    Returns ``(decision, offender_line)``; ``offender_line`` is non-``None`` only
    when the file never became a parsed JSON value, so the caller can keep that
    wording distinct from a classification verdict.

    Every failure becomes a named decision instead of an exception. ⛔ These are
    not adversarial inputs — a truncated UTF-16 product, a stray ``mkdir``, a
    dangling symlink and a name that vanished between listing and read are
    ordinary filesystem reality, and each of them used to kill the ledger writer
    outright (BLK-C).

    The ``is_file`` guard is a *boundary*, not a fourth exception clause: it
    covers directories, dangling symlinks and symlink loops under one rule, and
    it is the only thing that stops a fifo named ``*.json`` from blocking the
    read forever — a hang, which no ``except`` can catch.
    """
    path = Path(vector_dir) / name
    try:
        if not path.is_file():
            return _unintelligible(
                name,
                "not a readable regular file (directory, dangling symlink, "
                "symlink loop, fifo, or removed between listing and read)",
            )
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return _unintelligible(name, f"not valid UTF-8: {exc}")
    except OSError as exc:
        return _unintelligible(name, f"unreadable file: {type(exc).__name__}: {exc}")
    except Exception as exc:  # last-resort net; see discipline #6
        return _unintelligible(
            name,
            f"{UNEXPECTED_FAILURE_PREFIX}: reading raised {type(exc).__name__}: {exc}",
        )
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        # ⚠️ Wording preserved verbatim from before the BLK-C rework: the
        # ledger row says "invalid JSON: <exc>" and the offender line says
        # "<name>: invalid JSON (<exc>)".
        return (
            ContractDecision(CONTRACT_UNKNOWN, None, f"invalid JSON: {exc}"),
            f"{name}: invalid JSON ({exc})",
        )
    except Exception as exc:  # e.g. RecursionError on pathologically nested JSON
        return _unintelligible(
            name,
            f"{UNEXPECTED_FAILURE_PREFIX}: parsing raised {type(exc).__name__}: {exc}",
        )
    try:
        return classify_vector_json(raw), None
    except Exception as exc:
        return _unintelligible(
            name,
            f"{UNEXPECTED_FAILURE_PREFIX}: classification raised "
            f"{type(exc).__name__}: {exc}",
        )


def _classify_rows(
    vector_dir: Path, names: list[str]
) -> tuple[list[str], list[str], list[LedgerRow], list[str]]:
    """Shared core: (consumed, adapted, ledger rows, offender descriptions).

    Never raises.  ⭐ Module 7: ADAPT files are point-named in ``adapted``
    and in the ledger rows, ⛔ never offenders (the wire EXISTS) and ⛔ never
    in ``consumed`` (adapting and pasting are disjoint wires).
    """
    vector_dir = Path(vector_dir)
    rows: list[LedgerRow] = []
    consumed: list[str] = []
    adapted: list[str] = []
    offenders: list[str] = []
    for name in names:
        decision, unintelligible = _classify_one(vector_dir, name)
        if unintelligible is not None:
            rows.append(LedgerRow(name, CONTRACT_UNKNOWN, "error", decision.reason))
            offenders.append(unintelligible)
        elif decision.disposition is Disposition.CONSUME:
            consumed.append(name)
            rows.append(
                LedgerRow(
                    name, decision.contract_id, decision.disposition.value, None
                )
            )
        elif decision.disposition is Disposition.ADAPT:
            adapted.append(name)
            rows.append(
                LedgerRow(
                    name,
                    decision.contract_id,
                    decision.disposition.value,
                    "recognized; wired to the correction evidence adapter "
                    "(module 7) — the frozen bytes travel through "
                    "adapt_*, never the pasted-JSON prompt",
                )
            )
        elif decision.disposition is Disposition.EXCLUDE:
            rows.append(
                LedgerRow(
                    name,
                    decision.contract_id,
                    decision.disposition.value,
                    "recognized; 1_correction does not consume this contract",
                )
            )
        elif decision.disposition is Disposition.KNOWN_NOT_CONSUMED:
            rows.append(
                LedgerRow(
                    name, decision.contract_id, decision.disposition.value, None
                )
            )
            offenders.append(
                f"{name}: recognized as contract {decision.contract_id!r}, but "
                "1_correction has no wire for it (known contract, NOT unknown)"
            )
        else:
            rows.append(LedgerRow(name, CONTRACT_UNKNOWN, "error", decision.reason))
            offenders.append(f"{name}: unknown contract — {decision.reason}")
    return consumed, adapted, rows, offenders


def ledger_for(vector_dir: Path, names: list[str]) -> dict:
    """The consumption ledger for ``names`` — ⭐ never raises.

    Written before the prompt is assembled so that a run which fails
    classification still leaves a readable record naming every offending file.
    """
    consumed, adapted, rows, _ = _classify_rows(vector_dir, names)
    return VectorDirDecision(
        consumed=consumed, adapted=adapted, rows=rows
    ).as_ledger()


def classify_vector_dir(vector_dir: Path, names: list[str]) -> VectorDirDecision:
    """Classify ``names`` under ``vector_dir``; raise unless every file is consumable.

    ⭐ This is the point where "an undeclared shape" stops being a silent paste
    and becomes a named failure.  ``EXCLUDE`` files are dropped but recorded.
    ⭐ Module 7: ``ADAPT`` files are NOT offenders here — the wire exists; a
    run that carries them without taking the evidence chain is refused by
    the caller (``pipeline``), which knows which leg the run is on.
    """
    consumed, adapted, rows, offenders = _classify_rows(vector_dir, names)
    if offenders:
        raise UnconsumableVectorFile(
            "1_correction refuses to assemble a prompt from "
            f"{Path(vector_dir)}: {len(offenders)} file(s) carry no consumable "
            "contract (F-97: an undeclared shape must fail loudly, never be "
            "pasted as untyped text). Offending files:\n  - "
            + "\n  - ".join(offenders)
        )
    return VectorDirDecision(consumed=consumed, adapted=adapted, rows=rows)
