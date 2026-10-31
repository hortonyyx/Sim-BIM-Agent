"""Small durable operator inbox, separate from the run's single journal writer.

python -m src.agent_runtime.control RUN_DIR message --task TASK --text TEXT
python -m src.agent_runtime.control RUN_DIR pause --task TASK
python -m src.agent_runtime.control RUN_DIR resume --task TASK
python -m src.agent_runtime.control RUN_DIR status

Commands are immutable and acknowledged in events.jsonl by the target runtime.
Submitting a command never opens an EventStore writer or invokes a model.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from src.harness_contracts.base import ContractModel, NonEmptyStr
from src.harness_contracts import EventEnvelope, TaskControlPayload
from src.utils import file_lock
from .store import json_bytes, replace_file


class TaskCommand(ContractModel):
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    sequence: int = Field(ge=0)
    run_id: NonEmptyStr
    target_task_id: NonEmptyStr
    action: Literal["pause", "resume", "message"]
    text: str | None = Field(default=None, min_length=1, max_length=100_000)
    source: NonEmptyStr
    created_at: NonEmptyStr

    @model_validator(mode="after")
    def validate_text(self):
        if (self.action == "message") != (self.text is not None):
            raise ValueError("only message commands require text")
        return self


def read_commands(directory: Path) -> list[TaskCommand]:
    path = Path(directory) / "controls" / "commands"
    if not path.exists():
        return []
    commands = [TaskCommand.model_validate_json(p.read_bytes()) for p in path.glob("*.json")]
    sequences = [c.sequence for c in commands]
    if len(sequences) != len(set(sequences)):
        raise ValueError("control command sequence collision")
    return sorted(commands, key=lambda c: c.sequence)


def _events_for_status(directory):
    # Observers may see a writer midway through the final line. Read only the
    # complete prefix; never repair or modify the authoritative journal.
    path = directory / "events.jsonl"
    raw = path.read_bytes() if path.exists() else b""
    boundary = raw.rfind(b"\n") + 1
    return [EventEnvelope.model_validate_json(line) for line in raw[:boundary].splitlines()]


def submit_command(directory: Path, *, target_task_id: str, action: str,
                   text: str | None = None, source: str = "operator",
                   command_id: str | None = None) -> TaskCommand:
    directory = Path(directory).resolve()
    metadata = json.loads((directory / "journal.json").read_bytes())
    identity = command_id or uuid.uuid4().hex
    # Validate before using command_id in a path or acquiring the lock.
    candidate = TaskCommand(command_id=identity, sequence=0,
        run_id=metadata["run_id"], target_task_id=target_task_id, action=action,
        text=text, source=source, created_at=datetime.now(UTC).isoformat())
    folder = directory / "controls" / "commands"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder.parent / "inbox.lock").open("a+b") as lock:
        file_lock.flock(lock, file_lock.LOCK_EX)
        try:
            commands = read_commands(directory)
            previous = next((c for c in commands if c.command_id == identity), None)
            if previous:
                fields = ("run_id", "target_task_id", "action", "text", "source")
                if any(getattr(previous, key) != getattr(candidate, key) for key in fields):
                    raise ValueError("command ID already exists with different content")
                return previous
            events = _events_for_status(directory)
            known = {metadata["task_id"], *(e.task_id for e in events)}
            if target_task_id not in known:
                raise ValueError("target task has not started in this run")
            last = next((e for e in reversed(events) if e.task_id == target_task_id and
                         e.payload.event_type == "run_lifecycle"), None)
            if last and last.payload.action == "stop" and last.payload.reason == "completed":
                raise ValueError("completed task cannot accept new controls")
            candidate = candidate.model_copy(update={"sequence": max(
                (c.sequence for c in commands), default=-1) + 1})
            temporary = folder / (identity + ".pending")
            with temporary.open("wb") as stream:
                stream.write(json_bytes(candidate.model_dump(mode="json")))
                stream.flush()
                os.fsync(stream.fileno())
            replace_file(temporary, folder / (identity + ".json"))
            return candidate
        finally:
            file_lock.flock(lock, file_lock.LOCK_UN)


def control_status(directory: Path) -> dict:
    directory = Path(directory).resolve()
    metadata = json.loads((directory / "journal.json").read_bytes())
    events = _events_for_status(directory)
    applied = {e.payload.command_id: e for e in events if isinstance(e.payload, TaskControlPayload)}
    states = {}
    for event in events:
        state = states.setdefault(event.task_id, {"paused": False, "last_lifecycle": None})
        if isinstance(event.payload, TaskControlPayload):
            if event.payload.action in {"pause", "resume"}:
                state["paused"] = event.payload.action == "pause"
        elif event.payload.event_type == "run_lifecycle":
            state["last_lifecycle"] = {"action": event.payload.action, "reason": event.payload.reason}
    return {"run_id": metadata["run_id"], "tasks": states, "commands": [
        {**command.model_dump(mode="json"), "status": "applied" if command.command_id in applied else "queued",
         "acknowledgement_event_id": applied[command.command_id].event_id if command.command_id in applied else None}
        for command in read_commands(directory)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("action", choices=("message", "pause", "resume", "status"))
    parser.add_argument("--task")
    parser.add_argument("--text")
    parser.add_argument("--source", default="operator")
    parser.add_argument("--id", dest="command_id")
    args = parser.parse_args()
    if args.action == "status":
        result = control_status(args.directory)
    else:
        if not args.task:
            parser.error("--task is required")
        result = submit_command(args.directory, target_task_id=args.task,
            action=args.action, text=args.text, source=args.source,
            command_id=args.command_id).model_dump(mode="json")
        result["status"] = "queued"
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
