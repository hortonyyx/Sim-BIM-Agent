"""Deterministic Schedule:Compact day-type completeness validation.

A `Schedule:Compact` must assign a value to *every* day type EnergyPlus runs —
the seven weekdays plus Holiday, the two design days, and the two custom days. If
any are left uncovered the schedule is incomplete. A well-behaved EnergyPlus build
reports "Schedule:Compact='X' has missing day types" as a severe error; the
EP 25.1.0 build in this container instead **segfaults during input processing**
(exit 139, no `.err` written) — observed on sm21 0-5 e2e, where 4_MEP authored
`For: Weekdays` + `For: Weekends Holidays` and stopped, leaving SummerDesignDay /
WinterDesignDay / CustomDay1 / CustomDay2 uncovered.

This gate runs on the *assembled* IDF before EnergyPlus, so an incomplete schedule
fails fast with a precise message instead of a silent crash. It is the deterministic
counterpart to the 4_mep/authoring.md prose rule: the LLM is instructed to always
emit `For: AllOtherDays` (or `For: AllDays`), but prose compliance is not guaranteed,
so the invariant is enforced in code — same philosophy as the InterZone pair gate
(src/validator/interzone.py).

It inspects the eppy `IDF` object directly (not the natural-language `schedule_specs`),
so it validates whatever the schedule subagent actually produced, regardless of prompt.
"""

from __future__ import annotations

from eppy.modeleditor import IDF

from src.utils.logging import get_logger

logger = get_logger(__name__)

_SCHEDULE_OBJ = "SCHEDULE:COMPACT"

# The canonical day types EnergyPlus simulates; a Schedule:Compact must cover all.
_ALL_DAY_TYPES: frozenset[str] = frozenset(
    {
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Holiday",
        "SummerDesignDay",
        "WinterDesignDay",
        "CustomDay1",
        "CustomDay2",
    }
)

_WEEKDAYS = frozenset({"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"})
_WEEKENDS = frozenset({"Saturday", "Sunday"})

# Maps a single "For:" token (lower-cased) to the day types it covers. "AllDays" and
# "AllOtherDays" are handled specially (the latter is a remainder catch-all).
_TOKEN_COVERAGE: dict[str, frozenset[str]] = {
    "weekdays": _WEEKDAYS,
    "weekday": _WEEKDAYS,
    "weekends": _WEEKENDS,
    "weekend": _WEEKENDS,
    "holiday": frozenset({"Holiday"}),
    "holidays": frozenset({"Holiday"}),
    "summerdesignday": frozenset({"SummerDesignDay"}),
    "winterdesignday": frozenset({"WinterDesignDay"}),
    "customday1": frozenset({"CustomDay1"}),
    "customday2": frozenset({"CustomDay2"}),
    "sunday": frozenset({"Sunday"}),
    "monday": frozenset({"Monday"}),
    "tuesday": frozenset({"Tuesday"}),
    "wednesday": frozenset({"Wednesday"}),
    "thursday": frozenset({"Thursday"}),
    "friday": frozenset({"Friday"}),
    "saturday": frozenset({"Saturday"}),
}


def _for_clauses(schedule) -> list[str]:
    """Return the raw text after each `For:` keyword, in field order."""
    clauses: list[str] = []
    for fv in schedule.fieldvalues:
        text = str(fv).strip()
        if text.lower().startswith("for:"):
            clauses.append(text[len("for:") :].strip())
    return clauses


def _covered_day_types(schedule) -> tuple[set[str], list[str]]:
    """Accumulate the day types a schedule covers; collect any unknown tokens.

    `AllDays` covers everything; `AllOtherDays` (applied in field order) covers
    whatever has not yet been assigned — so it always completes coverage.
    """
    covered: set[str] = set()
    unknown: list[str] = []
    for clause in _for_clauses(schedule):
        for token in clause.split():
            key = token.lower()
            if key == "alldays":
                covered |= _ALL_DAY_TYPES
            elif key == "allotherdays":
                covered |= set(_ALL_DAY_TYPES)  # remainder catch-all completes it
            elif key in _TOKEN_COVERAGE:
                covered |= _TOKEN_COVERAGE[key]
            else:
                unknown.append(token)
    return covered, unknown


def validate_schedule_completeness(idf: IDF) -> list[str]:
    """Return Schedule:Compact day-type coverage issues (empty = clean).

    The string format mirrors `validate_interzone_surface_pairs` so callers can
    treat both gates the same way.
    """
    issues: list[str] = []
    for schedule in idf.idfobjects[_SCHEDULE_OBJ]:
        covered, unknown = _covered_day_types(schedule)
        if unknown:
            issues.append(
                f"Schedule:Compact '{schedule.Name}' has unrecognised For-day token(s) "
                f"{sorted(set(unknown))} — EnergyPlus may not assign them as expected"
            )
        missing = _ALL_DAY_TYPES - covered
        if missing:
            issues.append(
                f"Schedule:Compact '{schedule.Name}' is incomplete: day type(s) "
                f"{sorted(missing)} are uncovered. Add a 'For: AllOtherDays' catch-all "
                f"(or use 'For: AllDays'). EnergyPlus 25.1.0 here segfaults on this — "
                f"note 'For: Weekdays' + 'For: Weekends Holidays' still leaves the design "
                f"and custom days uncovered."
            )
    return issues
