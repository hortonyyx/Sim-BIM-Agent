#!/usr/bin/env python3
"""Validated cv_probe request wrapper for isolated reading workspaces."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This set is the authorization policy for what a reading worker may invoke —
# guard.py's PROBE_TOOL_NAMES exists only to phrase error messages and defers to
# this list. Withdrawing a tool here is what actually removes the capability.
#
# 2026-08-15: `prescan-plan` / `prescan-elevation` withdrawn from this table;
# 2026-08-19 it was WITHDRAWN FROM THE WORKING TREE (user ruling — a half-dead
# feature whose code shipped while its authorization did not). Code + tests + restore
# steps are archived under AI_agent/archive/reading_prescan/;
# whether prescan returns is a reading 专项 decision, not an abandonment. Historical rationale: six draws across 07-07..08-15 showed the reader
# always opens with prescan and then stops at the
# candidate layer; rolling the "spend fewer crops" wording back to the 07-07 text
# changed nothing (C1/C2). The remaining hypothesis is that the free macro probe
# is simply the path of least resistance, so the withdrawal is at the capability
# layer rather than in the skill doc. The recipes stay implemented for the reading
# workstream to revive deliberately — see
# AI_agent/logs/experiments/2026-08-15_reading_restart/speedup_batch_ledger.md.
ALLOWED_TOOLS = {
    "crop_zoom",
    "wall_line_profiler",
    "storey_line_profiler",
    "px_m_calibrator",
    "window_cc_detector",
    "overlay_logger",
}
PATH_KEYS = {"image", "out_dir", "anchors_json", "candidates_json"}
# These two cv_probe inputs intentionally accept either an inline JSON
# object/array or a path to a JSON file.  The dispatch is lexical and stable:
# after leading whitespace, `[` or `{` means inline JSON; every other string
# keeps the existing staging-contained path resolution below.
JSON_OR_PATH_KEYS = {"anchors_json", "candidates_json"}
# R2-2: parameters this wrapper turns into an OUTPUT LANDING POINT. `out_dir` is
# the only one across all of ALLOWED_TOOLS (sidecar/crop/overlay paths
# are all derived from it; `sidecar_name` and `label` are regex-pinned name
# components that cannot traverse). It must resolve into the writable root —
# "inside staging" is not enough, or a request can make this wrapper write into
# the read-only parts of the tree such as `tools/**`.
# This mirrors guard.py's REQUEST_OUTPUT_ROLE_KEYS / OUTPUT_ROOT_DIR on purpose:
# the hook and the wrapper must never disagree about where output may land. The
# rule for what counts as a writable root is not mirrored but SHARED — see
# _writable_root below (R3-2).
OUTPUT_ROLE_KEYS = {"out_dir"}
OUTPUT_ROOT_DIR = "out"
# P1-2: cv_probe options declared with `action="store_true"` — they take no value
# on the cv_probe command line. The direct form still spells them as a PAIR
# (`--no-cc true`) because the guard's parser is strictly paired; this is where
# the pair is folded back into the flag, exactly as the request JSON's boolean
# values already were (`_request_to_argv`'s bool branch).
# 2026-08-19: emptied when prescan was deleted — `no_cc` was its only boolean flag.
# Kept as a (now empty) hook because the parse path still branches on it; a non-empty
# set here must correspond to a flag some authorized tool actually accepts.
BOOLEAN_FLAG_KEYS: set[str] = set()
_BOOLEAN_WORDS = {"true": True, "false": False}

_USAGE_TEXT = """\
Usage:
  python tools/run_cv_probe.py --tool <tool> --image case_data/<image>.png --out-dir out/cv [--key value ...]
  python tools/run_cv_probe.py --request requests/<request>.json
  python tools/run_cv_probe.py --batch requests/<batch>.json

Copy/paste examples:
  python tools/run_cv_probe.py --tool px_m_calibrator --image case_data/<image>.png --out-dir out/cv --anchors-json '[{\"axis\":\"x\",\"px_a\":12345,\"px_b\":67890,\"value_m\":12.345,\"dimension_ref\":\"example_span\"}]'
  # requests/calibrate.json: {\"tool\":\"px_m_calibrator\",\"args\":{\"image\":\"case_data/<image>.png\",\"out_dir\":\"out/cv\",\"anchors_json\":[{\"axis\":\"x\",\"px_a\":12345,\"px_b\":67890,\"value_m\":12.345,\"dimension_ref\":\"example_span\"}]}}
  python tools/run_cv_probe.py --request requests/calibrate.json
  # requests/sweep.json: {\"requests\":[{\"id\":\"calibrate_x\",\"tool\":\"px_m_calibrator\",\"args\":{\"image\":\"case_data/<image>.png\",\"out_dir\":\"out/cv\",\"anchors_json\":[{\"axis\":\"x\",\"px_a\":12345,\"px_b\":67890,\"value_m\":12.345,\"dimension_ref\":\"example_span\"}]}}]}
  python tools/run_cv_probe.py --batch requests/sweep.json

Run `python tools/run_cv_probe.py --help` to see this text again.
"""


def _staging_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (FileNotFoundError, ValueError):
        return False


def _resolve(value: str, root: Path) -> Path:
    if value.startswith("~") or ".." in value:
        raise ValueError(f"path token forbidden: {value}")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    if not _under(resolved, root):
        raise ValueError(f"path escapes staging: {value}")
    if path.exists() and not _under(path.resolve(strict=True), root):
        raise ValueError(f"symlink target escapes staging: {value}")
    return resolved


def _writable_root(root: Path, name: str) -> Path:
    """R3-2: delegate to guard.py — the ONE implementation of "a root the reader
    may write into".

    This function used to be `(root / name).resolve(strict=False)`, i.e. exactly
    the definition R2-3 removed from the guard because it lets a symlinked root
    reverse-authorize its target. The two enforcement points therefore disagreed:
    with `out -> tools` pre-seeded, the hook refused every call while this
    wrapper, invoked directly, really wrote six entries under `tools/**`. Rather
    than copy the pinned rule (which is how policies drift), the wrapper now
    calls the guard's `writable_root`, so there is a single implementation.

    The import is deliberately unguarded: `guard.py` always sits at the staging
    root next to `tools/`, and if it is missing or unimportable the ImportError
    propagates and this wrapper refuses to run — fail-closed, which is the only
    safe reading of "the policy owner is gone".
    """
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from guard import writable_root  # noqa: WPS433 — staged next to tools/

    return writable_root(root, name)


def _parse_batch(data, root: Path) -> list[tuple[str, dict]]:
    """Use guard.py's canonical bounded-envelope parser.

    Sharing this parser keeps the batch-size cap, stable-id rule, and exact
    envelope shape identical at the authorization and execution layers.  As
    with :func:`_writable_root`, a missing policy owner fails closed.
    """
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from guard import parse_probe_batch  # noqa: WPS433 — staged next to tools/

    return parse_probe_batch(data)


def _resolve_output(value: str, root: Path) -> Path:
    resolved = _resolve(value, root)
    # Outside the try: a tampered/absent writable root must surface with the
    # guard's own reason, not be re-labelled as an out-of-root output path.
    allowed = _writable_root(root, OUTPUT_ROOT_DIR)
    try:
        resolved.relative_to(allowed)
    except ValueError:
        raise ValueError(
            f"output path must land under {OUTPUT_ROOT_DIR}/, not {value!r}"
        ) from None
    return resolved


def _request_to_argv(request: dict, root: Path) -> list[str]:
    tool = request.get("tool")
    args = request.get("args")
    if tool not in ALLOWED_TOOLS:
        raise ValueError(
            f"unsupported cv_probe tool: {tool!r}; use a listed tool after --tool "
            "(run --help for copyable examples)"
        )
    if not isinstance(args, dict):
        raise ValueError(
            "request args must be an object; use: "
            '{"tool":"px_m_calibrator","args":{"image":"case_data/<image>.png",'
            '"out_dir":"out/cv","anchors_json":[{"axis":"x","px_a":12345,'
            '"px_b":67890,"value_m":12.345,"dimension_ref":"example_span"}]}}'
        )
    argv = [tool]
    for key, value in args.items():
        opt = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            if value:
                argv.append(opt)
            continue
        if key in OUTPUT_ROLE_KEYS:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a path string")
            value = str(_resolve_output(value, root))
        elif key in JSON_OR_PATH_KEYS:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            elif not isinstance(value, str):
                raise ValueError(
                    f"{key} must be an inline JSON object/array or a path string"
                )
            elif not value.lstrip().startswith(("[", "{")):
                value = str(_resolve(value, root))
        elif key in PATH_KEYS:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a path string")
            value = str(_resolve(value, root))
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        elif value is None:
            continue
        argv.extend([opt, str(value)])
    return argv


def _direct_to_request(argv: list[str]) -> dict:
    """P1-2: fold the direct one-call form into the SAME request shape the JSON
    path already uses, so both forms converge on ``_request_to_argv``.

    Nothing about *where files may land* is decided twice: the tool allowlist,
    the path resolution and — critically — the ``out_dir`` writable-root
    constraint all keep running in exactly one place. This function only does the
    shape conversion, and it applies the same strict pairing the guard's parser
    does so a call that somehow reaches the wrapper without passing the hook
    (direct invocation, the TOCTOU shape) is refused on the same grounds.

    The per-parameter allowlist is deliberately NOT duplicated here: cv_probe's
    own argparse rejects an option that its subparser does not declare, and it
    does so per tool, which is strictly finer than a flat list. Copying the
    guard's 27-key tuple into a second file would only create a drift surface.
    """
    args: dict = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            hint = (
                f"; tool names go after --tool; did you mean --tool {token}?"
                if token in ALLOWED_TOOLS
                else "; use only --key value pairs (start with --tool <tool>)"
            )
            raise ValueError(
                "probe arguments must be paired --key value; "
                f"unexpected bare argument: {token}{hint}"
            )
        spelling = token[2:]
        key = spelling.replace("-", "_")
        if not key:
            raise ValueError("probe parameter name is empty")
        if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
            raise ValueError(
                f"probe parameter --{spelling} is missing its value; "
                f"write --{spelling} <value>"
            )
        if key in args:
            raise ValueError(f"repeated probe parameter --{spelling}")
        value = argv[index + 1]
        if key in BOOLEAN_FLAG_KEYS:
            if value.lower() not in _BOOLEAN_WORDS:
                raise ValueError(f"--{spelling} takes true or false, not {value!r}")
            value = _BOOLEAN_WORDS[value.lower()]
        args[key] = value
        index += 2
    if "tool" not in args:
        raise ValueError("direct probe form requires --tool")
    return {"tool": args.pop("tool"), "args": args}


def _run_batch(batch_data, root: Path) -> int:
    """Preflight every request, then execute and emit one aggregate result.

    No probe is executed until the complete envelope has passed the shared
    batch parser, path/output resolution, tool allowlist, and cv_probe's
    tool-specific argparse parser.  A partly invalid batch therefore produces
    no sidecars, including when this wrapper is invoked without the hook.
    """
    entries = _parse_batch(batch_data, root)
    cv_argvs = [
        (request_id, request["tool"], _request_to_argv(request, root))
        for request_id, request in entries
    ]

    if str(root / "tools") not in sys.path:
        sys.path.insert(0, str(root / "tools"))
    from cv_probe import execute_probe, parse_probe_args  # noqa: WPS433

    # Parse ALL requests before executing the first.  Keep each parser beside
    # its Namespace because parser.error remains the canonical tool error path.
    planned = [
        (request_id, tool, *parse_probe_args(cv_argv))
        for request_id, tool, cv_argv in cv_argvs
    ]

    results = []
    for request_id, tool, args, parser in planned:
        sidecar_path = Path(execute_probe(args, parser)).resolve(strict=True)
        try:
            sidecar_rel = sidecar_path.relative_to(root.resolve(strict=True))
        except ValueError:
            raise ValueError(
                f"cv_probe returned evidence outside staging: {sidecar_path}"
            ) from None
        result = json.loads(sidecar_path.read_text(encoding="utf-8"))
        results.append(
            {
                "id": request_id,
                "tool": tool,
                "sidecar": sidecar_rel.as_posix(),
                "result": result,
            }
        )

    print(
        json.dumps(
            {
                "batch_schema": "1",
                "request_count": len(results),
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


# F-54: every validation/parsing step below this point (path resolution, file
# reads, JSON decoding, the direct-form and request-form converters) can raise
# one of these two types. None of it used to be wrapped in a try/except, so a
# ValueError/OSError/json.JSONDecodeError (JSONDecodeError IS a ValueError
# subclass, so it needs no separate entry) propagated out of main() as a raw,
# uncaught Python traceback at exit code 1 -- unlike cv_probe.py's own
# argparse-based parser.error() paths, which give clean usage text at exit
# code 2. This is the wrapper's OWN entry point (guard.py's PreToolUse
# validation reuses these same exception types to reject bad input before the
# wrapper subprocess ever starts on the normal product path, so the gap is
# mostly invisible there) -- but this file is also invoked directly by
# anything that bypasses the hook, which is exactly the shape this project's
# own test methodology uses (real staging + subprocess against the staged
# wrapper, deliberately not going through guard.py).
_EXPECTED_ERRORS = (ValueError, OSError)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = _staging_root()
    if argv == ["--help"]:
        print(_USAGE_TEXT, end="")
        return 0
    try:
        if any(arg == "--request" or arg.startswith("--request=") for arg in argv):
            # Form A — unchanged: same parser, same required flag, same errors.
            parser = argparse.ArgumentParser(description=__doc__)
            parser.add_argument("--request", required=True, type=Path)
            ns = parser.parse_args(argv)
            request_path = _resolve(str(ns.request), root)
            request = json.loads(request_path.read_text(encoding="utf-8"))
        elif any(arg == "--batch" or arg.startswith("--batch=") for arg in argv):
            # Form C — one bounded batch file.  Parse the option exactly so it
            # cannot be mixed with direct arguments or the legacy request form.
            parser = argparse.ArgumentParser(description=__doc__)
            parser.add_argument("--batch", required=True, type=Path)
            ns = parser.parse_args(argv)
            batch_path = _resolve(str(ns.batch), root)
            if batch_path.suffix != ".json":
                raise ValueError("batch must be a JSON file")
            batch_data = json.loads(batch_path.read_text(encoding="utf-8"))
            return _run_batch(batch_data, root)
        else:
            # Form B (P1-1/P1-2) — direct `--key value` arguments.
            request = _direct_to_request(argv)
        cv_argv = _request_to_argv(request, root)
        sys.path.insert(0, str(root / "tools"))
        from cv_probe import main as cv_main  # noqa: WPS433

        return int(cv_main(cv_argv) or 0)
    except _EXPECTED_ERRORS as exc:
        # Preserve the exception's message verbatim -- these messages were
        # already written with care (e.g. _request_to_argv's copyable JSON
        # example, _direct_to_request's "did you mean --tool X?" hint); the
        # bug was that the text was buried in a stack trace, not that the
        # text itself was wrong. argparse's own error paths raise SystemExit,
        # which is not an Exception subclass, so they are never intercepted
        # here and keep their existing usage-text behavior untouched.
        print(f"run_cv_probe.py: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
