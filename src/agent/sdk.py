"""Python access to the existing BIM tools, with the same admission and handoff.

This module supplies no model, planner, evaluator or code sandbox. The caller's
Python runtime owns execution and filesystem permissions. Programs can compose
ordinary data/loops and reuse the domain tools without spelling JSON strings or
reimplementing geometry. Tool images are saved losslessly, not printed as base64.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any
import uuid

from src.agent.sdk_presentation import _display_data


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def edge_segment(p1, p2, *, offset: float, width: float) -> dict:
    """Return aperture p1/p2 at explicit distances along an explicitly chosen edge.

    No nearest-wall search, rounding, clipping or host inference occurs. Reuse
    the exact polygon endpoints; the normal BIM compiler still validates hosts.
    """
    def numbers(value, size):
        if not isinstance(value, (list, tuple)) or len(value) != size:
            raise ValueError(f"expected {size} finite numbers")
        if any(isinstance(x, bool) or not isinstance(x, (int, float))
               or not math.isfinite(x) for x in value):
            raise ValueError("expected finite numbers")
        return [float(x) for x in value]

    a, b = numbers(p1, 2), numbers(p2, 2)
    offset, width = numbers([offset, width], 2)
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if not math.isfinite(length) or length == 0 or offset < 0 or width <= 0 or offset + width > length:
        raise ValueError("segment must fit a positive explicit edge without clipping")

    def at(distance):
        if distance == 0:
            return a[:]
        if distance == length:
            return b[:]
        ratio = distance / length
        return [a[0] + ratio * dx, a[1] + ratio * dy]

    return {"p1": at(offset), "p2": at(offset + width)}


@dataclass(frozen=True)
class ToolReply:
    """A tool receipt; success is not architectural acceptance or image inspection."""
    data: Any
    texts: list[str]
    images: list[dict]
    is_error: bool
    receipt_file: str
    tool_name: str = ""

    def __str__(self):
        return _json(self.presentation())

    def presentation(self):
        """Display-only JSON; data/texts/images and the saved receipt stay full."""
        texts = []
        for text in self.texts:
            try:
                duplicate = json.loads(text) == self.data
            except (ValueError, TypeError):
                duplicate = False
            if not duplicate:
                texts.append("\n".join(
                    line.replace("保守剩余（本运行）：",
                        "硬执行剩余额度（本运行；任务目标可能更短，以任务为准）：", 1)
                    if line.startswith("保守剩余（本运行）：") else line
                    for line in text.split("\n")))
        return {"data": _display_data(self.data, self.tool_name, self.receipt_file, is_error=self.is_error),
                "texts": texts, "images": copy.deepcopy(self.images), "is_error": self.is_error,
                "receipt_file": self.receipt_file}

    def require_ok(self):
        """Raise for tool failure only, never treat a saved candidate as accepted."""
        if self.is_error:
            raise RuntimeError(str(self))
        return self.data


class BimSession:
    """Synchronous script interface to one admitted run; use with BimSession(run).

    One writer owns a run. Call sequentially; independent runs can run in
    parallel. Existing tools enforce readonly mode, input provenance, candidate
    budgets and source/handoff checks. This library never starts a model.
    """
    def __init__(self, run: str | Path, *, readonly: bool = False):
        self.run = Path(run).resolve()
        if not (self.run / "inputs.json").is_file():
            raise ValueError("run needs an admitted inputs.json; use prepare_bim_inputs first")
        from scripts.tool_scripts.run_bim_agent import build_tool_server

        self._runner = asyncio.Runner()
        self._closed = False
        self._server = build_tool_server(self.run, readonly=readonly, enabled_only=True)
        self._tools = None
        self._receipts = self.run / "sdk" / "receipts"
        self._images = self.run / "sdk" / "images"
        for path in (self._receipts, self._images):
            if not path.resolve().is_relative_to(self.run):
                raise ValueError("SDK artifacts must stay within this run")
            path.mkdir(parents=True, exist_ok=True)

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._runner.close()

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("BimSession is closed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError("BimSession is a synchronous script API; call outside an active asyncio loop")

    def _definitions(self):
        self._ensure_open()
        if self._tools is None:
            self._tools = {tool.name: tool.model_dump(mode="json", exclude_none=True)
                           for tool in self._runner.run(self._server.list_tools())
                           if tool.name != "review_detail"}
        return self._tools

    def catalog(self):
        """Short discovery list. describe(name) returns its exact current schema."""
        return [{"name": name, "description": tool.get("description", "").splitlines()[0]}
                for name, tool in self._definitions().items()]

    def check_plan(self, plan: dict):
        """Optional kernel check with .source/.report, no candidate or rendering.

        Useful inside ordinary construction code before the intended save;
        it is not a mandatory extra step or a substitute for building/review.
        """
        self._ensure_open()
        from src.agent.construction_check import check_parametric_plan
        return check_parametric_plan(self._server.toolkit, plan)

    def image(self, image: str, *, box=None, coordinate_grid=True, display_scale=1.0):
        """Issue original-image evidence once; actually open the returned paths.

        Uses the same image keyword as drawing projections. The underlying
        view_image tool and its view_id/coordinate/evidence contract are unchanged.
        """
        return self.call("view_image", name=image, box=box,
                         coordinate_grid=coordinate_grid, display_scale=display_scale)

    def build(self, plan: dict, *, drawings=(), mesh_observations=None):
        """Save once and render explicitly calibrated current views, without review.

        Each drawing specifies image, floor_id, x_anchors, y_anchors and basis.
        Partial view failure retains the candidate: retry its view, not its build.
        """
        from src.agent.sdk_workflow import build_with_views
        return build_with_views(self, plan, drawings=drawings, mesh_observations=mesh_observations)

    def finish_to(self, output_directory, candidate: str, *, reviews=None, inferences=None):
        """Finish normally and publish the same bound source/viewer in one call.

        The output directory must not exist. A failed or incomplete original
        finish cannot publish; no automatic review, repair or render occurs.
        """
        self._ensure_open()
        from src.agent.sdk_delivery import publication_target, publish_finished
        publication_target(self.run, output_directory)
        arguments = {"candidate": candidate}
        if reviews is not None:
            arguments["reviews_json"] = reviews
        if inferences is not None:
            arguments["inferences_json"] = inferences
        reply = self.call("finish_bim", **arguments)
        return publish_finished(self.run, candidate, reply, output_directory)

    def describe(self, name: str):
        if name not in self._definitions():
            raise ValueError(f"tool is unavailable in this session: {name}")
        # The caller cannot mutate the cached authority through a returned dict.
        return json.loads(_json(self._definitions()[name]))

    def call(self, tool_name: str, /, **arguments) -> ToolReply:
        """Call the normal tool. JSON-suffixed parameters also accept dict/list.

        Returned image paths must actually be viewed before claiming a visual
        review. No build/finish, repair, model call or retry happens implicitly.
        """
        definition = self.describe(tool_name)
        submitted = json.loads(_json(arguments))
        properties = definition.get("inputSchema", {}).get("properties", {})
        encoded = {key: (_json(value) if key in properties and key.endswith("_json")
                         and isinstance(value, (dict, list)) else value)
                   for key, value in submitted.items()}
        identity = uuid.uuid4().hex
        receipt_path = self._receipts / f"{identity}.json"
        started = time.time()
        clock = time.perf_counter()
        record = {"schema": "bim_sdk_call_v1", "tool": tool_name, "arguments": encoded,
                  "started_epoch": started, "execution": "existing_enabled_bim_tool",
                  "model_called_by_sdk": False}
        try:
            result = self._runner.run(self._server.call_tool(tool_name, encoded))
            from mcp.types import CallToolResult
            if isinstance(result, CallToolResult):
                content, data, error = result.content, result.structuredContent, bool(result.isError)
            elif isinstance(result, tuple):
                content, data = result
                error = False
            else:
                content, data, error = result, None, False
            blocks, texts, images, decoded = [], [], [], []
            for block in content:
                row = block.model_dump(mode="json", exclude_none=True)
                if row["type"] == "image":
                    pixels = base64.b64decode(row.pop("data"), validate=True)
                    digest = hashlib.sha256(pixels).hexdigest()
                    extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                        row.get("mimeType"), ".bin")
                    image_path = self._images / f"{digest}{extension}"
                    if image_path.exists():
                        if hashlib.sha256(image_path.read_bytes()).hexdigest() != digest:
                            raise ValueError("saved SDK image changed after capture")
                    else:
                        image_path.write_bytes(pixels)
                    row.update(path=str(image_path), sha256=digest, bytes=len(pixels))
                    images.append(row)
                elif row["type"] == "text":
                    try:
                        value = json.loads(row["text"])
                    except (ValueError, TypeError):
                        texts.append(row["text"])
                    else:
                        decoded.append(value)
                blocks.append(row)
            if data is None and len(decoded) == 1:
                data = decoded[0]
            else:
                texts.extend(_json(value) for value in decoded if value != data)
            # Some ordinary tools return a structured rejection instead of an
            # MCP exception (for example an exhausted candidate budget).
            # A saved-but-blocked source is different: keep its review facts.
            error = error or (isinstance(data, dict) and
                              (data.get("status") == "error" or bool(data.get("error"))))
            record.update(elapsed_seconds=time.perf_counter() - clock,
                          result={"content": blocks, "structuredContent": data, "isError": error})
            receipt_path.write_text(_json(record) + "\n", encoding="utf-8")
            return ToolReply(data, texts, images, error, str(receipt_path), tool_name)
        except BaseException as exc:
            record.update(elapsed_seconds=time.perf_counter() - clock,
                          exception={"type": type(exc).__name__, "message": str(exc)},
                          outcome="unknown_do_not_retry_a_write_without_reading_state")
            receipt_path.write_text(_json(record) + "\n", encoding="utf-8")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--readonly", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    describe = commands.add_parser("describe")
    describe.add_argument("tool")
    call = commands.add_parser("call")
    call.add_argument("tool")
    arguments_group = call.add_mutually_exclusive_group()
    arguments_group.add_argument("--arguments", type=Path, help="Path to a UTF-8 JSON object file")
    arguments_group.add_argument("--arguments-json", help="Inline JSON object; mutually exclusive with --arguments")
    args = parser.parse_args(argv)
    with BimSession(args.run, readonly=args.readonly) as session:
        if args.command == "catalog":
            print(_json(session.catalog()))
        elif args.command == "describe":
            print(_json(session.describe(args.tool)))
        else:
            if args.arguments and str(args.arguments).lstrip().startswith(("{", "[")):
                parser.error("--arguments expects a file path; use --arguments-json for inline JSON")
            arguments = (json.loads(args.arguments_json) if args.arguments_json is not None else
                         json.loads(args.arguments.read_text(encoding="utf-8-sig")) if args.arguments else {})
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
            result = session.call(args.tool, **arguments)
            print(result)
            if result.is_error:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
