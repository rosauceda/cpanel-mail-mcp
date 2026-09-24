"""Multi-user HTTP mode against a real uvicorn process.

Covers per-request account binding, session ownership, and users.json
changes (rotate/add/remove) taking effect without a restart.
"""
import json
import os
import subprocess
import sys
import time

import httpx
import pytest

from .test_server_wellknown import ROOT, _free_port

HDRS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _user(token: str, name: str) -> dict:
    return {"token": token, "account": {"name": name, "user": f"{name}@example.com",
                                        "password": "x", "host": "imap.invalid"}}


@pytest.fixture
def srv(tmp_path):
    port = _free_port()
    users_file = tmp_path / "users.json"
    users_file.write_text(json.dumps([_user("tok-alice-000000000000", "alice"),
                                      _user("tok-bob-00000000000000", "bob")]))
    env = {**os.environ, "EMAIL_USERS_FILE": str(users_file), "MCP_TRANSPORT": "http",
           "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "EMAIL_ENV_FILE": "/nonexistent"}
    for k in ("CPANEL_USER", "CPANEL_PASS", "EMAIL_ACCOUNTS_JSON", "EMAIL_ACCOUNTS_FILE"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "cpanel_mail_mcp"], env=env, cwd=str(ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                httpx.get(f"{base}/health", timeout=0.5)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        yield base, users_file
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _post(base, token, body, sid=None):
    h = dict(HDRS, authorization=f"Bearer {token}")
    if sid:
        h["mcp-session-id"] = sid
    return httpx.post(f"{base}/mcp", headers=h, json=body, timeout=10)


def _session(base, token) -> str:
    r = _post(base, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"}}})
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    _post(base, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def _whoami(base, token, sid):
    r = _post(base, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "list_accounts", "arguments": {}}}, sid)
    if r.status_code != 200:
        return r.status_code
    for line in r.text.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            return [a["user"] for a in d["result"]["structuredContent"]["accounts"]]
    return r.text


def test_each_user_sees_only_their_account(srv):
    base, _ = srv
    sa, sb = _session(base, "tok-alice-000000000000"), _session(base, "tok-bob-00000000000000")
    assert _whoami(base, "tok-alice-000000000000", sa) == ["alice@example.com"]
    assert _whoami(base, "tok-bob-00000000000000", sb) == ["bob@example.com"]


def test_session_cannot_be_used_by_another_user(srv):
    base, _ = srv
    sa = _session(base, "tok-alice-000000000000")
    assert _whoami(base, "tok-bob-00000000000000", sa) == 404


def test_rotate_add_remove_apply_without_restart(srv):
    base, users_file = srv
    sa = _session(base, "tok-alice-000000000000")
    data = json.loads(users_file.read_text())
    data[0]["token"] = "tok-alice-rotated-000000"
    data.append(_user("tok-carol-000000000000", "carol"))
    users_file.write_text(json.dumps(data))

    assert _whoami(base, "tok-alice-000000000000", sa) == 401
    s2 = _session(base, "tok-alice-rotated-000000")
    assert _whoami(base, "tok-alice-rotated-000000", s2) == ["alice@example.com"]
    sc = _session(base, "tok-carol-000000000000")
    assert _whoami(base, "tok-carol-000000000000", sc) == ["carol@example.com"]

    users_file.write_text(json.dumps([d for d in data if d["account"]["name"] != "carol"]))
    assert _whoami(base, "tok-carol-000000000000", sc) == 401
