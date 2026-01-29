"""HTTP server integration tests."""

import pytest
import subprocess
import time
import signal
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

        # Use a unique port for testing
        port = 8123

        # Start server
        process = subprocess.Popen(
            [
                str(venv_python),
                "-m",
                "repl_mcp.repl_mcp_server",
                "--port",
                str(port),
                "--no-autoconnect",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

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
