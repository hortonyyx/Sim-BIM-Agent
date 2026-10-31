"""Where the facts-layer TRIO lives before it has a promotion path (dispatch
②-1b R5).

⛔ NOT ``case_tests/test_baseline/gt/<case>/facts/`` (that is the ANSWER ROOT;
only ``promote_gt_v3`` -- untouched by this dispatch -- may write there) and
⛔ NOT ``case_tests/test_baseline/gt_sources/<case>/`` (F-117: promotion is
the only writer there too, and ``revisions``/``as_signed`` are a different
KIND of thing from the raw signed inputs that root holds).  This is a THIRD,
brand-new directory with no existing write-protection discipline attached,
precisely because it holds nothing signed yet:

    case_tests/test_baseline/gt_staging/<case>/facts/
        as_measured.json
        revisions.json      -- ⛔ UNSIGNED here (every ``verdict`` is
                                "unsigned"); the signing flow (ledger §五
                                step 5) is what would ever populate a real
                                one
        as_signed.json      -- mechanically derived from the two above;
                                identical to as_measured plus a derivation
                                key while nothing is signed (no
                                ``drawing_error`` record exists to apply)

## R1/R2 (dispatch ②-1b-T): both doors gated, not just the write side

⭐⭐ Both :func:`write_facts_candidate` and :func:`read_facts_candidate` now
call :func:`src.agent.judge.gt_revisions.verify_as_signed_reproduction`
themselves -- WRITE runs it BEFORE the first byte touches disk (a failing
trio leaves zero residual files, not a half-written directory), READ runs it
AFTER parsing (so a file that was placed in this directory by ANY means --
not just through this module -- is re-verified every time it is read, not
merely trusted because it once passed on write).  A reader can therefore
never observe a trio that does not currently reproduce, regardless of how
the bytes got onto disk.

## R3 (dispatch ②-1b-T): no path/directory accessor is public -- structurally, not lexically

⛔ ``FACTS_STAGING_ROOT`` and the ``case -> Path`` helper are deliberately
NOT in ``__all__`` (renamed with a leading underscore).  This module's ONLY
public exits are :func:`write_facts_candidate` (takes three already-typed,
already-*matching* documents and returns ``None``) and
:func:`read_facts_candidate` (returns three freshly-verified, already-typed
documents) -- neither one hands a caller a ``Path`` to this directory.

What this narrowing actually buys, restated after ②-1b-T-R's cross-review
(GLM F-5) MEASURED it to be weaker than the original wording claimed: it
raises the *discoverability* bar, not an *accessibility* one. ⛔ CORRECTED
CLAIM (the previous revision of this paragraph said "no path to copy a
directory at the type/API level" -- that overstated it): ``gfs.
_facts_staging_dir(case)`` followed by ``shutil.copytree`` is THREE LINES
and works today -- the leading underscore keeps ``facts_staging_dir`` out of
``from gt_facts_staging import *`` and out of the names a reader skimming
``__all__`` would find, but Python does not stop an explicit
``from src.agent.judge.gt_facts_staging import _facts_staging_dir`` (this
module's own test suite does exactly that). This is not a grep-able name ban
either way (⛔ the project's own precedent: "词法匹配判无界输入的防线永远补
不完") -- it was never trying to be one; what it removes is the *sanctioned,
star-imported* way to get a directory handle, nothing stronger. A
sufficiently motivated ``promote_gt_v3`` can reach the private helper, or
skip this module entirely and hardcode the literal staging path string, or
(worse, and NOT something this narrowing addresses at all) walk straight
through R1's own case-name admission gate below with a case value that
*is* a legitimate single path segment -- see F-1's finding.

GLM's F-5 names the actually-structural fix, now implemented by
``answer_compiler.read_facts_for_compilation`` (rework §四 / §五
"⛔ 出口全检"): hang the SAME reproducibility
gate this module already runs on read (:func:`verify_as_signed_reproduction`)
on the READ side of the ANSWER root's own ``gt/<case>/facts/``, once that
directory exists and has a consumer. That is an "exit gate", not an "entry
narrowing" -- it does not care HOW bytes arrived under ``gt/<case>/facts/``
(copied directory, hand-typed path, this module's write path taken via F-1's
own escape hatch before R1's fix, or anything else not yet invented), only
whether what is there NOW reproduces. An entry-side narrowing, however
tight, can only ever enumerate ways IN it has thought of; an exit-side gate
does not need to enumerate anything.  This staging module does not own the
answer-root reader; its compiler consumer now does.

F-128 (promotion's rollback asymmetry: its ``except`` only cleans the ``gt/``
side, not ``gt_sources/``) is UNCHANGED by this staging root -- it is a
defect in ``promote_gt_v3``, not in anything this module writes, and this
dispatch does not touch it either (ledger §八: "顺带处理" -- named, deferred,
not silently dropped).

## R1 (dispatch ②-1b-T-R, GLM F-1): `case` is now an admission-checked, single-segment token

⭐⭐⭐ MEASURED by cross-review (F-1), confirmed independently by the
orchestrator, and BROADER than either party first reported: before this
rework, ``_facts_staging_dir`` did zero validation on ``case`` before
building ``_FACTS_STAGING_ROOT / case / "facts"``. Two attacks on a
*completely legitimate, verify-passing* call to the PUBLIC API:

* ``case="../gt/sm25-L_anchor"`` lands in
  ``case_tests/test_baseline/gt/sm25-L_anchor/facts/`` -- the ANSWER ROOT
  this very module's opening paragraph says only ``promote_gt_v3`` may write.
* ``case="/tmp/evil"`` (any absolute path) replaces the staging root
  entirely: ``pathlib``'s ``/`` operator DISCARDS the left operand the
  instant the right operand is absolute (``Path("/a") / "/b" == Path("/b")``),
  so an absolute ``case`` is not "escaping under" the root -- it is not
  under the root's authority at all, from the first path segment.

Both attacks are now rejected by :func:`_validate_case_literal`, called
before anything else runs (before :func:`verify_as_signed_reproduction`,
before any filesystem touch). ⭐ TWO INDEPENDENT layers, per the rework
dispatch's own instruction that neither alone is enough:

1. **Literal admission** (:func:`_validate_case_literal`): rejects an empty
   string, the two filesystem navigation tokens (``"."``, ``".."``), any
   value containing ``/`` or ``\\``, any value ``pathlib`` itself considers
   absolute, and anything not matching a plain
   ``[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`` token -- WITH A NAMED REASON per
   failure mode, because a bare "does the resolved path land outside the
   root" check (layer 2 below) cannot say WHICH thing about the input was
   wrong, only that it was.
2. **Resolved-path containment** (:func:`_facts_staging_dir`'s own check,
   after ``.resolve()``): asserts the fully-resolved candidate path is
   still a descendant of the fully-resolved root. This is NOT redundant
   with layer 1 -- ``tests/test_gt_facts_staging_case_admission.py``'s
   symlink fixture proves a ``case`` value that passes EVERY literal check
   in layer 1 (it is a bare, legal token, no separators, no ``..``) can
   still escape the root if the directory that literal name resolves to is
   itself a symlink pointing outside -- something no amount of string
   inspection of ``case`` alone could ever catch, because the string is not
   where the escape lives.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePath

from .as_measured import AsMeasuredV1
from .as_measured import canonical_bytes as canonical_as_measured_bytes
from .gt_revisions import (AsSignedV1, RevisionsLedgerV1,
                           canonical_as_signed_bytes, canonical_revisions_bytes,
                           verify_as_signed_reproduction)
from .gt_schema import REPO_ROOT

#: ⭐ ②-1b-T R3: ⛔ deliberately just these three -- see the module docstring's
#: "R3" section for why no path/directory accessor is exported.
#: ``FactsStagingCaseError`` IS public (②-1b-T-R R1): a caller building
#: ``case`` from something outside its control (e.g. a filename or a user
#: field) needs a stable exception type to catch, not just "some ValueError".
__all__ = ["write_facts_candidate", "read_facts_candidate", "FactsStagingCaseError"]

_FACTS_STAGING_ROOT = REPO_ROOT / "case_tests/test_baseline/gt_staging"

#: ⭐⭐ ②-1b-T-R R1 (GLM F-1): a case name is a single, plain filesystem
#: segment -- never a path.  Deliberately NARROWER than
#: :data:`src.agent.judge.gt_schema.StableId` (which allows ``:``, a
#: character meaningless here and a literal drive-separator on Windows):
#: this module has no reason to accept anything ``StableId`` would but a
#: bare directory name would not.
_CASE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RESERVED_SEGMENTS = frozenset({".", ".."})


class FactsStagingCaseError(ValueError):
    """⭐⭐⭐ ②-1b-T-R R1 (GLM F-1): ``case`` failed the admission check before
    ever reaching a filesystem call.  Every raise site names WHICH of the two
    independent layers (module docstring's "R1 (dispatch ②-1b-T-R)" section)
    rejected it and why -- ⛔ never a bare ``ValueError`` a caller has to
    pattern-match on message text to understand.
    """


def _validate_case_literal(case: str) -> None:
    """Layer 1 (of 2): reject on the STRING alone, before any ``Path`` is
    ever built from it.  ⛔ Not sufficient by itself (see
    :func:`_facts_staging_dir`'s layer 2 and the module docstring's symlink
    finding) -- but doing this first means the common attacks (a relative
    escape, an absolute path, a bare ``..``) fail with a message that says
    WHICH rule ``case`` broke, not just "the result was outside the root".
    """
    if not case:
        raise FactsStagingCaseError(
            "facts_staging_case_empty: case must be a non-empty single path segment")
    if case in _RESERVED_SEGMENTS:
        raise FactsStagingCaseError(
            f"facts_staging_case_is_a_navigation_token: case={case!r} is a filesystem "
            "navigation segment, not a case name")
    if "/" in case or "\\" in case:
        raise FactsStagingCaseError(
            f"facts_staging_case_contains_a_path_separator: case={case!r} must be ONE path "
            "segment, not a path (no '/' or '\\\\')")
    if PurePath(case).is_absolute():
        raise FactsStagingCaseError(
            f"facts_staging_case_is_an_absolute_path: case={case!r} must not itself be a path")
    if not _CASE_NAME_RE.match(case):
        raise FactsStagingCaseError(
            f"facts_staging_case_has_illegal_characters: case={case!r} must match "
            f"{_CASE_NAME_RE.pattern!r}")


def _facts_staging_dir(case: str) -> Path:
    """Both admission layers, in order.  ⭐ Layer 2 (resolved-path
    containment) runs even though layer 1 already rejects every case this
    dispatch's own fixtures construct -- it is the independent backstop for
    an escape layer 1 structurally cannot see: a case name that is a
    perfectly legal bare token but resolves, via a symlink already sitting
    in the staging root, to somewhere else entirely (module docstring's "R1"
    section; fixture: ``tests/test_gt_facts_staging_case_admission.py``).
    """
    _validate_case_literal(case)
    root = _FACTS_STAGING_ROOT.resolve()
    candidate = (_FACTS_STAGING_ROOT / case / "facts").resolve()
    if candidate != root and root not in candidate.parents:
        raise FactsStagingCaseError(
            f"facts_staging_case_escapes_root: case={case!r} resolves to {candidate}, which "
            f"is not inside {root}")
    return candidate


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except BaseException:
        # ⭐ ②-1b-T-R R3 (GLM F-4's tail): best-effort cleanup of the ``.tmp``
        # this call itself created, on ANY failure between the write and the
        # rename (permission error, disk full, ^C).  ⛔ Cannot help against a
        # hard kill (SIGKILL / power loss) between the two syscalls -- no
        # process-level ``except`` runs after the process is already dead;
        # that residue is handled instead by the sweep in
        # :func:`write_facts_candidate` below, and is functionally harmless
        # either way: :func:`read_facts_candidate` never opens a ``*.tmp``
        # file, so an orphan changes nothing about what a reader sees.
        tmp.unlink(missing_ok=True)
        raise


def _sweep_stale_tmp_orphans(out_dir: Path) -> None:
    """⭐ ②-1b-T-R R3 (GLM F-4's tail): a ``*.tmp`` left by a PRIOR call that
    died between ``write_bytes`` and ``replace`` (the one case
    ``_write_atomic``'s own ``except`` cannot reach -- the process was
    already gone) is swept the next time anyone writes into this same
    directory, so residue from one dead attempt does not accumulate forever
    across retries.  ⛔ Not a correctness requirement (see
    ``_write_atomic``'s docstring: an orphan ``.tmp`` is invisible to
    :func:`read_facts_candidate`, which only ever opens the three named
    ``.json`` files) -- this is disk hygiene, done here rather than never,
    because "the next write into this directory" is the one moment this
    module already knows the directory's identity.
    """
    if not out_dir.is_dir():
        return
    for stale in out_dir.glob("*.json.tmp"):
        stale.unlink(missing_ok=True)


def write_facts_candidate(case: str, as_measured: AsMeasuredV1,
                          revisions: RevisionsLedgerV1, as_signed: AsSignedV1) -> None:
    """Write the trio into the staging root, atomically, one file at a time.

    ⭐⭐ ②-1b-T R1: runs :func:`src.agent.judge.gt_revisions.
    verify_as_signed_reproduction` FIRST, before a single byte is written.
    A trio that does not reproduce raises loudly and touches the filesystem
    not at all -- ⛔ never a half-written directory a caller has to notice
    and clean up.  "Landed in the staging root" and "passed the
    reproducibility gate" are now one atomic fact, not two the caller could
    forget to keep in sync.

    ⭐⭐⭐ ②-1b-T-R R1: ``case`` is resolved (and admission-checked, raising
    :class:`FactsStagingCaseError`) BEFORE even ``verify`` runs -- a
    malformed ``case`` is rejected without spending a single CPU cycle on
    the (potentially large) reproducibility check, and without ever
    constructing a ``Path`` outside the staging root.
    """
    out_dir = _facts_staging_dir(case)
    verify_as_signed_reproduction(as_measured, revisions, as_signed)
    _sweep_stale_tmp_orphans(out_dir)
    _write_atomic(out_dir / "as_measured.json", canonical_as_measured_bytes(as_measured))
    _write_atomic(out_dir / "revisions.json", canonical_revisions_bytes(revisions))
    _write_atomic(out_dir / "as_signed.json", canonical_as_signed_bytes(as_signed))


def read_facts_candidate(case: str) -> tuple[AsMeasuredV1, RevisionsLedgerV1, AsSignedV1]:
    """⭐⭐ ②-1b-T R2: re-verifies AFTER parsing, every call, regardless of who
    or what put the files there.  ⛔ NOT "trust it, it already passed on
    write" -- the write-side gate only proves what THAT write's caller
    handed in; this directory has no filesystem-level write protection
    (module docstring's opening paragraph), so a reader that skipped this
    check would be trusting an unauthenticated claim about what is on disk.
    """
    out_dir = _facts_staging_dir(case)
    as_measured = AsMeasuredV1.model_validate_json(
        (out_dir / "as_measured.json").read_bytes())
    revisions = RevisionsLedgerV1.model_validate_json(
        (out_dir / "revisions.json").read_bytes())
    as_signed = AsSignedV1.model_validate_json((out_dir / "as_signed.json").read_bytes())
    verify_as_signed_reproduction(as_measured, revisions, as_signed)
    return as_measured, revisions, as_signed
