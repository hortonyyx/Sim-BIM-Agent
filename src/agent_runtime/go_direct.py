"""Windows physical egress for Go only, including DNS; never falls back to a proxy.

No route/adapter/proxy settings are mutated. HTTP Host and TLS SNI remain the
original names. Socket selection precedes connect (httpcore socket_options do not).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import time

import anyio.abc
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOStream

DNS_HOST = "cloudflare-dns.com"
DNS_URL = "https://cloudflare-dns.com/dns-query"
DEFAULT_DNS_BOOTSTRAP = "1.1.1.1"
IP_UNICAST_IF = 31

# Enumerate current hardware adapters and their usable IPv4 default routes.
# UTF-8 is explicit because Windows PowerShell otherwise uses a local code page.
_ROUTE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$rows = @(foreach ($adapter in Get-NetAdapter -Physical | Where-Object Status -eq 'Up') {
    $routes = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
    if ($routes.Count -eq 0) { continue }
    $iface = Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4
    $address = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 |
        Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource } |
        Select-Object -First 1
    if ($null -eq $address) { continue }
    $routeMetric = ($routes | Measure-Object RouteMetric -Minimum).Minimum
    [pscustomobject]@{interface_index=[int]$adapter.ifIndex; source_ip=$address.IPAddress;
        metric=[int]($routeMetric + $iface.InterfaceMetric)}
})
ConvertTo-Json -InputObject $rows -Compress
"""


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError("Go direct DNS requires a public IPv4 address") from None
    if not address.is_global or address.is_multicast:
        raise ValueError("Go direct DNS requires a public IPv4 address")
    return str(address)


def select_route(rows):
    if not isinstance(rows, list) or not rows:
        raise httpcore.ConnectError("Go direct: no active physical IPv4 default route")
    best = min(int(row["metric"]) for row in rows)
    candidates = [row for row in rows if int(row["metric"]) == best]
    if len(candidates) != 1:
        raise httpcore.ConnectError("Go direct: ambiguous physical routes with equal metrics")
    row = candidates[0]
    address = ipaddress.IPv4Address(row["source_ip"])
    if address.is_unspecified or address.is_loopback or address.is_link_local or int(row["interface_index"]) <= 0:
        raise httpcore.ConnectError("Go direct: physical route has no usable IPv4 source")
    return {"interface_index": int(row["interface_index"]), "source_ip": str(address)}


async def discover_route():
    if sys.platform != "win32":
        raise httpcore.ConnectError("Go physical direct currently requires Windows; proxy fallback is disabled")
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    process = await asyncio.create_subprocess_exec(executable, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-Command", _ROUTE_SCRIPT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        async with asyncio.timeout(10):
            stdout, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode != 0:
        raise httpcore.ConnectError("Go direct: physical route discovery failed")
    try:
        return select_route(json.loads(stdout.decode("utf-8-sig")))
    except (ValueError, KeyError, TypeError) as exc:
        raise httpcore.ConnectError("Go direct: invalid physical route discovery result") from exc


async def open_socket(route, host, address, port, record):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", route["interface_index"]))
        sock.bind((route["source_ip"], 0))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setblocking(False)
        if sock.getsockopt(socket.IPPROTO_IP, IP_UNICAST_IF) != route["interface_index"]:
            raise httpcore.ConnectError("Go direct: physical interface selection was not applied")
        await asyncio.get_running_loop().sock_connect(sock, (address, port))
        record({"host": host, "interface_index": route["interface_index"],
                "local_address": sock.getsockname(), "peer_address": sock.getpeername()})
        return AnyIOStream(await anyio.abc.SocketStream.from_socket(sock))
    except BaseException:
        sock.close()
        raise


class _ResolverBackend(httpcore.AnyIOBackend):
    def __init__(self, route, address, record):
        self.route, self.address, self.record = route, address, record

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != DNS_HOST or port != 443:
            raise httpcore.ConnectError("Go direct: unreviewed DNS endpoint")
        try:
            async with asyncio.timeout(timeout):
                return await open_socket(self.route, host, self.address, port, self.record)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Go direct: DNS connection timed out") from exc
        except OSError as exc:
            raise httpcore.ConnectError("Go direct: DNS connection failed") from exc


class GoDirectBackend(httpcore.AnyIOBackend):
    def __init__(self, bootstrap_ipv4, record):
        self.bootstrap = public_ipv4(bootstrap_ipv4)
        self.record = record
        self.route = None
        self.address = None
        self.expires = 0.
        self.lock = asyncio.Lock()

    async def resolve(self):
        # This HTTPS client has no credentials or environment proxy. Its socket
        # uses the same physical interface as inference and does not call OS DNS.
        pool = _transport(_ResolverBackend(self.route, self.bootstrap, self.record))
        async with httpx.AsyncClient(transport=pool, trust_env=False, follow_redirects=False, timeout=10) as client:
            response = await client.get(DNS_URL, params={"name": "opencode.ai", "type": "A"},
                                        headers={"Accept": "application/dns-json"})
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or data.get("Status") != 0:
            raise httpcore.ConnectError("Go direct: DNS returned no usable answer")
        records = data.get("Answer", [])
        if not isinstance(records, list) or any(not isinstance(a, dict) for a in records):
            raise httpcore.ConnectError("Go direct: invalid DNS answer structure")
        answers = [a for a in records if a.get("type") == 1]
        if not answers:
            raise httpcore.ConnectError("Go direct: DNS returned no IPv4 answer")
        if (not isinstance(answers[0].get("data"), str)
                or type(answers[0].get("TTL")) is not int or answers[0]["TTL"] < 0):
            raise httpcore.ConnectError("Go direct: invalid DNS IPv4 answer")
        self.address = public_ipv4(answers[0]["data"])
        self.expires = time.monotonic() + max(0, min(300, int(answers[0]["TTL"])))

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != "opencode.ai" or port != 443:
            raise httpcore.ConnectError("Go direct: unreviewed inference endpoint")
        try:
            # Discovery, DNS (including any lock wait) and connect share the
            # original connect budget; the runtime also retains its total cap.
            async with asyncio.timeout(timeout):
                async with self.lock:
                    if self.route is None:
                        self.route = await discover_route()
                    if self.address is None or time.monotonic() >= self.expires:
                        await self.resolve()
                return await open_socket(self.route, host, self.address, port, self.record)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout("Go direct: route, DNS or connect timed out") from exc
        except (OSError, ValueError, KeyError, httpx.HTTPError) as exc:
            raise httpcore.ConnectError("Go direct: route, DNS or connection failed; no proxy fallback") from exc


def _transport(backend):
    # HTTPX's standard request/response/error mapping is retained. The only
    # replaced component is its HTTPcore pool, whose backend is a public API.
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    transport._pool = httpcore.AsyncConnectionPool(network_backend=backend,
        ssl_context=httpx.create_ssl_context(trust_env=False), http1=True, http2=False,
        retries=0, max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.)
    return transport


def direct_transport(bootstrap_ipv4=DEFAULT_DNS_BOOTSTRAP):
    evidence = []
    def record(value):
        evidence.append(value)
    transport = _transport(GoDirectBackend(bootstrap_ipv4, record))
    transport.network_evidence = evidence
    return transport
