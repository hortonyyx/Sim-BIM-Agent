"""Durable batch request cap, separate from provider token accounting.

Every attempted send consumes one ticket, including errors and interruption.
The quota file contains no credentials, prompts, images or responses.
"""

from __future__ import annotations

from src.utils import file_lock
import json
import os
from datetime import UTC, datetime
from pathlib import Path


class QuotaAdapter:
    def __init__(self, adapter, path: Path, *, limit: int, category: str):
        if type(limit) is not int or limit < 1:
            raise ValueError("request quota must be positive")
        self.adapter, self.path, self.limit, self.category = adapter, Path(path), limit, category
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, row):
        with self.path.open("a+b") as output:
            file_lock.flock(output, file_lock.LOCK_EX)
            output.seek(0)
            data = output.read()
            if data and not data.endswith(b"\n"):
                raise ValueError("quota journal has a torn tail; do not retry")
            prior = [json.loads(line) for line in data.splitlines()]
            if any(v["category"] != self.category or v["limit"] != self.limit for v in prior):
                raise ValueError("quota identity/limit cannot change in the same batch")
            if row["event"] == "attempt":
                count = sum(v["event"] == "attempt" for v in prior)
                if count >= self.limit:
                    raise ValueError("batch request quota exhausted")
                row["ticket"] = count + 1
            row.update(category=self.category, limit=self.limit, at=datetime.now(UTC).isoformat())
            output.seek(0, os.SEEK_END)
            output.write(json.dumps(row, ensure_ascii=False).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
            return row["ticket"]

    async def send(self, prepared, *, timeout):
        ticket = self._append({"event": "attempt", "model": prepared.body["model"]})
        try:
            raw = await self.adapter.send(prepared, timeout=timeout)
        except BaseException as exc:
            self._append({"event": "failure", "ticket": ticket, "error_type": type(exc).__name__})
            raise
        self._append({"event": "response", "ticket": ticket, "model": raw.get("model"),
                      "usage": raw.get("usage"), "usage_status": "reported" if raw.get("usage") else "missing"})
        return raw
