import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.converter_manager import ConverterManager
from src.mcp.interface import ToolResponse
from src.mcp.state import ConfigState
from src.runner.runner import EnergyPlusRunner, read_ep_end
from src.utils.logging import get_logger
from src.validator.interzone import (
    audit_interzone_surface_pairs,
    validate_interzone_surface_pairs,
)
from src.validator.schedules import validate_schedule_completeness

if TYPE_CHECKING:
    from src.agent.output_coordinates import (
        OutputCoordinateContract,
        OutputCoordinateValidationContext,
    )

logger = get_logger(__name__)


def _check_interzone_pairs(manager: ConverterManager) -> list[str]:
    """Deterministic InterZone surface-pair gate on the assembled IDF.

    Run after `convert_all()` and before EnergyPlus so a missing / non-
    reciprocal / degenerate pair fails fast with a precise message instead of a
    late EP fatal or a silent wrong-physics pass. See
    src/validator/interzone.py and the 2026-05-28 InterZone review.

    Reads the live `manager._idf` (read-only); the `manager.idf` property
    deep-copies an IDF backed by a StringIO that may already be closed.
    """
    idf = manager._idf
    issues = validate_interzone_surface_pairs(idf)
    audit = audit_interzone_surface_pairs(idf, issues=issues)
    logger.info("InterZone surface-pair audit: {}", audit)
    if issues:
        logger.error(
            "InterZone surface-pair validation found {} issue(s):", len(issues)
        )
        for issue in issues:
            logger.error("  - {}", issue)
    return issues


def _check_schedules(manager: ConverterManager) -> list[str]:
    """Deterministic Schedule:Compact day-type completeness gate on the IDF.

    Run before EnergyPlus so an incomplete schedule (missing AllOtherDays /
    design-day coverage) fails fast with a precise message instead of the EP
    25.1.0 input-stage segfault. See src/validator/schedules.py and the
    4_mep/authoring.md schedule rule it enforces in code.
    """
    issues = validate_schedule_completeness(manager._idf)
    n = len(manager._idf.idfobjects["SCHEDULE:COMPACT"])
    logger.info("Schedule completeness audit: {} Schedule:Compact checked, {} issue(s)", n, len(issues))
    if issues:
        logger.error("Schedule completeness validation found {} issue(s):", len(issues))
        for issue in issues:
            logger.error("  - {}", issue)
    return issues


class WorkflowTool:
    """Tool for EnergyPlus workflow operations.

    Provides high-level operations for exporting/loading YAML configurations,
    validating references, running simulations, and managing overall state.
    Unlike other tools, this does not inherit from BaseTool as it operates
    on the entire configuration rather than individual components.
    """

    def __init__(
        self,
        state: ConfigState,
        *,
        output_coordinates: "OutputCoordinateContract | None" = None,
        validation_context: "OutputCoordinateValidationContext | None" = None,
        zone_frame_normalizations=(),
    ):
        self.state = state
        # E4-output-contract spec v2 §5.2 call point 6 / §8.2: the contract is
        # handed in EXPLICITLY by the caller (simulate_node passes it from
        # AgentState); this tool never reverse-engineers a mode from GGR or
        # Building field values. `None` = the explicit `legacy_unbound`
        # standalone-MCP policy: only a World / North-Axis-0 legacy config may
        # be exported, and a Relative ConfigState without a contract is a hard
        # gate failure (a field value cannot "claim" E4 membership).
        self.output_coordinates = output_coordinates
        self.validation_context = validation_context
        # §7.4 evidence is immutable before it reaches this boundary.  Keep a
        # tuple even when callers hand us a list so a later caller mutation
        # cannot alter an already-decided export audit.
        self.zone_frame_normalizations = tuple(zone_frame_normalizations)

    def _coordinate_gate(self, *, idf=None) -> list[str]:
        """Output-coordinate gate, run pre-YAML-export (idf=None) and again
        post-convert (idf=<live eppy IDF>). Returns human-readable issue
        strings in the same shape as the interzone/schedule gates."""
        contract = self.output_coordinates
        if contract is None:
            issues: list[str] = []
            ggr = self.state.global_geometry_rules
            if ggr.coordinate_system == "Relative":
                issues.append(
                    "output-coordinates: ConfigState declares GlobalGeometryRules "
                    "Coordinate System=Relative but no OutputCoordinateContract was "
                    "provided — a Relative export requires the explicit E4 contract "
                    "(field values cannot claim E4 membership)"
                )
            building = self.state.building
            if building is not None and float(building.north_axis) != 0.0:
                issues.append(
                    "output-coordinates: standalone/legacy export requires "
                    f"Building.North Axis == 0.0, got {building.north_axis!r} — a "
                    "nonzero World-mode North Axis is ignored by EnergyPlus and "
                    "would only manufacture a false authority value"
                )
            return issues
        if self.validation_context is None:
            return [
                "output-coordinates: an OutputCoordinateContract was provided "
                "without its OutputCoordinateValidationContext — the hash-chain "
                "cannot be re-verified, refusing to export"
            ]
        from src.validator.output_coordinates import validate_output_coordinate_contract

        found = validate_output_coordinate_contract(
            self.state, contract, self.validation_context, idf=idf,
        )
        return [f"output-coordinates[{i.code}]: {i.message}" for i in found]

    def _write_coordinate_audit(self, output_dir: Path, *, yaml_path: Path, idf_path: Path, manager) -> None:
        """Downstream export audit (spec §7.4): bind the contract + snapshot
        hashes to the ACTUAL exported YAML/IDF raw bytes. Written only when an
        E4 contract is present; legacy standalone exports have no contract to
        bind. Best-effort content, but a write failure is loud."""
        from src.agent.output_coordinates import canonical_json_bytes, sha256_bytes
        from src.validator.output_coordinates import (
            COORDINATE_REGISTRY_VERSION,
            ExportCoordinateAuditV1,
            registry_candidate_sha256,
        )

        contract = self.output_coordinates
        if contract is None:
            return
        config_counts = sorted(
            (alias, len(getattr(self.state, name)))
            for name, alias in (("zones", "Zone"), ("surfaces", "BuildingSurface:Detailed"),
                                 ("fenestrations", "FenestrationSurface:Detailed"))
        )
        idf_counts = sorted(
            (obj_type, len(objs))
            for obj_type, objs in manager._idf.idfobjects.items()
            if objs
        )
        snapshot_bytes = (
            self.validation_context.raw_snapshot_bytes if self.validation_context is not None else None
        )
        audit = ExportCoordinateAuditV1(
            contract_sha256=sha256_bytes(canonical_json_bytes(contract)),
            snapshot_sha256=sha256_bytes(snapshot_bytes) if snapshot_bytes is not None else None,
            yaml_sha256=sha256_bytes(Path(yaml_path).read_bytes()),
            idf_sha256=sha256_bytes(Path(idf_path).read_bytes()),
            registry_version=COORDINATE_REGISTRY_VERSION,
            registry_candidate_sha256=registry_candidate_sha256(),
            config_counts=tuple(config_counts),
            idf_counts=tuple(idf_counts),
            zone_normalizations=self.zone_frame_normalizations,
            # This writer is reached only after both coordinate gates have
            # accepted the config and live IDF. A failed gate returns before
            # any accepted audit is written, so the tuple is exactly empty.
            offenders=(),
        )
        destination = Path(output_dir) / "output_coordinate_audit.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(audit.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(destination)

    def _write_ep_coordinate_audit(self, *, ep_run_dir: Path, idf_path: Path, completed: bool) -> None:
        """Bind the accepted export to actual post-EP bytes (§7.4).

        Export-only intentionally never calls this.  The generic workflow can
        attest the final IDF/EIO/ERR hashes and the forbidden World warning;
        the cross-variant azimuth/area assertions remain the dedicated E4 EP
        fixture's responsibility.
        """
        from src.agent.output_coordinates import canonical_json_bytes, sha256_bytes
        from src.validator.output_coordinates import EpCoordinateAuditV1

        contract = self.output_coordinates
        if contract is None:
            return
        eio = ep_run_dir / "eplusout.eio"
        err = ep_run_dir / "eplusout.err"
        err_bytes = err.read_bytes() if err.is_file() else None
        warning = b"Any non-zero Building/Zone North Axes or non-zero Zone Origins are ignored"
        audit = EpCoordinateAuditV1(
            contract_sha256=sha256_bytes(canonical_json_bytes(contract)),
            idf_sha256=sha256_bytes(idf_path.read_bytes()),
            eio_sha256=sha256_bytes(eio.read_bytes()) if eio.is_file() else None,
            err_sha256=sha256_bytes(err_bytes) if err_bytes is not None else None,
            ignored_warning_hits=(err_bytes or b"").count(warning),
            completed=completed,
        )
        destination = ep_run_dir / "output_coordinate_ep_audit.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(audit.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(destination)

    def export_yaml(self, output_path: str) -> ToolResponse:
        """Export the current configuration state to a YAML file.

        Args:
            output_path: File path for the output YAML file.

        Returns:
            ToolResponse with the absolute path of the exported file.
        """
        try:
            path = Path(output_path)
            self.state.export_yaml(path)
            return ToolResponse(
                success=True,
                message=f"Exported YAML to {path}",
                data={"path": str(path.absolute())},
            )
        except Exception as e:
            logger.exception("Error exporting YAML")
            return ToolResponse(
                success=False,
                message=f"Error exporting YAML: {e!s}",
            )

    def load_yaml(self, yaml_path: str) -> ToolResponse:
        """Load a YAML configuration file and replace the current state.

        Args:
            yaml_path: Path to the YAML file to load.

        Returns:
            ToolResponse with a configuration summary after loading.
        """
        try:
            path = Path(yaml_path)
            new_state = ConfigState.load_yaml(path)
            self.state.update_from(new_state)

            summary = self.state.get_summary()
            return ToolResponse(
                success=True,
                message=f"Loaded YAML from {path}",
                data={"summary": summary.model_dump()},
            )

        except Exception as e:
            logger.exception("Error loading YAML")
            return ToolResponse(
                success=False,
                message=f"Error loading YAML: {e!s}",
            )

    def validate_config(self) -> ToolResponse:
        """Validate all cross-references in the current configuration.

        Returns:
            ToolResponse with validation result. Includes error list on failure
            or configuration summary on success.
        """
        errors = self.state.validate_references()

        if errors:
            return ToolResponse(
                success=False,
                message=f"Validation failed: {len(errors)} reference errors found.",
                data={"errors": errors},
            )

        return ToolResponse(
            success=True,
            message="Validation passed.",
            data=self.state.get_summary().model_dump(),
        )

    def export_idf_only(self, output_dir: str = "./output") -> ToolResponse:
        """Run the validate -> export YAML -> convert IDF chain WITHOUT
        invoking EnergyPlus. Used by `--no-simulate` debugging path
        (2026-05-07 added for B0' surface bug iteration).
        """
        try:
            validation = self.validate_config()
            if not validation.success:
                return ToolResponse(
                    success=False,
                    message="Validation Reference Errors, cannot export IDF.",
                    data=validation.data,
                )

            pre_coord_issues = self._coordinate_gate(idf=None)
            if pre_coord_issues:
                return ToolResponse(
                    success=False,
                    message=(
                        f"Pre-export output-coordinate gate failed: "
                        f"{len(pre_coord_issues)} issue(s). YAML not exported."
                    ),
                    data={"output_coordinate_issues": pre_coord_issues},
                )

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            temp_yaml = Path(output_dir) / f"temp_{timestamp}.yaml"
            temp_idf = Path(output_dir) / f"temp_{timestamp}.idf"

            self.state.export_yaml(temp_yaml)
            manager = ConverterManager(temp_yaml)
            manager.convert_all()

            pair_issues = _check_interzone_pairs(manager)
            schedule_issues = _check_schedules(manager)
            coord_issues = self._coordinate_gate(idf=manager._idf)
            if pair_issues or schedule_issues or coord_issues:
                manager.save_idf(temp_idf)  # keep artifact for inspection
                gate_total = len(pair_issues) + len(schedule_issues) + len(coord_issues)
                return ToolResponse(
                    success=False,
                    message=(
                        f"Pre-EnergyPlus gate failed: {gate_total} issue(s) "
                        f"({len(pair_issues)} interzone, {len(schedule_issues)} "
                        f"schedule, {len(coord_issues)} output-coordinate). "
                        f"IDF not accepted."
                    ),
                    data={
                        "interzone_pair_issues": pair_issues,
                        "schedule_issues": schedule_issues,
                        "output_coordinate_issues": coord_issues,
                        "idf_path": str(temp_idf.absolute()),
                    },
                )

            manager.save_idf(temp_idf)
            self._write_coordinate_audit(
                Path(output_dir), yaml_path=temp_yaml, idf_path=temp_idf, manager=manager,
            )

            logger.info("IDF exported (no simulation): {}", temp_idf)
            return ToolResponse(
                success=True,
                message="IDF exported (simulation skipped).",
                data={"idf_path": str(temp_idf.absolute()), "output_dir": output_dir},
            )
        except Exception as e:
            logger.exception("Error exporting IDF")
            return ToolResponse(
                success=False, message=f"Error exporting IDF: {e!s}"
            )

    def run_simulation(
        self, epw_path: str, output_dir: str = "./output",
        ep_run_subdir: str | None = None,
    ) -> ToolResponse:
        """Run an EnergyPlus simulation with the current configuration.

        Validates references, exports to YAML, converts to IDF, and
        executes the EnergyPlus simulation.

        Args:
            epw_path: Path to the EPW weather data file.
            output_dir: Directory for IDF-related artifacts (temp_*.yaml/.idf).
            ep_run_subdir: When set, EnergyPlus run artifacts (eplusout.*) go to
                `output_dir/<ep_run_subdir>/` instead of `output_dir/` itself, so
                the IDF and the simulation outputs live in separate folders
                (standard case layout: EP/ holds the IDF, EP/EP_run/ the sim).
                When None (default), behaviour is unchanged — everything flat in
                output_dir.

        Returns:
            ToolResponse with IDF path and output directory on success.
        """
        try:
            validation = self.validate_config()
            if not validation.success:
                return ToolResponse(
                    success=False,
                    message="Validation Reference Errors, cannot run simulation.",
                    data=validation.data,
                )

            output_dir_path = Path(output_dir)
            output_dir_path.mkdir(parents=True, exist_ok=True)

            pre_coord_issues = self._coordinate_gate(idf=None)
            if pre_coord_issues:
                return ToolResponse(
                    success=False,
                    message=(
                        f"Pre-export output-coordinate gate failed: "
                        f"{len(pre_coord_issues)} issue(s). Simulation not started."
                    ),
                    data={"output_coordinate_issues": pre_coord_issues},
                )

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            temp_yaml = output_dir_path / f"temp_{timestamp}.yaml"
            temp_idf = output_dir_path / f"temp_{timestamp}.idf"

            self.state.export_yaml(temp_yaml)

            manager = ConverterManager(temp_yaml)
            manager.convert_all()

            pair_issues = _check_interzone_pairs(manager)
            schedule_issues = _check_schedules(manager)
            coord_issues = self._coordinate_gate(idf=manager._idf)
            if pair_issues or schedule_issues or coord_issues:
                manager.save_idf(temp_idf)  # keep artifact for inspection
                gate_total = len(pair_issues) + len(schedule_issues) + len(coord_issues)
                return ToolResponse(
                    success=False,
                    message=(
                        f"Pre-EnergyPlus gate failed: {gate_total} issue(s) "
                        f"({len(pair_issues)} interzone, {len(schedule_issues)} "
                        f"schedule, {len(coord_issues)} output-coordinate). "
                        f"Simulation not started."
                    ),
                    data={
                        "interzone_pair_issues": pair_issues,
                        "schedule_issues": schedule_issues,
                        "output_coordinate_issues": coord_issues,
                        "idf_path": str(temp_idf.absolute()),
                    },
                )

            manager.save_idf(temp_idf)
            self._write_coordinate_audit(
                output_dir_path, yaml_path=temp_yaml, idf_path=temp_idf, manager=manager,
            )

            # IDF stays in output_dir; EP run artifacts optionally nest in a
            # subdir so the assembled IDF and the simulation outputs separate.
            ep_run_dir = output_dir_path
            if ep_run_subdir:
                ep_run_dir = output_dir_path / ep_run_subdir
                ep_run_dir.mkdir(parents=True, exist_ok=True)

            runner = EnergyPlusRunner(idf=manager.idf)
            ok = runner.run_idf(epw_path, output_directory=ep_run_dir)
            end = read_ep_end(ep_run_dir)

            if not ok or end is None or not end["completed"]:
                # Read the tail of eplusout.err for diagnostic detail.
                err_file = ep_run_dir / "eplusout.err"
                err_tail: str | None = None
                try:
                    if err_file.exists():
                        lines = err_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        err_tail = "\n".join(lines[-40:])
                except Exception:
                    pass

                ep_end_msg = (
                    end["raw"]
                    if end is not None
                    else "no eplusout.end written (likely crash/segfault)"
                )
                logger.error("EnergyPlus FAILED: {}", ep_end_msg)
                return ToolResponse(
                    success=False,
                    message=f"EnergyPlus FAILED: {ep_end_msg}",
                    data={
                        "idf_path": str(temp_idf.absolute()),
                        "output_dir": str(output_dir_path.absolute()),
                        "ep_end": end,
                        "err_tail": err_tail,
                    },
                )

            logger.info(
                "Simulation completed: {} severe, {} warnings. Output directory: {}",
                end["severe"],
                end["warnings"],
                output_dir_path,
            )
            self._write_ep_coordinate_audit(
                ep_run_dir=ep_run_dir, idf_path=temp_idf, completed=True,
            )

            return ToolResponse(
                success=True,
                message=f"Simulation completed: {end['severe']} severe, {end['warnings']} warnings.",
                data={
                    "idf_path": str(temp_idf.absolute()),
                    "output_dir": str(output_dir_path.absolute()),
                    "ep_end": end,
                },
            )

        except Exception as e:
            logger.exception("Error running simulation")
            return ToolResponse(
                success=False,
                message=f"Error running simulation: {e!s}",
            )

    def get_summary(self) -> ToolResponse:
        """Get a summary of the current configuration state.

        Returns:
            ToolResponse with configuration summary data.
        """
        return ToolResponse(
            success=True,
            message="Configuration summary.",
            data=self.state.get_summary().model_dump(),
        )

    def clear_all(self) -> ToolResponse:
        """Clear all configuration state, resetting to defaults.

        Returns:
            ToolResponse confirming the state has been cleared.
        """
        self.state.clear()
        logger.info("All configuration cleared.")
        return ToolResponse(
            success=True,
            message="All configuration cleared.",
        )
