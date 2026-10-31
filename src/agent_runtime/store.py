"""Append-only event journal and verified content-addressed attachments."""

from __future__ import annotations

from src.utils import file_lock
import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from src.harness_contracts import (
    BlobCapture, BudgetAmounts, EventEnvelope, EventLog, HashedBlobRef,
    InlineCapture, JsonReferencedCapture, KnownParentTask, KnownTimestamp, RootTask, SourceRef,
)
from src.harness_contracts.incremental import EventValidationIndex


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def replace_file(source, target) -> None:
    """os.replace that rides out brief Windows sharing violations.

    Windows refuses to replace a file another process holds open (search
    indexer, scanners); in 10-07 sm24 run4 one such PermissionError on a
    reader's checkpoint.json ended that elevation reader. Retry briefly.
    """
    for delay in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 1.0):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt":
                raise
            time.sleep(delay)
    os.replace(source, target)


class EventStore:
    """One writer per run; flushed intent precedes every external operation.

    Torn JSONL tails are rejected unless explicit recovery preserves the exact
    tail and records its repair. Existing complete journals can
    be reopened; a nonblocking file lock prevents two writers using one run.
    """

    def __init__(self, directory: Path, *, run_id: str, task_id: str,
                 budget_limit: BudgetAmounts, recover_tail: bool = False):
        self._root = self
        self.directory = Path(directory).resolve()
        self.task_directory = self.directory
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "blobs").mkdir(exist_ok=True)
        self.run_id, self.task_id = run_id, task_id
        self.root_task_id, self.parent_task_id = task_id, None
        self.budget_limit = budget_limit
        self.path = self.directory / "events.jsonl"
        self._lock = (self.directory / "writer.lock").open("a+b")
        try:
            file_lock.flock(self._lock, file_lock.LOCK_EX | file_lock.LOCK_NB)
            metadata = {"run_id": run_id, "task_id": task_id,
                        "budget_limit": budget_limit.model_dump(mode="json")}
            path = self.directory / "journal.json"
            if path.exists():
                saved_metadata = json.loads(path.read_bytes())
                # Normalize optional dimensions added to the contract; an old
                # journal without a CNY limit must remain readable, not acquire one.
                saved_metadata["budget_limit"] = BudgetAmounts.model_validate_json(
                    json.dumps(saved_metadata["budget_limit"])).model_dump(mode="json")
                if saved_metadata != metadata:
                    raise ValueError("journal limits/identity cannot change on resume")
            repair = self._repair_tail() if recover_tail else None
            self._all_events = self.read_events(self.path)
            self._timing_totals = {"event_validation_seconds": 0.0,
                                   "journal_write_seconds": 0.0}
            self._append_failed = False
            if self._all_events:
                self.validate()
                if any(e.run_id != run_id for e in self._all_events):
                    raise ValueError("journal identity differs from requested run")
            self._index = EventValidationIndex(run_id=run_id, root_task_id=task_id,
                                               budget_limit=budget_limit)
            self._budget_events = []
            self._budget_events_by_task = {}
            self._budget_projection_events = []
            self._budget_projection_events_by_task = {}
            # Full validation above remains the recovery authority. Rebuild the
            # disposable hot-path indices once, without writing old events.
            started = time.perf_counter()
            try:
                for event in self._all_events:
                    update = self._index.budget.prepare(event)
                    self._index.commit(event, update)
                    self._index_budget_event(event)
            finally:
                self._timing_totals["event_validation_seconds"] += time.perf_counter() - started
            if not path.exists():
                self.write_json("journal.json", metadata)
            if repair:
                from src.harness_contracts import RunLifecyclePayload
                self.append(RunLifecyclePayload(action="failure", failure_stage="journal_append",
                    reason="explicit torn-tail recovery; original bytes preserved"),
                    source_refs=(self.source("journal-tail-repair", repair),))
        except BaseException:
            self.close()
            raise

    @property
    def events(self) -> list[EventEnvelope]:
        """Events belonging to this task facade, in global journal order."""

        return list(self._root._index.events_by_task.get(self.task_id, ()))

    @property
    def all_events(self) -> list[EventEnvelope]:
        """All events in the run, shared by every task facade."""

        return list(self._root._all_events)

    @property
    def event_revision(self) -> int:
        return len(self._root._all_events)

    @property
    def budget_revision(self) -> int:
        """Revision of all budget-affecting receipts and request/response links."""
        return len(self._root._budget_projection_events)

    def budget_events(self, task_id: str | None = None) -> list[EventEnvelope]:
        """Indexed budget events; None selects the shared run ledger."""
        root = self._root
        return list(root._budget_events if task_id is None
                    else root._budget_events_by_task.get(task_id, ()))

    def budget_projection_events(self, task_id: str | None = None) -> list[EventEnvelope]:
        """Indexed input for RuntimeBudget.from_events, including request/response."""
        root = self._root
        return list(root._budget_projection_events if task_id is None
                    else root._budget_projection_events_by_task.get(task_id, ()))

    @property
    def timing_totals(self) -> dict[str, float]:
        """Shared cumulative wall seconds, with no timing events or history scan."""
        return dict(self._root._timing_totals)

    def _index_budget_event(self, event):
        if event.payload.event_type == "budget":
            self._root._budget_events.append(event)
            self._root._budget_events_by_task.setdefault(event.task_id, []).append(event)
        if event.payload.event_type in {"budget", "adapter_request", "model_response"}:
            self._root._budget_projection_events.append(event)
            self._root._budget_projection_events_by_task.setdefault(event.task_id, []).append(event)

    @property
    def is_root_task(self) -> bool:
        return self.task_id == self.root_task_id

    def for_task(self, task_id: str, parent_task_id: str) -> EventStore:
        """Return a task-scoped facade over this run's journal and blobs."""

        root = self._root
        if task_id == root.root_task_id:
            if parent_task_id != root.root_task_id:
                raise ValueError("root task cannot have a parent")
            return root
        if task_id == parent_task_id:
            raise ValueError("task cannot be its own parent")
        if parent_task_id not in root._index.parents:
            raise ValueError("parent task does not exist in this run")
        if task_id in root._index.parents and root._index.parents[task_id] != parent_task_id:
            raise ValueError("task parent differs from persisted ancestry")

        child = object.__new__(EventStore)
        child._root = root
        child.directory = root.directory
        child.task_directory = root.directory / "tasks" / hashlib.sha256(
            task_id.encode("utf-8")
        ).hexdigest()
        child.task_directory.mkdir(parents=True, exist_ok=True)
        child.run_id = root.run_id
        child.task_id = task_id
        child.root_task_id = root.root_task_id
        child.parent_task_id = parent_task_id
        child.budget_limit = root.budget_limit
        child.path = root.path
        child._lock = root._lock
        return child

    @staticmethod
    def read_events(path: Path) -> list[EventEnvelope]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise ValueError("incomplete event journal tail; explicit recovery required")
        return [EventEnvelope.model_validate_json(line) for line in raw.splitlines()]

    def _repair_tail(self):
        if not self.path.exists():
            return None
        raw = self.path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return None
        boundary = raw.rfind(b"\n") + 1
        prefix, tail = raw[:boundary], raw[boundary:]
        # Verify every completed record before changing even the partial tail.
        events = [EventEnvelope.model_validate_json(line) for line in prefix.splitlines()]
        if any(e.run_id != self.run_id for e in events):
            raise ValueError("journal identity differs from requested run")
        if events:
            self._validate_events(events)
        saved = self.put_bytes(tail)
        record = {"original_sha256": hashlib.sha256(raw).hexdigest(),
                  "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
                  "offset": boundary, "tail": saved.model_dump(mode="json")}
        # A complete JSON event missing only its newline is retained as an event.
        try:
            last = EventEnvelope.model_validate_json(tail)
            EventLog(mode="complete", events=tuple([*events, last]), budget_limit=self.budget_limit)
        except ValueError:
            repaired, record["action"] = prefix, "archive_incomplete_tail"
        else:
            if last.run_id != self.run_id:
                raise ValueError("journal identity differs from requested run")
            repaired, record["action"] = raw + b"\n", "complete_newline"
        # The repair intent and removed bytes are durable before replacing JSONL.
        self.write_json("tail_repair.json", record)
        temporary = self.path.with_name(f"events.repair.{os.getpid()}.tmp")
        with temporary.open("wb") as output:
            output.write(repaired)
            output.flush()
            os.fsync(output.fileno())
        replace_file(temporary, self.path)
        self._sync_directory(self.directory)
        return record

    def latest_checkpoint(self):
        for event in reversed(self.events):
            if event.payload.event_type == "checkpoint":
                # Verify the blob now; never silently fall back past corruption.
                return event.payload.state, self.get_json_tree(event.payload.state), event.sequence
        pointer = self.task_directory / "checkpoint.json"
        if not pointer.exists():
            return None
        ref = HashedBlobRef.model_validate_json(pointer.read_bytes())
        snapshot = self.get_json_tree(ref)
        sequence = next(e.sequence for e in self.events if e.event_id == snapshot["last_event_id"])
        return ref, snapshot, sequence

    def close(self):
        if self is not self._root:
            return
        if not self._lock.closed:
            file_lock.flock(self._lock, file_lock.LOCK_UN)
            self._lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def put_bytes(self, data: bytes, media_type="application/octet-stream") -> HashedBlobRef:
        sha = hashlib.sha256(data).hexdigest()
        relative = f"blobs/{sha}"
        path = self.directory / relative
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("content-addressed blob was corrupted")
        else:
            # Atomic creation inside the run, with a durable directory entry.
            temp = self.directory / f"blobs/.{sha}.{os.getpid()}.tmp"
            with temp.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            replace_file(temp, path)
            self._sync_directory(path.parent)
        return HashedBlobRef(uri=relative, sha256=sha, media_type=media_type)

    def put_json(self, value) -> HashedBlobRef:
        return self.put_bytes(json_bytes(value), "application/json")

    def put_file(self, source: Path, media_type="application/octet-stream") -> HashedBlobRef:
        """Capture a file with bounded buffers, including existing-blob verification."""
        temporary = None
        try:
            digest = hashlib.sha256()
            with Path(source).open("rb") as incoming, tempfile.NamedTemporaryFile(
                    dir=self.directory / "blobs", prefix=".capture-", delete=False) as output:
                temporary = Path(output.name)
                while chunk := incoming.read(256 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            sha = digest.hexdigest()
            relative = f"blobs/{sha}"
            target = self.directory / relative
            if target.exists():
                with target.open("rb") as saved:
                    if hashlib.file_digest(saved, "sha256").hexdigest() != sha:
                        raise ValueError("content-addressed blob was corrupted")
            else:
                replace_file(temporary, target)
                self._sync_directory(target.parent)
            return HashedBlobRef(uri=relative, sha256=sha, media_type=media_type)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def put_json_tree(self, value) -> HashedBlobRef:
        from .json_tree import store_json_tree
        return store_json_tree(value, self)

    def get_json_tree(self, ref):
        from .json_tree import read_json_tree
        return read_json_tree(ref, self.get_bytes)

    def get_bytes(self, ref: HashedBlobRef) -> bytes:
        path = (self.directory / ref.uri).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("attachment reference escapes the run directory")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("attachment hash mismatch")
        return data

    def capture(self, value, *, force_blob=False):
        data = json_bytes(value)
        from .image_capture import capture_images
        referenced = capture_images(value, self, data)
        if referenced is not None:
            return referenced
        if len(data) > 8192:
            return JsonReferencedCapture(blob=self.put_json_tree(value),
                wire_sha256=hashlib.sha256(data).hexdigest())
        if force_blob:
            return BlobCapture(blob=self.put_bytes(data, "application/json"))
        return InlineCapture(value=value)

    def resolve(self, capture):
        if capture.kind == "inline":
            return capture.value
        if capture.kind == "blob":
            return json.loads(self.get_bytes(capture.blob))
        if capture.kind in {"image_references", "json_references"}:
            return json.loads(self.capture_bytes(capture))
        raise ValueError(f"capture unavailable: {capture.reason}")

    def capture_bytes(self, capture) -> bytes:
        """Return the exact captured bytes, expanding image references if needed."""
        if capture.kind == "json_references":
            wire = json_bytes(self.get_json_tree(capture.blob))
            if hashlib.sha256(wire).hexdigest() != capture.wire_sha256:
                raise ValueError("reconstructed capture hash mismatch")
            return wire
        if capture.kind == "image_references":
            from .image_capture import reconstruct_capture
            return reconstruct_capture(capture, self.get_bytes)
        if capture.kind == "blob":
            return self.get_bytes(capture.blob)
        if capture.kind == "inline":
            return json_bytes(capture.value)
        raise ValueError(f"capture unavailable: {capture.reason}")

    def source(self, name: str, value, *, kind="runtime") -> SourceRef:
        return SourceRef(source_id=name, source_kind=kind, locator=name,
                         blob=self.put_json(value))

    def append(self, payload, *, source_refs=(), task_id: str | None = None,
               parent_task_id: str | None = None) -> EventEnvelope:
        if self._lock.closed:
            raise ValueError("journal is closed")
        if self._root._append_failed:
            raise ValueError("journal append failed; reopen with explicit recovery before writing")
        if self is not self._root and (task_id is not None or parent_task_id is not None):
            raise ValueError("task facade cannot append for another task")
        effective_task = task_id or self.task_id
        if effective_task == self.root_task_id:
            if parent_task_id is not None:
                raise ValueError("root task cannot have a parent")
            parent = RootTask()
        else:
            effective_parent = parent_task_id or self.parent_task_id
            if effective_parent is None:
                raise ValueError("child task append requires a known parent")
            parent = KnownParentTask(task_id=effective_parent)
        all_events = self._root._all_events
        seq = all_events[-1].sequence + 1 if all_events else 0
        root = self._root
        started = time.perf_counter()
        try:
            event = EventEnvelope(event_id=f"event-{seq:06d}", run_id=self.run_id,
                task_id=effective_task, parent_task=parent, sequence=seq,
                occurred_at=KnownTimestamp(value=datetime.now(UTC)),
                source_refs=tuple(source_refs), payload=payload)
            # Validate before appending. In-flight request prefixes are valid.
            update = root._index.prepare(event)
        finally:
            root._timing_totals["event_validation_seconds"] += time.perf_counter() - started
        started = time.perf_counter()
        try:
            with self.path.open("ab") as output:
                output.write(event.model_dump_json().encode("utf-8") + b"\n")
                output.flush()
                os.fsync(output.fileno())
            self._sync_directory(self.directory)
        except BaseException:
            # A failed flush may have left a complete or torn record on disk.
            # Keep indices unchanged and prevent another append reusing its ID.
            root._append_failed = True
            raise
        finally:
            root._timing_totals["journal_write_seconds"] += time.perf_counter() - started
        all_events.append(event)
        root._index.commit(event, update)
        root._index_budget_event(event)
        return event

    def validate(self) -> EventLog:
        started = time.perf_counter()
        try:
            return self._validate_events(self._root._all_events)
        finally:
            self._root._timing_totals["event_validation_seconds"] += time.perf_counter() - started

    def _validate_events(self, events: list[EventEnvelope]) -> EventLog:
        log = EventLog(mode="complete", events=tuple(events),
                       budget_limit=self.budget_limit)
        parents: dict[str, str | None] = {self.root_task_id: None}
        for event in events:
            if event.task_id == self.root_task_id:
                if event.parent_task.kind != "root":
                    raise ValueError("root task events must use root ancestry")
                continue
            if event.parent_task.kind != "known":
                raise ValueError("child task events require a known parent")
            previous = parents.setdefault(event.task_id, event.parent_task.task_id)
            if previous != event.parent_task.task_id:
                raise ValueError("task parent changed within a run")
        for task_id, parent_id in parents.items():
            if parent_id is not None and parent_id not in parents:
                raise ValueError("parent task does not exist in this run")
            seen = {task_id}
            cursor = parent_id
            while cursor is not None:
                if cursor in seen:
                    raise ValueError("task ancestry contains a cycle")
                seen.add(cursor)
                cursor = parents.get(cursor)
        return log

    def reservation_id(self, ordinal: int) -> str:
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError("reservation ordinal must be a positive integer")
        return f"{self.task_id}:request-{ordinal}"

    def next_reservation_id(self) -> str:
        count = self._root._index.reservation_counts.get(self.task_id, 0)
        return self.reservation_id(count + 1)

    def write_json(self, name: str, value):
        path = (self.task_directory / name).resolve()
        if not path.is_relative_to(self.task_directory):
            raise ValueError("output path escapes run")
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".{os.getpid()}.tmp")
        with temp.open("wb") as output:
            output.write(json_bytes(value))
            output.flush()
            os.fsync(output.fileno())
        replace_file(temp, path)
        self._sync_directory(path.parent)

    @staticmethod
    def _sync_directory(path):
        # Windows has no POSIX directory fsync. Every file is still flushed
        # with fsync before atomic replacement. Directory-entry durability on
        # power loss is therefore filesystem-managed on Windows.
        if os.name == "nt":
            return
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
