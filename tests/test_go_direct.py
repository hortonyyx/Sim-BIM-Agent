"""Go-only physical routing: provider isolation, DNS/TLS, deadlines and socket order."""
import asyncio
import json
import socket
import ssl
import struct
from types import SimpleNamespace

import httpcore
import httpx
import pytest

from src.agent_runtime import go_direct as direct
from src.agent_runtime.adapter import HttpChatAdapter
from src.agent_runtime.connections import resolve_connection
from src.agent_runtime.opencode_go import BASE_URL, HttpGoAdapter


def test_physical_route_choice_is_dynamic_and_rejects_missing_or_ambiguous_routes():
    rows = [dict(interface_index=25, source_ip="192.168.10.4", metric=55),
            dict(interface_index=46, source_ip="10.2.0.7", metric=10)]
    assert direct.select_route(rows) == dict(interface_index=46, source_ip="10.2.0.7")
    with pytest.raises(httpcore.ConnectError, match="no active"):
        direct.select_route([])
    with pytest.raises(httpcore.ConnectError, match="ambiguous"):
        direct.select_route([rows[1], {**rows[0], "metric":10}])
    with pytest.raises(httpcore.ConnectError, match="usable"):
        direct.select_route([dict(interface_index=3, source_ip="127.0.0.1", metric=1)])


@pytest.mark.parametrize("address", ["198.18.0.68", "127.0.0.1", "10.0.0.3", "2001:2::50", "224.1.1.1", "bad"])
def test_dns_refuses_fake_local_multicast_or_non_ipv4(address):
    with pytest.raises(ValueError, match="public IPv4"):
        direct.public_ipv4(address)


def test_only_go_ignores_proxy_and_resolves_bootstrap_from_explicit_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:43210")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("OPENCODE_GO_DNS_BOOTSTRAP_IPV4", "9.9.9.9")
    path=tmp_path/"go.env"
    path.write_text(f"OPENCODE_GO_BASE_URL={BASE_URL}\nOPENCODE_GO_API_KEY=private\nOPENCODE_GO_DNS_BOOTSTRAP_IPV4=104.16.248.249\n")
    connection=resolve_connection("opencode-go",path,model="glm-5.3-flash")
    async def scenario():
        go=connection.create_adapter(session_id="cb6130de-46b0-4f0c-9baa-cb8fc04892d9")
        other=HttpChatAdapter(base_url="https://llmapi.paratera.com/v1",api_key="test")
        try:
            assert go.client._transport_for_url(httpx.URL(go.endpoint)) is go.client._transport
            assert go.client._transport._pool._network_backend.bootstrap == "104.16.248.249"
            assert other.client._transport_for_url(httpx.URL(other.endpoint)) is not other.client._transport
            assert go.client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert go.client._transport._pool._ssl_context.check_hostname
        finally:
            await go.close(); await other.close()
    asyncio.run(scenario())


def test_direct_socket_selects_interface_before_connect_and_closes_on_cancel(monkeypatch):
    calls=[]
    class Sock:
        def setsockopt(self,*args): calls.append(("option",*args))
        def bind(self,value): calls.append(("bind",value))
        def setblocking(self,value): pass
        def getsockopt(self,*args): return 46
        def close(self): calls.append(("close",))
    async def scenario():
        # Patch only inside the running loop so asyncio can create its own socketpair.
        monkeypatch.setattr(direct.socket,"socket",lambda *a,**kw:Sock())
        async def connect(sock,address):
            calls.append(("connect",address))
            assert calls[0] == ("option",socket.IPPROTO_IP,31,struct.pack("!I",46))
            assert ("bind",("10.2.0.7",0)) in calls
            raise asyncio.CancelledError()
        monkeypatch.setattr(asyncio.get_running_loop(),"sock_connect",connect)
        with pytest.raises(asyncio.CancelledError):
            await direct.open_socket(dict(interface_index=46,source_ip="10.2.0.7"),"opencode.ai","172.65.90.21",443,lambda _:None)
        assert calls[-1] == ("close",)
    asyncio.run(scenario())


def test_go_transport_preserves_target_host_tls_and_wire():
    seen=[]
    class Stream(httpcore.AsyncMockStream):
        async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
            seen.append(("tls",server_hostname))
            return self
        async def write(self,buffer,timeout=None):
            seen.append(("write",buffer))
    class Backend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self,host,port,**kw):
            seen.append(("host",host,port))
            return Stream([b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{"ok":true}'])
    async def scenario():
        adapter=HttpGoAdapter(base_url="https://opencode.ai/inference/go/openai/v1",api_key="test",
            protocol="openai_chat_completions",session_id="cb6130de-46b0-4f0c-9baa-cb8fc04892d9",
            transport=direct._transport(Backend()))
        wire=b'{"messages":[],"stream":false}'
        try: assert await adapter.send(SimpleNamespace(wire_bytes=wire),timeout=2) == {"ok":True}
        finally: await adapter.close()
        assert ("host","opencode.ai",443) in seen and ("tls","opencode.ai") in seen
        assert ("write",wire) in seen
        assert any(b"Host: opencode.ai\r\n" in r[1] for r in seen if r[0]=="write")
    asyncio.run(scenario())


def test_dns_uses_bound_resolver_without_auth_and_validates_answer(monkeypatch):
    calls=[]
    def handler(request):
        assert request.url.host == "cloudflare-dns.com"
        assert request.url.params["name"] == "opencode.ai"
        assert "authorization" not in request.headers and "x-opencode-session" not in request.headers
        return httpx.Response(200,json={"Status":0,"Answer":[{"type":1,"data":"172.65.90.21","TTL":60}]})
    def transport(backend):
        assert backend.route == dict(interface_index=46,source_ip="10.2.0.7")
        assert backend.address == "104.16.248.249"
        calls.append(backend)
        return httpx.MockTransport(handler)
    monkeypatch.setattr(direct,"_transport",transport)
    async def scenario():
        backend=direct.GoDirectBackend("104.16.248.249",lambda _:None)
        backend.route=dict(interface_index=46,source_ip="10.2.0.7")
        await backend.resolve()
        assert backend.address == "172.65.90.21" and backend.expires > direct.time.monotonic()
    asyncio.run(scenario())
    assert len(calls)==1


def test_dns_deadline_and_error_never_dial_or_fall_back(monkeypatch):
    async def route(): return dict(interface_index=46,source_ip="10.2.0.7")
    async def forbidden(*a,**k): raise AssertionError("must not dial after failed DNS")
    monkeypatch.setattr(direct,"discover_route",route)
    monkeypatch.setattr(direct,"open_socket",forbidden)
    async def scenario():
        backend=direct.GoDirectBackend("104.16.248.249",lambda _:None)
        async def slow(): await asyncio.sleep(1)
        backend.resolve=slow
        with pytest.raises(httpcore.ConnectTimeout):
            await backend.connect_tcp("opencode.ai",443,timeout=.01)
        async def bad(): raise ValueError("invalid resolver response")
        backend.resolve=bad
        with pytest.raises(httpcore.ConnectError,match="no proxy fallback"):
            await backend.connect_tcp("opencode.ai",443,timeout=1)
        with pytest.raises(httpcore.ConnectError,match="unreviewed"):
            await backend.connect_tcp("another-service.invalid",443,timeout=1)
    asyncio.run(scenario())


@pytest.mark.parametrize("data", [[], {"Status":0,"Answer":{}}, {"Status":0,"Answer":None},
    {"Status":0,"Answer":[None]}, {"Status":0,"Answer":[{"type":1,"data":"172.65.90.21","TTL":None}]}])
def test_malformed_dns_shape_is_a_connection_error(monkeypatch,data):
    monkeypatch.setattr(direct,"_transport",lambda _:httpx.MockTransport(lambda r:httpx.Response(200,json=data)))
    async def scenario():
        backend=direct.GoDirectBackend("104.16.248.249",lambda _:None)
        backend.route=dict(interface_index=46,source_ip="10.2.0.7")
        with pytest.raises(httpcore.ConnectError): await backend.resolve()
    asyncio.run(scenario())
