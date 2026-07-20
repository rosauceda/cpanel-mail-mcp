# Deploy — cpanel-mail-mcp as an HTTP server

The default `cpanel-mail-mcp` package runs over **stdio**: your MCP client
(Claude Desktop / Claude Code) launches it as a subprocess per session.
That's the right shape for personal use.

Sometimes you want it running **24/7** behind an HTTPS URL — e.g. an LXC
on your homelab exposed via Cloudflare Tunnel. This directory has what
you need for that shape.

## What you get

* `install.sh` — one-command installer for a fresh Debian/Ubuntu LXC or VM
* `cpanel-mail-mcp.service` — the systemd unit the installer drops (also
  visible standalone in this directory for reference)

Once installed, the server listens on `127.0.0.1:8080` (bind is
configurable) and exposes:

| endpoint       | method       | purpose                              |
|----------------|--------------|--------------------------------------|
| `/mcp`         | POST / GET   | MCP Streamable HTTP transport        |
| `/health`      | GET          | plain `ok` for reverse-proxy probes  |

Everything under `/mcp` requires a bearer token; `/health` is open.

## Quick install on the LXC

```bash
# on the LXC, as root
curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash
```

Then:

```bash
$EDITOR /etc/cpanel-mail-mcp/accounts.json     # fill in credentials
systemctl enable --now cpanel-mail-mcp
systemctl status cpanel-mail-mcp
curl -sf http://127.0.0.1:8080/health           # -> ok
```

## Cloudflare Tunnel

Assuming your tunnel already exists, add an ingress rule
(`~/.cloudflared/config.yml` on whatever host runs `cloudflared`):

```yaml
tunnel: <YOUR_TUNNEL_ID>
credentials-file: /root/.cloudflared/<YOUR_TUNNEL_ID>.json

ingress:
  - hostname: mcp.yourdomain.com
    service: http://127.0.0.1:8080
  - service: http_status:404
```

Then:

```bash
cloudflared tunnel route dns <YOUR_TUNNEL_ID> mcp.yourdomain.com
sudo systemctl restart cloudflared
curl -sf https://mcp.yourdomain.com/health      # -> ok
```

## Register from your Claude Code / Desktop

```bash
TOKEN=$(ssh root@LXC cat /etc/cpanel-mail-mcp/token)

claude mcp add --transport http --scope user cpanel-mail \
  --header "Authorization: Bearer $TOKEN" \
  https://mcp.yourdomain.com/mcp

claude mcp list | grep cpanel     # should show ✔ Connected
```

## Environment variables the server reads

| var                   | default        | purpose                                        |
|-----------------------|----------------|------------------------------------------------|
| `MCP_TRANSPORT`       | `stdio`        | `stdio` \| `http` \| `streamable-http` \| `sse` |
| `MCP_HOST`            | `127.0.0.1`    | bind host (HTTP mode)                          |
| `MCP_PORT`            | `8080`         | bind port                                      |
| `MCP_AUTH_TOKEN`      | —              | **required** in HTTP mode                      |
| `MCP_ALLOW_NO_AUTH`   | —              | set truthy to disable auth (dev only)          |
| `MCP_LOG_LEVEL`       | `INFO`         | stdlib logging level                           |
| `EMAIL_ACCOUNTS_FILE` | —              | path to accounts JSON                          |
| `EMAIL_ACCOUNTS_JSON` | —              | inline JSON of accounts                        |
| `EMAIL_SEND_CONFIRMATION_CODE` | —     | require `params.confirm=<code>` on sends       |

`accounts.json` schema: see the main [README](../README.md#account-fields).

## Upgrade the server

```bash
runuser -u cpanelmcp -- env HOME=/var/lib/cpanelmcp PATH=/var/lib/cpanelmcp/.local/bin:/usr/bin:/bin \
  pipx upgrade cpanel-mail-mcp
systemctl restart cpanel-mail-mcp
```

## Uninstall

```bash
systemctl disable --now cpanel-mail-mcp
rm /etc/systemd/system/cpanel-mail-mcp.service
systemctl daemon-reload
userdel -r cpanelmcp
rm -rf /etc/cpanel-mail-mcp
```

## Threat model / notes

* **The bearer token is the only thing between the Internet and your
  mailbox.** Treat it like a password. It sits in
  `/etc/cpanel-mail-mcp/env` (mode 0600, owned by `cpanelmcp`) and is
  used by systemd's `EnvironmentFile=` (kept out of `systemctl show`).
* Cloudflare Tunnel gives you TLS, DDoS protection, and the ability to
  layer **Cloudflare Access** in front (email/GitHub SSO). If you add
  Access, use **Service Tokens** (`CF-Access-Client-Id` / `-Secret` on
  the client side) so the MCP client can auth non-interactively.
* The server user (`cpanelmcp`) is unprivileged and locked down by the
  systemd hardening in the unit file (`ProtectSystem=strict`,
  `NoNewPrivileges`, `MemoryDenyWriteExecute`, restricted address
  families, etc.).
* This is **single-tenant** by design: one server → one account
  configuration. If you want per-user credential isolation across the
  same server, that's a bigger change.
