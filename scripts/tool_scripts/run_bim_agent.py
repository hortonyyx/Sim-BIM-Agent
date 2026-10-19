"""Small subscription-driven BIM experiment; no legacy flow or solver stages.

The model sees an explicit image inventory, an optional frozen user building
declaration, and the tools below. MCP owns file access and geometry execution;
the model has no shell/repository tools. This is an experimental entry point,
not a complete product orchestrator.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # Own checkout must precede any editable install.

from PIL import Image as PILImage, ImageDraw, ImageFont
from mcp.server.fastmcp import Image
from mcp.types import CallToolResult, TextContent


from scripts.tool_scripts.bim_agent_guidance import REFERENCES, build_guide, filter_tool_catalog, tool_capabilities
from scripts.tool_scripts.bim_agent_inputs import freeze_building_input, freeze_plan_input
from scripts.tool_scripts.bim_agent_feedback import ImageFilename


def dump(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inclusive_runs(flags):
    """Return exact inclusive intervals of truthy values without gap bridging."""
    runs = []
    start = None
    for index, flag in enumerate([*flags, False]):
        if bool(flag) and start is None:
            start = index
        elif not bool(flag) and start is not None:
            runs.append((start, index - 1))
            start = None
    return runs


def _empty_profile_diagnostics(pixels, rgb, counts, minimum_count, support_length):
    """Explain an empty colour filter without inferring walls or changing it."""
    import numpy as np
    if int(counts.max()) >= minimum_count:
        return None
    packed = pixels.astype(np.uint32)
    packed = ((packed[..., 0] << 16) | (packed[..., 1] << 8) | packed[..., 2]).ravel()
    values, frequencies = np.unique(packed, return_counts=True)
    colors = np.column_stack(((values >> 16) & 255, (values >> 8) & 255, values & 255))
    distances = np.linalg.norm(colors.astype(float) - np.asarray(rgb), axis=1)
    nearest = int(distances.argmin())
    order = np.argsort(-frequencies, kind="stable")[:8]
    return {
        "reason": "no_pixels_match_requested_color" if int(counts.sum()) == 0
                  else "matching_pixels_below_support_threshold",
        "nearest_observed_color": {"rgb": colors[nearest].tolist(),
                                   "distance": round(float(distances[nearest]), 6),
                                   "pixel_count": int(frequencies[nearest])},
        "frequent_observed_colors": [
            {"rgb": colors[i].tolist(), "pixel_count": int(frequencies[i])} for i in order],
        "total_crop_pixels": int(packed.size),
        "maximum_support_count": int(counts.max()),
        "maximum_support_fraction": round(float(counts.max()) / support_length, 6),
        "minimum_count": int(minimum_count),
        "next_action": ("Inspect the original crop; choose a listed observed RGB or increase tolerance "
                        "toward nearest_observed_color.distance. Widen/move the crop if it misses the target."
                        if int(counts.sum()) == 0 else
                        "Matching ink exists: lower min_fraction toward maximum_support_fraction, narrow the "
                        "cross-axis crop, or inspect cross_axis_profile before switching axis."),
        "interpretation": "Empty/filter-excluded support does not prove an absent wall or open connection; no parameters were changed.",
    }


def _profile_axis(mask, box, axis, min_fraction):
    """Measure one projection of an existing mask, retaining exact peak support."""
    counts = mask.sum(axis=0 if axis == "x" else 1)
    projection_offset = box[0] if axis == "x" else box[1]
    support_offset = box[1] if axis == "x" else box[0]
    support_length = mask.shape[0] if axis == "x" else mask.shape[1]
    minimum_count = max(1, math.ceil(support_length * float(min_fraction)))
    runs = []
    for start, end in _inclusive_runs(counts >= minimum_count):
        peak = start + int(counts[start:end + 1].argmax())
        support = mask[:, peak] if axis == "x" else mask[peak, :]
        runs.append({
            "pixels": [start + projection_offset, end + projection_offset],
            "peak": peak + projection_offset,
            "max_count": int(counts[peak]),
            "max_fraction": round(float(counts[peak]) / support_length, 6),
            "support_intervals_at_peak": [
                [lo + support_offset, hi + support_offset]
                for lo, hi in _inclusive_runs(support)],
        })
    return counts, minimum_count, support_length, runs


def _profile_support_peaks(counts, offset):
    """Retain every positive local maximum plateau, without smoothing or ranking."""
    peaks = []
    start = 0
    for end in range(1, len(counts) + 1):
        if end < len(counts) and counts[end] == counts[start]:
            continue
        count = int(counts[start])
        left = int(counts[start - 1]) if start else 0
        right = int(counts[end]) if end < len(counts) else 0
        if count > 0 and count > left and count > right:
            peaks.append({"pixels": [start + offset, end - 1 + offset], "count": count})
        start = end
    return peaks


def _profile_positive_runs(counts, offset):
    """Preserve the legacy unthresholded runs and every local maximum plateau."""
    rows = []
    for start, end in _inclusive_runs(counts > 0):
        peak = start + int(counts[start:end + 1].argmax())
        rows.append(dict(pixels=[start + offset, end + offset], peak=peak + offset,
            max_count=int(counts[peak]), support_peaks=_profile_support_peaks(counts[start:end + 1], start + offset)))
    return rows


def _profile_excluded_support(counts, offset, minimum_count, support_length):
    """Expose positive ink omitted by the caller's threshold, without new candidates."""
    excluded = (counts > 0) & (counts < minimum_count)
    intervals = []
    for start, end in _inclusive_runs(excluded):
        values = counts[start:end + 1]
        intervals.append({
            "pixels": [start + offset, end + offset],
            "min_count": int(values.min()), "max_count": int(values.max()),
        })
    return {
        "coordinate_count": int(excluded.sum()),
        "matching_pixels": int(counts[excluded].sum()),
        "minimum_count": minimum_count,
        "support_length": support_length,
        "intervals": intervals,
        "note": "These coordinates contain matching ink but are below min_fraction; "
                "they are not empty background. Intervals use inclusive original pixels. "
                "Counts do not prove the same stroke continues between rows/columns. "
                "Compare the clean crop and cross-axis support, or choose a narrower crop "
                "or lower threshold after inspecting the original. No wall, opening or "
                "candidate is inferred; existing candidate IDs are unchanged.",
    }


def coordinate_grid_view(pic, region):
    """Label original pixels on a disposable model view, keeping its affine frame."""
    if min(pic.size) < 100:
        return pic, {"shown": False, "reason": "small unscaled detail"}
    pic = pic.copy()
    draw = ImageDraw.Draw(pic)
    label_size = max(12, min(28, round(max(pic.size) / 80)))
    try:
        font = ImageFont.truetype("arial.ttf", label_size)
    except OSError:
        font = ImageFont.load_default(size=label_size)
    x0, y0, x1, y1 = region
    sx, sy = pic.width / (x1-x0), pic.height / (y1-y0)
    step = 100 if max(x1-x0, y1-y0) < 800 else 200
    ticks = {"x": [], "y": []}
    def label(point, value):
        width = draw.textbbox((0, 0), value, font=font)[2]
        point = (min(point[0], max(3, pic.width-width-4)),
                 min(point[1], max(3, pic.height-label_size-4)))
        bounds = draw.textbbox(point, value, font=font)
        draw.rectangle((bounds[0]-2, bounds[1]-1, bounds[2]+2, bounds[3]+1), fill="black")
        draw.text(point, value, fill="white", font=font)
    for value in range(((x0+step-1)//step)*step, x1, step):
        x = round((value-x0)*sx)
        for y in range(0, pic.height, 16):
            draw.line([(x,y),(x,min(y+4,pic.height-1))], fill=(80,160,190))
        label((min(max(x+3, 3), pic.width-48), 3), f"x={value}")
        ticks["x"].append({"original_pixel":value,"display_pixel":x})
    for value in range(((y0+step-1)//step)*step, y1, step):
        y = round((value-y0)*sy)
        for x in range(0, pic.width, 16):
            draw.line([(x,y),(min(x+4,pic.width-1),y)], fill=(80,160,190))
        label((3,max(y+3,label_size+8)), f"y={value}")
        ticks["y"].append({"original_pixel":value,"display_pixel":y})
    return pic, {"shown":True,"units":"original image pixels","ticks":ticks,"label_font_size_px":label_size,
                 "note":"Blue dotted grid/white labels are viewing aids, not drawing evidence."}


def terminate_subscription(process):
    """Stop this invocation and nested review sessions, including their MCPs."""
    if os.name == "nt":
        # Scope termination to this invocation's PID and its descendants.
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, timeout=15)
        process.wait(timeout=15)
        return
    rows = subprocess.check_output(["ps", "-eo", "pid=,ppid="], text=True)
    parents = {int(pid): int(parent) for pid, parent in (row.split() for row in rows.splitlines())}
    descendants = {process.pid}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    groups = set()
    for pid in descendants:
        try:
            group = os.getpgid(pid)
            if group != os.getpgrp():
                groups.add(group)
        except ProcessLookupError:
            pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()


def run_guide(run: Path) -> str:
    """System prompt matching the run's admitted inputs and declared image kind."""
    manifest = json.loads((run / "inputs.json").read_bytes())
    mesh = bool(manifest.get("mesh_input"))
    kind = manifest.get("image_kind") or (("unknown" if mesh else "drawings") if manifest.get("images") else None)
    guide = build_guide(images=kind, mesh=mesh, **tool_capabilities(manifest))
    if manifest.get("drawing_review_policy"):
        from scripts.tool_scripts.bim_agent_guidance import DRAWING_PROCESS
        guide += "\n" + DRAWING_PROCESS
    return guide


MAIN_MODELS = ("claude-sonnet-5", "claude-sonnet-5-5")
WORKER_MODELS = {"haiku": "claude-haiku-4-5-20251001"}
WORKER_AGENT = "worker"
# Built-in Claude Code subagents run on other models; a worker run allows only WORKER_AGENT.
BUILTIN_AGENTS = ("claude", "claude-code-guide", "Explore", "general-purpose", "Plan", "statusline-setup")


def worker_agents(run: Path, workers: str) -> str:
    """Claude Code --agents JSON: one worker with this run's tools and guide."""
    prompt = ("You are a worker delegated by the coordinating model of this BIM run. Do only the "
              "delegated sub-task with the BIM tools, keep to its floor, facade or objects, and do not "
              "call finish_bim. Report results the coordinator can verify: draft or candidate ids, "
              "values with their original-pixel evidence, and anything unresolved.\n\n" + run_guide(run))
    return json.dumps({WORKER_AGENT: {
        "description": ("Claude Haiku 4.5 worker with this run's BIM tools and inputs. Give it one "
                        "self-contained sub-task, e.g. read one floor plan and build it, or read one "
                        "elevation's opening positions and heights. Several calls in one message run "
                        "in parallel. It cannot delegate further."),
        "prompt": prompt, "model": WORKER_MODELS[workers]}}, ensure_ascii=False)


def subscription(run: Path, prompt: str, *, model: str, name: str,
                 readonly: bool = False, timeout: int = 900,
                 log_run: Path | None = None, receipt_context: dict | None = None,
                 effort: str | None = None, exploratory_opus: bool = False,
                 main_model: str | None = None, workers: str | None = None):
    """Only the logged-in subscription; isolated cwd/env, explicit MCP tools.

    ``run`` is the MCP-visible workspace. ``log_run`` can retain a child
    observation's request, stream and receipt alongside its parent run.
    ``main_model`` and ``workers`` apply to the Claude main Sonnet call only.
    """
    if effort not in {None, "low", "medium"}:
        raise ValueError("effort must be low or medium when supplied")
    if model == "haiku" and effort is not None:
        raise ValueError("explicit effort is supported only for sonnet in this experiment")
    if model not in ({"sonnet", "haiku", "opus"} if exploratory_opus else {"sonnet", "haiku"}):
        raise ValueError("only configured subscription aliases are allowed")
    run = run.resolve()
    log_run = (log_run or run).resolve()
    manifest = json.loads((run / "inputs.json").read_bytes())
    provider = manifest.get("provider", "claude")
    if provider not in {"claude", "glm"}:
        raise ValueError("unsupported subscription provider")
    if provider == "glm" and exploratory_opus:
        raise ValueError("GLM routing cannot be combined with exploratory Opus")
    if (main_model or workers) and (provider != "claude" or model != "sonnet" or readonly):
        raise ValueError("main_model and workers apply only to the Claude main Sonnet invocation")
    if main_model is not None and main_model not in MAIN_MODELS:
        raise ValueError("main_model must be one of " + ", ".join(MAIN_MODELS))
    if workers is not None and workers not in WORKER_MODELS:
        raise ValueError("workers must be one of " + ", ".join(WORKER_MODELS))
    from src.agent.execution.subscription_json import _isolated_env, _redact_secrets, subscription_model_id
    routed_model = "glm-5.3-flash" if provider == "glm" else (main_model or subscription_model_id(model))
    launcher = ([sys.executable, str(ROOT / "scripts/glm_code.py")] if os.name == "nt"
                else [str(ROOT / "scripts/glm_code.sh")]) if provider == "glm" else ["claude"]
    command = [*launcher, "-p", "--model", routed_model,
               "--tools", "Agent" if workers else "",
               "--allowedTools", f"mcp__bim__*,Agent({WORKER_AGENT})" if workers else "mcp__bim__*",
               *(["--disallowedTools", ",".join(f"Agent({agent})" for agent in BUILTIN_AGENTS),
                  "--agents", worker_agents(run, workers)] if workers else []),
               "--permission-mode", "dontAsk",
               "--strict-mcp-config", "--setting-sources", "",
               "--settings", '{"disableAllHooks":true}',
               "--no-session-persistence", "--output-format", "stream-json", "--verbose",
               "--system-prompt", ("Answer only the supplied local visual question using tools. "
                                    "Inspect a suitable crop; cite original pixel locations of marks. "
                                    "Separate door arcs, gaps and dimension ticks. State uncertainty; "
                                    "For line evidence, compare both profile axes; peak support at a junction "
                                    "is not a whole wall's extent or thickness. Inspect beyond both ends "
                                    "and trace adjoining space boundaries before calling an endpoint free. "
                                    "do not infer a connection just because rooms are adjacent. "
                                    "For dimension labels, use a magnified clean crop and bind each "
                                    "transcription to its actual label box, dimension line and adjacent "
                                    "extension endpoints in original pixels. Keep different chains separate. "
                                    "Use map_dimension_chain only after transcription. If arithmetic or "
                                    "the claimed endpoints conflict, recheck that local evidence once, then "
                                    "report unknown instead of inventing a missing segment. "
                                    "Check inputs or view_image metadata for remaining_seconds, answer early, "
                                    "and near the limit state explicit unexamined items. Do not plan the whole building."
                                    if readonly else run_guide(run))]
    if not readonly or model == "sonnet":
        command.extend(["--effort", effort or "medium"])
    server = [sys.executable, str(Path(__file__).resolve()), "serve", str(run), "--enabled-only"]
    if readonly:
        server.append("--readonly")
    command.extend(["--mcp-config", json.dumps({"mcpServers": {"bim": {
        "command": server[0], "args": server[1:], "alwaysLoad": True}}})])
    started = time.monotonic()
    from src.agent_runtime.versions import external_run_identity
    role_models = {"local_observer" if readonly else "coordinator": {
        "route_id": f"{provider}-subscription-claude-code", "model": routed_model,
        "reasoning_effort": (effort or "medium") if not readonly or model == "sonnet" else None,
        "output_tokens": None, "output_limit_source": "client_default_not_exposed",
        "parameters": {"effort": (effort or "medium") if not readonly or model == "sonnet" else None}}}
    if workers:
        role_models[WORKER_AGENT] = {"route_id": "claude-subscription-claude-code",
            "model": WORKER_MODELS[workers], "reasoning_effort": None, "output_tokens": None,
            "output_limit_source": "client_default_not_exposed", "parameters": {}}
    version_identity = external_run_identity(ROOT, mode="single_model", role_models=role_models)
    record = {"requested_model": routed_model, "requested_role": model, "provider": provider,
              **version_identity,
              "channel": f"{provider} subscription; no paid API/fallback",
              "readonly": readonly, "timeout_seconds": timeout,
              "exploratory_opus": exploratory_opus,
              "effort": (effort or "medium") if not readonly or model == "sonnet" else None}
    if workers:
        record.update(workers=workers, worker_model=WORKER_MODELS[workers])
    if receipt_context:
        record.update(receipt_context)
    # Caller context cannot override the verified identity. This sidecar and
    # request/receipt retain the same invocation-specific version/configuration.
    record.update(version_identity)
    dump(log_run / f"{name}_versions.json", version_identity)
    dump(log_run / f"{name}_request.json", {**record, "prompt": prompt,
                                       "system_prompt": command[command.index("--system-prompt")+1]})
    stdout_path, stderr_path = log_run / f"{name}_stream.jsonl", log_run / f"{name}_stderr.log"
    with tempfile.TemporaryDirectory(prefix="bim-agent-cwd-") as cwd:
        with stdout_path.open("x") as stdout, stderr_path.open("x") as stderr:
            # No CLI side calls (title/summary Haiku, updater) inside a measured run.
            env = {**_isolated_env(), "ENABLE_TOOL_SEARCH": "false",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
            if provider == "glm":
                env.update(GLM_MODEL=routed_model, GLM_SMALL_MODEL=routed_model)
            deadline = manifest.get("deadline_epoch")
            if deadline is not None and deadline <= time.time():
                record.update(timed_out=True, model_process_started=False)
            else:
                process = subprocess.Popen(command, cwd=cwd, env=env,
                                           stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                           text=True, start_new_session=True)
                try:
                    available = min(timeout, max(0, deadline - time.time())) if deadline else timeout
                    process.communicate(prompt, timeout=available)
                    record["returncode"] = process.returncode
                except subprocess.TimeoutExpired:
                    terminate_subscription(process)
                    record["timed_out"] = True
    for path in (stdout_path, stderr_path):
        path.write_text(_redact_secrets(path.read_text()))
    record["elapsed_seconds"] = round(time.monotonic() - started, 2)
    for line in stdout_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            record["actual_model"] = event.get("model")
        if event.get("type") == "result":
            record["result"] = event
    if provider == "glm" and record.get("actual_model") != routed_model:
        record["routing_error"] = "GLM invocation did not confirm the requested image model"
    if provider == "claude" and model == "sonnet":
        used_models = record.get("result", {}).get("modelUsage", {})
        allowed = {routed_model} | ({WORKER_MODELS[workers]} if workers else set())
        if record.get("actual_model") != routed_model or any(used not in allowed for used in used_models):
            record["routing_error"] = f"Claude invocation did not stay on {' and '.join(sorted(allowed))}"
    dump(log_run / f"{name}_receipt.json", record)
    return record


DETAIL_MAX_TIMEOUT_SECONDS = 240
DETAIL_COMPLETION_RESERVE_SECONDS = 45
DETAIL_MIN_TIMEOUT_SECONDS = 15
DETAIL_OBSERVATION_LOCK = threading.Lock()


def prepare_detail_observation(
        toolkit: "Toolkit", question: str, images: list[str], name: str, *,
        timeout_seconds: float | None = None, deadline_epoch: float | None = None):
    """Make one immutable, image-only MCP workspace for a local observation."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("local review question must be non-empty")
    if not images:
        raise ValueError("choose at least one original image for local review")
    if len(set(images)) != len(images):
        raise ValueError("choose each local review image only once")
    if timeout_seconds is not None and deadline_epoch is not None:
        raise ValueError("choose either timeout_seconds or deadline_epoch for local review")
    if timeout_seconds is not None:
        if timeout_seconds <= 0:
            raise ValueError("local review timeout_seconds must be positive")
        deadline_epoch = time.time() + timeout_seconds
    if deadline_epoch is not None and deadline_epoch <= time.time():
        raise ValueError("local review deadline_epoch must be in the future")
    sources = {image: toolkit.image_path(image) for image in images}
    child = toolkit.run / name
    child.mkdir(exist_ok=False)
    child_images = child / "images"
    child_images.mkdir()
    selected = {}
    for image, source in sources.items():
        target = child_images / image
        shutil.copy2(source, target)
        with PILImage.open(target) as picture:
            size = list(picture.size)
        selected[image] = {"size": size, "sha256": digest(target)}
        if selected[image]["sha256"] != toolkit.manifest["images"][image]["sha256"]:
            raise ValueError("selected original image changed while preparing local review")
    # Keep the caller's wording verbatim. File isolation cannot make a leading
    # question independent if the caller itself includes a candidate claim.
    (child / "question.txt").write_text(question, encoding="utf-8")
    manifest = {
        "images": selected,
        "provider": toolkit.manifest.get("provider", "claude"),
        "input_mode": "isolated_detail_observation",
        "only_input": (
            "selected original image copies and local question; no parent scope, "
            "seed, candidates, history or evaluation"
        ),
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
    }
    if deadline_epoch is not None:
        # This is part of the immutable child input before its digest is recorded.
        # The readonly server reads it to expose remaining_seconds to its tools.
        manifest["deadline_epoch"] = deadline_epoch
    dump(child / "inputs.json", manifest)
    return child, digest(child / "inputs.json")


def review_detail_observation(
        toolkit: "Toolkit", question: str, images: list[str], *,
        timeout_seconds: float = DETAIL_MAX_TIMEOUT_SECONDS, invoke=subscription) -> dict:
    """Run one bounded, readonly local observation and retain parent receipts."""
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not DETAIL_MIN_TIMEOUT_SECONDS <= timeout_seconds <= DETAIL_MAX_TIMEOUT_SECONDS):
        raise ValueError(f"timeout_seconds must be between {DETAIL_MIN_TIMEOUT_SECONDS} and {DETAIL_MAX_TIMEOUT_SECONDS}")
    # FastMCP may serve sync tools concurrently. Keep allocation, receipt creation
    # and the bounded invocation together so two calls cannot both claim detail_01.
    with DETAIL_OBSERVATION_LOCK:
        used = len(list(toolkit.run.glob("detail_*_request.json")))
        if used >= 2:
            return {"error": "local review budget exhausted", "completed": False}
        parent_deadline = toolkit.manifest.get("deadline_epoch")
        remaining = toolkit.remaining_seconds()
        if remaining is not None:
            child_deadline = min(time.time() + timeout_seconds,
                                 float(parent_deadline) - DETAIL_COMPLETION_RESERVE_SECONDS)
            timeout = child_deadline - time.time()
            if timeout < DETAIL_MIN_TIMEOUT_SECONDS:
                return {"error": "insufficient remaining budget for local review",
                        "completed": False, "remaining_seconds": remaining}
            # Keep the parent completion reserve outside the child workspace too.
            # This exact absolute deadline avoids extending the child's budget while
            # it is being prepared or while the parent waits for it to return.
            # ``timeout`` is also capped by that deadline, not a rounded display value.
        else:
            timeout = timeout_seconds
            child_deadline = time.time() + timeout
        name = f"detail_{used + 1:02d}"
        child, input_sha256 = prepare_detail_observation(
            toolkit, question, images, name, deadline_epoch=child_deadline)
        source = {"run": name, "input_sha256": input_sha256,
                  "images": {image: toolkit.manifest["images"][image]["sha256"] for image in images}}
        # Image copying/manifest writing consume time too. Do not let the process
        # outlive the deadline the readonly tools were shown.
        timeout = min(timeout_seconds, child_deadline - time.time())
        result = invoke(child, f"Images: {images}\nQuestion: {question}", model="haiku", name=name,
                        readonly=True, timeout=timeout, log_run=toolkit.run,
                        receipt_context={"observation_source": source})
        result_event = result.get("result")
        completed = (bool(result_event) and not result_event.get("is_error", False)
                     and not result.get("timed_out", False) and result.get("returncode") == 0
                     and not result.get("routing_error"))
        return {"actual_model": result.get("actual_model"),
                "timed_out": result.get("timed_out", False),
                "returncode": result.get("returncode"),
                "result": result_event.get("result", "No completed answer") if result_event else "No completed answer",
                "is_error": result_event.get("is_error", False) if result_event else True,
                "completed": completed,
                "routing_error": result.get("routing_error"),
                "observation_source": source,
                "remaining_seconds": toolkit.remaining_seconds()}


def cost_receipt_summary(run: Path):
    """Read each root receipt once; detail child folders deliberately have none."""
    receipts = [json.loads(path.read_bytes()) for path in sorted(run.glob("*_receipt.json"))]
    estimates = [receipt.get("result", {}).get("total_cost_usd") for receipt in receipts]
    complete = all(isinstance(value, (int, float)) for value in estimates)
    partial = sum(value for value in estimates if isinstance(value, (int, float)))
    return receipts, {"estimated_cost_usd": partial if complete else None,
                      "cost_receipts_complete": complete,
                      "reported_partial_cost_usd": partial}


def delivery_tool_reply(result: dict) -> dict:
    """Keep full reports on disk; avoid losing a large handoff to CLI truncation."""
    from collections import Counter

    # The subscription client pretty-prints structured tool results. Measuring
    # only dense JSON underestimated run53's actual response by almost 30k chars.
    def fits(value):
        return len(json.dumps(value, ensure_ascii=False, indent=2)) <= 18000

    reply = {k:v for k,v in result.items() if k != 'opening_inventory'}
    if fits(reply):
        return reply
    large = {'facade_inventory', 'opening_review_scopes', 'facade_review_scopes',
             'current_reviews', 'stale_reviews', 'current_claim_state',
             'source_image_feedback', 'space_relation_review'}
    compact = {k:v for k,v in reply.items() if k not in large}
    state = result.get('current_claim_state', {})
    compact['current_claim_summary'] = {
        'state_counts': dict(Counter(row['state'] for row in state.get('claims', []))),
        'claims_with_missing_bindings': [row['id'] for row in state.get('claims', [])
                                        if row.get('missing_bindings')],
        'inherited_observation_count': len(state.get('inherited_observations', [])),
        'drawing_fidelity': 'not_evaluated',
    }
    height = result.get('height_coverage')
    projection = result.get('source_image_feedback', {})
    compact['source_image_feedback_summary'] = {key + '_count': len(projection.get(key, [])) for key in (
        'current_source_projections', 'old_source_projections', 'projection_errors',
        'registered_calibration_uncovered_floors', 'floors_without_registered_views')}
    relations = result.get('space_relation_review', {})
    compact['space_relation_review_summary'] = {key: value for key, value in relations.items()
                                               if not isinstance(value, (dict, list))}
    compact.update(response_compacted=True, full_delivery_report='delivery.json',
                   omitted_detail_fields=sorted(large | {'opening_inventory'}),
                   detail_note='Full nested evidence remains in delivery.json. Height endpoints and IDs '
                               'are available from check_openings(heights_only=true); claim_status(candidate) '
                               'contains current bindings. Summarized counts are not fidelity approval.',
                   review_scope_status_counts={key:dict(Counter(
                       row['review_status'] for row in result.get(key, [])))
                       for key in ['opening_review_scopes', 'facade_review_scopes']},
                   current_review_count=len(result.get('current_reviews', [])),
                   stale_review_count=len(result.get('stale_reviews', [])))
    if fits(compact):
        return compact
    # Even a long user note or hundreds of floors must not hide the actual
    # completion/uncertainty state behind a transport error.
    minimal = {key: compact[key] for key in (
        'candidate', 'saved_candidate', 'save_effects', 'viewer', 'source_model', 'source_model_sha256', 'counts',
        'selection_origin', 'drawing_fidelity', 'response_compacted',
        'full_delivery_report', 'review_scope_status_counts', 'current_review_count',
        'stale_review_count', 'source_image_feedback_summary', 'space_relation_review_summary',
        'room_use_review',
    ) if key in compact}
    minimal.update(detail_level='counts_only', more_details_omitted=True,
        generation_state=result.get('generation_status', {}).get('state'),
        source_validation_status=(result.get('source_validation') or {}).get('status'),
        source_validation_finding_count=len((result.get('source_validation') or {}).get('findings', [])),
        assumption_count=len(result.get('assumptions', [])),
        unresolved_count=len(result.get('generation', {}).get('unresolved', [])),
        adopted_unapplied_claim_count=len(result.get('adopted_unapplied_claims', [])),
        claim_state_counts=compact['current_claim_summary']['state_counts'],
        claims_with_missing_bindings_count=len(compact['current_claim_summary']['claims_with_missing_bindings']),
        failed_claim_application_count=sum(row.get('status') == 'failed'
                                           for row in result.get('claim_applications', [])))
    if 'floor_completeness' in result:
        floors = result['floor_completeness']
        minimal['floor_completeness'] = {key: floors[key] for key in (
            'complete_building', 'missing_candidate_images', 'floor_scope_source')}
    if 'mesh_review' in result:
        minimal['mesh_review'] = {key: result['mesh_review'][key] for key in
            ('status', 'complete_building', 'coverage', 'record_sha256', 'fidelity')
            if key in result['mesh_review']}
    if 'facade_counts' in result:
        counts = result['facade_counts']
        minimal['facade_counts'] = {key: counts[key] for key in ('summary', 'delivery_blocked', 'status', 'reason') if key in counts}
        minimal['facade_counts']['full_scopes'] = 'delivery.json:facade_counts.scopes'
    if height is not None:
        minimal['height_coverage'] = {key: height[key] for key in
            ('schema_version', 'candidate', 'summary', 'table_file', 'status', 'reason') if key in height}
        minimal['height_coverage']['details'] = 'Full per-opening table in table_file or delivery.json'
    views = result.get('input_view_status', {})
    minimal['input_view_summary'] = {
        'no_direct_view_count': len(views.get('no_direct_view_images', [])),
        'crop_only_count': len(views.get('crop_only_images', []))}
    return minimal


class Toolkit:
    def __init__(self, run: Path, readonly=False):
        self.run = run.resolve()
        self.manifest = json.loads((self.run / "inputs.json").read_bytes())
        self.readonly = readonly

    def candidate_budget(self):
        # Missing values belong to historical six-candidate experiments.
        limit = self.manifest.get("max_candidates", 6)
        if type(limit) is not int or limit <= 0:
            raise ValueError("max_candidates must be a positive integer")
        used = len(list(self.run.glob("candidate_*")))
        return {"limit": limit, "used": used, "remaining": max(0, limit - used)}

    def claims(self):
        from scripts.tool_scripts.bim_agent_claims import AgentClaimStore
        return AgentClaimStore(self)

    def claim_transaction(self, candidate, entries_json):
        from scripts.tool_scripts.bim_agent_claims import claim_transaction
        return claim_transaction(self, candidate, json.loads(entries_json))

    def record_claim(self, claim_json):
        if self.readonly:
            raise ValueError("only the coordinator may record candidate claims")
        data = json.loads(claim_json)
        if isinstance(data, dict) and data.get("observation_type") == "facade_count":
            from scripts.tool_scripts.bim_agent_facade_checks import record_facade_count
            result = record_facade_count(self, data)
        else:
            result = self.claims().record(data)
        self.log("record_claim", result)
        return result

    def decide_claim(self, claim_id, disposition, reason):
        if self.readonly:
            raise ValueError("only the coordinator may decide candidate claims")
        result = self.claims().decide(claim_id, disposition, reason)
        self.log("decide_claim", result)
        return result

    def replace_claim_sources(self, claim_id, view_ids, reason):
        if self.readonly:
            raise ValueError("only the coordinator may replace claim sources")
        result = self.claims().replace_sources(claim_id, view_ids, reason)
        result["next_action"] = "Inspect the returned sources, then use claim_transaction with the new claim_id to confirm/apply."
        self.log("replace_claim_sources", result)
        return result

    def confirm_claims(self, candidate, operations_json):
        if self.readonly:
            raise ValueError("only the coordinator may confirm candidate claims")
        from src.agent.execution.bim_claim_state import confirm
        result = confirm(self.claims(), candidate, json.loads(operations_json))
        result["height_coverage"] = self.located_heights(candidate)
        self.log("confirm_claims", result)
        return result

    def revise(self, candidate, operations_json):
        """Resolve evidence values, use the existing edits, then record actual outcome."""
        import copy
        from src.agent.execution.bim_claims import geometry_state, objects, sha, parameter_slots
        from src.agent.geometry.component_attributes import thickness_record
        from src.agent.geometry.proposal_edits import apply_proposal_edits
        if self.readonly:
            raise ValueError("only the coordinator may revise BIM")
        store = self.claims()
        submitted = json.loads(operations_json)
        def references(value):
            if isinstance(value, dict):
                found = {value["claim"]} if isinstance(value.get("claim"), str) else set()
                return found | set().union(*(references(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(references(v) for v in value))
            return set()
        application = store._write("application", {"parent_candidate": candidate,
            "submitted_operations": submitted, "claim_ids": sorted(references(submitted)),
            "status": "pending", "drawing_fidelity": "not_evaluated"})
        application_path = store.folder / f"{application['id']}.json"
        try:
            parent, parent_source = store.candidate(candidate)
            operations, evidence = store.resolve_operations(candidate, submitted)
            application.update(evidence=evidence, resolved_operations=operations)
            updated = copy.deepcopy(parent)
            for operation in operations:
                if operation.get("op") != "set_component_thickness":
                    updated = apply_proposal_edits(updated, [operation])
                    continue
                if set(operation) != {"op", "boundary_id", "thickness_m", "basis", "reason", "source_refs"}:
                    raise ValueError("set_component_thickness requires boundary_id/thickness_m/basis/reason/source_refs")
                if not isinstance(operation["reason"], str) or not operation["reason"].strip():
                    raise ValueError("thickness edit requires a reason")
                record = thickness_record(parent_source, operation["boundary_id"], operation["thickness_m"],
                    basis=operation["basis"], source_refs=operation["source_refs"])
                ids = set(record["boundary_ids"])
                before = updated.get("component_attributes", [])
                updated["component_attributes"] = [row for row in before if not ids.intersection(row["boundary_ids"])] + [record]
                updated["geometry"].setdefault("corrections", []).append({"operation": "set_component_thickness",
                    "reason": operation["reason"], "before": before,
                    "after": updated["component_attributes"], "geometry_effect": "none"})
            # Evidence and computed parameter mapping survive proposal-only recovery.
            if evidence["bindings"]:
                updated["geometry"].setdefault("corrections", []).append({"operation": "claim_application",
                    "application_id": application["id"], **evidence})
            old_objects, new_objects = objects(parent, parent_source), objects(updated, parent_source)
            changes = [{"kind": key[0], "id": key[1], "before": old_objects.get(key), "after": new_objects.get(key)}
                       for key in sorted(old_objects.keys() | new_objects.keys())
                       if key[0] != "boundary" and old_objects.get(key) != new_objects.get(key)]
            expected = set()
            supported = True
            for operation in operations:
                name = operation["op"]
                if name in {"update_opening", "update_window"}:
                    expected.add((name.removeprefix("update_"), operation["id"]))
                elif name == "add_opening":
                    expected.add(("opening", operation["opening"]["id"]))
                elif name == "remove_opening":
                    expected.update((kind, operation["id"]) for kind in ("window", "opening")
                                    if (kind, operation["id"]) in old_objects)
                elif name == "move_shared_wall":
                    expected.update(("space", identity) for identity in operation["space_ids"])
                elif name == "reshape_spaces":
                    expected.update(("space", row["id"]) for row in operation["spaces"])
                elif name not in {"set_component_thickness", "replace_note", "set_notes", "resolve_unbuilt_observation"}:
                    supported = False
            for audit in updated["geometry"].get("corrections", [])[len(parent["geometry"].get("corrections", [])):]:
                expected.update(("opening", row["id"]) for row in audit.get("moved_openings", []))
            application.update(geometry_before_sha256=sha(geometry_state(parent)),
                geometry_after_sha256=sha(geometry_state(updated)), changes=changes,
                scope_check="checked" if supported else "not_supported_for_all_operations",
                outside_declared_scope=[row for row in changes if (row["kind"], row["id"]) not in expected] if supported else None)
            bound_slots = {(row["operation_index"], row["parameter"]) for row in evidence["bindings"]}
            application["parameters_without_claims"] = [
                {"operation_index": index, "parameter": field}
                for index, operation in enumerate(operations)
                for _, _, field, _, _ in parameter_slots(operation)
                if (index, field) not in bound_slots]
            result = self.build(updated, action="revise_bim", parent=candidate, operations=operations,
                                claim_application={"file": application_path.relative_to(self.run).as_posix(), **evidence})
            application.update(candidate=result.get("candidate"), source_model_sha256=result.get("source_model_sha256"),
                status="applied" if result.get("source_geometry_ready") else "failed",
                source_geometry_ready=result.get("source_geometry_ready", False),
                error=result.get("error"), source_validation=result.get("source_validation"))
        except Exception as error:
            application.update(status="failed", error=str(error))
            dump(application_path, application)
            self.log("claim_application", application)
            raise
        dump(application_path, application)
        if result.get("candidate"):
            dump(self.candidate_path(result["candidate"]) / "application.json", application)
            result["height_coverage"] = self.located_heights(result["candidate"])
        self.log("claim_application", application)
        result["claim_application"] = application
        # Keep the saved provenance and its source digest unchanged. Only the
        # model-facing copy drops evidence already in the application file.
        if "provenance" in result:
            result["provenance"] = {**result["provenance"],
                "claim_application": {"file": application_path.relative_to(self.run).as_posix()}}
        return result

    def log(self, action, data):
        with (self.run / "tools.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"time": time.time(), "readonly": self.readonly,
                                     "action": action, "data": data}, ensure_ascii=False) + "\n")

    def candidate_path(self, candidate):
        allowed = {p.name for p in self.run.glob("candidate_*") if p.is_dir()}
        if (self.run / "seed").is_dir():
            allowed.add("seed")
        if candidate not in allowed:
            raise ValueError("unknown candidate")
        return self.run / candidate

    def remaining_seconds(self):
        deadline = self.manifest.get("deadline_epoch")
        return max(0, round(deadline - time.time())) if deadline else None

    def located_heights(self, candidate, current_state=None, *, compact=True):
        from scripts.tool_scripts.bim_agent_facade_checks import located_height_report
        try:
            report = located_height_report(self, candidate, current_state)
            report["table_file"] = (self.candidate_path(candidate) / "height_coverage.json").relative_to(self.run).as_posix()
            dump(self.run / report["table_file"], report)
            return report
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Advisory calculations must not turn a successful save into a failed build.
            return {"status": "unavailable", "reason": str(error), "delivery_blocked": False}

    def facade_counts(self, candidate):
        from scripts.tool_scripts.bim_agent_facade_checks import facade_count_report
        try:
            return facade_count_report(self, candidate)
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {"status": "unavailable", "reason": str(error), "delivery_blocked": False}

    def input_view_status(self):
        """Report direct original-image returns, never inferred visual review."""
        images = self.manifest.get("images", {})
        views = {name: [] for name in images}
        log = self.run / "tools.jsonl"
        for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
            event = json.loads(line)
            if event.get("action") != "view_image":
                continue
            data = event["data"]
            name = data.get("name")
            # Older logs without a bound image hash remain uncounted.
            if name not in images or data.get("image_sha256") != images[name]["sha256"]:
                continue
            views[name].append(data["box_original_pixels"])
        rows = []
        for name, info in sorted(images.items()):
            whole = [0, 0, *info["size"]]
            full = sum(box == whole for box in views[name])
            rows.append({"image": name, "image_sha256": info["sha256"],
                         "full_view_count": full, "crop_view_count": len(views[name]) - full,
                         "status": "full_view_returned" if full else
                                   "crop_only_returned" if views[name] else "no_direct_view_record"})
        return {"schema_version": "input_direct_view_status_v1", "images": rows,
                "no_direct_view_images": [row["image"] for row in rows
                                          if row["status"] == "no_direct_view_record"],
                "crop_only_images": [row["image"] for row in rows
                                     if row["status"] == "crop_only_returned"],
                "drawing_fidelity": "not_evaluated", "delivery_blocked": False,
                "note": "Only this run's hash-bound direct original returns from view_image or paired elevation views are counted. Other image tools, "
                        "workers and prior runs are outside this scope. A returned image is not proof "
                        "of examination, complete coverage, correct interpretation or applied heights. "
                        "Use supplied relevant elevations to check each floor's opening heights; "
                        "do not transfer a typical height to an unexamined opening family."}

    def delivery(self, candidate, *, selection_origin, generation_status=None):
        """Build the handoff from saved source/check records, never model prose."""
        from scripts.tool_scripts.bim_agent_saved_result import opening_delivery_summary
        path = self.candidate_path(candidate)
        source = json.loads((path / "source_model.json").read_bytes())
        result = {"candidate": candidate, "selection_origin": selection_origin,
                  "viewer": f"{candidate}/viewer.html", "source_model": f"{candidate}/source_model.json",
                  "viewer_exists": (path / "viewer.html").is_file(),
                  "generation_status": generation_status or {"state":"in_progress"},
                  **opening_delivery_summary(self, source)}
        result["source_image_feedback"] = self._delivery_projection_status(candidate, source)
        result["space_relation_review"] = self.space_relation_status(source)
        result["input_view_status"] = self.input_view_status()
        if self.manifest.get("drawing_review_policy"):
            from scripts.tool_scripts.bim_agent_process_reviews import process_status
            result["process_review"] = process_status(self, candidate)
        from src.agent.roles import room_use_review
        result["room_use_review"] = room_use_review(source)
        claim_state = self.claims().status()
        from src.agent.execution.bim_claim_state import project
        current_claims = project(self.claims(), candidate)
        result["current_claim_state"] = current_claims
        result["height_coverage"] = self.located_heights(candidate, current_claims)
        result["facade_counts"] = self.facade_counts(candidate)
        from scripts.tool_scripts.bim_agent_precision import building_precision
        result["building_precision"] = building_precision(self, candidate, source)
        from scripts.tool_scripts.bim_agent_budget import saved_floor_status
        result["floor_completeness"] = saved_floor_status(self, candidate)
        if self.manifest.get('mesh_input'):
            from scripts.tool_scripts.bim_agent_mesh_review import mesh_review_status
            result['mesh_review'] = mesh_review_status(self, candidate)
        # Keep full claims in their files; summarize unresolved execution in handoff.
        result["claim_applications"] = [{key: row.get(key) for key in
            ("id", "parent_candidate", "candidate", "claim_ids", "status", "error", "parameters_without_claims")}
            for row in claim_state["applications"]]
        result["adopted_unapplied_claims"] = [row["id"] for row in current_claims["claims"]
            if row["state"] in {"pending_application", "partially_satisfied", "changed_since_check"}]
        from scripts.tool_scripts.bim_agent_saved_result import saved_source_result
        saved_source_result(self.run, result, selection=True)
        dump(self.run / "delivery.json", result)
        from scripts.tool_scripts.bim_agent_delivery_display import render_delivery_html
        (self.run / "delivery.html").write_text(
            render_delivery_html(result, source), encoding="utf-8", newline="\n")
        return result

    def inspect_plan(self, draft_id):
        """Read an admitted immutable draft or the explicitly supplied resume plan."""
        if self.readonly:
            raise ValueError("saved plans are available only to the coordinator")
        if draft_id == "resume":
            record = self.manifest.get("plan_recovery")
            if record is None:
                raise ValueError("no saved resume plan was supplied")
            path = self.run / record["frozen_path"]
            expected, image_name = record["raw_sha256"], record["image"]
        else:
            if not isinstance(draft_id, str) or not draft_id.startswith("draft_") or not draft_id[6:].isdigit():
                raise ValueError("choose resume or an existing draft_NNN")
            folder = self.run / "plan_drafts" / draft_id
            record = json.loads((folder / "input.json").read_bytes())
            path = folder / "plan.json"
            expected, image_name = record["plan_sha256"], record["image"]
        if digest(path) != expected or digest(self.image_path(image_name)) != record["image_sha256"]:
            raise ValueError("saved plan or original image changed")
        return dict(draft_id=draft_id, plan_sha256=expected, image=image_name,
                    declaration=json.loads(path.read_bytes()))

    def revise_plan(self, draft_id, expected_plan_sha256, operations_json):
        from src.agent.geometry.plan_revision import apply_plan_revision
        from src.agent.geometry.plan_feedback import plan_geometry_feedback, opening_geometry_changes
        parent = self.inspect_plan(draft_id)
        if parent["plan_sha256"] != expected_plan_sha256:
            raise ValueError("stale plan hash; inspect the intended saved draft before revision")
        folder = self.run / "plan_revisions"
        folder.mkdir(exist_ok=True)
        path = folder / f"revision_{len(list(folder.glob('revision_*.json'))) + 1:03d}.json"
        revision = dict(parent_draft_id=draft_id, parent_plan_sha256=expected_plan_sha256,
                        operations_json=operations_json, status="pending")
        dump(path, revision)
        try:
            updated, preservation = apply_plan_revision(parent["declaration"], json.loads(operations_json))
            revision.update(status="declaration_applied", **preservation)
            dump(path, revision)
            # Bind immutable operations and their preservation audit in the new
            # draft's provenance before compiling, including on compiler failure.
            binding = dict(file=path.relative_to(self.run).as_posix(), sha256=digest(path),
                           parent_draft_id=draft_id, parent_plan_sha256=expected_plan_sha256)
            result = self.build_plan(parent["image"], json.dumps(updated, ensure_ascii=False), revision=binding)
        except Exception as error:
            # Once bound, preserve the exact revision file even for internal faults.
            if revision["status"] == "pending":
                revision.update(status="rejected", error=str(error)); dump(path, revision)
            self.log("revise_plan_bim", dict(revision_file=path.relative_to(self.run).as_posix(), error=str(error)))
            raise
        result["plan_revision"] = dict(**binding, unchanged_ids=preservation["unchanged_ids"],
            changed_targets=[dict(field=r["field"], id=r["id"]) for r in preservation["changes"]])
        if result.get("regularization") is not None:
            result["plan_revision"]["preservation_scope"] = (
                "unchanged_ids describe the explicit edit; indirect regularization movements are recorded separately")
        from scripts.tool_scripts.bim_agent_saved_result import saved_source_result
        prior_result = self.run / "plan_drafts" / draft_id / "result.json"
        prior = json.loads(prior_result.read_bytes()).get("candidate") if prior_result.is_file() else None
        saved_source_result(self.run, result, parent=prior)
        saved_plan = updated
        try:
            saved_plan = json.loads((self.run / result["plan_input"]["plan_file"]).read_bytes())
            with PILImage.open(self.image_path(parent["image"])) as original:
                changes = opening_geometry_changes(
                    plan_geometry_feedback(parent["declaration"], original.size),
                    plan_geometry_feedback(saved_plan, original.size))
            feedback_path = self.run / result["plan_input"]["plan_file"]
            feedback_path = feedback_path.with_name("opening_changes.json")
            dump(feedback_path, changes)
            result["plan_revision"]["geometry_changes"] = dict(
                file=feedback_path.relative_to(self.run).as_posix(), sha256=digest(feedback_path),
                changed_count=len(changes["changed_openings"]),
                changed_openings=changes["changed_openings"][:24],
                truncated=len(changes["changed_openings"]) > 24,
                unchanged_opening_ids=changes["unchanged_opening_ids"], note=changes["note"])
        except (ValueError, TypeError, KeyError, IndexError) as error:
            from scripts.tool_scripts.bim_agent_feedback import plan_feedback_error
            result["plan_revision"]["geometry_changes"] = plan_feedback_error(error,
                parent=parent["declaration"], revised=saved_plan)
        dump((self.run / result["plan_input"]["plan_file"]).with_name("result.json"), result)
        self.log("revise_plan_bim", dict(candidate=result.get("candidate"),
            source_geometry_ready=result.get("source_geometry_ready"), plan_input=result.get("plan_input"),
            plan_revision=result["plan_revision"], error=result.get("error")))
        return result

    def build_plan(self, image, plan_json, *, revision=None):
        """Preserve a pixel declaration before deterministic compilation or errors."""
        import copy
        from src.agent.geometry.plan_partition import OpeningHostError, compile_plan_partition
        from src.agent.geometry.plan_draft_view import render_opening_host_failure, render_plan_draft
        from src.agent.geometry.profile_observation_binding import resolve_plan_pixels
        from src.agent.geometry.plan_feedback import resolve_plan_lengths, plan_geometry_feedback, compact_plan_feedback
        from src.agent.geometry.plan_input import (
            bind_missing_plan_sources, normalize_plan_fields, plan_error_hint,
        )
        from scripts.tool_scripts.bim_agent_regularization import (
            LEGACY_RULE, compact_plan_input, save_report, selected_rule, selected_grid_step,
        )
        from scripts.tool_scripts.bim_agent_saved_result import saved_result
        image_path = self.image_path(image)
        folder = self.run / "plan_drafts"
        folder.mkdir(exist_ok=True)
        draft = folder / f"draft_{len(list(folder.glob('draft_*'))) + 1:03d}"
        draft.mkdir(exist_ok=False)
        raw_path = draft / "plan.json"
        raw_path.write_text(plan_json, encoding="utf-8", newline="\n")
        record = {"plan_file": raw_path.relative_to(self.run).as_posix(),
                  "plan_sha256": digest(raw_path), "image": image,
                  "image_sha256": digest(image_path)}
        if revision is not None:
            record["revision"] = revision
        dump(draft / "input.json", record)
        input_stage = "parse_json"
        plan = None
        diagnostic_plan = None
        regularization = None
        regularization_error = None
        source_bindings = []
        try:
            plan = json.loads(plan_json)
            input_stage = "field_aliases"
            plan, aliases = normalize_plan_fields(plan)
            if aliases:
                record["field_aliases"] = aliases
            input_stage = "length_binding"
            plan, length_bindings = resolve_plan_lengths(plan)
            input_stage = "measurement_binding"
            plan, bindings = resolve_plan_pixels(plan, image=image,
                image_sha256=record["image_sha256"], load_profile=self.load_pixel_profile)
            input_stage = "input_provenance"
            plan, source_bindings = bind_missing_plan_sources(plan, image=image_path.name,
                image_sha256=record["image_sha256"])
            if source_bindings:
                source_binding_path = draft / "source_ref_bindings.json"
                dump(source_binding_path, {"image": image_path.name, "image_sha256": record["image_sha256"],
                    "scope": "input_provenance_only", "bindings": source_bindings,
                    "observation_verified": False, "height_evidence_verified": False})
                record["source_ref_bindings"] = {
                    "file": source_binding_path.relative_to(self.run).as_posix(),
                    "sha256": digest(source_binding_path), "count": len(source_bindings),
                    "scope": "input_provenance_only"}
            diagnostic_plan = copy.deepcopy(plan)
            input_stage = "regularization"
            if selected_rule(self.manifest) != LEGACY_RULE:
                from src.agent.geometry.plan_regularization import regularize_plan
                # The plan argument is model-supplied. Only the tool may write
                # the audit for this save; retained reading evidence stays separate.
                plan.pop("regularization", None)
                with PILImage.open(image_path) as original:
                    plan, regularization = regularize_plan(
                        plan, image_size=original.size, image_name=image,
                        grid_step_m=selected_grid_step(self.manifest))
                record["regularization"] = regularization
                record["regularization_report"] = save_report(draft, regularization, self.run)
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(getattr(error, "report", None), dict):
                regularization = error.report
                record["regularization"] = regularization
                record["regularization_report"] = save_report(draft, regularization, self.run)
            if input_stage == "regularization":
                # Rejected geometry still gets the usual original/draft evidence.
                # Never export a candidate merely because the submitted draft compiles.
                regularization_error = error
            else:
                record["draft_view_errors"] = [{
                    "path": "plan_json", "reason": f"{input_stage}: {error}",
                }]
                dump(draft / "input.json", record)
                result = {"status": "error", "error": str(error), "error_stage": input_stage,
                          "plan_input": compact_plan_input(record),
                          "repair_hint": plan_error_hint(plan, str(error)),
                          "remaining_seconds": self.remaining_seconds(),
                          "source_geometry_ready": False}
                saved_result(result, audit_written=True)
                dump(draft / "result.json", result)
                self.log("build_plan_bim", result)
                return result

        if source_bindings or bindings or length_bindings or aliases or (regularization is not None and regularization_error is None):
            submitted = draft / "submitted_plan.json"
            submitted.write_bytes(raw_path.read_bytes())
            dump(raw_path, plan)
            binding_path = draft / "measurement_bindings.json"
            dump(binding_path, {"bindings": bindings, "length_bindings": length_bindings, "drawing_fidelity": "not_evaluated",
                "interpretation": "Coordinates resolve caller-selected measurements only; object identity and representative planes remain caller observations."})
            record.update(plan_sha256=digest(raw_path),
                submitted_plan_file=submitted.relative_to(self.run).as_posix(),
                submitted_plan_sha256=digest(submitted),
                measurement_bindings={"file": binding_path.relative_to(self.run).as_posix(),
                    "sha256": digest(binding_path), "count": len(bindings), "length_count": len(length_bindings)})
            dump(draft / "input.json", record)

        try:
            with PILImage.open(image_path) as original:
                dimensions = plan_geometry_feedback(plan, original.size)
            dimensions_path = draft / "geometry_feedback.json"
            dump(dimensions_path, dimensions)
            record["geometry_feedback"] = dict(file=dimensions_path.relative_to(self.run).as_posix(),
                sha256=digest(dimensions_path), **compact_plan_feedback(dimensions))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            from scripts.tool_scripts.bim_agent_feedback import plan_feedback_error
            record["geometry_feedback"] = plan_feedback_error(error, current=plan)

        # Report-only comparison of the original's ink with this declaration, also for
        # drafts that fail to compile (run75/83/86/94 left drawn dividers out; run91/93/94
        # placed doors on solid wall).
        try:
            from src.agent.geometry.plan_drawing_differences import compact_differences, drawing_differences
            with PILImage.open(image_path) as original:
                differences = drawing_differences(original, plan, image_name=image,
                    image_sha256=record["image_sha256"], plan_sha256=record["plan_sha256"])
            differences_path = draft / "drawing_differences.json"
            dump(differences_path, differences)
            difference_reply = dict(file=differences_path.relative_to(self.run).as_posix(),
                sha256=digest(differences_path), **compact_differences(differences))
        except (ValueError, TypeError, KeyError, IndexError) as error:
            difference_reply = dict(status="unavailable", reason=str(error))

        try:
            with PILImage.open(image_path) as original:
                preview, preview_metadata = render_plan_draft(
                    original, plan, image_name=image,
                    image_sha256=record["image_sha256"],
                    plan_file=record["plan_file"], plan_sha256=record["plan_sha256"],
                )
            preview_path = draft / "draft_view.png"
            preview.save(preview_path)
            preview_metadata["preview"] = {
                "image_file": preview_path.relative_to(self.run).as_posix(),
                "image_sha256": digest(preview_path),
            }
            metadata_path = draft / "draft_view.json"
            dump(metadata_path, preview_metadata)
            record["draft_view"] = {
                **preview_metadata["preview"],
                "metadata_file": metadata_path.relative_to(self.run).as_posix(),
                "metadata_sha256": digest(metadata_path),
                "draft_only": True,
                "drawing_fidelity": "not_evaluated",
                "source_geometry_ready": False,
                "unrenderable_count": len(preview_metadata["unrenderable"]),
                "unrenderable": preview_metadata["unrenderable"],
            }
        except Exception as error:
            record.setdefault("draft_view_errors", []).append({
                "path": "draft_view", "reason": str(error),
            })
        dump(draft / "input.json", record)

        try:
            with PILImage.open(image_path) as original:
                proposal, metadata = compile_plan_partition(
                    plan, image_size=original.size, image_name=image)
            from src.agent.geometry.reader_wall_metadata import wall_reading_input
            proposal["wall_readings"] = [wall_reading_input(plan, metadata)]
            if regularization_error is not None:
                raise regularization_error
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, OpeningHostError):
                try:
                    with PILImage.open(image_path) as original:
                        local_view, local_metadata = render_opening_host_failure(
                            original, plan, opening_id=error.opening_id,
                            p1_pixel=error.p1_pixel, p2_pixel=error.p2_pixel,
                            image_name=image, image_sha256=record["image_sha256"],
                            plan_file=record["plan_file"], plan_sha256=record["plan_sha256"],
                        )
                    local_path = draft / "opening_host_failure.png"
                    local_view.save(local_path)
                    metadata_path = draft / "opening_host_failure.json"
                    dump(metadata_path, local_metadata)
                    record["host_failure_view"] = {
                        "image_file": local_path.relative_to(self.run).as_posix(),
                        "image_sha256": digest(local_path),
                        "metadata_file": metadata_path.relative_to(self.run).as_posix(),
                        "metadata_sha256": digest(metadata_path),
                        "opening_id": error.opening_id,
                        "p1_original_pixels": error.p1_pixel,
                        "p2_original_pixels": error.p2_pixel,
                        "metadata": local_metadata,
                    }
                except Exception as feedback_error:
                    record.setdefault("host_failure_view_errors", []).append(str(feedback_error))
                dump(draft / "input.json", record)
            remaining_error = str(regularization_error or error)
            preparation = (regularization or {}).get("junction_preparation", {})
            first_rejection = next(iter((regularization or {}).get("rejections", [])), {})
            if first_rejection.get("type") == "strict_compile_failed_after_regularization":
                remaining_error = first_rejection.get("error", str(error))
            if (preparation.get("status") == "rolled_back" and preparation.get("compile_error")
                    and first_rejection.get("type") == "strict_compile_failed_after_regularization"):
                remaining_error = preparation["compile_error"]
            result = {"status": "error", "error": remaining_error, "drawing_differences": difference_reply,
                      "repair_hint": plan_error_hint(diagnostic_plan or plan, remaining_error),
                      "plan_input": compact_plan_input(record), "remaining_seconds": self.remaining_seconds(),
                      "source_geometry_ready": False}
            if str(error) != remaining_error:
                # Both checks already ran. Preserve their distinct diagnostics
                # instead of making the model discover the second on a retry.
                result["validation_errors"] = [
                    {"stage": "regularization", "error": remaining_error,
                     "repair_hint": result["repair_hint"]},
                    {"stage": "compilation", "error": str(error),
                     "repair_hint": plan_error_hint(diagnostic_plan or plan, str(error))},
                ]
            if preparation.get("status") == "rolled_back":
                result["junction_preparation_note"] = (
                    "All tentative junction repairs were rolled back; the saved draft is the original. "
                    "The error describes what still failed after those tentative repairs. "
                    "Use the named original objects and exact coordinates in repair_hint.")
                result["original_compile_error"] = str(error)
            if regularization is not None:
                result["regularization"] = record["regularization_report"]
                result["error_stage"] = "regularization"
            saved_result(result, audit_written=True)
            dump(draft / "result.json", result)
            self.log("build_plan_bim", result)
            return result
        dump(draft / "compilation.json", metadata)
        record.update(compilation_file=(draft / "compilation.json").relative_to(self.run).as_posix(),
                      compilation_sha256=digest(draft / "compilation.json"))
        result = self.build(proposal, action="build_plan_bim", plan_input=record,
                            calibration={key: plan[key] for key in
                                         ("floor_id", "x_anchors", "y_anchors", "basis")})
        if "candidate" not in result:
            result.update(plan_input=record, source_geometry_ready=False,
                          remaining_seconds=self.remaining_seconds())
            self.log("build_plan_bim", result)
        result["drawing_differences"] = difference_reply
        if regularization is not None:
            # A later source-level refusal is the primary diagnostic. Its
            # report must not be replaced by a successful plan precheck.
            result.setdefault("regularization", record["regularization_report"])
            result["plan_input"] = compact_plan_input(record)
        dump(draft / "result.json", result)
        return result

    def assemble_plans(self, floors_json):
        """Recompile bound pixel drafts and combine their explicitly placed floors."""
        from src.agent.geometry.plan_assembly import assemble_plan_proposals
        from src.agent.geometry.plan_partition import compile_plan_partition
        from scripts.tool_scripts.bim_agent_regularization import (
            LEGACY_RULE, save_report, selected_rule, selected_cross_storey_failure_policy,
            selected_grid_step, summary,
        )
        if self.readonly:
            raise ValueError("floor assembly is available only to the coordinator")
        floors = json.loads(floors_json)
        if not isinstance(floors, list) or not 2 <= len(floors) <= 32:
            raise ValueError("supply 2–32 explicit floor declarations")
        items, bindings, calibrations, declared = [], [], [], []
        for row in floors:
            required = {"draft_id", "expected_plan_sha256", "floor_id", "z_floor", "evidence"}
            if (not isinstance(row, dict) or not required <= row.keys()
                    or row.keys() - required - {"elevation_reference"}):
                raise ValueError(f"each floor requires {sorted(required)}; elevation_reference is optional")
            if "elevation_reference" in row and type(row["elevation_reference"]) is not bool:
                raise ValueError("elevation_reference must be true or false")
            if not isinstance(row["evidence"], str) or not row["evidence"].strip():
                raise ValueError("each floor needs image evidence or an explicit placement assumption")
            saved = self.inspect_plan(row["draft_id"])
            if saved["plan_sha256"] != row["expected_plan_sha256"]:
                raise ValueError("stale plan hash; inspect each intended draft before assembly")
            plan, image = saved["declaration"], saved["image"]
            with PILImage.open(self.image_path(image)) as original:
                image_size = original.size
            declared.append(dict(plan=plan, image_name=image, image_size=image_size,
                floor_id=row["floor_id"], z_floor=row["z_floor"], source_ref=row["evidence"],
                elevation_reference=row.get("elevation_reference", False)))
            bindings.append(dict(**row, image=image, image_sha256=digest(self.image_path(image)),
                                 original_floor_id=plan["floor_id"], original_z_floor=plan["z_floor"],
                                 ceiling_height=plan["ceiling_height"]))
        folder = self.run / "plan_assemblies"
        folder.mkdir(exist_ok=True)
        path = folder / f"assembly_{len(list(folder.glob('assembly_*.json'))) + 1:03d}.json"
        regularization = None
        if selected_rule(self.manifest) != LEGACY_RULE:
            from src.agent.geometry.plan_regularization import regularize_plan_stack
            audit_folder = folder / path.stem
            audit_folder.mkdir(exist_ok=False)
            dump(audit_folder / "submitted_plans.json", declared)
            try:
                declared, regularization = regularize_plan_stack(declared,
                    cross_storey_failure_policy=selected_cross_storey_failure_policy(self.manifest),
                    grid_step_m=selected_grid_step(self.manifest))
            except (ValueError, TypeError, KeyError) as error:
                rejected = getattr(error, "report", None)
                result = {"status": "error", "error": str(error), "error_stage": "regularization",
                          "source_geometry_ready": False, "remaining_seconds": self.remaining_seconds()}
                if isinstance(rejected, dict):
                    result["regularization"] = save_report(audit_folder, rejected, self.run)
                dump(path, {"floors": bindings, "status": "rejected", "error": str(error),
                            "regularization": rejected})
                from scripts.tool_scripts.bim_agent_saved_result import saved_result
                saved_result(result, audit_written=True)
                self.log("assemble_plan_bim", result)
                return result
            report_ref = save_report(audit_folder, regularization, self.run)
        for index, (item, binding_row) in enumerate(zip(declared, bindings)):
            plan, image = item["plan"], item["image_name"]
            proposal, compilation = compile_plan_partition(
                plan, image_size=tuple(item["image_size"]), image_name=image)
            from src.agent.geometry.reader_wall_metadata import wall_reading_input
            proposal["wall_readings"] = [wall_reading_input(plan, compilation)]
            items.append(dict(proposal=proposal, floor_id=item["floor_id"], z_floor=item["z_floor"],
                              source_ref=item["source_ref"]))
            binding_row["compilation"] = compilation
            if regularization is not None:
                effective = audit_folder / f"floor_{index + 1:02d}.json"
                dump(effective, plan)
                binding_row.update(effective_plan_file=effective.relative_to(self.run).as_posix(),
                    effective_plan_sha256=digest(effective), effective_z_floor=item["z_floor"])
            calibrations.append(dict(image=image, floor_id=item["floor_id"],
                **{key: plan[key] for key in ("x_anchors", "y_anchors", "basis")}))
        proposal = assemble_plan_proposals(items)
        dump(path, {"floors": bindings,
                    "operation": "regularize_recompile_and_assemble" if regularization else "namespace_ids_and_translate_z_only",
                    **({"regularization": regularization} if regularization is not None else {}),
                    "height_policy": "preserve_declared_heights; revise a draft explicitly to change them"})
        binding = {"file": path.relative_to(self.run).as_posix(), "sha256": digest(path)}
        if regularization is not None:
            binding["regularization"] = regularization
        result = self.build(proposal, action="assemble_plan_bim", plan_assembly=binding,
                            assembly_calibrations=calibrations)
        from src.agent.geometry.plan_drawing_differences import compact_differences, drawing_differences
        differences = {}
        for index, (row, item) in enumerate(zip(bindings, declared)):
            saved = self.run / "plan_drafts" / row["draft_id"] / "drawing_differences.json"
            if regularization is not None:
                try:
                    with PILImage.open(self.image_path(item["image_name"])) as original:
                        current = drawing_differences(original, item["plan"], image_name=item["image_name"],
                            image_sha256=row["image_sha256"], plan_sha256=row["effective_plan_sha256"])
                    dump(audit_folder / f"floor_{index + 1:02d}_drawing_differences.json", current)
                    differences[row["floor_id"]] = compact_differences(current)
                except (ValueError, TypeError, KeyError, IndexError) as error:
                    differences[row["floor_id"]] = {"status": "unavailable", "reason": str(error)}
            elif saved.is_file():
                differences[row["floor_id"]] = dict(draft_id=row["draft_id"],
                    **compact_differences(json.loads(saved.read_bytes())))
        if differences:
            result["drawing_differences"] = differences
        if regularization is not None:
            result.setdefault("regularization", report_ref)
            result["plan_assembly"] = {**binding, "regularization": summary(regularization)}
        self.log("assemble_plan_bim", {"assembly": binding, "candidate": result.get("candidate"),
                                      "error": result.get("error")})
        return result

    def build(self, proposal, *, action="build_bim", parent=None, operations=None,
              plan_input=None, calibration=None, claim_application=None,
              plan_assembly=None, assembly_calibrations=None):
        from src.agent.execution.source_proposal import export_source_proposal
        from scripts.tool_scripts.bim_agent_saved_result import saved_result, saved_source_result
        from scripts.tool_scripts.bim_agent_regularization import reject_source_proposal
        rejection = reject_source_proposal(
            self, proposal, plan_assembly=plan_assembly, parent=parent)
        if rejection is not None:
            saved_result(rejection, audit_written=True)
            self.log(action, rejection)
            return rejection
        if isinstance(proposal, dict) and 'mesh_frame' in proposal:
            from src.agent.geometry.mesh_bim_frame import validate_mesh_frame
            frame = validate_mesh_frame(proposal['mesh_frame'])
            if frame['mesh_sha256'] != self.manifest.get('mesh_input', {}).get('sha256'):
                raise ValueError('candidate mesh_frame must refer to the admitted original mesh')
        budget = self.candidate_budget()
        index = budget["used"] + 1
        if budget["remaining"] == 0:
            return saved_result({"error": "candidate budget exhausted; report saved partial results",
                    "candidate_budget": budget, "remaining_seconds": self.remaining_seconds()})
        candidate = f"candidate_{index:02d}"
        provenance = {"input_manifest_sha256": digest(self.run/"inputs.json"),
                      "mode": self.manifest.get("input_mode", "original_images_agent_experiment"),
                      "generator": self.manifest.get("generator") or
                                   f"{self.manifest.get('provider', 'claude')} subscription tool loop"}
        if parent is not None:
            provenance.update(parent_candidate=parent,
                              parent_proposal_sha256=digest(self.candidate_path(parent)/"proposal.json"))
        if plan_input is not None:
            provenance["plan_input"] = plan_input
        if claim_application is not None:
            provenance["claim_application"] = claim_application
        if plan_assembly is not None:
            provenance["plan_assembly"] = plan_assembly
        report = export_source_proposal(proposal, self.run/candidate, provenance=provenance,
            mesh_path=self.run/'assets'/'input.glb' if proposal.get('mesh_frame') else None)
        if operations is not None:
            dump(self.run/candidate/"operations.json", operations)
        result = {"candidate": candidate, "remaining_seconds": self.remaining_seconds(), **report,
                  "candidate_budget": self.candidate_budget(),
                  "input_view_status": self.input_view_status()}
        if plan_assembly is not None:
            result["plan_assembly"] = plan_assembly
        if plan_input is not None:
            result["plan_input"] = plan_input
            result["plan_compilation"] = json.loads(
                (self.run / plan_input["compilation_file"]).read_bytes())
        source_path = self.run / candidate / "source_model.json"
        if source_path.exists():
            from src.agent.geometry.opening_review import opening_inventory
            from src.agent.roles import room_use_review
            source = json.loads(source_path.read_bytes())
            result["room_use_review"] = room_use_review(source, include_next_action=False)
            result["height_coverage"] = self.located_heights(candidate)
            result["facade_counts"] = self.facade_counts(candidate)
            result["opening_inventory"] = opening_inventory(source)
            result["opening_review"] = "not_reviewed; compare this inventory with distinct drawing marks"
            if calibration is not None:
                self._save_calibration(candidate=candidate, image=plan_input["image"],
                                       metadata=report, **calibration)
            for registered in assembly_calibrations or []:
                self._save_calibration(candidate=candidate, metadata=report, **registered)
            if parent and operations and all(operation.get("op") in {
                    "replace_note", "set_notes", "set_space_role"} for operation in operations):
                refreshed = self.revalidate_opening_reviews(parent, candidate, source)
                result["opening_review_revalidation"] = refreshed
                if refreshed["reports"]:
                    result["opening_review"] = (
                        "saved observations rechecked on this source; inspect opening_review_revalidation "
                        "for scopes and findings; drawing fidelity remains not_evaluated")
            if parent and operations and all(operation.get("op") in {
                    "replace_note", "set_notes"} for operation in operations):
                result["space_relation_revalidation"] = self.revalidate_space_relations(
                    parent, candidate, source)
            from scripts.tool_scripts.bim_agent_precision import building_precision
            result["building_precision"] = building_precision(self, candidate, source)
            projections, errors = self.project_registered_calibrations(candidate, action)
            result["source_image_projections"] = projections
            result["projection_errors"] = errors
            result["source_plan_views"], result["source_plan_errors"] = [], []
            source = json.loads(source_path.read_bytes())
            for floor in source["floors"]:
                try:
                    _, metadata = self.plan_view(candidate, floor["id"])
                    result["source_plan_views"].append(metadata)
                except Exception as error:
                    result["source_plan_errors"].append({"candidate": candidate,
                        "floor_id": floor["id"], "error": str(error)})
        saved_source_result(self.run, result, parent=parent)
        self.log(action, result)
        return result

    def revalidate_opening_reviews(self, parent, candidate, source):
        """Save fresh kernel reports, leaving the parent observations untouched."""
        from src.agent.geometry.opening_review import revalidate_saved_review
        folder = self.run / "opening_reviews"
        paths = sorted(folder.glob("review_*.json"))
        result = {"reports": [], "skipped": [], "drawing_fidelity": "not_evaluated",
                  "note": "Only saved observations are reused; each verdict is recomputed for the current source."}
        if not paths:
            return result
        try:
            parent_source = json.loads((self.candidate_path(parent) / "source_model.json").read_bytes())
            if not isinstance(parent_source, dict):
                raise ValueError('parent source must be an object')
            calibrations = {(row["image"], row["floor_id"]): (path, row)
                            for path, row in self.registered_calibrations()}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {**result, 'status': 'unavailable',
                    'reason': f'Could not load opening review revalidation context: {error}'}
        for path in paths:
            original = path.read_bytes()
            reference = path.relative_to(self.run).as_posix()
            try:
                saved = json.loads(original)
                if not isinstance(saved, dict):
                    raise ValueError("saved opening review must be an object")
            except ValueError as error:
                result["skipped"].append({"review_file": reference, "reason": str(error)})
                continue
            if (saved.get("candidate") != parent
                    or saved.get("source_model_sha256") != parent_source.get("source_model_sha256")):
                continue
            try:
                observations = saved.get("observations", {})
                if not isinstance(observations, dict):
                    raise ValueError("saved opening review has no original observations object")
                self.image_path(observations.get("image"))  # Verify actual admitted bytes, not just metadata.
                calibration_entry = calibrations.get((observations.get("image"), observations.get("floor_id")))
                calibration = calibration_entry[1] if calibration_entry else None
                report = revalidate_saved_review(parent_source, source, saved, self.manifest["images"],
                                                plan_calibration=calibration)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result["skipped"].append({"review_file": reference, "reason": str(error)})
                continue
            index = max((int(p.stem.removeprefix("review_")) for p in folder.glob("review_*.json")), default=0) + 1
            target = folder / f"review_{index:03d}.json"
            current = {"candidate": candidate, "review_file": target.relative_to(self.run).as_posix(),
                       **report, "observations": observations,
                       "revalidated_calibration": ({"file": calibration_entry[0].relative_to(self.run).as_posix(),
                           "sha256": digest(calibration_entry[0])} if calibration_entry else None),
                       "revalidated_from": {"review_file": reference,
                           "review_sha256": hashlib.sha256(original).hexdigest(),
                           "candidate": parent, "source_model_sha256": saved["source_model_sha256"],
                           "image": saved["image"], "location_check": saved["location_check"]}}
            # Exclusive creation prevents replacement of any earlier evidence.
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(current, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            result["reports"].append({"review_file": current["review_file"], "sha256": digest(target),
                "review_scope": report["review_scope"], "conclusion": report["conclusion"],
                "finding_codes": sorted({row["code"] for row in report["findings"]})})
        return result

    def plan_view(self, candidate, floor_id):
        from src.agent.geometry.source_plan_view import render_source_plan
        path = self.candidate_path(candidate)
        source = json.loads((path / "source_model.json").read_bytes())
        pic, metadata = render_source_plan(source, floor_id)
        # Public floor ordinals are unique and safe as filenames; source floor
        # identity remains in the metadata and every tool argument.
        stem = "plan_" + metadata["floor_name"]
        image_path = path / (stem + ".png")
        pic.save(image_path)
        metadata.update(candidate=candidate, plan_image=image_path.relative_to(self.run).as_posix())
        dump(path / (stem + ".json"), metadata)
        return Image(data=image_path.read_bytes(), format="png"), metadata

    def image_path(self, name):
        from scripts.tool_scripts.bim_agent_feedback import resolve_image_name
        name = resolve_image_name(self.manifest["images"], name)
        path = self.run / "images" / name
        if digest(path) != self.manifest["images"][name]["sha256"]:
            raise ValueError("input image changed")
        return path

    def load_pixel_profile(self, profile_id):
        """Load only a saved profile from this run; consumers bind image and axis."""
        if not isinstance(profile_id, str) or not re.fullmatch(r"profile_\d{3,}", profile_id):
            raise ValueError("choose an existing profile_id returned by view_pixel_profile")
        folder = (self.run / "pixel_profiles").resolve()
        path = folder / f"{profile_id}.json"
        if not path.is_file() or path.resolve().parent != folder:
            raise ValueError("choose an existing profile_id returned by view_pixel_profile")
        raw = path.read_bytes()
        return {"record": json.loads(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def compare_facade_spans(self, plan_image, elevation_image, observations_json,
                             plan_axis="y", elevation_axis="x", ambiguity_tolerance_m=0.05):
        """Persist caller observations bound to admitted originals; never change BIM."""
        from src.agent.geometry.facade_span_comparison import compare
        from src.agent.geometry.profile_observation_binding import resolve_observations
        def reject_constant(value):
            raise ValueError(f"observations_json contains non-finite constant {value}")

        observations = json.loads(observations_json, parse_constant=reject_constant)
        sources = {}
        for label, name, axis in (("plan", plan_image, plan_axis),
                                  ("elevation", elevation_image, elevation_axis)):
            if axis not in {"x", "y"}:
                raise ValueError("image axis must be x or y")
            path = self.image_path(name)
            with PILImage.open(path) as pic:
                size = list(pic.size)
            sources[label] = {"image": name, "sha256": digest(path),
                              "size": size, "axis": axis}

        def load_profile(profile_id):
            # resolve_observations validates the ID before calling this loader.
            path = self.run / "pixel_profiles" / f"{profile_id}.json"
            if not path.is_file() or path.resolve().parent != (self.run / "pixel_profiles"):
                raise ValueError("choose an existing profile_id returned by view_pixel_profile")
            raw = path.read_bytes()
            return {"record": json.loads(raw, parse_constant=reject_constant),
                    "sha256": hashlib.sha256(raw).hexdigest()}

        resolved, bindings = resolve_observations(observations, load_profile=load_profile,
                                                   views=sources)
        result = compare(resolved, ambiguity_tolerance_m)
        for label, source in sources.items():
            bound = source["size"][0 if source["axis"] == "x" else 1]
            if any(not 0 <= anchor[0] <= bound
                   for anchor in result["normalized_inputs"][label]["axis_anchors"]):
                raise ValueError(f"{label} axis anchors lie outside original image bounds")
        folder = self.run / "facade_comparisons"
        folder.mkdir(exist_ok=True)
        index = 1
        while (folder / f"comparison_{index:03d}.json").exists():
            index += 1
        path = folder / f"comparison_{index:03d}.json"
        result.update(original_images=sources, observations=resolved,
                      submitted_observations=observations, measurement_bindings=bindings,
                      evidence_status="caller_observations_not_independently_verified",
                      record=path.relative_to(self.run).as_posix())
        with path.open("x") as output:
            json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
        self.log("compare_facade_spans", {"record": result["record"],
                                         "direction_separation": result["direction_separation"]})
        return result

    def _calibration_records(self):
        """Return immutable explicit calibration records in registration order."""
        folder = self.run / "overlay_calibrations"
        records = []
        for path in sorted(folder.glob("calibration_*.json")) if folder.is_dir() else []:
            record = json.loads(path.read_bytes())
            if not isinstance(record, dict):
                raise ValueError(f"invalid overlay calibration record: {path.name}")
            records.append((path, record))
        return records

    def registered_calibrations(self):
        """The latest explicit calibration for each exact original-image/floor pair."""
        latest = {}
        for path, record in self._calibration_records():
            image = record.get("image")
            floor_id = record.get("floor_id")
            if not isinstance(image, str) or not isinstance(floor_id, str):
                raise ValueError(f"invalid overlay calibration identity: {path.name}")
            latest[(image, floor_id)] = (path, record)
        return list(latest.values())

    def check_space_relations(self, candidate, image, floor_id, observations_json):
        from src.agent.geometry.source_space_relations import review_space_relations
        self.image_path(image)
        calibrations = [(path, row) for path, row in self.registered_calibrations()
                        if (row['image'], row['floor_id']) == (image, floor_id)]
        if not calibrations:
            raise ValueError('register this image/floor calibration with overlay_candidate or build_plan_bim first')
        path, calibration = calibrations[0]
        if calibration['image_sha256'] != self.manifest['images'][image]['sha256']:
            raise ValueError('calibration original image changed')
        source = json.loads((self.candidate_path(candidate) / 'source_model.json').read_bytes())
        observations = json.loads(observations_json)
        report = review_space_relations(source, floor_id=floor_id,
            image_size=self.manifest['images'][image]['size'],
            x_anchors=calibration['x_anchors'], y_anchors=calibration['y_anchors'],
            observations=observations)
        return self._save_space_relation_review(candidate, image, path,
            {**report, 'submitted_observations': observations})

    def _save_space_relation_review(self, candidate, image, calibration_path, report, *, inherited=None):
        folder = self.run / 'space_relation_reviews'
        folder.mkdir(exist_ok=True)
        index = max((int(p.stem.removeprefix('review_')) for p in folder.glob('review_*.json')), default=0) + 1
        target = folder / f'review_{index:03d}.json'
        result = {**report, 'candidate': candidate, 'image': image,
                  'image_sha256': self.manifest['images'][image]['sha256'],
                  'image_size': self.manifest['images'][image]['size'],
                  'calibration_file': calibration_path.relative_to(self.run).as_posix(),
                  'calibration_sha256': digest(calibration_path),
                  'review_file': target.relative_to(self.run).as_posix(),
                  'remaining_seconds': self.remaining_seconds()}
        if inherited is not None:
            result['revalidated_from'] = inherited
        with target.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n')
        self.log('revalidate_source_space_relation' if inherited else 'check_source_space_relation', result)
        return result

    def revalidate_space_relations(self, parent, candidate, source):
        from src.agent.geometry.source_space_relations import revalidate_saved_relation_review
        from scripts.tool_scripts.bim_agent_replies import compact_reply, report_reference
        result = {'reports': [], 'skipped': [], 'drawing_fidelity': 'not_evaluated',
                  'note': 'Saved pixel observations are rechecked on this source; conflicts and uncertainty remain visible.'}
        paths = sorted((self.run / 'space_relation_reviews').glob('review_*.json'))
        if not paths:
            return result
        try:
            parent_source = json.loads((self.candidate_path(parent) / 'source_model.json').read_bytes())
            if not isinstance(parent_source, dict):
                raise ValueError('parent source must be an object')
            calibrations = {(row['image'], row['floor_id']): (path, row)
                            for path, row in self.registered_calibrations()}
        except (OSError, ValueError, KeyError, TypeError) as error:
            return {**result, 'status': 'unavailable',
                    'reason': f'Could not load space relation revalidation context: {error}'}
        # The existing append-only tool log retains the original report, including
        # legacy pixel observations. Do not adopt a subsequently edited report file.
        recorded = {}
        log = self.run / 'tools.jsonl'
        log_error = None
        try:
            if log.exists():
                with log.open(encoding='utf-8') as stream:
                    for line in stream:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError('tool log row must be an object')
                        if row.get('action') in {'check_source_space_relation', 'revalidate_source_space_relation'}:
                            data = row.get('data')
                            if not isinstance(data, dict) or not isinstance(data.get('review_file'), str):
                                raise ValueError('tool log relation record has no report identity')
                            recorded[data['review_file']] = data
        except (OSError, ValueError, TypeError) as error:
            log_error = f'Original space relation tool record unavailable: {error}'
        for path in paths:
            reference = path.relative_to(self.run).as_posix()
            try:
                original = path.read_bytes()
                saved = json.loads(original)
                if not isinstance(saved, dict):
                    raise ValueError('saved space relation review must be an object')
                if saved.get('candidate') != parent:
                    continue
                if log_error:
                    raise ValueError(log_error)
                if saved != recorded.get(reference):
                    raise ValueError('saved space relation review differs from its original tool record')
                image = saved['image']
                with PILImage.open(self.image_path(image)) as pixels:
                    if list(pixels.size) != self.manifest['images'][image]['size']:
                        raise ValueError('original image dimensions changed')
                old_calibration = (self.run / saved['calibration_file']).resolve()
                if (old_calibration.parent != (self.run / 'overlay_calibrations').resolve()
                        or not re.fullmatch(r'calibration_\d{3,}\.json', old_calibration.name)
                        or digest(old_calibration) != saved['calibration_sha256']):
                    raise ValueError('saved space relation calibration file changed')
                calibration_path, calibration = calibrations[(image, saved['floor_id'])]
                report = revalidate_saved_relation_review(parent_source, source, saved,
                    self.manifest['images'], calibration=calibration)
                fresh = self._save_space_relation_review(candidate, image, calibration_path, report,
                    inherited={'review_file': reference, 'review_sha256': hashlib.sha256(original).hexdigest(),
                        'candidate': parent, 'source_model_sha256': saved['source_model_sha256'],
                        'calibration_file': saved['calibration_file'],
                        'calibration_sha256': saved['calibration_sha256']})
                entry = {'review_file': fresh['review_file'],
                    'sha256': digest(self.run / fresh['review_file']), 'floor_id': fresh['floor_id'],
                    'sample_count': len(fresh['observations']), 'conflict_count': fresh['conflict_count'],
                    'unassessed_count': fresh['unassessed_count']}
                readable = compact_reply(self.run, 'space_relation_revalidation', entry,
                    full_result=fresh, force=True)
                entry['details'] = report_reference(readable['details_file'],
                    readable['details_sha256'], '', fresh)
                result['reports'].append(entry)
            except (OSError, ValueError, KeyError, TypeError) as error:
                result['skipped'].append({'review_file': reference, 'reason': str(error)})
        return result

    def space_relation_status(self, source):
        """Keep sampled expectations tied to the current source and calibration."""
        calibrations = {(row['image'], row['floor_id']): digest(path)
                        for path, row in self.registered_calibrations()}
        latest, stale_count = {}, 0
        for path in sorted((self.run / 'space_relation_reviews').glob('review_*.json')):
            report = json.loads(path.read_bytes())
            pair = (report['image'], report['floor_id'])
            if (report['source_model_sha256'] != source['source_model_sha256']
                    or report['calibration_sha256'] != calibrations.get(pair)
                    or report['image_sha256'] != self.manifest['images'].get(pair[0], {}).get('sha256')):
                stale_count += 1
                continue
            self.image_path(pair[0])
            for row in report['observations']:
                latest[(*pair, row['id'])] = {**row, 'review_file': path.relative_to(self.run).as_posix()}
        conflicts = [row for row in latest.values() if row['consistency'] == 'conflicts_with_supplied_expectation']
        unassessed = sum(row['consistency'] == 'not_assessed' for row in latest.values())
        status = ('not_reviewed' if not latest else 'observations_require_follow_up' if conflicts or unassessed
                  else 'consistent_with_supplied_samples')
        return {'status': status, 'sample_count': len(latest), 'conflict_count': len(conflicts),
                'unassessed_count': unassessed, 'stale_review_count': stale_count,
                'conflicts': [{'id': row['id'], 'expected': row['expected'],
                               'actual_relation': row['actual_relation'], 'review_file': row['review_file']}
                              for row in conflicts],
                'coverage': 'caller_selected_point_pairs_only', 'drawing_fidelity': 'not_evaluated'}

    def _save_calibration(self, *, candidate, image, floor_id, x_anchors, y_anchors, basis, metadata):
        """Append an explicit calibration. Later records supersede only the same pair."""
        folder = self.run / "overlay_calibrations"
        folder.mkdir(exist_ok=True)
        index = len(list(folder.glob("calibration_*.json"))) + 1
        path = folder / f"calibration_{index:03d}.json"
        record = {
            "schema_version": "source_overlay_calibration_v1",
            "calibration_id": path.stem,
            "image": image,
            "floor_id": floor_id,
            "image_sha256": self.manifest["images"][image]["sha256"],
            "x_anchors": x_anchors,
            "y_anchors": y_anchors,
            "basis": basis,
            "registered_by_candidate": candidate,
            "registered_source_model_sha256": metadata["source_model_sha256"],
            "calibration_basis": "caller_supplied_not_independently_verified",
            "calibration_unverified": True,
        }
        dump(path, record)
        return path, record

    def _save_overlay(self, pic, metadata, *, box=None):
        """Persist a full immutable projection and make the corresponding tool view."""
        folder = self.run / "image_overlays"
        folder.mkdir(exist_ok=True)
        stem = f"overlay_{len(list(folder.glob('overlay_*.json'))) + 1:03d}"
        original_size = list(pic.size)
        region = box or [0, 0, pic.width, pic.height]
        if box is not None:
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= pic.width and 0 <= y0 < y1 <= pic.height):
                raise ValueError("crop outside original image bounds")
        # The stored overlay is full resolution.  The returned image follows the
        # same bounded presentation convention as view_image.
        pic.save(folder / f"{stem}.png")
        presented = pic.crop(region)
        presented.thumbnail((1600, 1600))
        metadata.update(
            overlay_image=f"image_overlays/{stem}.png",
            original_size=original_size,
            box_original_pixels=region,
            returned_size=list(presented.size),
            original_pixels_per_returned_pixel=[
                (region[2] - region[0]) / presented.width,
                (region[3] - region[1]) / presented.height,
            ],
            remaining_seconds=self.remaining_seconds(),
        )
        dump(folder / f"{stem}.json", metadata)
        data = io.BytesIO()
        presented.save(data, "PNG")
        return Image(data=data.getvalue(), format="png"), metadata

    def project_overlay(self, candidate, image, floor_id, x_anchors, y_anchors, basis, *,
                        trigger_action, box=None, calibration=None, automatic=False):
        """Render and persist one projection from an immutable saved source BIM."""
        from src.agent.geometry.source_image_overlay import render_source_overlay
        path = self.candidate_path(candidate)
        source = json.loads((path / "source_model.json").read_bytes())
        image_path = self.image_path(image)
        with PILImage.open(image_path) as raw:
            pic, metadata = render_source_overlay(source, raw, floor_id=floor_id,
                x_anchors=x_anchors, y_anchors=y_anchors, basis=basis, image_name=image)
        metadata.update(
            candidate=candidate,
            image=image,
            image_sha256=self.manifest["images"][image]["sha256"],
            trigger_action=trigger_action,
            automatic_projection=automatic,
        )
        if calibration is not None:
            calibration_path, calibration_record = calibration
            metadata["reused_calibration"] = {
                "calibration_id": calibration_record["calibration_id"],
                "calibration_file": calibration_path.relative_to(self.run).as_posix(),
                "registered_by_candidate": calibration_record["registered_by_candidate"],
                "registered_source_model_sha256": calibration_record["registered_source_model_sha256"],
                "image_sha256": calibration_record["image_sha256"],
                "x_anchors": calibration_record["x_anchors"],
                "y_anchors": calibration_record["y_anchors"],
                "basis": calibration_record["basis"],
            }
        return self._save_overlay(pic, metadata, box=box)

    def project_registered_calibrations(self, candidate, action):
        """Reuse only caller-registered frames; projection failures preserve the BIM."""
        projections = []
        errors = []
        try:
            calibrations = self.registered_calibrations()
        except Exception as error:
            calibrations = []
            errors.append({"candidate": candidate, "trigger_action": action,
                           "error": f"could not load registered calibrations: {error}"})
        for calibration in calibrations:
            calibration_path, record = calibration
            error_context = {
                "candidate": candidate,
                "trigger_action": action,
                "image": record.get("image"),
                "floor_id": record.get("floor_id"),
                "calibration_file": calibration_path.relative_to(self.run).as_posix(),
            }
            try:
                _, metadata = self.project_overlay(
                    candidate, record["image"], record["floor_id"], record["x_anchors"],
                    record["y_anchors"], record["basis"], trigger_action=action,
                    calibration=calibration, automatic=True)
                projections.append(metadata)
            except Exception as error:
                errors.append({**error_context, "error": str(error)})
        if errors:
            dump(self.candidate_path(candidate) / "projection_errors.json", {
                "candidate": candidate,
                "trigger_action": action,
                "projection_errors": errors,
            })
        return projections, errors

    def overlay_image(self, metadata):
        """Load only a projection created by this toolkit for MCP image content."""
        relative = metadata.get("overlay_image")
        if not isinstance(relative, str) or not relative.startswith("image_overlays/"):
            raise ValueError("invalid saved overlay reference")
        path = (self.run / relative).resolve()
        folder = (self.run / "image_overlays").resolve()
        if not path.is_file() or folder not in path.parents:
            raise ValueError("saved overlay is unavailable")
        with PILImage.open(path) as raw:
            pic = raw.convert("RGB")
            pic.thumbnail((1600, 1600))
            data = io.BytesIO()
            pic.save(data, "PNG")
        return Image(data=data.getvalue(), format="png")

    def _delivery_projection_status(self, candidate, source):
        """Describe stored projection evidence without treating it as a visual verdict."""
        source_hash = source.get("source_model_sha256")
        folder = self.run / "image_overlays"
        projections = []
        for path in sorted(folder.glob("overlay_*.json")) if folder.is_dir() else []:
            record = json.loads(path.read_bytes())
            if isinstance(record, dict) and record.get("mode") == "source_image_overlay":
                projections.append(record)
        current = [row for row in projections
                   if row.get("candidate") == candidate and row.get("source_model_sha256") == source_hash]
        old = [row for row in projections if row.get("source_model_sha256") != source_hash]
        floor_ids = {row.get("id") for row in source.get("floors", []) if isinstance(row, dict)}
        uncovered = []
        try:
            calibrations = self.registered_calibrations()
            calibration_load_errors = []
        except Exception as error:
            calibrations = []
            calibration_load_errors = [{"candidate": candidate, "trigger_action": "finish_bim",
                                        "error": f"could not load registered calibrations: {error}"}]
        registered_floor_ids = {calibration["floor_id"] for _, calibration in calibrations}
        for calibration_path, calibration in calibrations:
            if calibration["floor_id"] not in floor_ids:
                uncovered.append({
                    "image": calibration["image"], "floor_id": calibration["floor_id"],
                    "calibration_file": calibration_path.relative_to(self.run).as_posix(),
                })
        error_path = self.candidate_path(candidate) / "projection_errors.json"
        errors = json.loads(error_path.read_bytes()).get("projection_errors", []) if error_path.is_file() else []
        errors = [*errors, *calibration_load_errors]
        fields = ("image", "floor_id", "overlay_image", "source_model_sha256", "trigger_action",
                  "automatic_projection", "reused_calibration", "anchors", "basis", "image_sha256")
        def compact(row):
            return {**{field: row[field] for field in fields if field in row},
                    "calibration_warnings": row.get("scale", {}).get("warnings", []),
                    "wall_evidence_projection": row.get("wall_evidence_projection")}
        return {
            "current_source_projections": [compact(row) for row in current],
            "old_source_projections": [compact(row) for row in old],
            "registered_calibration_uncovered_floors": uncovered,
            "floors_without_registered_views": sorted(floor_id for floor_id in floor_ids
                                                       if floor_id not in registered_floor_ids),
            "projection_errors": errors,
            "calibration_independently_verified": False,
            "drawing_fidelity": "not_evaluated",
        }

    def view_claim_evidence(self, claim_id, source_index=0, display_scale=1.0):
        """Render a saved reference, bound to its original bytes; never certify it."""
        if self.readonly:
            raise ValueError("saved claims are available only to the coordinator")
        row = self.claims().read(claim_id)
        if (not isinstance(source_index, int) or isinstance(source_index, bool)
                or not 0 <= source_index < len(row["sources"])):
            raise ValueError("source_index must select an existing zero-based claim source")
        ref = row["sources"][source_index]
        if digest(self.image_path(ref["image"])) != ref["sha256"]:
            raise ValueError("claim original image changed")
        # Enclose fractional references instead of rounding away narrow regions.
        left, top, right, bottom = ref["box"]
        box = [math.floor(left), math.floor(top), math.ceil(right), math.ceil(bottom)]
        picture, raw_metadata = self.view(ref["image"], box, coordinate_grid=False,
                                          display_scale=display_scale)
        metadata = json.loads(raw_metadata)
        if metadata["image_sha256"] != ref["sha256"] or metadata["original_size"] != ref["size"]:
            raise ValueError("claim original image changed")
        metadata.update(claim_id=claim_id, source_index=source_index,
                        claimed_box_original_pixels=ref["box"],
                        verification="not_independently_verified",
                        interpretation="Inspect the actual crop for the cited labels, endpoints and object context. "
                                       "If misplaced, record a corrected claim and retract the obsolete one.")
        self.log("view_claim_evidence", metadata)
        return CallToolResult(content=[picture.to_image_content(),
            TextContent(type="text", text=json.dumps(metadata))], structuredContent=metadata)

    def read_image_view(self, view_id):
        if not isinstance(view_id, str) or not re.fullmatch(r"view_\d{4,}", view_id):
            raise ValueError("choose a saved view_NNNN from view_image")
        path = self.run / "image_views" / f"{view_id}.json"
        if not path.is_file():
            raise ValueError("choose an existing view_NNNN from this run")
        raw = path.read_bytes()
        metadata = json.loads(raw)
        if digest(self.image_path(metadata["name"])) != metadata["image_sha256"]:
            raise ValueError("view original image changed")
        with PILImage.open(self.image_path(metadata["name"])) as image:
            if list(image.size) != metadata["original_size"]:
                raise ValueError("view original size changed")
        return {"record": metadata, "sha256": hashlib.sha256(raw).hexdigest()}

    def elevation_view(self, candidate, facade, image="", horizontal_anchors=None, z_anchors=None, basis=""):
        """Pair an explicitly chosen full original with the current source elevation."""
        from mcp.server.fastmcp import Image
        from src.agent.geometry.source_elevation_view import render_source_elevation
        path = self.candidate_path(candidate)
        source = json.loads((path / "source_model.json").read_bytes())
        if image:
            self.image_path(image)  # Validate before recording a comparison.
        calibrated = horizontal_anchors is not None or z_anchors is not None or bool(basis)
        if calibrated:
            if not image or horizontal_anchors is None or z_anchors is None:
                raise ValueError("calibrated elevation requires image, horizontal_anchors, z_anchors and basis")
            from src.agent.geometry.source_elevation_overlay import render_elevation_overlay
            with PILImage.open(self.image_path(image)) as original:
                pic, metadata = render_elevation_overlay(source, original, facade=facade,
                    horizontal_anchors=horizontal_anchors, z_anchors=z_anchors, basis=basis)
            metadata.update(candidate=candidate, image=image, image_sha256=digest(self.image_path(image)))
            source_picture, metadata = self._save_overlay(pic, metadata)
            image_path = self.run / metadata["overlay_image"]
        else:
            pic, metadata = render_source_elevation(source, facade)
            image_path = path / f"elevation_{facade}.png"
            pic.save(image_path)
            data = io.BytesIO()
            pic.save(data, "PNG")
            source_picture = Image(data=data.getvalue(), format="png")
        metadata.update(candidate=candidate, elevation_image=image_path.relative_to(self.run).as_posix(),
                        elevation_image_sha256=digest(image_path))
        pictures = []
        if image:
            original, original_metadata = self.view(image, coordinate_grid=False)
            original_metadata = json.loads(original_metadata)
            pictures.append(original)
            metadata["original_view"] = original_metadata
            metadata["comparison"] = {
                "image_order": ["full_original", "calibrated_source_elevation" if calibrated else "source_elevation"],
                "image_facade_binding": "caller_selected_not_verified",
                "pixel_alignment": ("caller-calibrated original frame; both returned images have the same scale"
                                    if calibrated else "none; the two images have independent scales and frames"),
                "review": "Match visible opening shapes to source IDs and their above-floor/absolute height intervals. "
                          "Use the full dimension chain and opening outline together; an ordinary-window height "
                          "does not establish other opening families. Reopen a clean crop with view_image when needed. "
                          "Revise only supported discrepancies and recheck the resulting source.",
            }
        pictures.append(source_picture)
        dump(path / f"elevation_{facade}.json", metadata)
        folder = self.run / "elevation_reviews"
        folder.mkdir(exist_ok=True)
        # Exclusive records retain repeated comparisons even when the same facade
        # is later paired with a different original. They are observations, not passes.
        index = len(list(folder.glob("review_*.json"))) + 1
        while True:
            target = folder / f"review_{index:04d}.json"
            metadata["review_file"] = target.relative_to(self.run).as_posix()
            try:
                with target.open("x") as saved:
                    saved.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                break
            except FileExistsError:
                index += 1
        self.log("view_elevation_candidate", metadata)
        return pictures, metadata

    def view(self, name, box=None, coordinate_grid=True, display_scale=1.0):
        from mcp.server.fastmcp import Image
        if (not isinstance(display_scale, (int, float)) or isinstance(display_scale, bool)
                or not math.isfinite(display_scale)):
            raise ValueError("display_scale must be a finite number; allowed range is 1 through 8")
        # GLM sm24 requested 0.7x. Preserve intent within the display-only limits.
        requested_scale = display_scale
        display_scale = max(1, min(8, display_scale))
        name = self.image_path(name).name
        with PILImage.open(self.image_path(name)) as raw:
            pic = raw.convert("RGB")
            original_size = list(pic.size)
            region = box or [0, 0, pic.width, pic.height]
            if box is not None:
                x0,y0,x1,y1 = box
                if not (0 <= x0 < x1 <= pic.width and 0 <= y0 < y1 <= pic.height):
                    raise ValueError("crop outside original image bounds")
                pic = pic.crop(box)
            crop_size = pic.size
            if display_scale == 1:
                # Preserve the existing default presentation, including its
                # downsampling behaviour for large full-image views.
                pic.thumbnail((1600,1600))
            else:
                # Calculate the capped target before resizing so a high requested
                # scale never creates a large temporary bitmap.
                actual_scale = min(float(display_scale), 1600 / max(crop_size))
                target_size = tuple(max(1, round(length * actual_scale)) for length in crop_size)
                pic = pic.resize(target_size, PILImage.Resampling.NEAREST)
            grid = {"shown": False}
            if coordinate_grid:
                pic, grid = coordinate_grid_view(pic, region)
            data = io.BytesIO(); pic.save(data, "PNG")
        actual_scale = [pic.width / crop_size[0], pic.height / crop_size[1]]
        metadata = {"name": name, "image_sha256": digest(self.image_path(name)),
                    "original_size": original_size, "coordinate_grid": grid,
                    "box_original_pixels": region, "returned_size": list(pic.size),
                    "display_scale_requested": requested_scale,
                    "display_scale_used": display_scale,
                    "display_scale_actual": actual_scale,
                    "original_pixels_per_returned_pixel": [
                        (region[2] - region[0]) / pic.width,
                        (region[3] - region[1]) / pic.height],
                    "coordinate_note": ("Grid labels show ORIGINAL pixels. Original pixel = crop origin + returned "
                                        "pixel * original_pixels_per_returned_pixel. Use ORIGINAL pixels for the next "
                                        "crop or measurement.")}
        if requested_scale != display_scale:
            metadata["display_scale_note"] = f"Requested {requested_scale}x; clamped to allowed [1, 8]: {display_scale}x."
        if display_scale > 1 and min(actual_scale) < display_scale - 0.05:
            # run94 asked 1.5-2x for 1100-1500 px strips and got 1.08-1.43x without noticing.
            metadata["magnification_note"] = (
                f"Returned images are at most 1600 px on their long side, so this {crop_size[0]}x{crop_size[1]} px "
                f"box was enlarged only {min(actual_scale):.2f}x; choose a smaller box to magnify more.")
        metadata["remaining_seconds"] = self.remaining_seconds()
        # Small immutable view records let callers reuse the actual returned region.
        # Exclusive creation also keeps simultaneous readers from overwriting it.
        folder = self.run / "image_views"
        folder.mkdir(exist_ok=True)
        index = len(list(folder.glob("view_*.json"))) + 1
        while True:
            metadata["view_id"] = f"view_{index:04d}"
            metadata["source_reference"] = {"view_id": metadata["view_id"]}
            metadata["returned_png_sha256"] = hashlib.sha256(data.getvalue()).hexdigest()
            path = folder / f"{metadata['view_id']}.json"
            raw = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode()
            try:
                with path.open("xb") as saved:
                    saved.write(raw)
                break
            except FileExistsError:
                index += 1
        metadata["view_record_sha256"] = hashlib.sha256(raw).hexdigest()
        self.log("view_image", metadata)
        return [Image(data=data.getvalue(), format="png"), json.dumps(metadata)]

    def plan_wall_support(self, draft_id, rgb, tolerance=70, radius_pixels=6, minimum_ink_pixels=1):
        """Review exactly one saved declaration; never mutate or approve it."""
        from src.agent.geometry.plan_wall_support import measure_plan_wall_support
        if self.readonly:
            raise ValueError("saved plan drafts are available only to the coordinator")
        if (not isinstance(draft_id, str) or not draft_id.startswith("draft_")
                or not draft_id[6:].isdigit()):
            raise ValueError("choose a saved draft_NNN from build_plan_bim")
        folder = self.run / "plan_drafts" / draft_id
        if not (folder / "input.json").is_file():
            raise ValueError("choose an existing saved plan draft")
        admitted = json.loads((folder / "input.json").read_bytes())
        plan_path = folder / "plan.json"
        if digest(plan_path) != admitted["plan_sha256"]:
            raise ValueError("saved plan changed")
        image_path = self.image_path(admitted["image"])
        if digest(image_path) != admitted["image_sha256"]:
            raise ValueError("draft original image changed")
        compilation_path = folder / "compilation.json"
        compilation = None
        if compilation_path.is_file():
            saved = json.loads((folder / "result.json").read_bytes())["plan_input"]
            if (saved["plan_sha256"] != admitted["plan_sha256"]
                    or digest(compilation_path) != saved["compilation_sha256"]):
                raise ValueError("saved compilation changed")
            compilation = json.loads(compilation_path.read_bytes())
        with PILImage.open(image_path) as original:
            pic, result = measure_plan_wall_support(original, json.loads(plan_path.read_bytes()),
                rgb=rgb, tolerance=tolerance, radius_pixels=radius_pixels,
                minimum_ink_pixels=minimum_ink_pixels,
                spaces=compilation["space_mapping"] if compilation else None)
        target = self.run / "plan_wall_support"
        target.mkdir(exist_ok=True)
        stem = f"support_{len(list(target.glob('support_*.json'))) + 1:03d}"
        pic.save(target / f"{stem}.png")
        result.update(draft_id=draft_id, plan_sha256=admitted["plan_sha256"],
                      image=admitted["image"], image_sha256=admitted["image_sha256"],
                      support_image=f"plan_wall_support/{stem}.png",
                      support_record=f"plan_wall_support/{stem}.json",
                      support_image_sha256=digest(target / f"{stem}.png"),
                      remaining_seconds=self.remaining_seconds())
        returned = pic.copy()
        returned.thumbnail((1600, 1600))
        data = io.BytesIO()
        returned.save(data, "PNG")
        result["returned_size"] = list(returned.size)
        result["original_combined_size"] = list(pic.size)
        dump(target / f"{stem}.json", result)
        self.log("view_plan_wall_support", {"draft_id": draft_id, "record": result["support_record"],
                                             "review_interval_count": len(result["review_intervals"])})
        return [Image(data=data.getvalue(), format="png"), json.dumps(result)]

    def profile(self, name, box, axis, rgb, tolerance):
        import numpy as np
        with PILImage.open(self.image_path(name)) as raw:
            x0,y0,x1,y1 = box
            if not (0 <= x0 < x1 <= raw.width and 0 <= y0 < y1 <= raw.height):
                raise ValueError("box outside original image bounds")
            pixels = np.asarray(raw.convert("RGB").crop(box)).astype(float)
        if len(rgb) != 3 or not all(0 <= c <= 255 for c in rgb) or not 0 <= tolerance <= 442:
            raise ValueError("RGB in 0..255 and distance tolerance in 0..442 required")
        mask = np.linalg.norm(pixels - np.asarray(rgb), axis=2) <= tolerance
        if axis not in {"x", "y"}:
            raise ValueError("axis must be x or y")
        counts = mask.sum(axis=0 if axis == "x" else 1)
        offset = x0 if axis == "x" else y0
        # Return runs of positive support plus maxima, without naming objects.
        runs = _profile_positive_runs(counts, offset)
        result = {"axis": axis, "runs": runs, "matching_pixels": int(mask.sum()),
                  "name": name, "box_original_pixels": list(box),
                  "support_length": mask.shape[0] if axis == "x" else mask.shape[1],
                  "coordinate_system": "original image pixels; interval endpoints inclusive",
                  "evidence_note": "pixels spans any positive color support, including connected "
                      "lines, text and arrow tips; its ends are not measured dimension or object endpoints. "
                      "peak is only the FIRST global maximum in the run. support_peaks lists every "
                      "positive local maximum plateau with its count, including weaker peaks, in "
                      "original coordinates; no smoothing, threshold or endpoint selection is applied. "
                      "A plateau is higher than both adjacent counts (zero outside the run). "
                      "Peaks can include noise and do not identify ticks, walls or openings. Inspect "
                      "the original crop or use view_pixel_profile with the same input image name and "
                      "a chosen min_fraction before adopting coordinates."}
        diagnostic = _empty_profile_diagnostics(
            pixels, rgb, counts, 1, mask.shape[0] if axis == "x" else mask.shape[1])
        if diagnostic is not None:
            result["empty_filter_diagnostics"] = diagnostic
        self.log("pixel_profile", {"name": name, "box": box, "rgb": rgb,
                                   "tolerance": tolerance, "result": result})
        return result

    def view_profile(self, name, box, axis, rgb, tolerance, min_fraction):
        """Return a checkable profile table and image without assigning semantics."""
        import numpy as np
        if (not isinstance(min_fraction, (int, float)) or isinstance(min_fraction, bool)
                or not 0 < min_fraction <= 1):
            raise ValueError("min_fraction must be a number greater than 0 and at most 1")
        with PILImage.open(self.image_path(name)) as raw:
            x0, y0, x1, y1 = box
            if not (0 <= x0 < x1 <= raw.width and 0 <= y0 < y1 <= raw.height):
                raise ValueError("box outside original image bounds")
            crop = raw.convert("RGB").crop(box)
            original_size = raw.size
            pixels = np.asarray(crop).astype(float)
        if len(rgb) != 3 or not all(0 <= c <= 255 for c in rgb) or not 0 <= tolerance <= 442:
            raise ValueError("RGB in 0..255 and distance tolerance in 0..442 required")
        if axis not in {"x", "y"}:
            raise ValueError("axis must be x or y")
        mask = np.linalg.norm(pixels - np.asarray(rgb), axis=2) <= tolerance
        counts, minimum_count, support_length, runs = _profile_axis(mask, box, axis, min_fraction)
        projection_offset = x0 if axis == "x" else y0
        support_offset = y0 if axis == "x" else x0
        candidates = [{"id": f"C{i + 1:02d}", **row} for i, row in enumerate(runs)]
        other_axis = "y" if axis == "x" else "x"
        other_counts, other_minimum, other_length, other_runs = _profile_axis(mask, box, other_axis, min_fraction)
        edge_support = {}
        for edge, support, offset in (("left", mask[:, 0], y0), ("right", mask[:, -1], y0),
                                      ("top", mask[0, :], x0), ("bottom", mask[-1, :], x0)):
            edge_support[edge] = [[lo + offset, hi + offset] for lo, hi in _inclusive_runs(support)]

        # Keep the original crop untouched on the left.  The right panel is a
        # separate exact mask view with candidate bands and peaks labelled.
        panel_pixels = np.full((crop.height, crop.width, 3), 255, dtype=np.uint8)
        panel_pixels[mask] = (150, 150, 150)
        panel = PILImage.fromarray(panel_pixels, "RGB")
        draw = ImageDraw.Draw(panel)
        for candidate in candidates:
            lo = candidate["pixels"][0] - projection_offset
            hi = candidate["pixels"][1] - projection_offset
            peak = candidate["peak"] - projection_offset
            if axis == "x":
                draw.rectangle((lo, 0, hi, crop.height - 1), outline=(220, 0, 120), width=1)
                for support_lo, support_hi in candidate["support_intervals_at_peak"]:
                    draw.line((peak, support_lo - support_offset,
                               peak, support_hi - support_offset), fill=(0, 110, 220), width=1)
                label_xy = (lo + 1, 1)
            else:
                draw.rectangle((0, lo, crop.width - 1, hi), outline=(220, 0, 120), width=1)
                for support_lo, support_hi in candidate["support_intervals_at_peak"]:
                    draw.line((support_lo - support_offset, peak,
                               support_hi - support_offset, peak), fill=(0, 110, 220), width=1)
                label_xy = (1, lo + 1)
            draw.text(label_xy, candidate["id"], fill="black")
        combined = PILImage.new("RGB", (crop.width * 2 + 3, crop.height), "white")
        combined.paste(crop, (0, 0))
        combined.paste(panel, (crop.width + 3, 0))
        ImageDraw.Draw(combined).rectangle((crop.width, 0, crop.width + 2, crop.height - 1), fill="black")

        folder = self.run / "pixel_profiles"
        folder.mkdir(exist_ok=True)
        stem = f"profile_{len(list(folder.glob('profile_*.json'))) + 1:03d}"
        image_path = folder / f"{stem}.png"
        record_path = folder / f"{stem}.json"
        combined.save(image_path)
        result = {
            "name": name,
            "profile_id": stem,
            "image_sha256": self.manifest["images"][name]["sha256"],
            "axis": axis,
            "box_original_pixels": box,
            "rgb": rgb,
            "tolerance": tolerance,
            "min_fraction": float(min_fraction),
            "minimum_count": minimum_count,
            "support_length": support_length,
            "matching_pixels": int(mask.sum()),
            "candidates": candidates,
            "positive_support_runs": _profile_positive_runs(counts, projection_offset),
            "threshold_excluded_support": _profile_excluded_support(
                counts, projection_offset, minimum_count, support_length),
            "cross_axis_profile": {
                "axis": other_axis, "min_fraction": float(min_fraction),
                "minimum_count": other_minimum, "support_length": other_length,
                "runs": other_runs,
                "threshold_excluded_support": _profile_excluded_support(
                    other_counts, support_offset, other_minimum, other_length),
                "note": "Same exact mask and fraction, measured along the other axis. "
                        "Compare long traces with local junction peaks; neither is a wall label. "
                        "For bindable C IDs on this axis, request a profile using this axis.",
            },
            "crop_context": {
                "edge_support_intervals": edge_support,
                "suggested_view_box": [max(0, x0 - 32), max(0, y0 - 32),
                                       min(original_size[0], x1 + 32), min(original_size[1], y1 + 32)],
                "note": "Edge intervals are matching pixels on the crop border (right/bottom are exclusive "
                        "box limits, so samples are at right-1/bottom-1). A crop edge is not an object endpoint. "
                        "The suggested box is only an initial 32px context expansion; inspect further as needed "
                        "to establish both endpoints and adjoining space boundaries. No continuation is inferred.",
            },
            "profile_image": image_path.relative_to(self.run).as_posix(),
            "profile_record": record_path.relative_to(self.run).as_posix(),
            "profile_image_sha256": digest(image_path),
            "panel_layout": {
                "original_crop_combined_pixels": [0, 0, crop.width, crop.height],
                "mask_panel_combined_pixels": [crop.width + 3, 0, combined.width, crop.height],
                "separator_width": 3,
            },
            "panel_note": "Left is the untouched original crop. Right is an exact color-distance mask: magenta outlines candidate bands; blue marks only actual support at the peak coordinate. Marks are coordinate references, not entities. Right-panel local coordinates correspond to the same original crop after removing its combined-image offset.",
            "evidence_note": "Support intervals are measured only at each candidate peak. They do not prove a whole band is continuous or identify a wall. Filtered or empty results do not prove an object is absent.",
            "remaining_seconds": self.remaining_seconds(),
        }
        diagnostic = _empty_profile_diagnostics(pixels, rgb, counts, minimum_count, support_length)
        if diagnostic is not None:
            result["empty_filter_diagnostics"] = diagnostic
        returned = combined.copy()
        returned.thumbnail((1600, 1600))
        data = io.BytesIO()
        returned.save(data, "PNG")
        result["returned_size"] = list(returned.size)
        result["original_pixels_per_returned_pixel"] = [
            combined.width / returned.width, combined.height / returned.height]
        result["display_note"] = "For a returned-image point (rx, ry), first multiply by original_pixels_per_returned_pixel to obtain full combined-image coordinates (fx, fy). On the left, original image coordinates are (box_left + fx, box_top + fy). On the right, subtract mask_panel_combined_pixels[0] from fx before adding box_left. Prefer candidates and support intervals, which already use original-image coordinates."
        dump(record_path, result)
        self.log("view_pixel_profile", {"name": name, "box": box, "rgb": rgb,
                                        "tolerance": tolerance, "min_fraction": min_fraction,
                                        "result": result})
        return [Image(data=data.getvalue(), format="png"), json.dumps(result)]


    def pixel_region_overview(self, name, background_rgb, tolerance, min_pixels, max_regions, include_border):
        from src.agent.geometry.pixel_region_overview import render_pixel_region_overview
        with PILImage.open(self.image_path(name)) as original:
            picture, record = render_pixel_region_overview(original, background_rgb=background_rgb,
                tolerance=tolerance, min_pixels=min_pixels, max_regions=max_regions, include_border=include_border)
        folder = self.run / "pixel_region_overviews"
        folder.mkdir(exist_ok=True)
        overview_id = f"overview_{len(list(folder.glob('overview_*.json'))) + 1:03d}"
        picture.save(folder / f"{overview_id}.png")
        record.update(overview_id=overview_id, name=name, image_sha256=digest(self.image_path(name)),
                      overview_image=f"pixel_region_overviews/{overview_id}.png",
                      remaining_seconds=self.remaining_seconds())
        dump(folder / f"{overview_id}.json", record)
        self.log("view_pixel_region_overview", record)
        buffer = io.BytesIO(); picture.save(buffer, "PNG")
        return [Image(data=buffer.getvalue(), format="png"), json.dumps(record)]

    def pixel_region(self, name, seed_pixel, background_rgb, tolerance, simplify_pixels):
        from src.agent.geometry.pixel_region import render_pixel_region
        with PILImage.open(self.image_path(name)) as original:
            picture, record = render_pixel_region(original, seed_pixel=seed_pixel,
                background_rgb=background_rgb, tolerance=tolerance, simplify_pixels=simplify_pixels)
        folder = self.run / "pixel_regions"
        folder.mkdir(exist_ok=True)
        region_id = f"region_{len(list(folder.glob('region_*.json'))) + 1:03d}"
        picture.save(folder / f"{region_id}.png")
        record.update(region_id=region_id, name=name, image_sha256=digest(self.image_path(name)),
                      region_image=f"pixel_regions/{region_id}.png", remaining_seconds=self.remaining_seconds())
        dump(folder / f"{region_id}.json", record)
        self.log("view_pixel_region", record)
        buffer = io.BytesIO(); picture.save(buffer, "PNG")
        return [Image(data=buffer.getvalue(), format="png"), json.dumps(record)]

    def preview_trace(self, name, polygon_pixels, openings, x_anchors, y_anchors, basis):
        from src.agent.geometry.space_trace import render_space_trace
        with PILImage.open(self.image_path(name)) as original:
            picture, result = render_space_trace(original, polygon_pixels=polygon_pixels,
                openings=openings, x_anchors=x_anchors, y_anchors=y_anchors, basis=basis)
        folder = self.run / "space_traces"
        folder.mkdir(exist_ok=True)
        trace = f"trace_{len(list(folder.glob('trace_*.json'))) + 1:03d}"
        picture.save(folder / f"{trace}.png")
        result.update(trace_id=trace, name=name, image_sha256=digest(self.image_path(name)),
                      trace_image=f"space_traces/{trace}.png", remaining_seconds=self.remaining_seconds())
        dump(folder / f"{trace}.json", result)
        self.log("preview_space_trace", result)
        buffer = io.BytesIO(); picture.save(buffer, "PNG")
        return [Image(data=buffer.getvalue(), format="png"), json.dumps(result)]

    def view_trace(self, trace_id):
        allowed = {p.stem for p in (self.run / "space_traces").glob("trace_*.json")}
        if trace_id not in allowed:
            raise ValueError("unknown trace_id")
        record = json.loads((self.run / "space_traces" / f"{trace_id}.json").read_bytes())
        if digest(self.image_path(record["name"])) != record["image_sha256"]:
            raise ValueError("trace original image changed")
        self.log("view_space_trace", {"trace_id": trace_id})
        return [Image(data=(self.run / "space_traces" / f"{trace_id}.png").read_bytes(), format="png"), json.dumps(record)]

    def select_trace(self, trace_id):
        allowed = {p.stem for p in (self.run / "space_traces").glob("trace_*.json")}
        if trace_id not in allowed:
            raise ValueError("unknown trace_id; first preview_space_trace")
        result = json.loads((self.run / "space_traces" / f"{trace_id}.json").read_bytes())
        if not result["geometrically_executable"]:
            raise ValueError("trace has geometry errors; revise the contour/aperture endpoints first")
        if digest(self.image_path(result["name"])) != result["image_sha256"]:
            raise ValueError("trace original image changed")
        selection = {"trace_id": trace_id, "trace_sha256": digest(self.run / "space_traces" / f"{trace_id}.json"),
                     "drawing_fidelity": "not_evaluated", "selection_origin": "model_selected"}
        dump(self.run / "trace_selection.json", selection)
        self.log("select_space_trace", selection)
        return selection


def build_tool_server(run: Path, readonly=False, *, enabled_only=False):
    """Construct the sole tool catalog without starting transport or executing tools."""
    from mcp.server.fastmcp import Image
    from scripts.tool_scripts.bim_agent_feedback import FeedbackMCP
    toolkit = Toolkit(run, readonly)
    server = FeedbackMCP(toolkit, "bim", log_level="WARNING")
    from scripts.tool_scripts.bim_agent_mesh import register_mesh_tools
    register_mesh_tools(server, toolkit)
    from scripts.tool_scripts.bim_agent_inference import register_inference_tools
    register_inference_tools(server, toolkit)

    @server.tool()
    def get_bim_reference(topic: Literal[tuple(REFERENCES)]) -> dict:
        """Read a generic format, editing, evidence or modelling reference.
        Choose a topic from the schema. References contain no case answers.
        """
        if topic not in REFERENCES:
            raise ValueError("unknown topic; choose " + ", ".join(REFERENCES))
        toolkit.log("get_bim_reference", {"topic": topic})
        return {"topic": topic, "reference": REFERENCES[topic],
                "remaining_seconds": toolkit.remaining_seconds()}

    @server.tool()
    def inputs() -> dict:
        """List admitted original images, native mesh, building declaration and scope."""
        toolkit.log("inputs", {})
        return {**toolkit.manifest, "input_view_status": toolkit.input_view_status(),
                "candidate_budget": toolkit.candidate_budget(),
                "remaining_seconds": toolkit.remaining_seconds()}

    @server.tool()
    def view_image(name: ImageFilename = "", box: list[int] | None = None, coordinate_grid: bool = True,
                   display_scale: float = 1.0, claim_id: str = "", source_index: int = 0):
        """View all or crop [left,top,right,bottom] in ORIGINAL pixels.
        Returned images are at most 1600 px on their long side; grid labels keep original
        coordinates. display_scale enlarges up to that limit, so a box whose longest side
        is under ~500 px can be shown 3x or more; coordinates stay original pixels.
        Use coordinate_grid=false for unmarked evidence; stored originals are unchanged.
        Returned view_id can be used directly in claim sources or replace_claim_sources;
        do not copy crop coordinates again when citing exactly this view.
        Or supply claim_id/source_index to reopen that hash-checked saved evidence;
        omit name/box in this mode. display_scale still applies.
        """
        if claim_id:
            if name or box is not None:
                raise ValueError("claim_id selects the saved image/box; omit name and box")
            return toolkit.view_claim_evidence(claim_id, source_index, display_scale)
        return toolkit.view(name, box, coordinate_grid, display_scale)

    @server.tool()
    def pixel_profile(name: ImageFilename, box: list[int], axis: Literal["x", "y"],
                      rgb: list[int], tolerance: float = 70) -> dict:
        """Historical replay only; use view_pixel_profile, optionally include_image=false."""
        return toolkit.profile(name, box, axis, rgb, tolerance)

    @server.tool()
    def view_pixel_profile(name: ImageFilename, box: list[int], axis: Literal["x", "y"],
                           rgb: list[int], tolerance: float = 70,
                           min_fraction: float = 0.1, include_image: bool = True):
        """Show a color profile in ORIGINAL pixels.
        axis=x searches x coordinates and reports unbridged y support at each
        peak; axis=y does the converse. min_fraction is the required matching
        share along the other axis. Results are pixel evidence, not object labels.
        Left panel is the original crop; right is the exact color mask, magenta
        candidate bands and blue peak support. Multiply returned-image coordinates
        by original_pixels_per_returned_pixel, subtract the right panel offset
        when applicable, then add the crop origin. Candidates already use original pixels.
        Peak support is not whole-band continuity; empty/filtered ink is not absence.
        Auxiliary summaries count unthresholded peaks, cross-axis support and cut
        crop edges; their full intervals and notes are in details_file, readable
        with read_candidate_items(collection="report", report_file=details_file).
        Use profile_id/C IDs in build_plan_bim or compare_facade_spans pixel slots.
        include_image=false returns the same saved measurement without its picture.
        """
        picture, metadata = toolkit.view_profile(name, box, axis, rgb, tolerance, min_fraction)
        from scripts.tool_scripts.bim_agent_replies import compact_reply
        reply = compact_reply(toolkit.run, 'view_pixel_profile', json.loads(metadata))
        return [picture, json.dumps(reply)] if include_image else reply

    @server.tool()
    def view_pixel_region_overview(name: ImageFilename, background_rgb: list[int], tolerance: float = 60,
                                   min_pixels: int = 500, max_regions: int = 40,
                                   include_border: bool = False):
        """Locate numbered colour-connected candidates in the full original image.
        background_rgb is the target colour: either ink (e.g. a frame) or a
        clear area's background. Compare the labelled overview to the original,
        then pass a returned ORIGINAL-pixel seed to view_pixel_region.
        Small ink strokes may need a lower min_pixels than clear floor regions.
        Area/border filters and truncation are reported. Candidates can include
        furniture, dimension marks or connected spaces; IDs are not object labels.
        """
        return toolkit.pixel_region_overview(name, background_rgb, tolerance, min_pixels, max_regions, include_border)

    @server.tool()
    def view_pixel_region(name: ImageFilename, seed_pixel: list[int], background_rgb: list[int],
                          tolerance: float = 60, simplify_pixels: float = 1.5):
        """Display the complete 4-connected target-colour region at a selected pixel.
        background_rgb names the target colour, including ink or clear floor.
        Choose a seed on that colour, preferably an overview candidate's seed.
        For clear floor choose away from furniture; for frames choose on the ink.
        Returns a non-semantic contour and half-open bbox, NOT a room/aperture.
        One physical frame may contain disconnected pieces; touching same-colour
        marks may merge. simplify_pixels=0 keeps the exact pixel outline.
        Check the original surrounding wall and jambs before interpreting it.
        All points and seed coordinates refer to the ORIGINAL image.
        """
        return toolkit.pixel_region(name, seed_pixel, background_rgb, tolerance, simplify_pixels)

    @server.tool()
    def preview_space_trace(name: ImageFilename, polygon_pixels: list[list[float]], openings: list[dict],
                            x_anchors: list[list[float]], y_anchors: list[list[float]], basis: str):
        """Historical replay: preview an original-pixel contour without changing BIM."""
        return toolkit.preview_trace(name, polygon_pixels, openings, x_anchors, y_anchors, basis)

    @server.tool()
    def view_space_trace(trace_id: str):
        """Historical replay: reopen a saved local trace."""
        return toolkit.view_trace(trace_id)

    @server.tool()
    def select_space_trace(trace_id: str) -> dict:
        """Historical replay: record trace selection, without changing BIM."""
        return toolkit.select_trace(trace_id)

    @server.tool()
    def map_dimension_chain(lengths: list[float], unit: Literal["mm", "m"] = "mm",
                            origin_m: float = 0.0, direction: Literal[-1, 1] = 1,
                            expected_total: float | None = None) -> dict:
        """Accumulate observed dimension labels into ordered world-metre spans.
        lengths and expected_total use unit mm or m; origin_m is always metres.
        direction -1 walks from a known high coordinate toward lower coordinates.
        No OCR or semantic validation: retain evidence for labels and orientation.
        """
        from src.agent.geometry.dimension_chain import map_dimension_chain as calculate
        result = calculate(lengths, unit=unit, origin_m=origin_m,
                           direction=direction, expected_total=expected_total)
        toolkit.log("map_dimension_chain", {"lengths": lengths, "result": result})
        return result

    @server.tool()
    def compare_facade_spans(plan_image: ImageFilename, elevation_image: ImageFilename, observations_json: str,
                             plan_axis: Literal["x", "y"] = "y", elevation_axis: Literal["x", "y"] = "x",
                             ambiguity_tolerance_m: float = 0.05) -> dict:
        """Compare complete independently observed opening lists in BOTH axis directions.
        Exact original image names and original pixel coordinates only. JSON has plan
        and elevation, each with axis_anchors [[pixel,0],[pixel,L_metres]] in either
        row order and openings
        [{"id":"observed_id","pixels":[start,end]}]. Retain types/evidence as extra fields.
        Any pixel slot may instead be {"profile":"profile_001","candidate":"C01",
        "at":"peak"}; at=peak/start/end, default peak. Uses saved measurement pixels
        directly, checks image and axis, and preserves the binding. No numeric offset.
        Read facade_correspondence for an example and limitations. No automatic OCR,
        direction acceptance, missing-opening repair or source mutation.
        """
        return toolkit.compare_facade_spans(plan_image, elevation_image, observations_json,
                                             plan_axis, elevation_axis, ambiguity_tolerance_m)

    def candidate_result(result) -> CallToolResult:
        """Keep JSON structured output while attaching newly generated feedback views."""
        from scripts.tool_scripts.bim_agent_saved_result import saved_result
        if "saved_candidate" not in result:
            saved_result(result)
        # Put drawing differences, then actionable dimensions/edits, ahead of the large inventories.
        result = {**{k: result[k] for k in ("drawing_differences", "plan_revision", "plan_input") if k in result}, **result}
        content = []
        draft_view = result.get("plan_input", {}).get("draft_view")
        if not result.get("source_geometry_ready") and draft_view:
            try:
                content.append(Image(data=(toolkit.run / draft_view["image_file"]).read_bytes(),
                                     format="png").to_image_content())
            except Exception as error:
                result.setdefault("plan_input", {}).setdefault("draft_view_errors", []).append({
                    "path": draft_view.get("image_file"),
                    "reason": f"MCP result packaging failed: {error}",
                })
        host_failure_view = result.get("plan_input", {}).get("host_failure_view")
        if not result.get("source_geometry_ready") and host_failure_view:
            try:
                content.append(Image(data=(toolkit.run / host_failure_view["image_file"]).read_bytes(),
                                     format="png").to_image_content())
            except Exception as error:
                result.setdefault("plan_input", {}).setdefault("host_failure_view_errors", []).append({
                    "path": host_failure_view.get("image_file"),
                    "reason": f"MCP result packaging failed: {error}",
                })
        for metadata in result.get("source_image_projections", []):
            try:
                content.append(toolkit.overlay_image(metadata).to_image_content())
            except Exception as error:
                result.setdefault("projection_errors", []).append({
                    "candidate": result.get("candidate"), "trigger_action": "mcp_result_packaging",
                    "image": metadata.get("image"), "floor_id": metadata.get("floor_id"),
                    "overlay_image": metadata.get("overlay_image"), "error": str(error),
                })
        for metadata in result.get("source_plan_views", []):
            try:
                content.append(Image(data=(toolkit.run / metadata["plan_image"]).read_bytes(),
                                     format="png").to_image_content())
            except Exception as error:
                result.setdefault("source_plan_errors", []).append({
                    "candidate": result.get("candidate"), "floor_id": metadata.get("floor_id"),
                    "trigger_action": "mcp_result_packaging", "error": str(error)})
        from scripts.tool_scripts.bim_agent_replies import compact_reply
        result = compact_reply(toolkit.run, "build_bim", result)
        content.append(TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":"))))
        return CallToolResult(content=content, structuredContent=result)

    @server.tool()
    def map_pixels(points: list[list[float]], x_anchors: list[list[float]],
                   y_anchors: list[list[float]]) -> dict:
        """Convert selected original pixel points to metres with two anchors per axis.
        Each anchor is [pixel_position, world_metres]. Y normally has negative
        slope. Anchors must come from your observed dimensions or explicit assumption;
        this tool does arithmetic and does not establish the drawing scale for you.
        """
        import math
        def fit(anchors):
            if len(anchors)!=2 or any(len(a)!=2 for a in anchors):
                raise ValueError("exactly two [pixel, metre] anchors per axis")
            if not all(math.isfinite(v) for a in anchors for v in a):
                raise ValueError("anchors must be finite")
            (p0,w0),(p1,w1)=anchors
            if p0==p1 or w0==w1: raise ValueError("distinct anchors required")
            slope=(w1-w0)/(p1-p0)
            return slope,w0-slope*p0
        ax,bx=fit(x_anchors); ay,by=fit(y_anchors)
        if any(len(p)!=2 or not all(math.isfinite(v) for v in p) for p in points):
            raise ValueError("points must be finite [x,y]")
        result={"world_points":[[round(ax*x+bx,6),round(ay*y+by,6)] for x,y in points],
                "metres_per_pixel":[ax,ay]}
        toolkit.log("map_pixels",{"points":points,"x_anchors":x_anchors,
                                  "y_anchors":y_anchors,"result":result})
        return result

    @server.tool()
    def read_candidate_items(candidate: str, collection: Literal["cells", "windows", "openings", "unsupported", "boundaries", "report"], floor_id: str | None = None,
                             offset: int = 0, limit: int | None = None, report_file: str = "",
                             json_pointer: str | None = None) -> dict:
        """Read exact saved proposal cells, windows, openings or unsupported records.
        collection=report reads a returned details_file via report_file, with
        candidate="" and offset/limit in characters (limit up to 12000).
        Optional json_pointer browses that exact report as bounded JSON; offset
        then counts container children or text characters. Follow returned next.
        collection=boundaries reads actual saved source boundary IDs, vertices,
        adjacency and opening_ids, with source file/model hashes. Wall IDs can
        scope overlay_mesh_candidate. floor_id includes spanning spaces. Large
        boundary records expose a bounded view and immutable details_file.
        Each cell includes floor_id. Read next_offset for more; page_is_partial
        means this is not a complete build_bim input. Prefer revise_bim to keep
        unexamined objects. Other collections read the proposal. These are saved
        model records, not original mesh/GT observations.
        """
        if limit is None:
            limit = 8000 if collection == 'report' else 20
        if collection == 'report':
            from scripts.tool_scripts.bim_agent_replies import read_report
            return read_report(toolkit.run, report_file, offset, limit, json_pointer=json_pointer)
        if json_pointer is not None:
            raise ValueError('json_pointer requires collection=report and its saved report_file')
        if collection not in {'cells','windows','openings','unsupported','boundaries'}:
            raise ValueError('collection must be cells, windows, openings, unsupported or boundaries')
        if not 1 <= limit <= 50 or offset < 0:
            raise ValueError('requires offset >= 0 and limit 1..50')
        path = toolkit.candidate_path(candidate)
        if collection == 'boundaries':
            from src.agent.geometry.source_model import _digest
            source_raw = (path/'source_model.json').read_bytes()
            source = json.loads(source_raw)
            expected = _digest({k: v for k, v in source.items() if k != 'source_model_sha256'})
            if source.get('schema_version') not in {'source_bim_v2', 'source_bim_v3'} or source.get('source_model_sha256') != expected:
                raise ValueError('invalid source BIM schema or digest')
            if floor_id is not None and floor_id not in {f['id'] for f in source['floors']}:
                raise ValueError('unknown floor_id')
            spaces = {s['id']: s for s in source['spaces']}
            ids = {s['id'] for s in spaces.values() if floor_id is None or s['floor_id'] == floor_id}
            if floor_id is not None:
                floor = next(f for f in source['floors'] if f['id'] == floor_id)
                ids.update(floor.get('spanning_space_ids', []))
            openings_by_host = {}
            for opening_id, hosts in source.get('opening_hosts', {}).items():
                for host in hosts:
                    openings_by_host.setdefault(host, []).append(opening_id)
            items = [{**boundary, 'floor_id': spaces[boundary['space_id']]['floor_id'],
                      'opening_ids': openings_by_host.get(boundary['id'], [])}
                     for boundary in source['boundaries'] if boundary['space_id'] in ids]
            identity = {'source_file_sha256': hashlib.sha256(source_raw).hexdigest(),
                        'source_model_sha256': expected}
        else:
            proposal_path = path/'proposal.json'
            geometry = json.loads(proposal_path.read_bytes())['geometry']
            if floor_id is not None and floor_id not in {f['name'] for f in geometry['floors']}:
                raise ValueError('unknown floor_id')
            floors = [f for f in geometry['floors'] if floor_id is None or f['name'] == floor_id]
            cells = [{**c,'floor_id':f['name']} for f in floors for c in f['cells']]
            ids = {c['id'] for c in cells}
            items = (cells if collection == 'cells' else
                [r for r in geometry.get('unsupported', []) if floor_id is None or r.get('floor_id') == floor_id]
                if collection == 'unsupported' else
                [w for w in geometry.get('windows',[]) if floor_id is None or w['floor'] == floor_id]
                if collection == 'windows' else
                [o for o in geometry.get('openings',[]) if floor_id is None or o['space_id'] in ids
                 or o.get('other_space_id') in ids])
            identity = {'proposal_sha256':digest(proposal_path)}
        page = []
        for item in items[offset:offset+limit]:
            if len(json.dumps(page+[item])) > 18000:
                if not page:
                    if collection == 'boundaries':
                        page.append(item)  # Immutable report browsing bounds large source records.
                        break
                    raise ValueError('single object exceeds reply size; exact proposal remains on disk')
                break
            page.append(item)
        result = {'candidate':candidate, **identity,
            'collection':collection,'floor_id':floor_id,'total':len(items),'offset':offset,
            'items':page,'returned':len(page),
            'next_offset':offset+len(page) if offset+len(page)<len(items) else None,
            'page_is_partial':True}
        toolkit.log('read_candidate_items', {k:v for k,v in result.items() if k!='items'})
        if collection == 'boundaries':
            from scripts.tool_scripts.bim_agent_replies import compact_reply
            return compact_reply(toolkit.run, 'read_candidate_boundaries', result, force=True)
        return result

    if not readonly:
        @server.tool()
        def inspect_plan_draft(draft_id: str, field: str | None = None,
                               object_id: str | None = None) -> dict:
            """Inspect a saved draft_NNN or resume, retaining its immutable revision hash.
            Default returns field/object summaries. Read field='plan', a field name
            with optional stable object_id, or field='plan:/openings/0' for JSON pointers.
            field='report:/drawing_differences' or 'report:/regularization' browses
            that draft's bound findings, including rejected drafts.
            Large values return references/entries; follow next, or object_id='page:8'.
            Use plan_sha256 with revise_plan_bim; never resend generated audit records.
            """
            from src.agent.runtime_roles.trial_feedback import (
                _bytes, _page_offset, bounded_json_view, read_plan,
            )
            saved = toolkit.inspect_plan(draft_id)
            if field == 'report:/regularization' or (field or '').startswith('report:/regularization/'):
                report = toolkit.run / "plan_drafts" / str(draft_id) / "regularization.json"
                record = json.loads(report.with_name('input.json').read_bytes())
                binding = record.get('regularization_report')
                raw = report.read_bytes()
                if (not isinstance(binding, dict)
                        or binding.get('file') != report.relative_to(toolkit.run).as_posix()
                        or binding.get('sha256') != hashlib.sha256(raw).hexdigest()):
                    raise ValueError('saved regularization report changed or has no bound hash')
                saved['regularization'] = json.loads(raw)
            report = toolkit.run / "plan_drafts" / str(draft_id) / "drawing_differences.json"
            if draft_id != "resume" and report.is_file():
                receipt = report.with_name('result.json')
                binding = (json.loads(receipt.read_bytes()).get('drawing_differences', {})
                           if receipt.is_file() else {})
                if (binding.get('file') != report.relative_to(toolkit.run).as_posix()
                        or binding.get('sha256') != digest(report)):
                    raise ValueError('saved drawing differences report changed or has no bound hash')
                data = json.loads(report.read_bytes())
                if data.get("plan_sha256") != saved["plan_sha256"]:
                    raise ValueError('saved drawing differences report names a different plan')
                saved["drawing_differences"] = data
            identity = {key: saved[key] for key in ('draft_id', 'plan_sha256', 'image')}

            def report_reference(pointer, value):
                raw = _bytes(value)
                return {**identity, 'json_pointer': pointer,
                    'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'read': {'tool': 'inspect_plan_draft', 'arguments': {
                        'draft_id': draft_id, 'field': 'report:' + pointer}}}

            if field is not None and field.startswith('report:'):
                pointer = field.removeprefix('report:')
                result = {**identity, **bounded_json_view(saved, pointer=pointer,
                    offset=_page_offset(object_id), reference_for=report_reference)}
                if 'next_offset' in result:
                    result['next'] = {'draft_id': draft_id, 'field': field,
                                      'object_id': f"page:{result.pop('next_offset')}"}
                return result
            result = {**identity, **read_plan(saved['declaration'], identity, field=field,
                object_id=object_id, read_tool='inspect_plan_draft',
                read_arguments={'draft_id': draft_id})}
            if field is None and 'drawing_differences' in saved:
                result['drawing_differences'] = bounded_json_view(saved,
                    pointer='/drawing_differences', reference_for=report_reference)
            return result

        @server.tool()
        def revise_plan_bim(draft_id: str, expected_plan_sha256: str, operations_json: str,
                            reviews_json: str = "") -> CallToolResult:
            """Locally edit saved pixel wall/opening/seed declarations by ID.
            Read plan_partition reference for operations. Untouched declarations
            stay exact; compiler and source/overlay feedback run normally. This
            rebuilds ONE floor and may change room topology, never auto-fixes it.
            Optional reviews_json carries judgments of prior returned views; each
            plan/building entry names its candidate. See drawing_process.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews
            carry_reviews(toolkit, reviews_json)
            return candidate_result(toolkit.revise_plan(draft_id, expected_plan_sha256, operations_json))

        @server.tool()
        def view_plan_wall_support(draft_id: str, rgb: list[int], tolerance: float = 70,
                                   radius_pixels: int = 6, minimum_ink_pixels: int = 1):
            """Check complete partition paths from a saved build_plan_bim draft_NNN.
            Choose wall RGB and strip radius from the original. Returns clean/marked
            original, exact supported and unsupported intervals, declared apertures
            separately, and proposed adjacent rooms if compilation exists. Gaps are
            prompts to reobserve, not missing-wall verdicts. No BIM change or approval.
            Supports failed drafts too. Coordinates are original pixels, never GT.
            """
            return toolkit.plan_wall_support(draft_id, rgb, tolerance, radius_pixels, minimum_ink_pixels)

        def claim_result(row) -> CallToolResult:
            if row.get("observation_type") == "facade_count":
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(row))], structuredContent=row)
            content, previews = [], []
            for index in range(min(3, len(row["sources"]))):
                result = toolkit.view_claim_evidence(row["id"], index)
                content.extend(result.content)
                previews.append(result.structuredContent)
            reply = {**row, "evidence_previews": previews,
                     "unpreviewed_source_indices": list(range(3, len(row["sources"])))}
            # run94 claim_0001 applied a 3.6 m chain to 3 m storey windows on three facades
            # and two widths; show those facts beside the claim without judging them.
            try:
                from src.agent.execution.bim_claim_facts import claim_facts
                reply = {"facts": claim_facts(row, toolkit.claims().candidate(row["claim"]["candidate"])[0]), **reply}
            except (OSError, ValueError, KeyError, TypeError) as error:
                reply["facts"] = {"status": "unavailable", "reason": str(error)}
            content.append(TextContent(type="text", text=json.dumps(reply)))
            return CallToolResult(content=content, structuredContent=reply)

        @server.tool()
        def claim_transaction(entries_json: str, candidate: str = "") -> CallToolResult:
            """Batch record/adopt/confirm or apply evidence with per-entry audit.
            Entries commit independently; failed entries remain explicit. Read claims
            reference for claim/claim_id, action, reason and $claim operation bindings.
            Only unchanged targets inherit across candidates; retracted evidence cannot.
            Empty candidate is allowed only for facade_count records before building.
            """
            return candidate_result(toolkit.claim_transaction(candidate, entries_json))

        @server.tool()
        def record_claim(claim_json: str) -> CallToolResult:
            """Compatibility: record one claim/count. Models use claim_transaction."""
            return claim_result(toolkit.record_claim(claim_json))

        @server.tool()
        def replace_claim_sources(claim_id: str, view_ids: list[str], reason: str) -> CallToolResult:
            """Replace wrong claim regions using actual view_image IDs, without retyping values.
            Creates a new immutable claim on the SAME saved candidate, preserving objects,
            value definitions, targets, basis and unresolved items; replaces sources/reason.
            Retracts the old claim explicitly, returns new crops. New claim is unadopted:
            use claim_transaction to adopt and confirm/apply it. No BIM change.
            Full original views are valid; seeing a view does not certify interpretation.
            """
            return claim_result(toolkit.replace_claim_sources(claim_id, view_ids, reason))

        @server.tool()
        def view_claim_evidence(claim_id: str, source_index: int = 0,
                                display_scale: float = 1.0) -> CallToolResult:
            """Historical replay only; use view_image(claim_id=..., source_index=...)."""
            return toolkit.view_claim_evidence(claim_id, source_index, display_scale)

        @server.tool()
        def decide_claim(claim_id: str, disposition: Literal["adopted", "deferred", "retracted"], reason: str) -> dict:
            """Compatibility: decide one claim. Models use claim_transaction."""
            return toolkit.decide_claim(claim_id, disposition, reason)

        @server.tool()
        def claim_status(candidate: str | None = None) -> dict:
            """With candidate, project checks onto its actual ancestry and geometry.
            Without candidate, read complete run history. Historical application alone
            is not current applicability or drawing truth.
            """
            if candidate is not None:
                from src.agent.execution.bim_claim_state import project
                return project(toolkit.claims(), candidate)
            return toolkit.claims().status()

        @server.tool()
        def confirm_claims(candidate: str, operations_json: str) -> dict:
            """Compatibility: confirm bound values without geometry changes. Models use claim_transaction."""
            return toolkit.confirm_claims(candidate, operations_json)

        @server.tool()
        def check_wall_dimensions(candidate: str, references_json: str = "", dimensions_json: str = "",
                                  include_inventory: bool = False, floor_id: str | None = None,
                                  offset: int = 0, limit: int = 30, positions_json: str = "") -> dict:
            """List real wall hosts or convert explicit wall-face dimensions without changing geometry.
            See get_bim_reference("wall_dimensions") for the input format. Empty strings
            reuse saved evidence. Persist new evidence separately via revise_bim.
            Inventory is included only when no references exist or explicitly requested.
            Its wall list is paged (limit 1..50); floor_id narrows the inventory only,
            without changing explicit reference/dimension conversion. Read next_offset
            until null when you need the full selected inventory.
            positions_json checks explicit annotated chain endpoints on named walls;
            wall_placement reports deviations only, including partial shared walls.
            """
            from src.agent.geometry.wall_reference import resolve_wall_references, convert_wall_dimensions
            path = toolkit.candidate_path(candidate)
            source = json.loads((path / "source_model.json").read_bytes())
            proposal = json.loads((path / "proposal.json").read_bytes())
            references = json.loads(references_json) if references_json else proposal.get("wall_references", [])
            dimensions = json.loads(dimensions_json) if dimensions_json else proposal.get("wall_dimensions", [])
            for d in dimensions:
                for end in ("start", "end"):
                    endpoint = d[end]
                    x, y = endpoint["pixel"]
                    with PILImage.open(toolkit.image_path(endpoint["image"])) as pic:
                        if not (0 <= x < pic.width and 0 <= y < pic.height):
                            raise ValueError("dimension endpoint outside original image")
            walls = resolve_wall_references(source, references)
            result = {"candidate": candidate, "source_model_sha256": source["source_model_sha256"],
                      "dimension_report": convert_wall_dimensions(walls, dimensions), "walls": walls}
            from scripts.tool_scripts.bim_agent_precision import annotated_wall_placement
            result['wall_placement'] = annotated_wall_placement(toolkit, source, references, dimensions,
                positions=json.loads(positions_json) if positions_json else None)
            if include_inventory or not references:
                if not 1 <= limit <= 50 or offset < 0:
                    raise ValueError('inventory requires offset >= 0 and limit 1..50')
                if floor_id is not None and floor_id not in {f['id'] for f in source['floors']}:
                    raise ValueError('unknown floor_id')
                selected_spaces = {s['id'] for s in source['spaces']
                                   if floor_id is None or s['floor_id'] == floor_id}
                inventory = [{k: b[k] for k in ('id', 'space_id', 'vertices', 'counterpart_ids')}
                             for b in source['boundaries'] if b['geometry_type'] == 'wall'
                             and b['space_id'] in selected_spaces]
                result['boundary_inventory'] = inventory[offset:offset+limit]
                result['boundary_inventory_page'] = {'floor_id':floor_id, 'total':len(inventory),
                    'offset':offset, 'returned':len(result['boundary_inventory']),
                    'next_offset':offset+limit if offset+limit < len(inventory) else None}
            toolkit.log("check_wall_dimensions", result)
            return result

        @server.tool()
        def inspect_candidate(candidate: str = "seed", include_geometry: bool = True,
                              floor_id: str | None = None, include_plan: bool = False):
            """Read a saved candidate's proposal and production geometry checks.
            include_geometry=False returns notes/frame and a floor summary without
            the expanded rooms/apertures; useful for registration of large candidates.
            It also returns bounded current_checks from existing domain projections,
            with immutable report reads for issue IDs and scopes. Unknown, stale and
            unexamined scopes stay distinct; this is not a quality gate or checklist.
            current_checks always covers the whole candidate, even with floor_id.
            floor_id selects one floor. If geometry is still too large, the response
            keeps only the summary; read_candidate_items pages cells/windows/openings.
            A selected page is NOT a whole replacement proposal.
            No independent evaluation or reference answer is exposed.
            include_plan=true also renders the selected floor (floor_id required
            for several floors); this is a source projection, not original evidence.
            """
            path = toolkit.candidate_path(candidate)
            review_requested = include_geometry is False
            proposal = json.loads((path/"proposal.json").read_bytes())
            original_geometry = proposal['geometry']
            floors = [{'id': f['name'], 'z_floor': f['z_floor'], 'height': f['ceiling_height'],
                       'space_count': len(f['cells'])} for f in proposal['geometry']['floors']]
            if floor_id is not None:
                selected = [f for f in original_geometry['floors'] if f['name'] == floor_id]
                if not selected:
                    raise ValueError('unknown floor_id')
                ids = {cell['id'] for f in selected for cell in f['cells']}
                proposal['geometry'] = {**original_geometry, 'floors':selected,
                    'windows':[w for w in original_geometry.get('windows',[]) if w['floor'] == floor_id],
                    'openings':[o for o in original_geometry.get('openings',[])
                                if o['space_id'] in ids or o.get('other_space_id') in ids]}
            summary_due_to_size = include_geometry and len(json.dumps(proposal)) > 22000
            if summary_due_to_size:
                include_geometry = False
            if not include_geometry:
                proposal = {key: value for key, value in proposal.items() if key != 'geometry'}
            report = json.loads((path/"report.json").read_bytes())
            from src.agent.roles import room_use_review
            source_path = path / "source_model.json"
            result = {"candidate": candidate, "proposal": proposal, "floors": floors,
                      "room_use_review": room_use_review(json.loads(source_path.read_bytes()))
                          if source_path.exists() else None,
                      "geometry_included": include_geometry,
                      'floor_filter':floor_id, 'summary_due_to_size':summary_due_to_size,
                      'geometry_read_hint':'Use floor_id or read_candidate_items for bounded reads; partial results must not replace the full proposal.',
                      "wall_dimension_report": report.get("wall_dimension_report"),
                      "source_validation": report.get("source_validation"),
                      "counts": report.get("counts"),
                      "remaining_seconds": toolkit.remaining_seconds()}
            if source_path.exists():
                from scripts.tool_scripts.bim_agent_precision import annotated_wall_placement
                result['wall_placement'] = annotated_wall_placement(toolkit, json.loads(source_path.read_bytes()),
                    proposal.get('wall_references', []), proposal.get('wall_dimensions', []))
            if review_requested:
                from scripts.tool_scripts.bim_agent_saved_result import candidate_review, candidate_review_summary
                from scripts.tool_scripts.bim_agent_replies import compact_reply, report_reference
                review = (candidate_review(toolkit, candidate, json.loads(source_path.read_bytes()), report)
                          if source_path.exists() else {"candidate": candidate, "status": "source_not_available"})
                full = {**result, "current_checks": review}
                # Bind every detail read to this immutable inspection, not to a
                # mutable candidate-side cache or whichever draft becomes latest.
                raw = (json.dumps(full, ensure_ascii=False, indent=2) + '\n').encode()
                sha = hashlib.sha256(raw).hexdigest()
                relative = f'tool_reports/inspect_candidate_{sha}.json'
                small = candidate_review_summary(review) if source_path.exists() else dict(review)
                small['details'] = report_reference(relative, sha, '/current_checks', review)
                result = compact_reply(run, 'inspect_candidate', {**result, 'current_checks': small},
                                       full_result=full, force=True)
                # Generic field compaction can replace a summary with a view of
                # the full report. Retain these exact totals even when other
                # fields (e.g. author notes) force the whole reply to be bounded.
                result['current_checks'] = small
                if len(json.dumps(result, ensure_ascii=False).encode()) > 24000:
                    result = {key: result[key] for key in ('candidate', 'geometry_included',
                        'floor_filter', 'current_checks', 'details_file', 'details_sha256', 'details_read')
                        if key in result}
            toolkit.log("inspect_candidate", {"candidate": candidate, 'include_geometry': include_geometry})
            if include_plan:
                if floor_id is None and len(floors) != 1:
                    raise ValueError("include_plan requires floor_id for a multi-floor candidate")
                picture, metadata = toolkit.plan_view(candidate, floor_id or floors[0]['id'])
                result['source_plan_view'] = metadata
                return CallToolResult(content=[picture.to_image_content(),
                    TextContent(type="text", text=json.dumps(result, ensure_ascii=False))], structuredContent=result)
            return result

        @server.tool()
        def check_source_space_relation(candidate: str, image: ImageFilename, floor_id: str,
                                        observations_json: str) -> dict:
            """Compare original-plan observations with actual source space ownership.
            observations_json is a list of {id, points:[[original_px_x,original_px_y],
            [original_px_x,original_px_y]], expected:"same_space"|"separate_spaces"|
            "uncertain", evidence:"what the original shows"}. Pick points well inside
            the observed spaces. Uses the latest registered image/floor calibration;
            first build_plan_bim or overlay_candidate if none exists. Returns actual
            space IDs and any direct door/open connection; connected does NOT mean the
            same space. Saves a source/calibration-bound review; a revised candidate
            needs its own checks. Never edits geometry or certifies drawing truth.
            """
            return toolkit.check_space_relations(candidate, image, floor_id, observations_json)

        @server.tool()
        def check_openings(candidate: str, review_json: str = "", heights_only: bool = False) -> dict:
            """List actual openings, or check original-image marks against them.
            Use heights_only=true for actual z and current height-observation coverage.
            See get_bim_reference("opening_review") for review_json. Saves a source-hash-bound
            review independently; never modifies the BIM or certifies image truth.
            """
            from src.agent.geometry.opening_review import facade_inventory, opening_inventory, review_openings
            path = toolkit.candidate_path(candidate)
            source = json.loads((path / "source_model.json").read_bytes())
            if heights_only:
                if review_json:
                    raise ValueError('heights_only cannot submit an opening review')
                result = {"candidate": candidate}
            elif not review_json:
                result = {"candidate": candidate, "inventory": opening_inventory(source),
                          "facade_inventory": facade_inventory(source),
                          "drawing_fidelity": "not_evaluated"}
            else:
                observations = json.loads(review_json)
                toolkit.image_path(observations["image"])
                calibration = next((row for _, row in toolkit.registered_calibrations()
                    if (row["image"], row["floor_id"]) == (observations["image"], observations["floor_id"])), None)
                report = review_openings(source, observations, toolkit.manifest["images"],
                                        plan_calibration=calibration)
                folder = run / "opening_reviews"
                folder.mkdir(exist_ok=True)
                target = folder / f"review_{len(list(folder.glob('review_*.json'))) + 1:03d}.json"
                result = {"candidate": candidate, "review_file": target.relative_to(run).as_posix(), **report}
                dump(target, {**result, "observations": observations})
            result["remaining_seconds"] = toolkit.remaining_seconds()
            result["input_view_status"] = toolkit.input_view_status()
            result["height_coverage"] = toolkit.located_heights(candidate)
            result["facade_counts"] = toolkit.facade_counts(candidate)
            toolkit.log("check_openings", result)
            from scripts.tool_scripts.bim_agent_replies import compact_reply
            return compact_reply(toolkit.run, "check_openings", result)

        @server.tool()
        def record_work_review(candidate: str, decision: Literal["continue", "stop"], reason: str,
                               next_action: str = "") -> dict:
            """During a continuation turn, choose continue or stop against saved work.
            Explain scope/evidence; continue requires a concrete next_action to execute
            THIS turn. A stop is a model judgment, not proof of task or image fidelity.
            """
            from scripts.tool_scripts.bim_agent_continuation import record_work_review as record
            return record(toolkit, candidate, decision, reason, next_action)

        @server.tool()
        def finish_bim(candidate: str, reviews_json: str = "") -> dict:
            """Select a saved BIM and persist a handoff based on actual checks.
            Unreviewed/pending scopes remain explicit. This does not certify image
            fidelity. Whether it ends work is determined by the running entrypoint:
            the single-model runtime ends after a successful complete handoff;
            other MCP clients may continue and select another candidate.
            Carry the final architectural judgment in reviews_json after inspecting
            this saved candidate. Complete intended edits before this call.
            For native mesh, reviews_json may be {"mesh_review":{"coverage":
            "whole_building" or "partial","summary":"what was checked and remains
            uncertain","overlay_ids":["overlay_001"]}}. Cite current source overlays
            you inspected; no prescribed camera count. This is model-reported scope,
            not independent quality approval. Missing/partial/stale mesh scope does
            not complete the runtime even when a useful candidate is saved.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews, require_process_reviews
            from scripts.tool_scripts.bim_agent_mesh_review import carry_mesh_review
            if carry_mesh_review(toolkit, candidate, reviews_json) is None:
                carry_reviews(toolkit, reviews_json, candidate)
            require_process_reviews(toolkit, candidate)
            result = toolkit.delivery(candidate, selection_origin="agent_selected",
                generation_status={"state": "in_progress", "handoff_saved": True,
                                   "agent_response_completed": False})
            dump(run / "delivery_selection.json", {
                "candidate": candidate, "source_model_sha256": result["source_model_sha256"]})
            toolkit.log("finish_bim", result)
            # Bind the successful handoff to the files actually saved. A runtime
            # may attest completion from this small record without re-rendering
            # or mistaking a later selection/source/viewer for the delivered one.
            artifacts = {name: digest(run / name) for name in (
                "inputs.json", "delivery_selection.json", "delivery.json", "delivery.html",
                f"{candidate}/source_model.json", f"{candidate}/viewer.html")}
            mesh_review_path = run / candidate / 'mesh_review.json'
            if toolkit.manifest.get('mesh_input') and mesh_review_path.is_file():
                artifacts[f'{candidate}/mesh_review.json'] = digest(mesh_review_path)
            from scripts.tool_scripts.bim_agent_replies import compact_reply
            return compact_reply(run, "finish_bim", {
                **delivery_tool_reply(result), "delivery_artifact_sha256": artifacts,
                "remaining_seconds": toolkit.remaining_seconds()},
                full_result={**result, "delivery_artifact_sha256": artifacts})

        @server.tool()
        def overlay_candidate(candidate: str, image: ImageFilename, floor_id: str,
                              x_anchors: list[list[float]], y_anchors: list[list[float]],
                              basis: str, box: list[int] | None = None,
                              reuse_on_revision: bool = True):
            """Project actual source geometry onto an AXIS-ALIGNED original plan.
            Each axis needs two [ORIGINAL pixel position, world metres] anchors,
            like map_pixels; basis explains the observed dimension and wall reference.
            No GT, auto-registration, perspective correction or visual verdict.
            Optional box crops the result in ORIGINAL pixels. Colours: magenta
            source boundaries, orange doors/passages, lime windows.
            Wall/evidence labels compare saved segment extents with original
            dimension pixels on this exact image. Distances depend on your
            calibration; extension-line ticks may lie outside a valid host.
            By default, this explicit caller-supplied calibration is saved and only
            reused for the same image/floor after later build_bim or revise_bim.
            """
            image_content, metadata = toolkit.project_overlay(
                candidate, image, floor_id, x_anchors, y_anchors, basis,
                trigger_action="overlay_candidate", box=box)
            if reuse_on_revision:
                calibration_path, calibration = toolkit._save_calibration(
                    candidate=candidate, image=image, floor_id=floor_id,
                    x_anchors=metadata["anchors"]["x"], y_anchors=metadata["anchors"]["y"],
                    basis=basis, metadata=metadata)
                metadata["registered_calibration"] = {
                    "calibration_id": calibration["calibration_id"],
                    "calibration_file": calibration_path.relative_to(run).as_posix(),
                    "reuse_on_revision": True,
                }
                # This sidecar was created during this same explicit request;
                # earlier projection files are never changed or replaced.
                dump(run / metadata["overlay_image"].replace(".png", ".json"), metadata)
            else:
                metadata["registered_calibration"] = {"reuse_on_revision": False}
            toolkit.log("overlay_candidate", metadata)
            return [image_content, json.dumps(metadata)]

        @server.tool()
        def revise_bim(candidate: str, operations_json: str, reviews_json: str = "") -> CallToolResult:
            """Apply local edits/reflection with code and save a new checked BIM.
            See get_bim_reference("edits") for operations whose format the system prompt
            does not give. Prior candidates stay unchanged.
            Opening changes/removals and shared-wall moves require a reason and source_refs.
            set_space_role assigns catalog use and its evidence, preserving geometry.
            Optional reviews_json describes already inspected views of candidate,
            not this action's new result. See drawing_process.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews
            carry_reviews(toolkit, reviews_json, candidate)
            return candidate_result(toolkit.revise(candidate, operations_json))

        @server.tool()
        def review_detail(question: str, images: list[str], timeout_seconds: float = 120) -> dict:
            """Ask the local image model (Haiku) one small visual question in a region.
            Give image names and original crop coordinates, and describe observable
            original-image evidence rather than a candidate conclusion. The submitted
            question is not text-cleaned, so this only isolates file context. At most
            two local reviews are available in this experiment. Choose 15–240
            seconds (default 120), capped by the parent's remaining time with a
            completion reserve. Keep the task small enough to return within it.
            """
            response = review_detail_observation(toolkit, question, images, timeout_seconds=timeout_seconds)
            toolkit.log("review_detail", {"question": question, "images": images,
                                          "timeout_seconds_requested": timeout_seconds,
                                          "response": response})
            return response

        @server.tool()
        def build_plan_bim(image: ImageFilename, plan_json: str, reviews_json: str = "") -> CallToolResult:
            """Build one floor from observed original-pixel walls, openings and calibration.
            Read plan_partition for JSON. Orthogonal, possibly concave outer footprint
            without holes. New drawing runs apply their manifest's 0.1 m Lite BIM grid
            automatically, preserving rooms, opening hosts and connections. Historical
            manifests retain their policy. Remaining hard-rule violations reject the save.
            Returns actual source/overlay images, drawing_differences, IDs/heights and
            details_file (read_candidate_items collection=report). Declare omissions in
            unresolved. Each export consumes the shared candidate budget.
            Optional reviews_json carries judgments of prior results; plan/building
            entries must name their candidate. See drawing_process.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews
            carry_reviews(toolkit, reviews_json)
            return candidate_result(toolkit.build_plan(image, plan_json))

        @server.tool()
        def assemble_plan_bim(floors_json: str, reviews_json: str = "") -> CallToolResult:
            """Assemble 2–32 saved pixel plans without rewriting their rooms or openings.
            Read get_bim_reference('plan_assembly'). floors_json is a JSON list; each item contains draft_id,
            expected_plan_sha256, floor_id, z_floor and evidence. Recompiles bound drafts,
            namespaces IDs and translates all opening z values by the floor-base change.
            The manifest's Lite grid applies to all floors; target z_floor must be on grid.
            Near wall planes and stacked faces are regularized by editing drafts and recompiling.
            No height scaling, floor copying or inferred vertical connection.
            For layer height or aperture changes first revise that draft explicitly.
            Returns actual source views/overlays for every included floor.
            Carry local judgments of the saved floor candidates in reviews_json;
            each plan entry names its candidate. See drawing_process.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews
            carry_reviews(toolkit, reviews_json)
            return candidate_result(toolkit.assemble_plans(floors_json))

        @server.tool()
        def build_parametric_bim(plan_json: str) -> CallToolResult:
            """Expand model-declared floor templates and window rows, then build source BIM.
            Read get_bim_reference('parametric') first. Never infers or trims geometry.
            Relative aperture heights are offset by each explicit instance base.
            """
            from src.agent.geometry.parametric_proposal import expand_parametric_proposal
            folder = toolkit.run / 'parametric_drafts'
            folder.mkdir(exist_ok=True)
            draft = folder / f'draft_{len(list(folder.glob("*.json")))+1:03d}.json'
            draft.write_text(plan_json, encoding="utf-8", newline="\n")
            try:
                plan = json.loads(plan_json)
                proposal = expand_parametric_proposal(plan)
            except (ValueError, TypeError, KeyError) as error:
                result = {'error': str(error), 'draft': draft.relative_to(toolkit.run).as_posix(),
                          'remaining_seconds': toolkit.remaining_seconds()}
                toolkit.log('build_parametric_bim', result)
                return candidate_result(result)
            result = toolkit.build(proposal, action='build_parametric_bim')
            if result.get('candidate'):
                dump(toolkit.run / result['candidate'] / 'parametric_plan.json', plan)
            # Full inventories and every plan remain on disk/available on demand.
            # Return representative template plans without repeating identical floors.
            result.pop('opening_inventory', None)
            representatives = {}
            for instance in plan['instances']:
                representatives.setdefault(instance['template'], instance['id'])
            result['source_plan_views'] = [v for v in result.get('source_plan_views', [])
                                         if v['floor_id'] in representatives.values()]
            return candidate_result(result)

        @server.tool()
        def inspect_parametric_plan(candidate: str) -> dict:
            """Read the exact saved compact plan for revision; original evidence is separate."""
            value = json.loads((toolkit.candidate_path(candidate) / 'parametric_plan.json').read_bytes())
            toolkit.log('inspect_parametric_plan', {'candidate': candidate})
            return value

        @server.tool()
        def build_bim(proposal_json: str) -> CallToolResult:
            """Build/check/save a candidate; get_bim_reference("geometry") describes proposal JSON.
            Returns errors or actual geometry checks. All exports share the quota
            reported by inputs.candidate_budget, including floor builds and revisions.
            """
            return candidate_result(toolkit.build(json.loads(proposal_json)))

        @server.tool()
        def prepare_elevation_reading(image: ImageFilename, facade: Literal["North", "South", "East", "West"],
                                      reading_json: str, reviews_json: str = "", replaces_reading_id: str = "") -> CallToolResult:
            """Save an independent facade reading and return its adopted-value overlay.
            Read get_bim_reference('drawing_process') for the compact schema. Supply
            original-pixel calibration, located levels and every opening; counts
            can be derived. Equivalent anchor pairs/list order are normalized.
            Compare the returned original/overlay, revise by preparing a new reading,
            then carry its judgment on the next normal action's reviews_json.
            reviews_json here refers to prior results only. No BIM is changed.
            If correcting the assigned facade of a previous reading on this same
            image, name replaces_reading_id; other facades on a shared sheet remain.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import carry_reviews
            carry_reviews(toolkit, reviews_json)
            from scripts.tool_scripts.bim_agent_drawing_readings import prepare_elevation_reading as prepare
            pictures, metadata = prepare(toolkit, image, facade, reading_json,
                                         replaces_reading_id=replaces_reading_id)
            contents = []
            for picture in pictures:
                original_size = list(picture.size)
                picture = picture.copy()
                picture.thumbnail((1600, 1600))
                metadata.update(original_size=original_size, returned_size=list(picture.size),
                    original_pixels_per_returned_pixel=[original_size[0] / picture.width, original_size[1] / picture.height])
                stream = io.BytesIO()
                picture.save(stream, "PNG")
                contents.append(Image(data=stream.getvalue(), format="png").to_image_content())
            return CallToolResult(content=[*contents, TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False))],
                                  structuredContent=metadata)

        @server.tool()
        def apply_elevation_readings(candidate: str, reading_ids: list[str], reviews_json: str = "") -> CallToolResult:
            """Match locally reviewed elevation readings and apply safe heights in one batch.
            Reuses the existing conservative matcher and claim_transaction. Does not
            change floor heights, positions, hosts or counts. Resolve floor levels in
            the plan first. Missing/extra/ambiguous objects remain located conflicts;
            inspect_candidate(include_geometry=false) reports their current state.
            Carry the inspected reading judgments in reviews_json in this same
            call; they bind reading_id. No separate review/count/claim call needed.
            """
            from scripts.tool_scripts.bim_agent_process_reviews import apply_readings, carry_reviews
            carry_reviews(toolkit, reviews_json, candidate)
            return candidate_result(apply_readings(toolkit, candidate, reading_ids))

        @server.tool()
        def view_elevation_candidate(candidate: str, facade: Literal["North", "South", "East", "West"], image: ImageFilename = "",
                                     horizontal_anchors: list[list[float]] | None = None,
                                     z_anchors: list[list[float]] | None = None,
                                     basis: str = "") -> CallToolResult:
            """Inspect actual source wall/window/door heights across all floors.
            facade: North, South, East or West. Set image to an exact original filename
            to receive its complete clean image alongside the source elevation and an
            ID/height table (absolute z and height above each floor). You choose the
            correspondence; no image-name inference, alignment or fidelity pass.
            The returned original view_id can be cited in claims. Omit image for source only.
            Optional calibrated overlay: provide image, horizontal_anchors and z_anchors,
            each two [original_pixel, world_metres] pairs, plus basis describing observed
            references. Horizontal metres are world x for North/South, world y for East/West
            (NOT distance from the image's left edge); z is absolute source height, not
            height above a floor. For axis-aligned drawings only. Returns clean original
            and source overlay in the same frame. Calibration is unverified; keep these
            original references unchanged when repeating after revision. No auto-fit/pass.
            """
            pictures, metadata = toolkit.elevation_view(candidate, facade, image,
                                                        horizontal_anchors, z_anchors, basis)
            return CallToolResult(content=[
                *[picture.to_image_content() for picture in pictures],
                TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
            ], structuredContent=metadata)

        @server.tool()
        def view_candidate(candidate: str, floor_id: str) -> Image:
            """Historical replay only; use inspect_candidate(floor_id=..., include_plan=true)."""
            image, metadata = toolkit.plan_view(candidate, floor_id)
            toolkit.log("view_candidate", metadata)
            return image

    if enabled_only:
        # The unfiltered service remains available for immutable version checks.
        capabilities = tool_capabilities(toolkit.manifest)
        for name in tuple(server._tool_manager._tools):
            if not filter_tool_catalog([{"name": name}], **capabilities):
                server.remove_tool(name)
    return server


def serve(run: Path, readonly=False, *, enabled_only=False):
    if os.name == "nt":
        # SciPy's Fortran DLL initialization can wait on CRT stdio locks.
        # Import before MCP starts its blocking stdin reader, not during the
        # first geometry call while that reader already owns a stdio lock.
        import scipy.signal  # noqa: F401
    server = build_tool_server(run, readonly=readonly, enabled_only=enabled_only)
    server.run()


def run_experiment(args):
    if getattr(args, "timeout", None) is None:
        args.timeout = 6000 if getattr(args, "provider", "claude") == "glm" else 900
    if isinstance(args.timeout, bool) or not isinstance(args.timeout, (int, float)) or not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be positive finite seconds")
    max_candidates = getattr(args, "max_candidates", 24)
    if type(max_candidates) is not int or max_candidates <= 0:
        raise ValueError("max_candidates must be a positive integer")
    provider = getattr(args, "provider", "claude")
    continuation_rounds = getattr(args, "continuation_rounds", 0)
    if type(continuation_rounds) is not int or not 0 <= continuation_rounds <= 4:
        raise ValueError("continuation_rounds must be an integer from 0 to 4")
    if provider == "glm" and getattr(args, "exploratory_opus", False):
        raise ValueError("--provider glm cannot be combined with --exploratory-opus")
    main_model, workers = getattr(args, "main_model", None), getattr(args, "workers", None)
    if (main_model or workers) and (provider != "claude" or getattr(args, "exploratory_opus", False)):
        raise ValueError("--main-model and --workers apply only to the Claude Sonnet route")
    from src.agent.bim_inputs import prepare_bim_inputs
    run = args.out.resolve()
    seed_path = getattr(args, "resume_candidate", None)
    plan_image = getattr(args, "plan_image", None)
    manifest = prepare_bim_inputs(run, images_path=args.images,
        mesh_path=getattr(args, "mesh", None), building_input_path=getattr(args, "building_input", None),
        scope=args.scope, image_kind=getattr(args, "image_kind", None), max_candidates=max_candidates,
        floor_images=getattr(args, "floor_plan_images", None),
        started_epoch=None if getattr(args, "prepare_only", False) else time.time(), seconds=args.timeout,
        provider=provider, continuation_rounds=continuation_rounds,
        review_detail_enabled=getattr(args, "review_detail", False),
        exploratory_opus=getattr(args, "exploratory_opus", False), seed_path=seed_path,
        resume_plan_path=getattr(args, "resume_plan", None), plan_image=plan_image)
    building_input, plan_recovery, mesh_input = (manifest.get(name) for name in
        ("building_input", "plan_recovery", "mesh_input"))
    continuation = ("The previous pixel-plan declaration is available at inputs.plan_recovery.declaration "
                    f"and is bound to original image {plan_image}. It is unverified and may fail compilation; "
                    "importing it does not establish a valid source BIM. Follow this run's scope: you may "
                    "first submit it unchanged to reproduce the failure and inspect feedback, or revise "
                    "it using evidence from the supplied original."
                    if plan_recovery else
                    "A saved proposal is available as seed. Compare its actual spatial partitions, "
                    "openings and connectivity with the original images. Choose substantive "
                    "discrepancies for local review or revision, while preserving reliable geometry; "
                    "do not redo a full reading." if seed_path else
                    "Observe the exterior evidence, state architectural hypotheses for missing parts "
                    "and interiors, and build at the requested space detail. Inspect the actual saved "
                    "source against the input and repair substantive discrepancies." if mesh_input else
                    "No saved proposal is supplied; work from the original inputs.")
    declaration_prompt = (
        " A structured user building declaration is available from inputs under "
        "building_input.declaration. Preserve each field's stated meaning. In particular, "
        "thermal_zones describes downstream simulation zoning and is not the physical source-room "
        "count. Compare declaration claims with the supplied drawings and report conflicts or "
        "uncertainty explicitly."
        if building_input else
        " No structured building declaration was supplied for this experiment."
    )
    prompt = (f"Scope: {args.scope}\nBudget: {args.timeout} seconds. "
              f"Start by listing supplied inputs.{declaration_prompt} {continuation} "
              "Report limitations honestly, and finish within the budget.")
    if getattr(args, "prepare_only", False):
        (run / "task.txt").write_text(prompt, encoding="utf-8")
        (run / "guide.txt").write_text(run_guide(run), encoding="utf-8")
        prepared = {"status": "prepared_not_run", "run": str(run),
                    "model_calls": 0, "deadline_epoch": None,
                    "note": "Frozen inputs and common instructions only. An external controller must "
                            "record its model, start/deadline and receipt before claiming an experiment."}
        dump(run / "preparation.json", prepared)
        print(json.dumps(prepared, ensure_ascii=False, indent=2))
        return prepared
    record = subscription(run, prompt,
                          model="opus" if getattr(args, "exploratory_opus", False) else "sonnet",
                          name="agent", timeout=args.timeout,
                          effort=getattr(args, "effort", None),
                          exploratory_opus=getattr(args, "exploratory_opus", False),
                          main_model=main_model, workers=workers)
    from scripts.tool_scripts.bim_agent_continuation import run_continuations, response_completed as completed
    def invoke_followup(prompt, *, name, timeout):
        return subscription(run, prompt,
            model="opus" if getattr(args, "exploratory_opus", False) else "sonnet",
            name=name, timeout=timeout, effort=getattr(args, "effort", None),
            exploratory_opus=getattr(args, "exploratory_opus", False),
            main_model=main_model, workers=workers,
            receipt_context={"role": "main_agent_continuation", "continuation_turn": name})
    records, continuation_status = run_continuations(Toolkit(run), record,
        max_rounds=continuation_rounds, invoke=invoke_followup, compact=delivery_tool_reply)
    record = records[-1]
    total_elapsed = round(sum(row["elapsed_seconds"] for row in records), 2)
    candidates = []
    for path in sorted(run.glob("candidate_*/report.json")):
        try:
            report = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            # A hard stop may leave the newest export unfinished. Preserve the
            # failure and continue to the last readable saved building.
            report = {"status": "unreadable_after_interruption", "error": str(error)}
        candidates.append({"candidate":path.parent.name,"status":report.get("status"),
                           "source_geometry_ready":report.get("source_geometry_ready"),
                           "viewer_exists":(path.parent/"viewer.html").is_file(),
                           "counts":report.get("counts"),
                           **({"error": report["error"]} if report.get("error") else {})})
    selection = run / "delivery_selection.json"
    delivery = None
    response_completed = completed(record)
    generation_status = {"state":"completed" if response_completed else "interrupted",
                         "agent_response_completed":response_completed,
                         "elapsed_seconds":total_elapsed,
                         "continuation": continuation_status,
                         "returncode":record.get("returncode"),
                         "timed_out":record.get("timed_out", False)}
    if record.get("result", {}).get("is_error"):
        generation_status["error"] = str(record["result"].get("result", "Model invocation failed"))[:1000]
    if record.get("routing_error"):
        generation_status["error"] = record["routing_error"]
    if selection.exists() and not record.get("timed_out"):
        chosen = json.loads(selection.read_bytes())["candidate"]
        delivery = Toolkit(run).delivery(chosen, selection_origin="agent_selected", generation_status=generation_status)
    else:
        from scripts.tool_scripts.bim_agent_budget import fallback_selection
        chosen, origin = fallback_selection(Toolkit(run))
        if chosen:
            delivery = Toolkit(run).delivery(chosen, selection_origin=origin, generation_status=generation_status)
    receipts, cost_summary = cost_receipt_summary(run)
    summary = {"input_mode":manifest["input_mode"],
               "source_input_mode": manifest["source_input_mode"],
               "input_contents": manifest["input_contents"],
               "building_input_sha256": (building_input["raw_sha256"] if building_input else None),
               "candidate_results":candidates,
               "agent_response_completed":response_completed,
               "has_viewable_candidate":any(c["viewer_exists"] for c in candidates) or bool(delivery and delivery["viewer_exists"]),
               "elapsed_seconds":total_elapsed,"drawing_fidelity":"not_evaluated",
               "continuation": continuation_status,
               "opening_reviews":[p.relative_to(run).as_posix() for p in sorted((run/"opening_reviews").glob("review_*.json"))],
               "delivery": {"candidate":delivery["candidate"], "selection_origin":delivery["selection_origin"],
                            "report":"delivery.json", "viewer":"delivery.html"} if delivery else None,
               "subscription_invocations":len(receipts),
               **cost_summary,
               "not_evaluated":["independent GT comparison","human approval","EnergyPlus"],
               "estimated_cost_note":"CLI estimates are not subscription bills"}
    dump(run/"summary.json",summary)
    from src.agent.runtime_behaviour import write_behaviour_report
    write_behaviour_report(run, run / "behaviour")
    print(json.dumps(summary,ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    run=commands.add_parser("run")
    run.add_argument("--images",type=Path,help="Optional original PNG image directory")
    run.add_argument("--mesh",type=Path,help="Original self-contained GLB; observed on demand by the agent")
    run.add_argument("--image-kind", choices=("drawings", "mesh_views", "photos", "unknown"),
                     help="What the PNG images are; default drawings without --mesh, unknown with it")
    run.add_argument("--building-input", type=Path,
                     help="Explicit user building declaration JSON; omitted runs remain PNG-only")
    run.add_argument("--out",type=Path,required=True)
    run.add_argument("--scope",default="Reconstruct the building shown in all supplied drawings.")
    run.add_argument("--timeout",type=int,default=None, help="Seconds; default 6000 for GLM, 900 for Claude")
    run.add_argument("--floor-plan-image", dest="floor_plan_images", action="append",
                     help="Explicit expected floor-plan filename (repeat for all floors); otherwise numeric F filenames are shown as hints")
    run.add_argument("--prepare-only", action="store_true",
                     help="Freeze admitted inputs and instructions without a model call or ticking deadline")
    run.add_argument("--max-candidates", type=int, default=24,
                     help="Shared export quota for floor builds, assembly and revisions (default: 24)")
    run.add_argument("--continuation-rounds", type=int, choices=range(5), default=0,
                     help="Experimental bounded main-agent follow-ups within the SAME total deadline")
    run.add_argument("--review-detail", action="store_true",
                     help="Enable the bounded local image-model review tool")
    run.add_argument("--provider", choices=("claude", "glm"), default="claude",
                     help="Subscription route; glm uses glm-5.3-flash for main and local image tasks")
    run.add_argument("--exploratory-opus", action="store_true",
                     help="Explicit task-authorized exploratory Opus subscription run; default remains Sonnet")
    run.add_argument("--effort", choices=("low", "medium"), default="medium",
                     help="Sonnet reasoning effort for this run; local Haiku configuration is unchanged")
    run.add_argument("--main-model", choices=MAIN_MODELS,
                     help="Claude main model; default stays claude-sonnet-5")
    run.add_argument("--workers", choices=tuple(WORKER_MODELS),
                     help="Let the Claude main model delegate sub-tasks to one worker subagent on this model")
    recovery = run.add_mutually_exclusive_group()
    recovery.add_argument("--resume-candidate",type=Path,
                          help="Recover from a saved proposal directory, not an independent cold start")
    recovery.add_argument("--resume-plan",type=Path,
                          help="Resume from one unverified pixel-plan JSON; does not auto-build a BIM")
    run.add_argument("--plan-image", help="Exact supplied PNG filename associated with --resume-plan")
    server=commands.add_parser("serve")
    server.add_argument("run",type=Path)
    server.add_argument("--readonly",action="store_true")
    server.add_argument("--enabled-only", action="store_true",
                        help="Expose only capabilities enabled in the run manifest")
    args=parser.parse_args()
    if args.command=="serve": serve(args.run.resolve(),args.readonly, enabled_only=args.enabled_only)
    else: run_experiment(args)


if __name__=="__main__": main()
