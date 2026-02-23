"""HTTP server integration tests."""

import pytest
import subprocess
import time
import signal
import socket
import uuid
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


@pytest.mark.skipif(not HAS_REQUESTS, reason="requests not installed")
class TestHTTPServer:
    """Test HTTP server functionality."""

    @pytest.fixture
    def http_server(self):
        """Start HTTP server for testing."""
        venv_python = Path(__file__).parent.parent / ".venv" / "bin" / "python3"

        def get_free_port() -> int:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return int(s.getsockname()[1])

        process = None
        port = None

        # Start server (retry in case the selected port is taken by the time we spawn)
        for _ in range(5):
            port = get_free_port()
            process = subprocess.Popen(
                [
                    str(venv_python),
                    "-m",
                    "repl_mcp.repl_mcp_server",
                    "--transport",
                    "sse",
                    "--port",
                    str(port),
                    "--no-autoconnect",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.1)
            if process.poll() is None:
                break
            # If we crashed immediately, try another port.
            process.terminate()
            process.wait(timeout=2)
            process = None
            port = None

        if process is None or port is None:
            pytest.fail("Server failed to start after multiple port attempts")

        # Wait for server to start
        max_wait = 5
        start_time = time.time()
        server_ready = False

        while time.time() - start_time < max_wait:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"Server failed to start: {stderr}")

            try:
                response = requests.get(
                    f"http://localhost:{port}/sse", stream=True, timeout=1
                )
                if response.status_code == 200:
                    server_ready = True
                    break
            except requests.RequestException:
                time.sleep(0.5)

        if not server_ready:
            process.terminate()
            process.wait()
            pytest.fail("Server did not start in time")

        yield f"http://localhost:{port}"

        # Cleanup
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def test_sse_endpoint_available(self, http_server):
        """Test SSE endpoint is available."""
        response = requests.get(f"{http_server}/sse", stream=True, timeout=5)
        assert response.status_code == 200

    def test_server_responds(self, http_server):
        """Test server responds to requests."""
        # Just verify the endpoint exists
        response = requests.get(f"{http_server}/sse", stream=True, timeout=5)
        assert response.ok

    def test_bulk_upload_endpoint(self, http_server):
        """Test control-plane bulk upload endpoint."""
        file_rel_path = f"tmp/http-upload-{uuid.uuid4().hex}.txt"
        file_abs_path = Path.cwd() / file_rel_path

        payload = {
            "files": [
                {
                    "path": file_rel_path,
                    "content": "hello from bulk upload endpoint",
                    "is_base64": False,
                }
            ]
        }
        response = requests.post(
            f"{http_server}/api/sessions/test-chat/uploads/bulk",
            json=payload,
            timeout=5,
        )
        assert response.status_code == 200

        data = response.json()
        assert data["uploaded"] == 1
        assert data["total"] == 1
        assert data["results"][0]["status"] == "ok"

        assert file_abs_path.exists()
        assert file_abs_path.read_text(encoding="utf-8") == "hello from bulk upload endpoint"

        # Cleanup test artifact
        file_abs_path.unlink(missing_ok=True)
