"""S5 — 5_intakeoutput assembly backstop + EP baseline assertion (M2c, gate ①).

5_intakeoutput is deliberately thin (contracts §1 5): assemble + Pydantic + a
**backstop** of the 4_mep contract check. It does NOT own the MEP reference graph
(4 does) and writes no new parser — it re-runs the SAME ``validate_contract`` and
labels the results defense-in-depth, so they never participate in root-stage
attribution.

This module also hosts the EP end-state baseline assertion (contracts §1
downstream / build plan M2c): parse ``eplusout.end`` via runner.read_ep_end and
assert completion + 0 severe + a warning whitelist/threshold for test_baseline.
"""

from __future__ import annotations

from pathlib import Path

from src.agent.intakeoutput import validate_contract
from src.agent.state import IntakeOutput
from src.validator.checks.schema import CheckLayer, CheckReport, CheckStatus, RunProfile


def check_assembly(
    intake: IntakeOutput,
    used_constructions: set[str],
    *,
    capability_profile: str = "rectangular",
    run_profile: RunProfile = "exploratory",
) -> CheckReport:
    """Backstop: re-run the contract check (defense-in-depth) on the assembled
    IntakeOutput. A failure here means 4_mep's owner check was bypassed — still
    block, but attribution stays with 4_mep."""
    rep = CheckReport(
        stage="5_intakeoutput",
        capability_profile=capability_profile,
        run_profile=run_profile,
    )
    issues = validate_contract(intake, used_constructions)
    if issues:
        rep.add_fail("assembly.contract_backstop", CheckLayer.INVARIANT,
                     f"{len(issues)} contract issue(s) (owner: 4_mep)",
                     evidence={"issues": issues, "owner_stage": "4_mep"})
    else:
        rep.add_pass("assembly.contract_backstop", CheckLayer.INVARIANT,
                     evidence={"checked": len(used_constructions)})
    return rep


def check_ep_baseline(
    ep_run_dir: Path | str,
    *,
    max_warnings: int | None = None,
    capability_profile: str = "rectangular",
) -> CheckReport:
    """EP end-state baseline assertion: completed + 0 severe + (optional) warning
    threshold. ``eplusout.end`` missing → ERROR (fail-closed: a fatal/segfault
    leaves no .end, which must not read as PASS — the H3 class)."""
    from src.runner.runner import read_ep_end

    rep = CheckReport(stage="downstream", capability_profile=capability_profile)
    end = read_ep_end(ep_run_dir)
    if end is None:
        rep.add("ep.end_present", CheckStatus.ERROR, CheckLayer.INVARIANT,
                message="eplusout.end missing — EP did not finish (fatal/segfault), "
                        "not a PASS")
        return rep
    rep.add_pass("ep.end_present", CheckLayer.INVARIANT)

    if end["completed"]:
        rep.add_pass("ep.completed", CheckLayer.INVARIANT)
    else:
        rep.add_fail("ep.completed", CheckLayer.INVARIANT,
                     "EP did not report 'Completed Successfully'",
                     evidence={"raw": end["raw"][:200]})

    if end["severe"] == 0:
        rep.add_pass("ep.zero_severe", CheckLayer.INVARIANT, evidence={"severe": 0})
    else:
        rep.add_fail("ep.zero_severe", CheckLayer.INVARIANT,
                     f"{end['severe']} severe error(s)", evidence={"severe": end["severe"]})

    if max_warnings is None:
        rep.add("ep.warning_threshold", CheckStatus.NOT_APPLICABLE,
                CheckLayer.CROSS_CHECK,
                message=f"no threshold set (observed {end['warnings']} warnings)",
                evidence={"warnings": end["warnings"]})
    elif end["warnings"] <= max_warnings:
        rep.add_pass("ep.warning_threshold", CheckLayer.CROSS_CHECK,
                     evidence={"warnings": end["warnings"], "max": max_warnings})
    else:
        rep.add_fail("ep.warning_threshold", CheckLayer.CROSS_CHECK,
                     f"{end['warnings']} warnings > threshold {max_warnings}",
                     evidence={"warnings": end["warnings"], "max": max_warnings})
    return rep
