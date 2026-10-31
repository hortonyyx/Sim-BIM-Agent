"""Run manifest + append-only attempt layout (M0 audit foundation).

Two guarantees this module exists to provide (施工方案 M0 / contracts §3.1):

1. **Append-only attempts.** Every time a stage produces a draw we file it under
   ``<case>/<stage>/attempts/NNN/`` — a fresh, zero-padded, monotonically
   increasing directory. A rejected draw is NEVER overwritten; it stays on disk
   for audit / hard-sample mining. The "accepted" attempt is recorded by pointer
   in ``run_manifest.json``, not by clobbering files.

2. **Content addressing.** Artifacts and attempts are hashed (canonical-JSON or
   raw-bytes sha256) so a manifest entry binds an accepted attempt to the exact
   bytes it accepted, and so the geometry-approval digest (approval.py) and the
   resume cache (invalidation.py) can detect drift.

The manifest is deliberately small and JSON-serializable: it indexes stages →
accepted attempt + input artifact hashes + stage/check version, nothing more.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from src.agent.execution.run_meta import run_meta_path

MANIFEST_NAME = "run_manifest.json"
ATTEMPTS_DIRNAME = "attempts"

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


# --------------------------------------------------------------------------- #
# hashing
# --------------------------------------------------------------------------- #
def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def hash_obj(obj) -> str:
    """Hash any JSON-able object by its canonical (sorted-key, compact) form, so
    semantically-equal objects with different key order hash identically."""
    return hash_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def hash_file(path: Path) -> str:
    return hash_bytes(Path(path).read_bytes())


def combined_digest(parts: list[str]) -> str:
    """Order-independent digest of a set of hashes (used for checkpoint digests)."""
    return hash_text("\n".join(sorted(parts)))


# --------------------------------------------------------------------------- #
# append-only attempts
# --------------------------------------------------------------------------- #
def _attempts_root(stage_dir: Path) -> Path:
    return stage_dir / ATTEMPTS_DIRNAME


def next_attempt_index(stage_dir: Path) -> int:
    """Smallest unused 1-based attempt index under ``<stage>/attempts/``."""
    root = _attempts_root(stage_dir)
    if not root.exists():
        return 1
    used = []
    for p in root.iterdir():
        if p.is_dir() and p.name.isdigit():
            used.append(int(p.name))
    return (max(used) + 1) if used else 1


def new_attempt_dir(stage_dir: Path) -> Path:
    """Create and return a fresh ``<stage>/attempts/NNN/`` directory.

    Never reuses or overwrites an existing index — the append-only guarantee.
    """
    idx = next_attempt_index(stage_dir)
    d = _attempts_root(stage_dir) / f"{idx:03d}"
    if d.exists():  # defensive: a race or manual tampering
        raise FileExistsError(f"attempt dir already exists: {d}")
    d.mkdir(parents=True, exist_ok=False)
    return d


def attempt_index_of(attempt_dir: Path) -> int:
    return int(Path(attempt_dir).name)


# --------------------------------------------------------------------------- #
# manifest model
# --------------------------------------------------------------------------- #
class StageRecord(BaseModel):
    """One stage's accepted-attempt pointer + provenance."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    accepted_attempt: int           # 1-based index under attempts/
    output_hash: str                # hash of the accepted output artifact
    input_hashes: dict[str, str] = Field(default_factory=dict)  # name -> hash
    stage_version: str = "1"
    check_version: str = "1"
    capability: str = "deterministic"
    check_passed: bool = True


class RunManifest(BaseModel):
    """Indexes each stage to its accepted attempt. Persisted as run_manifest.json
    under the run metadata dir. Append-only attempts live beside it under each
    stage dir."""

    model_config = ConfigDict(extra="forbid")

    case: str = ""
    manifest_version: str = "1"
    stages: dict[str, StageRecord] = Field(default_factory=dict)

    # ---- io ----
    @classmethod
    def load(cls, case_dir: Path) -> "RunManifest":
        p = run_meta_path(case_dir, MANIFEST_NAME)
        if not p.exists():
            return cls(case=Path(case_dir).name)
        return cls.model_validate_json(p.read_text(encoding="utf-8"))

    def save(self, case_dir: Path, *, filename: str = MANIFEST_NAME) -> Path:
        # Delegates to the shared versioned serializer (§5.1: "普通 save() 与
        # isolation _atomic_save_manifest() 共用同一 versioned serializer") so a
        # V1 write and a V2 write can never drift onto two code paths. Byte
        # shape for a V1 instance is unchanged (same model, same
        # indent=2 model_dump_json — only the write mechanics gained atomicity).
        return save_run_manifest(self, case_dir, filename=filename)

    # ---- mutation ----
    def accept(self, record: StageRecord) -> None:
        """Record (or replace the pointer to) a stage's accepted attempt. This
        only moves the pointer; it never touches prior attempt directories."""
        self.stages[record.stage] = record

    def accepted(self, stage: str) -> StageRecord | None:
        return self.stages.get(stage)


# --------------------------------------------------------------------------- #
# §5.1 RunManifestV1/V2 + StageRecordV1/V2 wire (C2 B-M, r4/r5 裁决: this module
# is the single regulatory owner of this wire; B2 consumes StageRecordV2 later).
#
# StageRecordV1/RunManifestV1 are literal aliases of the two classes above —
# "现类原封" is satisfied by construction (same class object, not a re-declared
# lookalike), so load/save bytes for any V1 run are provably unchanged.
# --------------------------------------------------------------------------- #
StageRecordV1 = StageRecord
RunManifestV1 = RunManifest

ArtifactKey = Literal[
    "output", "checks", "audit", "feature_states", "isolation_provenance",
    "output_coordinate_contract", "output_coordinate_snapshot",
    "window_resolver_inputs", "window_hosts", "deterministic_core_proof",
    # W#3 (wallhunt 2026-09-08b): the as_drawn chain leg's provenance carrier
    # (frozen per-storey compilations) — filed by the writer on every
    # as_drawn B5 write, structurally absent from every legacy one.
    "chain_provenance",
]
ArtifactContract = Literal[
    "migrated_v1", "base_v2", "reading_isolated_v2", "correction_b2_v1",
    "correction_e4_orientation_v1", "correction_b5_v1",
    "correction_b5_orientation_v1", "assembly_e4_v1",
]

# Keys a writer for this contract MUST have populated (loader-enforced).
_CONTRACT_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "migrated_v1": frozenset(),  # only whatever a backfill found on disk — no fixed floor
    "base_v2": frozenset({"output", "checks"}),
    "reading_isolated_v2": frozenset({"output", "checks", "isolation_provenance"}),
    "correction_b2_v1": frozenset({"output", "checks", "audit", "feature_states"}),
    # E4-output-contract spec v2 §3.4: two new versioned artifact contracts.
    # `correction_e4_orientation_v1` is the orientation-enrichment attempt
    # written on top of a `correction_b2_v1` base (same 4 keys, different
    # geom content — north_axis is now populated); `assembly_e4_v1` is the S5
    # attempt that additionally carries the two output-coordinate sidecars.
    "correction_e4_orientation_v1": frozenset({"output", "checks", "audit", "feature_states"}),
    "correction_b5_v1": frozenset({
        "output", "checks", "audit", "feature_states",
        "window_resolver_inputs", "window_hosts",
    }),
    "correction_b5_orientation_v1": frozenset({
        "output", "checks", "audit", "feature_states",
        "window_resolver_inputs", "window_hosts",
    }),
    "assembly_e4_v1": frozenset({
        "output", "checks", "audit", "output_coordinate_contract", "output_coordinate_snapshot",
    }),
}
# Keys a writer for this contract is even PERMITTED to have populated. A v1->v2
# migration backfill can only observe what a pre-B2 attempt directory could
# possibly contain (output.json + checks.json — the "audit"/"feature_states"/
# "isolation_provenance" sidecars are version-specific artifacts that did not
# exist yet) — so a `migrated_v1` record carrying e.g. "audit" is structurally
# impossible for a real migration and is rejected as a forged/mislabeled record
# whose provenance does not match its claimed contract.
#
# F-22 BLOCKER-1 round 2 (2026-08-13): `deterministic_core_proof` is
# deliberately ALLOWED but NOT required for the two B5 contracts below.
# Making it required would retroactively invalidate every already-persisted
# manifest whose StageRecordV2 predates this key (this model's own
# `_contract_keys_and_output_identity` validator raises on a missing
# required key at LOAD time, not just at write time -- so `RunManifestV2.
# model_validate_json` would crash on any historical run's manifest.json
# just from being read). A record written by the current writer always
# carries it (StageRunner.record signs and binds it unconditionally on every
# real B5 write); a record from before this fix simply lacks the key, which
# is exactly the "declared, not trusted; rerun to regain a score"
# consequence the user ratified for the original stamp — see
# `judge.correction_score._is_trusted_output_convention`.
_CONTRACT_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "migrated_v1": frozenset({"output", "checks"}),
    "base_v2": frozenset({"output", "checks"}),
    "reading_isolated_v2": frozenset({"output", "checks", "isolation_provenance"}),
    "correction_b2_v1": frozenset({"output", "checks", "audit", "feature_states"}),
    "correction_e4_orientation_v1": frozenset({"output", "checks", "audit", "feature_states"}),
    "correction_b5_v1": frozenset({
        "output", "checks", "audit", "feature_states",
        "window_resolver_inputs", "window_hosts", "deterministic_core_proof",
        "chain_provenance",
    }),
    "correction_b5_orientation_v1": frozenset({
        "output", "checks", "audit", "feature_states",
        "window_resolver_inputs", "window_hosts", "deterministic_core_proof",
        "chain_provenance",
    }),
    "assembly_e4_v1": frozenset({
        "output", "checks", "audit", "output_coordinate_contract", "output_coordinate_snapshot",
    }),
}


class StageRecordV2(BaseModel):
    """StageRecordV1's fields, unchanged, plus the v2 artifact-contract wire."""

    model_config = ConfigDict(extra="forbid")

    stage: str
    accepted_attempt: int
    output_hash: str
    input_hashes: dict[str, str] = Field(default_factory=dict)
    stage_version: str = "1"
    check_version: str = "1"
    capability: str = "deterministic"
    check_passed: bool = True

    record_schema_version: Literal["2"] = "2"
    artifact_contract: ArtifactContract
    artifact_hashes: dict[ArtifactKey, Hex64] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _contract_keys_and_output_identity(self) -> "StageRecordV2":
        keys = set(self.artifact_hashes)
        required = _CONTRACT_REQUIRED_KEYS[self.artifact_contract]
        allowed = _CONTRACT_ALLOWED_KEYS[self.artifact_contract]
        missing = required - keys
        if missing:
            raise ValueError(
                f"artifact_contract={self.artifact_contract!r} is missing required "
                f"artifact_hashes key(s): {sorted(missing)}"
            )
        forbidden = keys - allowed
        if forbidden:
            raise ValueError(
                f"artifact_contract={self.artifact_contract!r} may not carry "
                f"artifact_hashes key(s) {sorted(forbidden)} — provenance does not "
                "match the claimed contract"
            )
        if "output" in self.artifact_hashes and self.artifact_hashes["output"] != self.output_hash:
            raise ValueError(
                "StageRecordV2.output_hash and artifact_hashes['output'] disagree — "
                "two accepted-identity fields must never contradict each other"
            )
        return self


class RunInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view_manifest_sha256: Hex64


class RunManifestV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case: str = ""
    manifest_version: Literal["2"] = "2"
    run_id: Hex32
    run_inputs: RunInputs
    stages: dict[str, StageRecordV2] = Field(default_factory=dict)

    def accept(self, record: StageRecordV2) -> None:
        self.stages[record.stage] = record

    def accepted(self, stage: str) -> StageRecordV2 | None:
        return self.stages.get(stage)

    def save(self, run_dir: Path, *, filename: str = MANIFEST_NAME) -> Path:
        """Same call shape as :meth:`RunManifest.save`, so version-dispatched
        command flows (`run`/`flow`/`resample`) can hold either manifest and
        persist it identically — both delegate to the one versioned serializer."""
        return save_run_manifest(self, run_dir, filename=filename)


def new_run_id() -> str:
    """128-bit random hex run identity — immutable, never derived from the run
    directory's name/path (§5.1 r3 裁决: a moved/copied run must not change
    identity, or silently share identity with another run)."""
    return secrets.token_hex(16)


def _fsync_temp_write(dir_path: Path, name: str, text: str) -> str:
    """Create + write + fsync a same-directory temp file; return its path.

    The caller owns the eventual ``os.replace`` (and cleanup on a later
    failure); this helper cleans up its own temp only if the write itself
    fails. Split out so the dual-temp migration commit protocol (CR-03) can
    prepare BOTH final files' temps before replacing either."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    return tmp_name


def save_run_manifest(
    manifest: "RunManifestV1 | RunManifestV2", run_dir: Path, *, filename: str = MANIFEST_NAME
) -> Path:
    """The single versioned serializer both the plain V1 ``save()`` path and
    isolation's merge writer use (§5.1: "消灭双 writer 漂移"). Atomic
    temp+fsync+replace, matching the M0 append-only-attempts write discipline."""
    path = run_meta_path(run_dir, filename, for_write=True)
    tmp_name = _fsync_temp_write(path.parent, filename, manifest.model_dump_json(indent=2))
    try:
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def load_run_manifest(
    run_dir: Path, *, filename: str = MANIFEST_NAME
) -> "RunManifestV1 | RunManifestV2 | None":
    """Version dispatcher. Returns ``None`` when no manifest file exists yet —
    a genuinely unprovisioned run; callers that need a definite identity
    (isolation's formal builder) go through :func:`ensure_run_manifest_v2`
    instead of fabricating a default here. Never writes."""
    p = run_meta_path(run_dir, filename)
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run manifest at {p} is not valid JSON: {exc}") from exc
    version = obj.get("manifest_version") if isinstance(obj, dict) else None
    if version == "2":
        return RunManifestV2.model_validate_json(raw)
    if version in (None, "1"):
        return RunManifestV1.model_validate_json(raw)
    raise ValueError(f"run manifest at {p} has unknown manifest_version: {version!r}")


def reading_attempt_allowed(run_dir: Path) -> tuple[bool, str]:
    """Grandfather guard (§5.1): a run whose *persisted* manifest is already v1
    is read-only for NEW 0_reading attempts — flow reading / resample /
    isolation merge must all refuse and point at explicit migration. A run with
    no manifest yet, or an already-v2 run, is unaffected."""
    existing = load_run_manifest(run_dir)
    if existing is None or isinstance(existing, RunManifestV2):
        return True, ""
    return False, (
        "run manifest is v1 (grandfathered legacy run) — new 0_reading attempts "
        "are blocked; migrate explicitly first (`provision --migrate`)"
    )


def ensure_run_manifest_v2(run_dir: Path, *, view_manifest_sha256: str) -> RunManifestV2:
    """Bind (or create) a run's v2 identity — the "run 绑定 + ensure 前置" half
    of isolation's formal-builder contract (§5.2). Creates a fresh v2 manifest
    (new run_id) when the run has no manifest yet; returns the existing v2
    manifest unchanged when inputs match; raises on an inputs mismatch or on an
    already-persisted v1 (grandfathered) manifest — a v1 run is never silently
    upgraded, only explicitly migrated (:func:`migrate_run_to_v2`)."""
    existing = load_run_manifest(run_dir)
    if existing is None:
        fresh = RunManifestV2(
            case=Path(run_dir).name,
            run_id=new_run_id(),
            run_inputs=RunInputs(view_manifest_sha256=view_manifest_sha256),
        )
        save_run_manifest(fresh, run_dir)
        return fresh
    if isinstance(existing, RunManifestV2):
        if existing.run_inputs.view_manifest_sha256 != view_manifest_sha256:
            raise ValueError(
                "run manifest run_inputs.view_manifest_sha256 does not match this "
                "build's view manifest — inputs drifted for an already-provisioned run"
            )
        return existing
    raise ValueError(
        "run manifest is v1 (grandfathered legacy run) — migrate explicitly via "
        "`provision --migrate` before binding a formal isolated build to it"
    )


def migrate_run_to_v2(case_dir: Path, run_dir: Path) -> RunManifestV2:
    """Explicit v1->v2 migration (§5.1) — the *only* other write path besides
    :func:`~src.agent.execution.view_manifest.provision_view_manifest` and
    :func:`ensure_run_manifest_v2`, invoked only via the ``provision --migrate``
    CLI flag (never automatically).

    Commit protocol (§5.1, frozen order — CR-03 r2 double-temp): (1)
    **everything in memory first**: run_id generation, view-manifest build,
    and the FULL stages backfill — each accepted pointer's ``output.json`` AND
    ``checks.json`` must exist on disk with ``output.json`` matching the
    accepted hash (M0 discipline: an accepted attempt always filed both; only
    version-specific sidecars that never existed pre-B2 — audit /
    feature_states — are legal omissions). Any backfill failure aborts BEFORE
    any final file is written, including view_manifest.json. (2) pre-serialize
    BOTH final texts and write BOTH same-directory temps with fsync — neither
    final path is touched until *both* temps are safely on disk. (3) commit in
    the frozen order: replace view_manifest.json first (an orphan with no v2
    run_manifest is inert — the v1 loader never looks at it), replace
    RunManifestV2 LAST — the single commit point. Any failure before the final
    replace leaves the run's semantics exactly V1 (re-running is idempotent:
    an already-v2 run short-circuits, an orphan view_manifest is reused if
    content-identical or overwritten if not).
    """
    from src.agent.execution.view_manifest import (  # local import: avoids a
        VIEW_MANIFEST_NAME,                          # manifest.py <-> view_manifest.py
        ViewManifest,                                # import cycle (view_manifest.py
        build_view_manifest,                         # imports hash_* from this module).
        canonical_view_manifest_json,
    )

    case_dir = Path(case_dir)
    run_dir = Path(run_dir)
    existing = load_run_manifest(run_dir)
    if isinstance(existing, RunManifestV2):
        return existing  # already migrated — idempotent no-op
    v1: RunManifestV1 = existing or RunManifestV1(case=case_dir.name)

    # --- step 1 (ALL in memory, nothing written yet) ---
    expected_vm = build_view_manifest(case_dir)

    stages_v2: dict[str, StageRecordV2] = {}
    for stage, rec in v1.stages.items():
        attempt_dir = run_dir / stage / "attempts" / f"{rec.accepted_attempt:03d}"
        output_path = attempt_dir / "output.json"
        if not output_path.is_file():
            raise ValueError(
                f"migration backfill for stage {stage!r}: accepted attempt "
                f"{rec.accepted_attempt} has no output.json on disk — a v1 accepted "
                "pointer without its artifact cannot be migrated (M0 append-only "
                "attempts always file it); repair or drop the pointer first"
            )
        real_hash = hash_file(output_path)
        if real_hash != rec.output_hash:
            raise ValueError(
                f"migration backfill for stage {stage!r}: on-disk output.json hash "
                f"({real_hash}) does not match the v1 manifest's accepted pointer "
                f"({rec.output_hash}) — the attempt file changed since acceptance"
            )
        checks_path = attempt_dir / "checks.json"
        if not checks_path.is_file():
            raise ValueError(
                f"migration backfill for stage {stage!r}: accepted attempt "
                f"{rec.accepted_attempt} has no checks.json on disk — gate① reports "
                "are mandatory attempt artifacts under the M0 discipline (only "
                "audit/feature_states-class version-specific sidecars may be absent)"
            )
        stages_v2[stage] = StageRecordV2(
            stage=rec.stage,
            accepted_attempt=rec.accepted_attempt,
            output_hash=rec.output_hash,
            input_hashes=rec.input_hashes,
            stage_version=rec.stage_version,
            check_version=rec.check_version,
            capability=rec.capability,
            check_passed=rec.check_passed,
            artifact_contract="migrated_v1",
            artifact_hashes={"output": real_hash, "checks": hash_file(checks_path)},
        )

    v2 = RunManifestV2(
        case=v1.case or case_dir.name,
        run_id=new_run_id(),
        run_inputs=RunInputs(view_manifest_sha256=expected_vm.content_sha256),
        stages=stages_v2,
    )

    # --- step 2: pre-serialize BOTH final texts; write BOTH temps + fsync ---
    vm_path = run_meta_path(run_dir, VIEW_MANIFEST_NAME, for_write=True)
    manifest_path = run_meta_path(run_dir, MANIFEST_NAME, for_write=True)
    write_vm = True
    if vm_path.exists():
        try:
            on_disk_vm = ViewManifest.model_validate_json(vm_path.read_text(encoding="utf-8"))
            write_vm = on_disk_vm.content_sha256 != expected_vm.content_sha256
        except Exception:  # noqa: BLE001 — an unreadable orphan is overwritten
            write_vm = True

    vm_tmp: str | None = None
    v2_tmp: str | None = None
    try:
        if write_vm:
            vm_tmp = _fsync_temp_write(
                vm_path.parent, VIEW_MANIFEST_NAME, canonical_view_manifest_json(expected_vm)
            )
        v2_tmp = _fsync_temp_write(
            manifest_path.parent, MANIFEST_NAME, v2.model_dump_json(indent=2)
        )
        # --- step 3: frozen commit order — view_manifest first, V2 last ---
        if vm_tmp is not None:
            os.replace(vm_tmp, vm_path)
            vm_tmp = None
        os.replace(v2_tmp, manifest_path)
        v2_tmp = None
    finally:
        for leftover in (vm_tmp, v2_tmp):
            if leftover is not None and os.path.exists(leftover):
                os.unlink(leftover)
    return v2


def assert_stage_artifact_contracts(
    manifest: RunManifestV2, allowed_contracts_by_stage: dict[str, frozenset[str]]
) -> None:
    """Cross-check a v2 manifest's per-stage ``artifact_contract`` against a
    caller-supplied allowlist (e.g. "1_correction may only be
    correction_b2_v1 or migrated_v1"). B-M owns the *mechanism*; the concrete
    per-stage table is a B2-era business rule this module does not invent —
    B2's correction writer supplies its own mapping when it lands."""
    for stage, rec in manifest.stages.items():
        allowed = allowed_contracts_by_stage.get(stage)
        if allowed is not None and rec.artifact_contract not in allowed:
            raise ValueError(
                f"stage {stage!r} record has artifact_contract="
                f"{rec.artifact_contract!r}, not in the allowed set {sorted(allowed)}"
            )
