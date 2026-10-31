"""File-backed stdio client for the SAME BIM tools used by subscription runs.

Useful for a development agent with code/image tools but no dynamically mounted
MCP server. This is a client, not a model provider or a separate BIM pipeline.
It records requests and complete replies, and writes returned images to PNGs.
Local model delegation is intentionally unavailable through this client.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts/tool_scripts/run_bim_agent.py"
EXCLUDED_TOOLS = {"review_detail"}


async def invoke(run: Path, requests: list[dict] | None = None) -> dict:
    """None lists tool schemas; a list executes named calls in order.

    A failed call stops the batch. Nothing interprets or repairs the model's
    arguments. Returned image paths refer to the exact MCP image bytes.
    """
    run = run.resolve()
    if not (run / "inputs.json").is_file():
        raise ValueError("run must contain prepared inputs.json")
    if requests is not None:
        if not isinstance(requests, list) or not requests:
            raise ValueError("requests must be a nonempty list")
        for request in requests:
            if (not isinstance(request, dict) or set(request) != {"tool", "arguments"}
                    or not isinstance(request["tool"], str)
                    or request["tool"] in EXCLUDED_TOOLS
                    or not isinstance(request["arguments"], dict)):
                raise ValueError("each request needs an admitted tool and arguments")
    params = StdioServerParameters(command=sys.executable,
        args=[str(SERVER), "serve", str(run)], cwd=str(ROOT))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            catalog = {tool.name: tool for tool in (await session.list_tools()).tools
                       if tool.name not in EXCLUDED_TOOLS}
            if requests is None:
                return {"tools": [tool.model_dump(exclude_none=True) for tool in catalog.values()],
                        "excluded_tools": sorted(EXCLUDED_TOOLS),
                        "note": "Same common BIM MCP tools; model delegation disabled in this bridge."}
            if not isinstance(requests, list) or not requests:
                raise ValueError("requests must be a nonempty list")
            for request in requests:
                if set(request) != {"tool", "arguments"} or request["tool"] not in catalog:
                    raise ValueError("each request needs an admitted tool and arguments")
                if not isinstance(request["arguments"], dict):
                    raise ValueError("arguments must be an object")
            folder = run / "bridge" / uuid.uuid4().hex
            folder.mkdir(parents=True)
            (folder / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
            replies = []
            for index, request in enumerate(requests, 1):
                response = await session.call_tool(request["tool"], request["arguments"])
                blocks = []
                for number, block in enumerate(response.content, 1):
                    if block.type == "image":
                        suffix = ".png" if block.mimeType == "image/png" else ".jpg"
                        path = folder / f"{index:03d}_{number}{suffix}"
                        path.write_bytes(base64.b64decode(block.data))
                        blocks.append({"type": "image", "path": str(path), "mimeType": block.mimeType})
                    else:
                        blocks.append(block.model_dump(exclude_none=True))
                reply = {"tool": request["tool"], "isError": bool(response.isError), "content": blocks}
                if response.structuredContent is not None:
                    reply["structuredContent"] = response.structuredContent
                    # The standard text copy duplicates structured JSON, often large.
                    reply["content"] = [row for row in blocks if row["type"] != "text"]
                replies.append(reply)
                (folder / "replies.json").write_text(json.dumps(replies, ensure_ascii=False, indent=2))
                if response.isError:
                    break
            return {"record": str(folder), "replies": replies,
                    "completed": len(replies) == len(requests) and not any(r["isError"] for r in replies)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("requests", nargs="?", type=Path,
                        help="JSON list of {tool,arguments}; omit to list tools, '-' reads stdin")
    args = parser.parse_args()
    raw = (sys.stdin.read() if str(args.requests) == "-" else args.requests.read_text()) if args.requests else None
    result = asyncio.run(invoke(args.run, json.loads(raw) if raw is not None else None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("completed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
