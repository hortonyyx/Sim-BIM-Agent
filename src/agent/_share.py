import os
from pathlib import Path
from typing import Final

from src.validator import BaseSchema

MAX_RETRIES: Final[int] = 0

DEFAULT_OUTPUT_DIR: Final[Path] = Path("output")

IDD_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "dependencies"
    / "Energy+.idd"
)

AGENT_LANGUAGE: Final[str] = os.getenv("AGENT_LANGUAGE", "English")
"""Language every agent uses for narrative / summary / explanation text.

Applied project-wide by `language_directive()`. Override via the
`AGENT_LANGUAGE` env var (e.g. "English", "日本語"). Empty / "English"
collapses to a no-op.

EnergyPlus identifiers (tool arg values like object names, schedule
names, construction names, IDD enum choices such as 'Outdoors' /
'Weekdays' / 'FullExterior', numeric values) always remain English
ASCII regardless of this setting — they are consumed by the IDF parser,
not by humans.
"""


def language_directive() -> str:
    """Return a system-prompt suffix enforcing AGENT_LANGUAGE.

    Emits an empty string when the configured language is English, so
    existing prompts stay byte-identical in the default case.
    """
    lang = AGENT_LANGUAGE.strip()
    if lang.lower() in {"", "english", "en"}:
        return ""
    return (
        "\n\n=== Language ===\n"
        f"Write all narrative / summary / explanation text in {lang}. "
        f"This includes your final AIMessage, intake `*_specs` strings, "
        f"and any natural-language error reports.\n"
        "HOWEVER, the following MUST remain English ASCII regardless:\n"
        "  - Tool names and argument keys (name, zone_name, variable_name, ...)\n"
        "  - Argument values that are EnergyPlus identifiers (object names,\n"
        "    schedule names, construction names, material names, layer names)\n"
        "  - Enum choices defined by the IDD (e.g. 'Outdoors', 'Weekdays',\n"
        "    'FullExterior', 'MediumRough', 'SunExposed', 'NoSun', 'Yes', 'No')\n"
        "  - Numeric values, paths, file names\n"
        "Those identifiers are read by the EnergyPlus IDF parser, not by\n"
        "humans — never translate or transliterate them.\n"
    )


_SCHEMA_INITIALIZED = False


def ensure_schema_initialized() -> None:
    """Load the EnergyPlus IDD into BaseSchema once per process."""
    global _SCHEMA_INITIALIZED
    if _SCHEMA_INITIALIZED:
        return
    if not IDD_PATH.exists():
        raise FileNotFoundError(
            f"Energy+.idd not found at {IDD_PATH}. "
            "Ensure data/dependencies/Energy+.idd exists in the project root."
        )
    BaseSchema.set_idf(IDD_PATH)
    _SCHEMA_INITIALIZED = True
