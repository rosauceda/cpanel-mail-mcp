"""MCP_AUTH_MODE=credentials: header parsing, verification cache, throttling,
and the full HTTP flow n8n uses (X-Email-User / X-Email-Password)."""
import asyncio
import base64
import contextlib
import json
import threading
import time

import httpx
import pytest

from cpanel_mail_mcp import imap_ops, passthrough, server
from cpanel_mail_mcp.passthrough import (
    CredentialVerifier,
    DomainNotAllowed,
    InvalidCredentials,
    MailTemplate,
    Throttled,
    UpstreamUnavailable,
)

from .fake_imap import FakeServer
from .test_server_wellknown import _free_port

TEMPLATE = MailTemplate(imap_host="mail.ex.com", smtp_host="mail.ex.com", allowed_domains=("ex.com",))


def _basic(user: str, pw: str) -> bytes:
    return b"Basic " + base64.b64encode(f"{user}:{pw}".encode())


# ── header parsing ─────────────────────────────────────────────────────


def test_extract_from_email_headers():
    h = [(b"x-email-user", b" ana@ex.com "), (b"x-email-password", b"p:w d")]
    assert passthrough.extract_credentials(h) == ("ana@ex.com", "p:w d")


def test_extract_from_basic_auth_keeps_colons_in_password():
    assert passthrough.extract_credentials([(b"authorization", _basic("ana@ex.com", "a:b:c"))]) == \
        ("ana@ex.com", "a:b:c")


@pytest.mark.parametrize("headers", [
    [],
    [(b"x-email-user", b"ana@ex.com")],
    [(b"authorization", b"Basic !!!notbase64")],
    [(b"authorization", b"Basic " + base64.b64encode(b"no-colon"))],
    [(b"authorization", b"Bearer abc")],
])
def test_extract_missing_or_malformed(headers):
    assert passthrough.extract_credentials(headers) is None


# ── verifier ───────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, good: dict[str, str]):
        self.good, self.calls = good, 0

    def __call__(self, account):
        self.calls += 1
        if self.good.get(account.user) != account.password:
            raise InvalidCredentials("bad")


def test_valid_login_is_cached_and_builds_account():
    rec = Recorder({"ana@ex.com": "pw"})
    v = CredentialVerifier(TEMPLATE, verify=rec, cache_seconds=60)
    a1 = v.check("Ana@Ex.com", "pw")
    a2 = v.check("ana@ex.com", "pw")
    assert rec.calls == 1 and a1 is a2
    assert (a1.user, a1.imap_host, a1.sent_folder) == ("ana@ex.com", "mail.ex.com", "INBOX.Sent")


def test_changed_password_is_verified_again():
    rec = Recorder({"ana@ex.com": "new"})
    v = CredentialVerifier(TEMPLATE, verify=rec, cache_seconds=60)
    with pytest.raises(InvalidCredentials):
        v.check("ana@ex.com", "old")
    assert v.check("ana@ex.com", "new").password == "new"
    assert rec.calls == 2


def test_domain_allowlist_and_email_shape():
    v = CredentialVerifier(TEMPLATE, verify=Recorder({}))
    with pytest.raises(DomainNotAllowed):
        v.check("x@gmail.com", "pw")
    for bad in ("not-an-email", "a b@ex.com", "a@ex.com\r\n"):
        with pytest.raises(InvalidCredentials):
            v.check(bad, "pw")


def test_failed_logins_are_throttled_per_mailbox_but_cached_logins_still_work():
    rec = Recorder({"ana@ex.com": "pw", "luis@ex.com": "pw"})
    v = CredentialVerifier(TEMPLATE, verify=rec, max_failures_per_user=3)
    v.check("luis@ex.com", "pw")  # cached before the attack
    for _ in range(3):
        with pytest.raises(InvalidCredentials):
            v.check("ana@ex.com", "guess")
    calls = rec.calls
    with pytest.raises(Throttled) as ei:
        v.check("ana@ex.com", "pw")  # even the right password waits
    assert ei.value.retry_after > 0 and rec.calls == calls  # no IMAP login attempted
    assert v.check("luis@ex.com", "pw").user == "luis@ex.com"


def test_global_throttle_stops_password_spraying():
    v = CredentialVerifier(TEMPLATE, verify=Recorder({}), max_failures_global=4)
    for i in range(4):
        with pytest.raises(InvalidCredentials):
            v.check(f"u{i}@ex.com", "guess")
    with pytest.raises(Throttled):
        v.check("fresh@ex.com", "guess")


def test_mail_server_outage_is_not_counted_as_failure():
    def down(account):
        raise UpstreamUnavailable("timeout")
    v = CredentialVerifier(TEMPLATE, verify=down, max_failures_per_user=1)
    for _ in range(3):
        with pytest.raises(UpstreamUnavailable):
            v.check("ana@ex.com", "pw")


def test_template_from_env(monkeypatch):
    monkeypatch.setenv("CPANEL_HOST", "mail.dom.com")
    monkeypatch.delenv("CPANEL_IMAP_HOST", raising=False)
    monkeypatch.delenv("CPANEL_SMTP_HOST", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_EMAIL_DOMAINS", "dom.com, @Otra.mx")
    t = passthrough.template_from_env()
    assert (t.imap_host, t.smtp_host, t.allowed_domains) == ("mail.dom.com", "mail.dom.com", ("dom.com", "otra.mx"))
    monkeypatch.delenv("CPANEL_HOST")
    with pytest.raises(SystemExit):
        passthrough.template_from_env()


# ── API key gate (middleware only) ─────────────────────────────────────


def test_api_key_is_required_when_configured():
    async def ok_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    v = CredentialVerifier(TEMPLATE, verify=Recorder({"ana@ex.com": "pw"}))
    app = server.CredentialsAuthASGI(ok_app, verifier=v, api_key="k-123")
    creds = {"x-email-user": "ana@ex.com", "x-email-password": "pw"}

    async def go(headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return (await c.post("/mcp", headers=headers)).status_code

    assert asyncio.run(go(creds)) == 401
    assert asyncio.run(go({**creds, "x-api-key": "wrong"})) == 401
    assert asyncio.run(go({**creds, "x-api-key": "k-123"})) == 200


# ── full HTTP flow, in-process uvicorn + fake IMAP per mailbox ─────────


@pytest.fixture(scope="module")
def live():
    import uvicorn

    boxes = {}
    for user, subjects in (("ana@ex.com", ["factura ana"]), ("luis@ex.com", ["nota luis"])):
        s = FakeServer()
        s.passwords = {user: "pw-" + user.split("@")[0]}
        for i, subj in enumerate(subjects):
            s.add("INBOX", f"From: x@y.com\nSubject: {subj}\n\nhola\n", uid=100 + i)
        boxes[user] = s

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MCP_AUTH_MODE", "credentials")
        mp.setenv("CPANEL_HOST", "mail.ex.com")
        mp.setenv("MCP_ALLOWED_EMAIL_DOMAINS", "ex.com")
        mp.delenv("MCP_API_KEY", raising=False)
        mp.delenv("MCP_RESOURCE_URL", raising=False)
        mp.delenv("EMAIL_USERS_FILE", raising=False)
        mp.setattr(imap_ops, "_connect", lambda a: boxes[a.user].connect(a) if a.user in boxes
                   else FakeServer().connect(a))
        app = server.create_http_app()
        port = _free_port()
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            srv.should_exit = True
            t.join(timeout=5)


H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _creds(user):
    return {"x-email-user": user, "x-email-password": "pw-" + user.split("@")[0]}


def _rpc(base, headers, body, sid=None):
    h = {**H, **headers, **({"mcp-session-id": sid} if sid else {})}
    return httpx.post(f"{base}/mcp", headers=h, json=body, timeout=10)


def _init(base, headers):
    r = _rpc(base, headers, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "n8n", "version": "1"}}})
    assert r.status_code == 200, r.text
    sid = r.headers["mcp-session-id"]
    _rpc(base, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    return sid


def _result(r):
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])["result"]
    raise AssertionError(r.status_code, r.text)


def _call(base, headers, sid, name, args=None):
    r = _rpc(base, headers, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": name, "arguments": args or {}}}, sid)
    return r if r.status_code != 200 else _result(r)["structuredContent"]


def test_http_rejects_missing_wrong_and_foreign_credentials(live):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    r = _rpc(live, {}, body)
    assert r.status_code == 401 and r.json()["error"] == "missing_credentials"
    assert r.headers["www-authenticate"].startswith("Basic")
    r = _rpc(live, {"x-email-user": "ana@ex.com", "x-email-password": "nope"}, body)
    assert r.status_code == 401 and r.json()["error"] == "invalid_credentials"
    r = _rpc(live, {"x-email-user": "ana@gmail.com", "x-email-password": "x"}, body)
    assert r.status_code == 403
    assert httpx.get(f"{live}/health").text == "ok"


def test_http_n8n_flow_lists_tools_and_reads_own_mailbox(live):
    sid = _init(live, _creds("ana@ex.com"))
    r = _rpc(live, _creds("ana@ex.com"), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    names = {t["name"] for t in _result(r)["tools"]}
    assert {"list_recent", "search_emails", "read_email", "send_email"} <= names and len(names) == 22
    assert [a["user"] for a in _call(live, _creds("ana@ex.com"), sid, "list_accounts")["accounts"]] == ["ana@ex.com"]
    msgs = _call(live, _creds("ana@ex.com"), sid, "list_recent")["messages"]
    assert [m["subject"] for m in msgs] == ["factura ana"]


def test_http_basic_auth_and_mailbox_isolation(live):
    luis = {"authorization": _basic("luis@ex.com", "pw-luis").decode()}
    sid = _init(live, luis)
    assert [m["subject"] for m in _call(live, luis, sid, "list_recent")["messages"]] == ["nota luis"]
    ana_sid = _init(live, _creds("ana@ex.com"))
    r = _call(live, luis, ana_sid, "list_recent")  # Luis can't ride Ana's session
    assert r.status_code == 404
