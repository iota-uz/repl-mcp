"""Hermetic native HTTP/OAuth broker acceptance; no real accounts or credentials."""
import base64
import hashlib
import json
import os
import queue
import subprocess
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from tests.test_native_protocol import NativeClient, binary as native_binary_fixture

binary = native_binary_fixture


@pytest.fixture
def oauth_peer():
    state = {"grants": [], "effects": 0, "tokens": 0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, value, status=200, headers=None, omit_length=False):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if not omit_length:
                self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path.startswith("/.well-known/oauth-protected-resource"):
                return self.reply({"resource": origin + "/mcp", "authorization_servers": [origin],
                                   "scopes_supported": ["tools"]})
            if self.path.startswith("/.well-known/oauth-authorization-server"):
                return self.reply({"issuer": origin, "authorization_endpoint": origin + "/authorize",
                                   "token_endpoint": origin + "/token", "registration_endpoint": origin + "/register",
                                   "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
                                   "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
                                   "code_challenge_methods_supported": ["S256"], "scopes_supported": ["tools"]})
            if self.path.startswith("/authorize?"):
                args = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                state["challenge"] = args["code_challenge"][0]
                state["redirect"] = args["redirect_uri"][0]
                state["csrf"] = args["state"][0]
                callback = args["redirect_uri"][0] + "?" + urllib.parse.urlencode({
                    "code": "hermetic-test-code", "state": "wrong-csrf" if state.get("reject") == "state" else args["state"][0],
                    "iss": "https://wrong-issuer.invalid" if state.get("reject") == "issuer" else origin})
                return self.reply({}, 302, {"Location": callback})
            self.reply({}, 405)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/register":
                args = json.loads(body)
                return self.reply({"client_id": "hermetic-public-client", "redirect_uris": args["redirect_uris"],
                                   "token_endpoint_auth_method": "none"})
            if self.path == "/token":
                args = urllib.parse.parse_qs(body.decode())
                grant = args["grant_type"][0]
                state["grants"].append(grant)
                assert args.get("resource") == [origin + "/mcp"]
                if grant == "authorization_code":
                    verifier = args["code_verifier"][0].encode()
                    assert base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=").decode() == state["challenge"]
                state["tokens"] += 1
                return self.reply({"access_token": f"hermetic-access-{state['tokens']}", "token_type": "Bearer",
                                   "expires_in": 3600, "scope": "tools", "refresh_token": f"hermetic-refresh-{state['tokens']}"})
            request = json.loads(body)
            if "id" not in request:
                return self.reply({}, 202)
            method, params = request["method"], request.get("params", {})
            if method == "server/discover":
                return self.reply({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "legacy"}})
            if method == "initialize":
                state["initializations"] = state.get("initializations", 0) + 1
                result = {"protocolVersion": params.get("protocolVersion", "2025-11-25"), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "hermetic-http", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "inputSchema": {"type": "object", "additionalProperties": False}}
                                    for name in ["echo", "oversized", "delivery_large"]]}
            elif method == "tools/call":
                assert self.headers.get("Authorization", "").startswith("Bearer hermetic-access-")
                if state.pop("protocol_error_once", False):
                    return self.reply({"jsonrpc":"2.0", "id":request["id"],
                                       "error":{"code":-32602,"message":"hermetic argument rejection"}})
                state["effects"] += 1
                text = "x" * 1100000 if params["name"] == "oversized" else "x" * 950000 if params["name"] == "delivery_large" else "http works"
                result = {"content": [{"type": "text", "text": text}],
                          "structuredContent": {"ok": True}, "isError": False}
            else:
                result = {}
            result["resultType"] = "complete"
            self.reply({"jsonrpc": "2.0", "id": request["id"], "result": result}, headers={"Mcp-Session-Id": "hermetic"},
                       omit_length=state.get("stream_response", False) and method == "tools/call" and params["name"] == "oversized")

        def do_DELETE(self):
            self.reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/mcp", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def registry(path, url, oauth):
    path.write_text(json.dumps({"mcpServers": {"http": {"type": "http", "url": url, "oauth": oauth}}}))
    return path


@pytest.mark.parametrize("stream_response", [False, True])
def test_client_credentials_and_bounded_http(binary, tmp_path, oauth_peer, stream_response):
    url, state = oauth_peer
    state["stream_response"] = stream_response
    config = registry(tmp_path / "broker.json", url, {"grantType": "client_credentials", "clientId": "hermetic-client", "clientSecret": "hermetic-secret", "scopes": ["tools"]})
    client = NativeClient(binary, tmp_path, config)
    try:
        result = client.execute("mcp.call('http', 'echo')")
        assert not result["isError"], result
        assert state["grants"] == ["client_credentials"]
        result = client.execute("mcp.call('http', 'oversized')")
        assert result["isError"], result
        assert state["effects"] == 2  # No automatic write retry after a lost/oversized response.
    finally:
        client.close()


def test_browser_pkce_private_store_and_reuse(binary, tmp_path, oauth_peer):
    url, state = oauth_peer
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    credentials = private / "broker-token.json"
    config = registry(tmp_path / "broker.json", url, {"credentialFile": str(credentials), "scopes": ["tools"]})
    process = subprocess.Popen([str(binary), "--config", str(config), "--oauth-login", "http"],
                               cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stderr], daemon=True)
    reader.start()
    try:
        auth_url = None
        for _ in range(4):
            line = lines.get(timeout=15)
            if line.startswith("http"):
                auth_url = line.strip()
                break
        assert auth_url is not None
        assert "code_challenge_method=S256" in auth_url
        with urllib.request.urlopen(auth_url, timeout=10) as response:
            assert response.status == 200
        assert process.wait(timeout=15) == 0
        assert credentials.stat().st_mode & 0o777 == 0o600
        assert state["grants"] == ["authorization_code"]
        client = NativeClient(binary, tmp_path, config)
        try:
            result = client.execute("mcp.call('http', 'echo')")
            assert not result["isError"], result
            assert state["grants"] == ["authorization_code"]
        finally:
            client.close()
        saved = json.loads(credentials.read_text())
        saved["token_received_at"] = 1
        credentials.write_text(json.dumps(saved))
        client = NativeClient(binary, tmp_path, config)
        try:
            assert not client.execute("mcp.call('http', 'echo')")["isError"]
            assert state["grants"] == ["authorization_code", "refresh_token"]
            assert json.loads(credentials.read_text())["token_response"]["refresh_token"] == "hermetic-refresh-2"
        finally:
            client.close()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        reader.join(timeout=2)


def test_oauth_rejects_public_store_directory(binary, tmp_path, oauth_peer):
    url, _ = oauth_peer
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    config = registry(tmp_path / "broker.json", url, {"credentialFile": str(public / "token.json")})
    result = subprocess.run([str(binary), "--config", str(config), "--oauth-login", "http"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "0700" in result.stderr


@pytest.mark.parametrize("rejected", ["state", "issuer"])
def test_browser_rejects_tampered_authorization(binary, tmp_path, oauth_peer, rejected):
    url, state = oauth_peer
    state["reject"] = rejected
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    credentials = private / "token.json"
    config = registry(tmp_path / "broker.json", url, {"credentialFile": str(credentials)})
    process = subprocess.Popen([str(binary), "--config", str(config), "--oauth-login", "http"],
                               cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stderr], daemon=True)
    reader.start()
    try:
        lines.get(timeout=15)
        auth_url = lines.get(timeout=15).strip()
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(auth_url, timeout=10)
        assert error.value.code == 400
        assert process.wait(timeout=15) != 0
        assert state["grants"] == []
        assert not credentials.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        reader.join(timeout=2)


def test_durable_journal_contains_metadata_and_recovers_unknown(binary, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "registry.json"
    config.write_text(json.dumps({"mcpServers": {"fixture": {
        "command": os.sys.executable, "args": [str(root / "tests/fixtures/native_mcp_fixture.py")]
    }}}))
    journal = tmp_path / "journal.json"
    client = NativeClient(binary, tmp_path, config, extra_args=["--journal", str(journal)])
    try:
        locked = subprocess.run([str(binary), "--mcp-scope", "none", "--journal", str(journal)],
                                cwd=tmp_path, input="", capture_output=True, text=True, timeout=10)
        assert locked.returncode != 0
        assert "owned by another runtime" in locked.stderr
        result = client.execute("mcp.call('fixture', 'echo', text='payload-is-not-journal-metadata')")
        assert not result["isError"], result
        records = json.loads(journal.read_text())
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert "payload-is-not-journal-metadata" not in journal.read_text()
        assert journal.stat().st_mode & 0o777 == 0o600
        result = client.execute("mcp.call('fixture', 'slow_write', marker='never-written', timeout=.05)")
        assert result["isError"]
        assert json.loads(journal.read_text())[-1]["status"] == "outcome_unknown"
    finally:
        client.close()
    records[-1]["status"] = "dispatched"
    journal.write_text(json.dumps(records))
    recovered = NativeClient(binary, tmp_path, config, extra_args=["--journal", str(journal)])
    try:
        assert json.loads(journal.read_text())[-1]["status"] == "outcome_unknown"
        assert not (tmp_path / "never-written").exists()
    finally:
        recovered.close()


def test_bad_registry_does_not_break_python_or_health_and_can_recover(binary, tmp_path):
    config = tmp_path / "invalid.json"
    config.write_text(json.dumps({"mcpServers": {"bad": {"type": "http", "url": "https://redacted.invalid",
                                                           "headers": {"Authorization": "Bearer "}}}}))
    client = NativeClient(binary, tmp_path, config)
    try:
        assert client.execute("2 + 2")["structuredContent"]["value"] == 4
        health = client.request("tools/call", {"name": "python_health", "arguments": {}})["structuredContent"]
        assert "authorization" in health["broker"]["configuration_error"]
        assert client.execute("mcp.servers()")["isError"]
        config.write_text(json.dumps({"mcpServers": {}}))
        assert not client.execute("mcp.refresh()")["isError"]
        health = client.request("tools/call", {"name": "python_health", "arguments": {}})["structuredContent"]
        assert health["broker"]["configuration_error"] is None
    finally:
        client.close()


def test_oversized_stdio_catalogue_fails_but_native_server_survives(binary, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"mcpServers": {"oversized": {"command": os.sys.executable,
                    "args": [str(root / "tests/fixtures/broker_oversized_stdio.py")]}}}))
    client = NativeClient(binary, tmp_path, config)
    try:
        assert client.execute("mcp.list_tools('oversized')", timeout=5)["isError"]
        assert client.execute("42")["structuredContent"]["value"] == 42
    finally:
        client.close()


def test_aliases_share_transport_but_keep_distinct_tool_policy(binary, tmp_path):
    root = Path(__file__).resolve().parents[1]
    transport = {"command": os.sys.executable, "args": [str(root / "tests/fixtures/native_mcp_fixture.py")]}
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"mcpServers": {
        "read_alias": {**transport, "allowedTools": ["echo"]},
        "second_alias": {**transport, "allowedTools": ["multi"], "brokerAllowed": True},
    }}))
    client = NativeClient(binary, tmp_path, config)
    try:
        assert not client.execute("mcp.call('read_alias', 'echo')")["isError"]
        assert not client.execute("mcp.call('second_alias', 'multi')")["isError"]
        assert client.execute("mcp.call('read_alias', 'multi')")["isError"]
        health = client.request("tools/call", {"name": "python_health", "arguments": {}})["structuredContent"]
        assert health["broker"]["connected_transports"] == 1
    finally:
        client.close()


def test_protocol_error_does_not_destroy_healthy_shared_http_transport(binary, tmp_path, oauth_peer):
    url, state = oauth_peer
    state["protocol_error_once"] = True
    config = registry(tmp_path / "broker.json", url,
                      {"grantType":"client_credentials", "clientId":"hermetic-client", "clientSecret":"hermetic-secret"})
    client = NativeClient(binary, tmp_path, config)
    try:
        assert client.execute("mcp.call('http', 'echo')")["isError"]
        assert not client.execute("mcp.call('http', 'echo')")["isError"]
        assert state["initializations"] == 1, "Protocol rejection unnecessarily reconnected the shared transport"
        journal = client.execute("mcp.journal()")["structuredContent"]["value"]
        assert journal[0]["status"] == "outcome_unknown"
        assert journal[1]["status"] == "completed"
        assert journal[0]["session_id"] == journal[1]["session_id"]
    finally:
        client.close()


def test_large_known_result_reports_delivery_failure_without_retry_or_losing_completion(binary, tmp_path, oauth_peer):
    url, state = oauth_peer
    config = registry(tmp_path / "broker.json", url,
                      {"grantType":"client_credentials", "clientId":"hermetic-client", "clientSecret":"hermetic-secret"})
    client = NativeClient(binary, tmp_path, config)
    try:
        failed = client.execute("mcp.call('http', 'delivery_large')")
        assert failed["isError"] and "known outcome" in failed["structuredContent"]["error"], failed
        assert state["effects"] == 1
        journal = client.execute("mcp.journal()")["structuredContent"]["value"]
        assert journal[-1]["status"] == "completed", journal
        assert journal[-1]["id"] in failed["structuredContent"]["error"]
        assert not client.execute("mcp.call('http', 'echo')")["isError"]
        assert state["initializations"] == 1 and state["effects"] == 2
    finally:
        client.close()


def test_cancelling_one_call_keeps_shared_peer_and_other_effect_owned(binary, tmp_path):
    root = Path(__file__).resolve().parents[1]
    config = tmp_path / "broker.json"
    config.write_text(json.dumps({"mcpServers":{"peer":{"command":os.sys.executable,
                    "args":[str(root / "tests/fixtures/native_mcp_fixture.py")]}}}))
    cancelled_marker, survivor_marker = tmp_path / "cancelled", tmp_path / "survivor"
    client = NativeClient(binary, tmp_path, config)
    try:
        code = f"""import asyncio
first = asyncio.create_task(mcp.acall('peer', 'slow_write', arguments={{'marker': {str(cancelled_marker)!r}}}))
second = asyncio.create_task(mcp.acall('peer', 'slow_write', arguments={{'marker': {str(survivor_marker)!r}}}))
for _ in range(30):
    if len([entry for entry in mcp.journal() if entry['status'] == 'dispatched']) >= 2:
        break
    await asyncio.sleep(.02)
else:
    raise AssertionError('Both requests never reached dispatch')
first.cancel()
await asyncio.gather(first, return_exceptions=True)
await second
mcp.journal()
"""
        result = client.execute(code)
        assert not result["isError"], result
        assert not cancelled_marker.exists() and survivor_marker.read_text() == "committed"
        journal = result["structuredContent"]["value"]
        assert sorted(entry["status"] for entry in journal) == ["completed", "outcome_unknown"]
        assert len({entry["run_id"] for entry in journal}) == 1
        assert len({entry["session_id"] for entry in journal}) == 1
        assert not client.execute("mcp.call('peer', 'echo')")["isError"]
        assert client.request("tools/call", {"name":"python_health", "arguments":{}})["structuredContent"]["broker"]["connected_transports"] == 1
    finally:
        client.close()
