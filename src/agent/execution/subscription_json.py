"""Isolated Claude subscription CLI calls for JSON-only correction work.

This module deliberately has no API-client fallback.  It runs the locally
authenticated ``claude`` executable in a new temporary working directory, with
tools and MCP disabled, so a correction prompt cannot read project instructions
or mutate the repository through Claude Code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger


_ALLOWED_MODELS = frozenset(
    {
        "haiku",
        "sonnet",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
    }
)
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL")
# Windows: the CLI cannot open network connections without SystemRoot and finds
# the logged-in subscription under USERPROFILE; the rest are its standard paths.
_WINDOWS_ENV_ALLOWLIST = ("SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA",
                          "LOCALAPPDATA", "TEMP", "TMP", "PATHEXT", "COMSPEC")
_MCP_CONFIG = '{"mcpServers":{}}'
_SECRET_VALUE = re.compile(
    r"(?i)(\b(?:[A-Za-z0-9_]*?(?:api[_ -]?key|authorization|"
    r"auth[_ -]?token|access[_ -]?token|oauth[_ -]?token|password|secret|token)))"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SK_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]+")


def subscription_model_id(model: str) -> str:
    """Keep the recovery baseline on Sonnet 5 across Claude CLI upgrades."""
    return "claude-sonnet-5" if model == "sonnet" else model


def _safe_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _redact_secrets(text: str) -> str:
    """Keep diagnostics useful without filing credentials emitted by a CLI."""
    text = _SECRET_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text
    )
    text = _BEARER_VALUE.sub("Bearer [REDACTED]", text)
    return _SK_VALUE.sub("sk-[REDACTED]", text)


def _prompt_summary(system_prompt: str, human: str, guidance: str) -> dict[str, Any]:
    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    return {
        "system_chars": len(system_prompt),
        "human_chars": len(human),
        "retry_guidance_chars": len(guidance),
        "system_sha256": digest(system_prompt),
        "human_sha256": digest(human),
        "retry_guidance_sha256": digest(guidance) if guidance else None,
    }


def _isolated_env() -> dict[str, str]:
    """Pass only OAuth's home and locale/path plumbing to the subscription CLI."""
    keys = _ENV_ALLOWLIST + (_WINDOWS_ENV_ALLOWLIST if os.name == "nt" else ())
    env = {key: os.environ[key] for key in keys if key in os.environ}
    if os.name == "nt":
        # Python tool servers started by the CLI read UTF-8 run files; without UTF-8
        # mode they decode with the console code page (GBK here) and fail (10-07 A1).
        env["PYTHONUTF8"] = "1"
    return env


def _model_name(section: dict[str, Any]) -> str:
    model_name = section.get("model_name")
    if model_name not in _ALLOWED_MODELS:
        allowed = ", ".join(sorted(_ALLOWED_MODELS))
        raise RuntimeError(
            "claude_subscription: model_name must be one of "
            f"{allowed}; got {model_name!r}"
        )
    return model_name


def _validate_section(section: dict[str, Any]) -> tuple[str, float]:
    # This route must use the locally logged-in subscription.  Supplying either
    # API setting is almost certainly an accidental cross-provider route.
    if section.get("api_key"):
        raise RuntimeError("claude_subscription rejects non-empty api_key")
    if section.get("base_url"):
        raise RuntimeError("claude_subscription rejects non-empty base_url")
    model_name = _model_name(section)
    timeout = float(section.get("timeout_seconds", 600.0))
    if timeout <= 0:
        raise RuntimeError("claude_subscription timeout_seconds must be positive")
    return model_name, timeout


def _command(model_name: str, system_prompt: str) -> list[str]:
    """Build a print-mode CLI call; never add --bare (it disables OAuth)."""
    return [
        "claude",
        "-p",
        "--model",
        subscription_model_id(model_name),
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        _MCP_CONFIG,
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--system-prompt",
        system_prompt,
    ]


def _command_record(command: list[str]) -> list[str]:
    """Record flags while keeping the system prompt out of artifacts."""
    recorded = list(command)
    index = recorded.index("--system-prompt") + 1
    recorded[index] = "[prompt supplied separately]"
    return recorded


def _attempt_dir(out_dir: Path | None, prefix: str, attempt: int) -> Path | None:
    if out_dir is None:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    # UUID keeps a retry after a prior failed invocation from overwriting any
    # failure evidence, even if the caller reuses the same run directory.
    path = out_dir / f"{prefix}_claude_subscription_attempt_{attempt}_{uuid4().hex}"
    path.mkdir()
    return path


def _write_json(path: Path | None, name: str, value: dict[str, Any]) -> None:
    if path is not None:
        (path / name).write_text(
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )


def _write_output(path: Path | None, stdout: str, stderr: str) -> None:
    if path is not None:
        (path / "stdout.txt").write_text(_redact_secrets(stdout), encoding="utf-8")
        (path / "stderr.txt").write_text(_redact_secrets(stderr), encoding="utf-8")


def _usage_record(response: dict[str, Any]) -> dict[str, Any]:
    """File the CLI-reported estimate verbatim enough for later accounting."""
    return {
        "is_error": response.get("is_error"),
        "modelUsage": response.get("modelUsage"),
        "usage": response.get("usage"),
        "estimated_cost_usd": response.get(
            "total_cost_usd", response.get("estimated_cost_usd")
        ),
        "estimated_cost_note": "Claude CLI estimate; not a bill.",
        "duration_ms": response.get("duration_ms"),
        "duration_api_ms": response.get("duration_api_ms"),
        "num_turns": response.get("num_turns"),
    }


def call_subscription_json(
    section: dict[str, Any],
    system_prompt: str,
    human: str,
    *,
    out_dir: Path | None,
    prefix: str,
    attempts: int = 1,
    validate: Callable[[dict], None] | None = None,
    retry_guidance: Callable[[BaseException], str | None] | None = None,
    extract_json: Callable[[str], str],
    runner: Callable[..., Any] = subprocess.run,
) -> dict:
    """Call the logged-in Claude subscription CLI and return a validated object.

    ``runner`` is injectable solely for offline tests.  Production always uses
    ``subprocess.run`` and never invokes an API SDK or a fallback provider.
    """
    model_name, timeout = _validate_section(section)
    if attempts < 1:
        raise RuntimeError("claude_subscription attempts must be at least 1")

    guidance_text = ""
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        command = _command(model_name, system_prompt)
        prompt = human if not guidance_text else f"{human}\n\n{guidance_text}"
        artifact_dir = _attempt_dir(out_dir, prefix, attempt)
        _write_json(
            artifact_dir,
            "request.json",
            {
                "attempt": attempt,
                "attempts": attempts,
                "model": model_name,
                "timeout_seconds": timeout,
                "command": _command_record(command),
                "prompt_summary": _prompt_summary(system_prompt, human, guidance_text),
            },
        )

        stdout = ""
        stderr = ""
        model_response_received = False
        try:
            # A fresh cwd keeps repository instructions and settings outside the
            # command's discovery scope.  OAuth still works because HOME is in
            # the narrow child environment.  The prompt travels over stdin to
            # avoid an OS command-line length limit.
            with tempfile.TemporaryDirectory(prefix="energyplus-claude-json-") as cwd:
                completed = runner(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    cwd=cwd,
                    env=_isolated_env(),
                    timeout=timeout,
                    check=False,
                )
            stdout = _safe_text(getattr(completed, "stdout", ""))
            stderr = _safe_text(getattr(completed, "stderr", ""))
            _write_output(artifact_dir, stdout, stderr)
            returncode = getattr(completed, "returncode", None)
            if returncode != 0:
                raise RuntimeError(
                    f"claude_subscription CLI exited with status {returncode}"
                )
            response = json.loads(stdout)
            if not isinstance(response, dict):
                raise ValueError("claude_subscription CLI response must be a JSON object")
            _write_json(artifact_dir, "cli_usage.json", _usage_record(response))
            if response.get("is_error"):
                raise RuntimeError("claude_subscription CLI reported is_error")
            result = response.get("result")
            if isinstance(result, str):
                model_response_received = bool(result.strip())
                parsed = json.loads(extract_json(result))
            elif isinstance(result, dict):
                model_response_received = True
                parsed = result
            else:
                raise ValueError("claude_subscription CLI result must be a JSON object string")
            if not isinstance(parsed, dict):
                raise ValueError("claude_subscription model result must be a JSON object")
            if validate is not None:
                validate(parsed)
            return parsed
        except subprocess.TimeoutExpired as exc:
            stdout = _safe_text(
                exc.stdout if exc.stdout is not None else getattr(exc, "output", None)
            )
            stderr = _safe_text(exc.stderr)
            _write_output(artifact_dir, stdout, stderr)
            _write_json(
                artifact_dir,
                "cli_usage.json",
                {
                    "timed_out": True,
                    "timeout_seconds": timeout,
                    "estimated_cost_note": "No cost estimate available after timeout; not a bill.",
                },
            )
            last_error = RuntimeError(f"claude_subscription CLI timed out after {timeout} seconds")
        except Exception as exc:  # retry the same classes as _call_json_llm
            last_error = exc
            _write_output(artifact_dir, stdout, stderr)
        else:  # pragma: no cover - every successful path returns above
            raise AssertionError("unreachable")

        assert last_error is not None
        _write_json(
            artifact_dir,
            "failure.json",
            {
                "attempt": attempt,
                "error_type": type(last_error).__name__,
                "error": _redact_secrets(str(last_error)),
            },
        )
        if attempt < attempts:
            if retry_guidance is not None and model_response_received:
                try:
                    guidance_text = retry_guidance(last_error) or ""
                except Exception:  # guidance must never stop a retry
                    guidance_text = ""
            else:
                guidance_text = ""
            logger.warning(
                "{}: Claude subscription attempt {}/{} rejected ({}); retrying",
                prefix,
                attempt,
                attempts,
                type(last_error).__name__,
            )
            continue
        raise RuntimeError(
            f"{prefix}: failed after {attempts} attempt(s): "
            f"{type(last_error).__name__}: {_redact_secrets(str(last_error))}"
        ) from last_error

    raise AssertionError("unreachable")
