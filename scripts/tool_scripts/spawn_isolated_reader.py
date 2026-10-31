#!/usr/bin/env python3
"""Build, spawn, review, and merge isolated 0_reading workspaces."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.agent.execution.vision_resize import VISION_RESIZE_CHOICES
from src.agent.execution.isolation import (  # noqa: E402
    approve_pilot,
    build_isolation_workspace,
    merge_isolated_output,
    prepare_single_plan_experiment,
    spawn_command,
    write_feedback,
)


def _cmd_build(args: argparse.Namespace) -> int:
    manifest = build_isolation_workspace(
        args.case_dir,
        run_dir=args.run_dir,
        staging_root=args.staging_root,
        pilot_review_gate=not args.no_pilot_gate,
        guard_profile=args.guard_profile,
        vision_resize_tier=args.vision_resize_tier,
    )
    print(manifest.staging_root)
    return 0


def _cmd_prepare_single_plan(args: argparse.Namespace) -> int:
    result = prepare_single_plan_experiment(
        args.case_dir,
        args.run_dir,
        args.staging_root,
        input_id=args.input_id,
        model=args.model,
        capability_profile=args.capability_profile,
        guard_profile=args.guard_profile,
        vision_resize_tier=args.vision_resize_tier,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_spawn(args: argparse.Namespace) -> int:
    cmd = spawn_command(
        args.staging_root,
        model=args.model,
        execute=args.execute,
        directive=args.directive,
        resume=args.resume,
    )
    if not args.execute:
        print(shlex.join(cmd))
    return 0


def _cmd_feedback(args: argparse.Namespace) -> int:
    text = args.text
    if args.file:
        text = args.file.read_text(encoding="utf-8")
    path = write_feedback(args.staging_root, text, name=args.name)
    print(path)
    return 0


def _cmd_approve_pilot(args: argparse.Namespace) -> int:
    path = approve_pilot(args.staging_root)
    print(path)
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    attempt_dir = merge_isolated_output(
        args.staging_root,
        args.run_dir,
        output_path=args.output,
        accept=not args.no_accept,
    )
    print(json.dumps({"attempt_dir": str(attempt_dir)}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build")
    p.add_argument("--case-dir", required=True, type=Path)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--staging-root", type=Path)
    p.add_argument(
        "--no-pilot-gate",
        action="store_true",
        help=(
            "control-arm form: the generated kickoff explicitly overrides "
            "session_kickoff.md's pilot-review step, so the reader works straight "
            "through all images in one turn. Default keeps the gate."
        ),
    )
    p.add_argument(
        "--guard-profile",
        choices=["strict", "relaxed", "observe"],
        default="observe",
        help=(
            "'observe' (DEFAULT, exploratory tier): evaluate exactly as strict, "
            "log the would-be verdict as shadow_decision, enforce nothing. "
            "'relaxed' drops FRICTION ONLY (compound shell operators, the command "
            "allowlist, the words grade/attempts/verdict, URL-scheme strings in the "
            "all-file scan). 'strict' enforces. The answer boundary is byte-identical "
            "in all three (gt is physically absent from staging either way). "
            "Recorded in the audit dir and stamped on every access_log entry."
        ),
    )
    p.add_argument(
        "--vision-resize-tier",
        choices=list(VISION_RESIZE_CHOICES),
        default=None,
        help=(
            "F-51 staging resize tier. 'none' (the DEFAULT since 2026-08-19) stages "
            "the original drawing untouched. 'standard' matches "
            "Haiku 4.5 and other pre-4.7 Anthropic vision models; 'high_res' matches "
            "Claude 4.7+ (e.g. Sonnet 5) and is also the right choice for a reader "
            "that does NOT go through the Anthropic vision pipeline at all (codex / "
            "gpt-5.4-mini), because for these drawings it leaves the file untouched. "
            "Picking the wrong tier silently changes what the reader sees relative to "
            "what cv_probe measures."
        ),
    )
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser(
        "prepare-single-plan",
        help=(
            "create a new run, freeze one plan input + exact reader model, and "
            "build a same-session pilot-review isolation workspace"
        ),
    )
    p.add_argument("--case-dir", required=True, type=Path)
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--staging-root", required=True, type=Path)
    p.add_argument("--input-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument(
        "--capability-profile",
        choices=["rectangular", "orthogonal_polygon"],
        default="orthogonal_polygon",
    )
    p.add_argument(
        "--guard-profile", choices=["strict", "relaxed", "observe"], default="observe"
    )
    p.add_argument(
        "--vision-resize-tier",
        choices=list(VISION_RESIZE_CHOICES),
        default=None,
        help=(
            "F-51 staging resize tier. 'none' (the DEFAULT since 2026-08-19) stages "
            "the original drawing untouched. 'standard' matches "
            "Haiku 4.5 and other pre-4.7 Anthropic vision models; 'high_res' matches "
            "Claude 4.7+ (e.g. Sonnet 5) and is also the right choice for a reader "
            "that does NOT go through the Anthropic vision pipeline at all (codex / "
            "gpt-5.4-mini), because for these drawings it leaves the file untouched. "
            "Picking the wrong tier silently changes what the reader sees relative to "
            "what cv_probe measures."
        ),
    )
    p.set_defaults(func=_cmd_prepare_single_plan)

    p = sub.add_parser("spawn")
    p.add_argument("--staging-root", required=True, type=Path)
    p.add_argument("--model")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--directive", type=Path, help="per-run directive file appended to the spawn prompt (contamination-checked, persisted as staging/directive.md)")
    p.add_argument(
        "--resume",
        action="store_true",
        help=(
            "A1 session form: resume the session the previous round started "
            "(id read from the audit sibling dir, never from staging) instead of "
            "cold-starting. The first round must run without it. Default = cold "
            "start, i.e. today's behaviour, so turning this on is a single-variable change."
        ),
    )
    p.set_defaults(func=_cmd_spawn)

    p = sub.add_parser("feedback")
    p.add_argument("--staging-root", required=True, type=Path)
    p.add_argument("--text", default="")
    p.add_argument("--file", type=Path)
    p.add_argument("--name", default="feedback.md")
    p.set_defaults(func=_cmd_feedback)

    p = sub.add_parser(
        "approve-pilot",
        help=(
            "externally approve the sole completed plan pilot, binding its current "
            "hash; required after any feedback round before batch or merge"
        ),
    )
    p.add_argument("--staging-root", required=True, type=Path)
    p.set_defaults(func=_cmd_approve_pilot)

    p = sub.add_parser("merge")
    p.add_argument("--staging-root", required=True, type=Path)
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--no-accept", action="store_true")
    p.set_defaults(func=_cmd_merge)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
