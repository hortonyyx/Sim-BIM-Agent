import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import cast

from dotenv import load_dotenv
from eppy.modeleditor import IDF

from src.utils.logging import get_logger

load_dotenv()

# Resolution order for the EnergyPlus executable:
#   1) $ENERGYPLUS_EXE env var (explicit override, set in .env)
#   2) `energyplus` on PATH (Linux container / CI default; nrel/energyplus image)
#   3) DEFAULT_ENERGYPLUS_EXE below (platform-specific last-resort fallback)
# On Windows this points at the known local install; on Linux/macOS we fall
# back to the bare name so a PATH lookup (step 2) is the only sane resolution.
DEFAULT_ENERGYPLUS_EXE = (
    r"D:\EnergyPlusV25-2-0\energyplus.exe"
    if sys.platform == "win32"
    else "energyplus"
)


def read_ep_end(output_directory: "Path | str") -> "dict | None":
    """Parse <output_directory>/eplusout.end.

    Returns a dict with keys:
        "completed": bool  — True if the line contains "Completed Successfully"
        "warnings":  int   — warning count (−1 if not found)
        "severe":    int   — severe error count (−1 if not found)
        "raw":       str   — the trimmed raw line

    Returns None if the file does not exist (e.g. EP crashed before writing it)
    or if any read error occurs.
    """
    end_file = Path(output_directory) / "eplusout.end"
    try:
        if not end_file.exists():
            return None
        raw = end_file.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None

    completed = "Completed Successfully" in raw

    warnings_match = re.search(r"(\d+)\s+Warning", raw)
    severe_match = re.search(r"(\d+)\s+Severe", raw)
    warnings = int(warnings_match.group(1)) if warnings_match else -1
    severe = int(severe_match.group(1)) if severe_match else -1

    return {"completed": completed, "warnings": warnings, "severe": severe, "raw": raw}


def resolve_energyplus_exe() -> str:
    """Return absolute path to the EnergyPlus executable, or raise."""
    candidate = os.environ.get("ENERGYPLUS_EXE") or shutil.which("energyplus") or DEFAULT_ENERGYPLUS_EXE
    if not Path(candidate).is_file():
        raise FileNotFoundError(
            f"EnergyPlus executable not found. Tried $ENERGYPLUS_EXE, PATH, and "
            f"default {DEFAULT_ENERGYPLUS_EXE}. Set ENERGYPLUS_EXE in .env or add "
            f"the install dir to PATH."
        )
    return candidate


class EnergyPlusRunner:
    def __init__(self, idf: IDF | None = None, idd_file_path: Path | None = None):
        """
        Initialize the EnergyPlusRunner.

        Args:
            idf: An instance of eppy.modeleditor.IDF
            idd_file_path: EnergyPlus IDD file path, required if idf is not provided
        """
        self.logger = get_logger(__name__)
        if idf:
            self.idf = idf
        else:
            try:
                IDF.setiddname(str(idd_file_path))
                self.idf = IDF(StringIO(""))
            except Exception:
                self.logger.exception(
                    "Must provide either an IDF instance or a valid IDD file path."
                )
                raise

        self.logger.info("EnergyPlusRunner initialized.")

    def run_idf(
        self,
        epw_file_path: Path | str,
        idf_file_path: Path | str | None = None,
        output_directory: Path | None = None,
    ) -> bool:
        """
        Run EnergyPlus IDF file

        Args:
            idf_file_path: IDF file path
            epw_file_path: EPW weather file path
            output_directory: Output directory, if None, a default directory will be created

        Returns:
            bool: True if the simulation ran successfully, False otherwise
        """
        if idf_file_path:
            self.idf_path = Path(idf_file_path)
            self.idf = IDF(str(self.idf_path))
        elif self.idf.idfname:
            idf_file_path = cast(str, self.idf.idfname)
            self.idf_path = Path(idf_file_path)
        else:
            raise ValueError(
                "IDF file path must be provided either via parameter or IDF instance."
            )
        self.epw_path = Path(epw_file_path)

        if not self.idf_path.exists():
            raise FileNotFoundError(f"IDF file not found: {self.idf_path}")
        if not self.epw_path.exists():
            raise FileNotFoundError(f"EPW file not found: {self.epw_path}")

        if output_directory is None:
            output_directory = (
                Path(__file__).parent.parent.parent
                / "output"
                / "results"
                / f"energyplus_runs_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
        else:
            output_directory = Path(output_directory)
        output_directory.mkdir(parents=True, exist_ok=True)
        # Remove any stale eplusout.end from a previous run so that a crash
        # (e.g. segfault, exit 139) cannot be mistaken for success by
        # read_ep_end() reading the old file.
        (output_directory / "eplusout.end").unlink(missing_ok=True)

        self.logger.info("Starting EnergyPlus simulation...")
        self.logger.info("IDF file: {}", self.idf_path)
        self.logger.info("EPW file: {}", self.epw_path)
        self.logger.info("Output directory: {}", output_directory)

        try:
            energyplus_exe = resolve_energyplus_exe()
            self.logger.info("EnergyPlus exe: {}", energyplus_exe)

            cmd = [
                energyplus_exe,
                "-x",
                "-w",
                str(self.epw_path),
                "-d",
                str(output_directory),
                "-r",
                str(self.idf_path),
            ]

            self.logger.info("Running command: {}", " ".join(cmd))

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines = []
            for line in process.stdout or []:
                line = line.rstrip()
                self.logger.info("[EnergyPlus] {}", line)
                output_lines.append(line)

            return_code = process.wait()

            if return_code != 0:
                self.logger.error("EnergyPlus exited with code {}", return_code)
                return False

            self.logger.info("EnergyPlus simulation completed successfully.")
            return True

        except FileNotFoundError:
            self.logger.error("EnergyPlus executable not found.")
            return False

        except Exception:
            self.logger.exception("Running EnergyPlus simulation failed")
            raise
