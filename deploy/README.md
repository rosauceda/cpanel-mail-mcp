# Deploy — cpanel-mail-mcp as a multi-user HTTP server

The default `cpanel-mail-mcp` package runs over **stdio** — the MCP client
launches it as a subprocess per session. That's the personal-use shape.

`deploy/` gives you the **server** shape: one instance running 24/7 on an
LXC or VPS, HTTPS via Cloudflare Tunnel, and **per-user bearer tokens** so
several people can share the same install with their own mailboxes.

## What you get

* `install.sh` — one-command installer for a fresh Debian/Ubuntu LXC or VM
* `cpanel-mail-mcp.service` — reference systemd unit
* Admin CLI on the server: `cpanel-mail-mcp admin {add-user,list-users,remove-user,rotate-token}`

Once installed, the server listens on `127.0.0.1:8080` and exposes:

| endpoint  | method     | auth                                | purpose                          |
|-----------|------------|-------------------------------------|----------------------------------|
| `/mcp`    | POST / GET | bearer token → user's account       | MCP Streamable HTTP transport    |
| `/health` | GET        | none                                | plain `ok` for proxy probes      |

Every request under `/mcp` must send `Authorization: Bearer <token>`. The
token identifies the caller; the server enforces that they can only act on
their own mailbox — the `account` field in tool params is ignored in
multi-user mode.

## Quick install on the LXC

```bash
# on the LXC, as root. Base image may lack curl:
apt install -y curl

curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash
```

Then reload the shell (or `source /etc/profile.d/cpanel-mail-mcp.sh`) so
`EMAIL_USERS_FILE` is exported.

## Add users

```bash
# One user at a time. --password will prompt if omitted (safer — not in shell history).
cpanel-mail-mcp admin add-user \
  --email juan@dominio.com \
  --host mail.dominio.com

# add-user prints the bearer token ONCE. Share it with that user by a secure
# channel (Signal, 1Password Send, in person). It is not recoverable.

cpanel-mail-mcp admin list-users
cpanel-mail-mcp admin rotate-token --email juan@dominio.com   # if a token leaks
cpanel-mail-mcp admin remove-user  --email juan@dominio.com
```

Full `add-user` options:

| flag                | default          | notes                              |
|---------------------|------------------|------------------------------------|
| `--email`           | required         | mailbox login (also acts as ID)    |
| `--password`        | prompt           | omit to be prompted (no history)   |
| `--host`            | —                | shortcut for --imap-host + --smtp-host |
| `--imap-host`       | from `--host`    |                                    |
| `--smtp-host`       | from `--host`    |                                    |
| `--imap-port`       | 993              |                                    |
| `--smtp-port`       | 465              | (587 = STARTTLS, handled)          |
| `--sent-folder`     | INBOX.Sent       |                                    |
| `--drafts-folder`   | INBOX.Drafts     |                                    |
| `--no-save-to-sent` | (save enabled)   |                                    |
| `--from-name`       | —                | display name in From:              |
| `--name`            | email            | friendly handle                    |

## Start the service

```bash
systemctl enable --now cpanel-mail-mcp
systemctl status cpanel-mail-mcp
curl -sf http://127.0.0.1:8080/health         # -> ok
journalctl -u cpanel-mail-mcp -f              # follow logs
```

## Cloudflare Tunnel

Assuming your tunnel already exists, add an ingress rule
(`~/.cloudflared/config.yml` on whichever host runs `cloudflared`):

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
systemctl restart cloudflared
curl -sf https://mcp.yourdomain.com/health   # -> ok, from anywhere
```

## Register from each user's Claude Code / Desktop

You (as operator) give each user their token privately. They run:

```bash
claude mcp add --transport http --scope user cpanel-mail \
  --header "Authorization: Bearer <THEIR_TOKEN>" \
  https://mcp.yourdomain.com/mcp

claude mcp list | grep cpanel     # should show ✔ Connected
```

## Environment variables the server reads

| var                            | default        | purpose                                        |
|--------------------------------|----------------|------------------------------------------------|
| `MCP_TRANSPORT`                | `stdio`        | `stdio` \| `http` \| `streamable-http` \| `sse` |
| `MCP_HOST`                     | `127.0.0.1`    | bind host (HTTP mode)                          |
| `MCP_PORT`                     | `8080`         | bind port                                      |
| `EMAIL_USERS_FILE`             | —              | path to users.json → **multi-user mode**       |
| `MCP_AUTH_TOKEN`               | —              | single-tenant bearer (ignored if users.json exists) |
| `MCP_ALLOW_NO_AUTH`            | —              | set truthy to disable auth (dev only)          |
| `MCP_LOG_LEVEL`                | `INFO`         | stdlib logging level                           |
| `EMAIL_ACCOUNTS_FILE`          | —              | single-tenant accounts JSON                    |
| `EMAIL_ACCOUNTS_JSON`          | —              | single-tenant inline JSON                      |
| `EMAIL_SEND_CONFIRMATION_CODE` | —              | require `params.confirm=<code>` on sends       |

## Upgrade the server

```bash
runuser -u cpanelmcp -- env HOME=/var/lib/cpanelmcp PATH=/var/lib/cpanelmcp/.local/bin:/usr/bin:/bin \
  pipx upgrade cpanel-mail-mcp
systemctl restart cpanel-mail-mcp
```

## Migrate 0.3.0 single-tenant → 0.4.0 multi-user

If you had 0.3.0 running with `accounts.json` + `MCP_AUTH_TOKEN`:

```bash
# 1) upgrade the package (see above)
# 2) create the users.json and add your existing user
export EMAIL_USERS_FILE=/etc/cpanel-mail-mcp/users.json
[[ -f $EMAIL_USERS_FILE ]] || echo '[]' | install -m 600 -o cpanelmcp -g cpanelmcp /dev/stdin $EMAIL_USERS_FILE
cpanel-mail-mcp admin add-user --email you@example.com --host mail.example.com

# 3) drop the old single-tenant systemd env
sed -i '/^Environment=EMAIL_ACCOUNTS_FILE=/d; /^EnvironmentFile=/d' /etc/systemd/system/cpanel-mail-mcp.service
# 4) point the unit at the users.json
grep -q EMAIL_USERS_FILE /etc/systemd/system/cpanel-mail-mcp.service || \
  sed -i "/^Environment=MCP_PORT=/a Environment=EMAIL_USERS_FILE=$EMAIL_USERS_FILE" /etc/systemd/system/cpanel-mail-mcp.service
# 5) reload + restart
systemctl daemon-reload && systemctl restart cpanel-mail-mcp

# 6) optional: keep the old accounts.json/token file around for a bit, then rm
```

Every existing user has to re-register their Claude Code with the new
per-user token — the old `MCP_AUTH_TOKEN` no longer works.

## Uninstall

```bash
systemctl disable --now cpanel-mail-mcp
rm /etc/systemd/system/cpanel-mail-mcp.service
rm /etc/profile.d/cpanel-mail-mcp.sh
rm /usr/local/bin/cpanel-mail-mcp
systemctl daemon-reload
userdel -r cpanelmcp
rm -rf /etc/cpanel-mail-mcp
```

## Threat model / notes

* **Bearer tokens are passwords for your mailbox.** Store them in a
  password manager (yours and each user's), share via a secure channel,
  rotate immediately if leaked (`admin rotate-token`).
* **Passwords sit at rest in `/etc/cpanel-mail-mcp/users.json`** (mode 0600,
  owned by `cpanelmcp`). This is fine for a trusted, closed team; NOT
  fine as a public service. For public use you'd want at-rest encryption
  and a signup flow — that's a bigger project.
* **Per-request account isolation is enforced server-side.** A user cannot
  address another user's mailbox by passing `params.account="other"` —
  the middleware forces the request onto whichever account the token maps
  to.
* **Cloudflare Tunnel** gives you TLS, DDoS protection, and the option to
  layer **Cloudflare Access** in front (email/GitHub SSO). If you add
  Access, use **Service Tokens** (`CF-Access-Client-Id` /
  `CF-Access-Client-Secret` headers) so MCP clients can auth
  non-interactively.
* The server user (`cpanelmcp`) is unprivileged and hardened by
  `ProtectSystem=strict`, `NoNewPrivileges`, `MemoryDenyWriteExecute`,
  restricted address families.
