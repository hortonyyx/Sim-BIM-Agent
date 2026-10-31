"""Sign in with ChatGPT credentials for the local Sim BIM Agent runtime.

This module implements the public-client OAuth flow documented at
https://developers.openai.com/siwc/token-sharing-open-source.  It deliberately
does not discover or reuse credentials belonging to Codex or another app.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import hashlib
import json
import os
import queue
import secrets
import sys
import tempfile
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from src.utils import file_lock

ISSUER = "https://auth.openai.com"
DISCOVERY_ENDPOINT = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/api/accounts/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
MODELS_ENDPOINT = f"{RESOURCE}/models"
DYNAMIC_CLIENT_ID = "dynamic_agent_client"
APP_NAME = "Sim BIM Agent"
CALLBACK_PATH = "/auth/callback"
REQUIRED_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "resource.invoke",
    REQUIRED_SCOPE,
)
_REFRESH_SKEW_SECONDS = 60.0
_SCHEMA_VERSION = 1
_DPAPI_ENTROPY = b"EnergyPlus-Agent/openai-subscription/v1"
_UNUSABLE_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


class SubscriptionAuthError(RuntimeError):
    """A safe-to-display subscription authentication failure."""


@dataclass(frozen=True, slots=True)
class SubscriptionModel:
    """A model explicitly exposed in the selected ChatGPT account catalog."""

    slug: str
    display_name: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    client_id: str
    email: str | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LogoutResult:
    signed_out: bool
    revocation_confirmed: bool


class _Protector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _PlaintextProtector:
    """Owner-permission storage used only on platforms without DPAPI."""

    def protect(self, value: bytes) -> bytes:
        return value

    def unprotect(self, value: bytes) -> bytes:
        return value


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class _WindowsDpapiProtector:
    """Encrypt credential bytes for the current Windows user with DPAPI."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    @staticmethod
    def _blob(value: bytes) -> tuple[_DataBlob, Any]:
        buffer = ctypes.create_string_buffer(value)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
        return _DataBlob(len(value), pointer), buffer

    def _transform(self, value: bytes, *, protect: bool) -> bytes:
        if os.name != "nt":
            raise SubscriptionAuthError("Windows DPAPI is unavailable on this platform.")
        input_blob, input_buffer = self._blob(value)
        entropy_blob, entropy_buffer = self._blob(_DPAPI_ENTROPY)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            succeeded = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        # Keep buffers live across the native call.
        del input_buffer, entropy_buffer
        if not succeeded:
            raise SubscriptionAuthError("The local credential store could not be unlocked.")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, protect=True)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, protect=False)


def _default_directory() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise SubscriptionAuthError("LOCALAPPDATA is required for subscription login.")
        return Path(local_app_data) / "EnergyPlus-Agent" / "openai-subscription"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "EnergyPlus-Agent" / "openai-subscription"


def _default_protector() -> _Protector:
    if os.name == "nt":
        return _WindowsDpapiProtector()
    return _PlaintextProtector()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with contextlib.suppress(OSError):
            temporary.chmod(0o600)
        os.replace(temporary, path)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class _CredentialStore:
    def __init__(self, directory: Path, protector: _Protector) -> None:
        self.directory = directory
        self.credentials_path = directory / "credentials.bin"
        self.host_path = directory / "host.json"
        self.lock_path = directory / "credentials.lock"
        self._protector = protector

    @contextlib.contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as stream:
            file_lock.flock(stream, file_lock.LOCK_EX)
            try:
                yield
            finally:
                file_lock.flock(stream, file_lock.LOCK_UN)

    def host_id(self) -> str:
        if self.host_path.exists():
            try:
                value = json.loads(self.host_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SubscriptionAuthError("The local host identity is unreadable.") from exc
            host_id = value.get("ext_agent_host_id") if isinstance(value, dict) else None
            if isinstance(host_id, str) and host_id.startswith("urn:uuid:"):
                try:
                    uuid.UUID(host_id.removeprefix("urn:uuid:"))
                except ValueError as exc:
                    raise SubscriptionAuthError("The local host identity is invalid.") from exc
                return host_id
            raise SubscriptionAuthError("The local host identity is invalid.")
        host_id = f"urn:uuid:{uuid.uuid4()}"
        payload = json.dumps(
            {"ext_agent_host_id": host_id}, separators=(",", ":"), sort_keys=True
        ).encode()
        _atomic_write(self.host_path, payload)
        return host_id

    def load(self) -> dict[str, Any]:
        if not self.credentials_path.exists():
            return {"version": _SCHEMA_VERSION, "active_client_id": None, "registrations": {}}
        try:
            protected = self.credentials_path.read_bytes()
            decoded = self._protector.unprotect(protected)
            value = json.loads(decoded.decode("utf-8"))
        except SubscriptionAuthError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise SubscriptionAuthError("The local credential store is unreadable.") from exc
        if not isinstance(value, dict) or value.get("version") != _SCHEMA_VERSION:
            raise SubscriptionAuthError("The local credential store has an unsupported format.")
        if not isinstance(value.get("registrations"), dict):
            raise SubscriptionAuthError("The local credential store is invalid.")
        active = value.get("active_client_id")
        if active is not None and not isinstance(active, str):
            raise SubscriptionAuthError("The local credential store is invalid.")
        return value

    def save(self, value: Mapping[str, Any]) -> None:
        serialized = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        _atomic_write(self.credentials_path, self._protector.protect(serialized))

    @contextlib.asynccontextmanager
    async def async_locked(self):
        """Acquire the process lock without blocking the caller's event loop."""

        self.directory.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        acquired = False
        try:
            while not acquired:
                try:
                    file_lock.flock(
                        stream, file_lock.LOCK_EX | file_lock.LOCK_NB
                    )
                    acquired = True
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            yield
        finally:
            if acquired:
                with contextlib.suppress(Exception):
                    file_lock.flock(stream, file_lock.LOCK_UN)
            stream.close()


class _OAuthHttpError(Exception):
    def __init__(self, operation: str, status: int, code: str | None = None) -> None:
        super().__init__(operation, status, code)
        self.operation = operation
        self.status = status
        self.code = code


class _LoopbackReceiver:
    def __init__(self) -> None:
        self._callbacks: queue.Queue[dict[str, str]] = queue.Queue(maxsize=1)
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_error(404)
                    return
                values = parse_qs(parsed.query, keep_blank_values=True)
                callback: dict[str, str] = {}
                for name in ("code", "state", "client_id", "error"):
                    entries = values.get(name)
                    if entries:
                        if len(entries) != 1:
                            self.send_error(400)
                            return
                        callback[name] = entries[0]
                with contextlib.suppress(queue.Full):
                    receiver._callbacks.put_nowait(callback)
                body = b"Sign-in received. You can close this window."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.timeout = 0.25

    @property
    def redirect_uri(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}{CALLBACK_PATH}"

    def wait(self, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._server.handle_request()
            try:
                return self._callbacks.get_nowait()
            except queue.Empty:
                continue
        raise SubscriptionAuthError("ChatGPT sign-in timed out.")

    def close(self) -> None:
        self._server.server_close()


def _required_string(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise SubscriptionAuthError("The authentication response was incomplete.")
    return item


def _scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        result = tuple(dict.fromkeys(value.split()))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        result = tuple(item for item in value if isinstance(item, str) and item)
    else:
        result = ()
    if REQUIRED_SCOPE not in result:
        raise SubscriptionAuthError("ChatGPT plan access was not granted.")
    return result


def _earliest_refresh_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class SubscriptionCredentials:
    """Protected ChatGPT subscription credentials for one active registration.

    ``transport`` exists for deterministic offline tests.  Production callers
    should leave it unset so HTTPS uses httpx's normal verified TLS transport.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        _protector: _Protector | None = None,
        _clock: Callable[[], float] = time.time,
        _browser_opener: Callable[[str], bool] = webbrowser.open,
    ) -> None:
        self.directory = Path(directory) if directory is not None else _default_directory()
        self._store = _CredentialStore(self.directory, _protector or _default_protector())
        self._transport = transport
        self._clock = _clock
        self._browser_opener = _browser_opener
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
        )

    async def _json_request(
        self,
        operation: str,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            # Do not retain the httpx request object: model-catalog requests
            # contain the bearer token in their headers.
            raise SubscriptionAuthError(f"{operation} could not reach OpenAI.") from None
        if response.status_code < 200 or response.status_code >= 300:
            code: str | None = None
            with contextlib.suppress(ValueError):
                body = response.json()
                if isinstance(body, dict):
                    error = body.get("error")
                    if isinstance(error, str):
                        code = error
                    elif isinstance(error, dict) and isinstance(error.get("code"), str):
                        code = error["code"]
            raise _OAuthHttpError(operation, response.status_code, code)
        try:
            value = response.json()
        except ValueError as exc:
            raise SubscriptionAuthError(f"{operation} returned invalid data.") from exc
        if not isinstance(value, dict):
            raise SubscriptionAuthError(f"{operation} returned invalid data.")
        return value

    async def _openid_configuration(self) -> dict[str, Any]:
        if self._discovery is None:
            try:
                config = await self._json_request(
                    "OpenID discovery", "GET", DISCOVERY_ENDPOINT
                )
            except _OAuthHttpError as exc:
                raise SubscriptionAuthError(
                    f"OpenID discovery failed with HTTP {exc.status}."
                ) from exc
            if config.get("issuer") != ISSUER:
                raise SubscriptionAuthError("OpenID discovery returned an unexpected issuer.")
            for name in ("jwks_uri", "revocation_endpoint"):
                endpoint = config.get(name)
                if not isinstance(endpoint, str):
                    raise SubscriptionAuthError("OpenID discovery returned invalid data.")
                parsed = urlparse(endpoint)
                if parsed.scheme != "https" or parsed.hostname != "auth.openai.com":
                    raise SubscriptionAuthError("OpenID discovery returned an unsafe endpoint.")
            self._discovery = config
        return self._discovery

    async def _load_jwks(self, *, force: bool = False) -> dict[str, Any]:
        if self._jwks is None or force:
            config = await self._openid_configuration()
            try:
                self._jwks = await self._json_request(
                    "JWKS retrieval", "GET", config["jwks_uri"]
                )
            except _OAuthHttpError as exc:
                raise SubscriptionAuthError(
                    f"JWKS retrieval failed with HTTP {exc.status}."
                ) from exc
        return self._jwks

    async def _validate_id_token(
        self,
        id_token: str,
        *,
        client_id: str,
        expected_nonce: str | None,
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SubscriptionAuthError("The ID token was invalid.") from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise SubscriptionAuthError("The ID token used an unsupported signature.")

        selected: dict[str, Any] | None = None
        for force in (False, True):
            jwks = await self._load_jwks(force=force)
            keys = jwks.get("keys")
            if isinstance(keys, list):
                selected = next(
                    (
                        key
                        for key in keys
                        if isinstance(key, dict) and key.get("kid") == header["kid"]
                    ),
                    None,
                )
            if selected is not None:
                break
        if selected is None:
            raise SubscriptionAuthError("The ID token signing key was unavailable.")
        try:
            key = RSAAlgorithm.from_jwk(json.dumps(selected))
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=ISSUER,
                leeway=5,
                options={"require": ["sub", "exp", "iat", "iss", "aud"]},
            )
        except (jwt.PyJWTError, ValueError) as exc:
            raise SubscriptionAuthError("The ID token could not be verified.") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise SubscriptionAuthError("The ID token did not contain an identity.")
        if expected_nonce is not None:
            nonce = claims.get("nonce")
            if not isinstance(nonce, str) or not secrets.compare_digest(
                nonce, expected_nonce
            ):
                raise SubscriptionAuthError("The ID token nonce did not match.")
        return claims

    def _active_record(self, state: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        active = state.get("active_client_id")
        registrations = state.get("registrations")
        if not isinstance(active, str) or not isinstance(registrations, dict):
            raise SubscriptionAuthError("No ChatGPT subscription account is signed in.")
        record = registrations.get(active)
        if not isinstance(record, dict):
            raise SubscriptionAuthError("No ChatGPT subscription account is signed in.")
        return active, record

    def status(self) -> dict[str, Any]:
        with self._store.locked():
            state = self._store.load()
        try:
            client_id, record = self._active_record(state)
        except SubscriptionAuthError:
            return {
                "signed_in": False,
                "account_count": len(state["registrations"]),
                "accounts": self._safe_accounts(state),
            }
        signed_in = all(
            isinstance(record.get(name), str) and record[name]
            for name in ("access_token", "refresh_token", "id_token")
        )
        return {
            "signed_in": bool(signed_in),
            "client_id": client_id,
            "email": record.get("email") if isinstance(record.get("email"), str) else None,
            "scopes": list(record.get("scopes", [])),
            "account_count": len(state["registrations"]),
            "accounts": self._safe_accounts(state),
        }

    @staticmethod
    def _safe_accounts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        active = state.get("active_client_id")
        registrations = state.get("registrations")
        if not isinstance(registrations, dict):
            return []
        result: list[dict[str, Any]] = []
        for client_id, value in registrations.items():
            if not isinstance(client_id, str) or not isinstance(value, dict):
                continue
            result.append(
                {
                    "client_id": client_id,
                    "email": value.get("email")
                    if isinstance(value.get("email"), str)
                    else None,
                    "active": client_id == active,
                    "signed_in": all(
                        isinstance(value.get(name), str) and bool(value[name])
                        for name in ("access_token", "refresh_token", "id_token")
                    ),
                }
            )
        return result

    async def get_access_token(self) -> str:
        """Return a usable bearer token, rotating refresh credentials if needed."""

        # The lock covers reload, refresh, and atomic replacement so separate
        # runtime processes cannot race the single-use rotating refresh token.
        async with self._store.async_locked():
            state = self._store.load()
            client_id, record = self._active_record(state)
            access_token = _required_string(record, "access_token")
            expires_at = record.get("access_expires_at")
            if not isinstance(expires_at, (int, float)):
                raise SubscriptionAuthError("The saved access-token expiry is invalid.")
            now = self._clock()
            if now < float(expires_at) - _REFRESH_SKEW_SECONDS:
                return access_token
            earliest = _earliest_refresh_timestamp(record.get("earliest_refresh_at"))
            if earliest is not None and now < earliest:
                if now < float(expires_at):
                    return access_token
                raise SubscriptionAuthError("The access token cannot be refreshed yet.")
            await self._refresh_locked(state, client_id, record)
            refreshed = state["registrations"][client_id]
            return _required_string(refreshed, "access_token")

    async def _refresh_locked(
        self,
        state: dict[str, Any],
        client_id: str,
        record: dict[str, Any],
    ) -> None:
        refresh_token = _required_string(record, "refresh_token")
        try:
            response = await self._json_request(
                "Token refresh",
                "POST",
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "resource": RESOURCE,
                },
                headers={"Accept": "application/json"},
            )
        except _OAuthHttpError as exc:
            if exc.code in _UNUSABLE_REFRESH_ERRORS:
                for name in ("access_token", "refresh_token", "id_token"):
                    record.pop(name, None)
                self._store.save(state)
                raise SubscriptionAuthError(
                    "The ChatGPT session expired; sign in again."
                ) from exc
            raise SubscriptionAuthError(
                f"Token refresh failed with HTTP {exc.status}."
            ) from exc

        replacement = self._token_fields(response, fallback_scopes=record.get("scopes"))
        new_id_token = response.get("id_token")
        if new_id_token is not None:
            if not isinstance(new_id_token, str) or not new_id_token:
                raise SubscriptionAuthError("The refreshed ID token was invalid.")
            claims = await self._validate_id_token(
                new_id_token, client_id=client_id, expected_nonce=None
            )
            if not secrets.compare_digest(str(claims["sub"]), str(record.get("subject", ""))):
                raise SubscriptionAuthError("The refreshed account identity changed.")
            replacement["id_token"] = new_id_token
        record.update(replacement)
        record["saved_at"] = datetime.now(UTC).isoformat()
        self._store.save(state)

    def _token_fields(
        self,
        response: Mapping[str, Any],
        *,
        fallback_scopes: Any = None,
    ) -> dict[str, Any]:
        access_token = _required_string(response, "access_token")
        refresh_token = _required_string(response, "refresh_token")
        token_type = _required_string(response, "token_type")
        if token_type.casefold() != "bearer":
            raise SubscriptionAuthError("The authentication token type was unsupported.")
        expires_in = response.get("expires_in")
        if (
            not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise SubscriptionAuthError("The authentication token expiry was invalid.")
        scope_value = response.get("scope", fallback_scopes)
        scopes = _scopes(scope_value)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": float(expires_in),
            "access_expires_at": self._clock() + float(expires_in),
            "earliest_refresh_at": response.get("earliest_refresh_at"),
            "scopes": list(scopes),
        }

    async def list_models(self) -> list[SubscriptionModel]:
        """Return this account's displayable model catalog in server order."""

        token = await self.get_access_token()
        try:
            response = await self._json_request(
                "Model catalog",
                "GET",
                MODELS_ENDPOINT,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        except _OAuthHttpError as exc:
            raise SubscriptionAuthError(
                f"Model catalog failed with HTTP {exc.status}."
            ) from exc
        values = response.get("models")
        if not isinstance(values, list):
            raise SubscriptionAuthError("The model catalog returned invalid data.")
        models: list[SubscriptionModel] = []
        for value in values:
            if not isinstance(value, dict) or value.get("visibility") != "list":
                continue
            slug = value.get("slug")
            display_name = value.get("display_name")
            if isinstance(slug, str) and slug and isinstance(display_name, str) and display_name:
                models.append(SubscriptionModel(slug=slug, display_name=display_name))
        return models

    async def login(
        self,
        *,
        new_account: bool = False,
        client_id: str | None = None,
        timeout: float = 300.0,
    ) -> LoginResult:
        """Open the system browser and complete a loopback PKCE sign-in."""

        async with self._store.async_locked():
            state = self._store.load()
            host_id = self._store.host_id()
        if new_account and client_id is not None:
            raise SubscriptionAuthError(
                "Choose either a saved account or a new account, not both."
            )
        selected_id: str | None = None
        selected: dict[str, Any] | None = None
        if client_id is not None:
            candidate = state["registrations"].get(client_id)
            if not isinstance(candidate, dict):
                raise SubscriptionAuthError("The selected ChatGPT account is unknown.")
            selected_id, selected = client_id, candidate
        elif not new_account:
            with contextlib.suppress(SubscriptionAuthError):
                selected_id, selected = self._active_record(state)

        state_value = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        receiver = _LoopbackReceiver()
        try:
            client_id = selected_id or DYNAMIC_CLIENT_ID
            query: dict[str, str] = {
                "client_id": client_id,
                "ext_agent_host_id": host_id,
                "response_type": "code",
                "redirect_uri": receiver.redirect_uri,
                "scope": " ".join(SCOPES),
                "resource": RESOURCE,
                "state": state_value,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
            }
            if selected_id is None:
                query["agent_name_hint"] = APP_NAME
            elif selected is not None:
                id_hint = selected.get("id_token")
                email = selected.get("email")
                if isinstance(id_hint, str) and id_hint:
                    query["id_token_hint"] = id_hint
                if isinstance(email, str) and email:
                    query["login_hint"] = email
            authorization_url = f"{AUTHORIZATION_ENDPOINT}?{urlencode(query)}"
            try:
                opened = self._browser_opener(authorization_url)
            except Exception:
                # Browser-launch exceptions can retain the authorization URL,
                # which may contain an ID-token hint on reauthorization.
                raise SubscriptionAuthError("The system browser could not be opened.") from None
            if not opened:
                raise SubscriptionAuthError("The system browser could not be opened.")
            callback = await asyncio.to_thread(receiver.wait, timeout)
        finally:
            receiver.close()

        returned_state = callback.get("state")
        if not isinstance(returned_state, str) or not secrets.compare_digest(
            returned_state, state_value
        ):
            raise SubscriptionAuthError("The OAuth callback state did not match.")
        if callback.get("error"):
            raise SubscriptionAuthError("ChatGPT sign-in was not completed.")
        code = callback.get("code")
        if not isinstance(code, str) or not code:
            raise SubscriptionAuthError("The OAuth callback did not contain a code.")
        returned_client_id = callback.get("client_id")
        if selected_id is None:
            if (
                not isinstance(returned_client_id, str)
                or not returned_client_id
                or returned_client_id == DYNAMIC_CLIENT_ID
            ):
                raise SubscriptionAuthError("Dynamic client registration was incomplete.")
            issued_client_id = returned_client_id
        else:
            if returned_client_id is not None and returned_client_id != selected_id:
                raise SubscriptionAuthError("The OAuth callback client did not match.")
            issued_client_id = selected_id

        try:
            token_response = await self._json_request(
                "Authorization-code exchange",
                "POST",
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "client_id": issued_client_id,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": receiver.redirect_uri,
                    "resource": RESOURCE,
                },
                headers={"Accept": "application/json"},
            )
        except _OAuthHttpError as exc:
            raise SubscriptionAuthError(
                f"Authorization-code exchange failed with HTTP {exc.status}."
            ) from exc
        id_token = _required_string(token_response, "id_token")
        claims = await self._validate_id_token(
            id_token, client_id=issued_client_id, expected_nonce=nonce
        )
        if selected is not None and not secrets.compare_digest(
            str(claims["sub"]), str(selected.get("subject", ""))
        ):
            raise SubscriptionAuthError("The selected ChatGPT account did not match.")
        token_fields = self._token_fields(token_response)
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        record = {
            "email": email,
            "issuer": ISSUER,
            "subject": claims["sub"],
            "client_id": issued_client_id,
            "ext_agent_host_id": host_id,
            "id_token": id_token,
            "saved_at": datetime.now(UTC).isoformat(),
            **token_fields,
        }
        async with self._store.async_locked():
            current = self._store.load()
            registrations = current["registrations"]
            previous = registrations.get(issued_client_id)
            if isinstance(previous, dict) and previous.get("subject") != claims["sub"]:
                raise SubscriptionAuthError("The issued client identity changed.")
            registrations[issued_client_id] = record
            current["active_client_id"] = issued_client_id
            self._store.save(current)
        return LoginResult(
            client_id=issued_client_id,
            email=email,
            scopes=tuple(token_fields["scopes"]),
        )

    async def logout(self) -> LogoutResult:
        """Revoke the active renewable session and clear its local tokens."""

        async with self._store.async_locked():
            state = self._store.load()
            try:
                client_id, record = self._active_record(state)
            except SubscriptionAuthError:
                return LogoutResult(signed_out=True, revocation_confirmed=True)
            refresh_token = record.get("refresh_token")

            confirmed = not isinstance(refresh_token, str) or not refresh_token
            if not confirmed:
                try:
                    config = await self._openid_configuration()
                    async with self._client() as client:
                        response = await client.post(
                            config["revocation_endpoint"],
                            data={
                                "token": refresh_token,
                                "token_type_hint": "refresh_token",
                                "client_id": client_id,
                            },
                            headers={"Accept": "application/json"},
                        )
                    confirmed = response.status_code == 200
                except (httpx.HTTPError, SubscriptionAuthError):
                    confirmed = False

            registration = state["registrations"].get(client_id)
            if isinstance(registration, dict):
                for name in ("access_token", "refresh_token", "id_token"):
                    registration.pop(name, None)
            self._store.save(state)
        return LogoutResult(signed_out=True, revocation_confirmed=confirmed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ChatGPT subscription sign-in")
    parser.add_argument("--directory", type=Path, help="override the protected local store")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="sign in through the system browser")
    login.add_argument(
        "--new-account",
        action="store_true",
        help="register another ChatGPT account or workspace",
    )
    login.add_argument(
        "--client-id",
        help="reauthorize one of the client IDs shown by the status command",
    )
    commands.add_parser("status", help="show safe local sign-in metadata")
    commands.add_parser("models", help="list models available to the active account")
    commands.add_parser("logout", help="revoke and clear the active token set")
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    credentials = SubscriptionCredentials(args.directory)
    if args.command == "login":
        print("Opening the system browser for ChatGPT authorization...")
        result = await credentials.login(
            new_account=args.new_account,
            client_id=args.client_id,
        )
        print(
            json.dumps(
                {
                    "signed_in": True,
                    "client_id": result.client_id,
                    "email": result.email,
                    "scopes": list(result.scopes),
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "status":
        print(json.dumps(credentials.status(), ensure_ascii=False))
    elif args.command == "models":
        models = await credentials.list_models()
        print(json.dumps([asdict(model) for model in models], ensure_ascii=False))
    elif args.command == "logout":
        result = await credentials.logout()
        print(json.dumps(asdict(result), ensure_ascii=False))
        if not result.revocation_confirmed:
            print(
                "Local tokens were cleared, but remote revocation was not confirmed. "
                "Disconnect the app in ChatGPT Settings if needed."
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run_cli(args))
    except SubscriptionAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
