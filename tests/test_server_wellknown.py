"""End-to-end HTTP smoke: /health, /.well-known metadata, 401 with WWW-Authenticate.

Uses a real uvicorn server on a private port + urllib requests. No mocks of
the network layer.
"""
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(tmp_path):
    port = _free_port()
    users_file = tmp_path / "users.json"
    users_file.write_text(json.dumps([{
        "token": "test-token-1234567890",
        "account": {
            "name": "x", "user": "u@v.com", "password": "p",
            "smtp_host": "s", "imap_host": "i",
        },
    }]))
    env = {
        **os.environ,
        "EMAIL_USERS_FILE": str(users_file),
        "MCP_TRANSPORT": "http",
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "MCP_RESOURCE_URL": f"http://127.0.0.1:{port}",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "cpanel_mail_mcp"],
        env=env, cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("server never came up")
        yield port
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


def test_health(running_server):
    port = running_server
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/health").read()
    assert body == b"ok"


def test_protected_resource_metadata(running_server):
    port = running_server
    body = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource"
    ).read()
    doc = json.loads(body)
    assert doc["resource"] == f"http://127.0.0.1:{port}"
    assert doc["bearer_methods_supported"] == ["header"]


def test_path_suffixed_metadata_also_works(running_server):
    port = running_server
    body = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/.well-known/oauth-protected-resource/mcp"
    ).read()
    doc = json.loads(body)
    assert "resource" in doc


def test_mcp_without_auth_returns_401_with_resource_metadata(running_server):
    port = running_server
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp", method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        data=b"{}",
    )
    try:
        urllib.request.urlopen(req)
        pytest.fail("expected 401")
    except urllib.error.HTTPError as e:
        assert e.code == 401
        wa = e.headers.get("www-authenticate", "")
        assert "Bearer" in wa
        assert "resource_metadata=" in wa
        assert "/.well-known/oauth-protected-resource" in wa
