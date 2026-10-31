"""Conservative transient-error classification and credential-free diagnostics."""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from src.harness_contracts import UsageMissing, UsageReported
from src.harness_contracts.events import ModelFailureDetails


def _sanitize(value, secret=""):
    text = str(value)
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r'(?i)([\"\']?(?:api[_-]?key|authorization|access[_-]?token|password|secret|credential)[\"\']?\s*[:=]\s*)'
        r'([\"\'][^\"\']*[\"\']|[^\s,;}]+)', r'\1"[redacted]"', text)
    return text


def safe_exception_details(exc: BaseException, *, stage: str) -> dict[str, str]:
    """Keep local failure identity while conservatively omitting arbitrary text."""

    def safe_text(value, fallback):
        try:
            text = str(value)
        except BaseException:
            return fallback
        encoded = text.encode("utf-8", errors="backslashreplace")[:2048]
        normalized = encoded.decode("utf-8", errors="replace")
        return normalized or fallback

    try:
        raw_exception_type = (
            f"{getattr(type(exc), '__module__', 'unknown')}."
            f"{getattr(type(exc), '__qualname__', 'Exception')}"
        )
    except BaseException:
        raw_exception_type = "unknown.Exception"
    exception_type = safe_text(raw_exception_type, "unknown.Exception")
    safe_stage = safe_text(stage, "tool:unknown")
    try:
        message = str(exc)
    except BaseException:
        diagnostic = "exception message unavailable because __str__ failed"
    else:
        # Exception text can contain arbitrary tool inputs, URI userinfo,
        # credentials or tokens. Type and stage retain the actionable signal.
        diagnostic = (
            "exception carried no message"
            if not message
            else "exception message omitted by safety policy"
        )
    return {
        "exception_type": exception_type,
        "stage": safe_stage,
        "diagnostic": safe_text(diagnostic, "exception diagnostic unavailable"),
    }


class ModelServiceError(Exception):
    def __init__(self, details: dict, usage):
        # Never include the original HTTP exception, URL or response in str().
        super().__init__(details["category"])
        self.details = details
        self.usage = usage


def rejected_request_usage():
    """Explicitly labelled accounting zero, not a provider token measurement."""
    return UsageReported(raw_usage={
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "runtime_usage_basis": "rejected_before_processing",
        "runtime_zero_reason": "HTTP 429 explicitly refused a temporary rate/concurrency limit",
    })


def is_unprocessed_rejection(failure):
    return (failure.http_status == 429 and failure.category == "temporary_rate_limit"
            and not failure.usage_received)


def is_context_length_error(error):
    """Only explicit machine-readable codes establish a context rejection."""
    return isinstance(error, dict) and any(
        str(error.get(key, "")).casefold() in {"context_length_exceeded", "context_window_exceeded",
                                                "max_context_length_exceeded"}
        for key in ("code", "type"))


def transport_diagnostics(exc: BaseException, *, phase: str = "unknown") -> dict:
    """Record types and observable transport stage, never exception text/URLs."""
    for classes, name in (
        ((httpx.ConnectError, httpx.ConnectTimeout), "connect"),
        ((httpx.ReadError, httpx.ReadTimeout), "read"),
        ((httpx.WriteError, httpx.WriteTimeout), "write"),
        ((httpx.PoolTimeout,), "pool"),
    ):
        if isinstance(exc, classes):
            phase = name
            break
    chain, codes, seen, current = [], [], set(), exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        name = f"{type(current).__module__}.{type(current).__qualname__}"
        # Runtime exception classes have identifier names. Reject arbitrary
        # custom metadata instead of attempting to redact unknown credentials.
        chain.append(name[:256] if re.fullmatch(r"[\w.]+", name, flags=re.ASCII) else "unknown.Exception")
        if isinstance(current, OSError):
            for attribute in ("errno", "winerror"):
                code = getattr(current, attribute, None)
                if type(code) is int and -(2 ** 31) <= code < 2 ** 31 and code not in codes and len(codes) < 8:
                    codes.append(code)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return {"transport_phase": phase, "exception_chain": tuple(chain), "os_error_codes": tuple(codes)}


def service_diagnostics(error, *, secret: str = "", headers=None, envelope=None) -> dict:
    """Allowlisted machine fields; arbitrary bodies/messages can echo requests."""
    error = error if isinstance(error, dict) else {}
    envelope = envelope if isinstance(envelope, dict) else {}

    def identifier(value):
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str) or not value:
            return None
        sanitized = _sanitize(value, secret)
        return sanitized[:256] if re.fullmatch(r"[A-Za-z0-9_.:-]+", sanitized) else "[redacted]"

    hints = []
    for source in (headers, error.get("headers")):
        if hasattr(source, "items"):
            hints.extend(value for key, value in source.items()
                         if str(key).casefold() == "x-retry-metadata")
    if error.get("retry_hint") is not None:
        hints.append(error["retry_hint"])
    # Conflicting or unfamiliar hints never grant permission to retry.
    hint = ("NO_MORE_RETRY" if any(isinstance(v, str) and v.strip().upper() == "NO_MORE_RETRY" for v in hints)
            else identifier(hints[0]) if len(hints) == 1
            else "UNRECOGNIZED_RETRY_HINT" if hints else None)
    if hints and hint is None:
        hint = "UNRECOGNIZED_RETRY_HINT"
    header_id = None
    if hasattr(headers, "items"):
        header_id = next((value for key, value in headers.items()
                          if str(key).casefold() in {"x-request-id", "request-id"}), None)
    result = {
        "service_error_type": identifier(error.get("type", error.get("code"))),
        "service_error_code": identifier(error.get("code")),
        "retry_hint": hint,
        "request_id": identifier(header_id or error.get("request_id") or envelope.get("request_id")),
    }
    # No response body, headers, arbitrary message or echoed input enters the
    # journal. These fields retain the signal used for the retry decision.
    result["body_excerpt"] = json.dumps({**result, "message": "service message omitted; structured fields retained"})
    return result


def sse_failure_classification(error, *, retry_hint=None):
    """Only explicit temporary overload without any retry hint is retryable."""
    error = error if isinstance(error, dict) else {}
    codes = {str(error.get(key, "")).casefold() for key in ("code", "type")}
    if codes & {"authentication_error", "permission_error", "permission_denied", "unauthorized", "forbidden"}:
        return "permission_denied", False
    if codes & {"insufficient_quota", "quota_exhausted", "usage_limit_reached", "billing_hard_limit_reached"}:
        return "quota_exhausted", False
    if is_context_length_error(error):
        return "context_length_exceeded", False
    if "server_is_overloaded" in codes:
        return "service_unavailable", retry_hint is None
    return "service_error", False


def http_failure(response: httpx.Response, *, secret: str, provider: str | None = None) -> ModelServiceError:
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error", body) if isinstance(body, dict) else {"message": response.text}
    error = error if isinstance(error, dict) else {"message": str(error)}
    diagnostic = " ".join(str(error.get(k, "")) for k in ("type", "code", "message")).casefold()
    status = response.status_code
    # Provider-specific codes are meaningful only on the identified route.
    # Source: https://docs.bigmodel.cn/cn/api/api-code (checked 2026-10-04).
    glm_code = str(error.get("code", "")) if provider == "glm" else ""
    glm_quota = glm_code in {"1113", "1308", "1309", "1310", "1314",
                            "1316", "1317", "1318", "1319", "1320", "1321"}
    glm_permission = glm_code in {"1000", "1001", "1003", "1005", "1220", "1311", "1315"}
    # Ambiguous 429 is a stop. Only an explicitly temporary rate/concurrency
    # limit is retryable; account, balance and subscription exhaustion win.
    quota = glm_quota or any(word in diagnostic for word in (
        "quota", "balance", "credit", "billing", "daily limit", "usage limit",
        "monthly limit", "subscription limit", "额度", "配额", "余额", "欠费", "套餐", "用尽", "用完",
        "使用上限", "消费上限", "限额", "每周", "每月"))
    permission = glm_permission or any(word in diagnostic for word in (
        "permission", "unauthorized", "forbidden", "权限", "无权", "身份验证"))
    transient_rate = any(word in diagnostic for word in (
        "rate_limit", "rate limit", "too many requests", "concurrency", "frequency",
        "temporar", "限流", "频率", "并发"))
    context_rejection = status in {400, 413, 422} and is_context_length_error(error)
    if status in {401, 403} or permission:
        category, retryable = "permission_denied", False
    elif quota:
        category, retryable = "quota_exhausted", False
    elif context_rejection:
        # Runtime may reorganize context once; resending identical input is not
        # a transient retry. Do not infer zero usage or match arbitrary prose.
        category, retryable = "context_length_exceeded", False
    elif status == 429:
        if glm_code in {"1302", "1305"}:
            category, retryable = "temporary_rate_limit", True
        elif glm_code or "公平使用" in diagnostic or "fair use" in diagnostic:
            # 1313 requires an account-side policy action, despite mentioning
            # request frequency. Unknown business codes are not guessed.
            category, retryable = "unclassified_rate_limit", False
        else:
            category, retryable = ("temporary_rate_limit", True) if transient_rate else ("unclassified_rate_limit", False)
    elif status == 408:
        category, retryable = "timeout", True
    elif 500 <= status <= 599:
        category, retryable = "service_unavailable", True
    else:
        category, retryable = "invalid_request", False
    usage_raw = body.get("usage") if isinstance(body, dict) else None
    usage_received = bool(usage_raw)
    # Preserve reported usage separately, without copying arbitrary service
    # payload fields (which can echo authorization) into the ledger.
    usage_raw = {key: value for key, value in (usage_raw or {}).items()
                 if key in {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
                 and type(value) is int and value >= 0} if isinstance(usage_raw, dict) else {}
    usage = UsageReported(raw_usage=usage_raw) if usage_raw else UsageMissing(reason="HTTP error omitted token usage")
    if not usage_received and status == 429 and category == "temporary_rate_limit":
        usage = rejected_request_usage()
    details = service_diagnostics(error, secret=secret, headers=response.headers, envelope=body)
    if details["retry_hint"] is not None:
        retryable = False
    return ModelServiceError({
        "category": category, "retryable": retryable, "http_status": status,
        **details,
        "usage_received": usage_received,
    }, usage)


def classify_failure(exc: BaseException, request_event_id: str) -> ModelFailureDetails:
    if isinstance(exc, ModelServiceError):
        return ModelFailureDetails(request_event_id=request_event_id, **exc.details)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        category, retryable = "timeout", True
    elif isinstance(exc, (ConnectionError, httpx.TransportError)):
        category, retryable = "transport_error", True
    elif isinstance(exc, asyncio.CancelledError):
        category, retryable = "cancelled", False
    else:
        category, retryable = "unknown_error", False
    return ModelFailureDetails(request_event_id=request_event_id, category=category,
        retryable=retryable, service_error_type=type(exc).__name__, usage_received=False,
        **transport_diagnostics(exc))
