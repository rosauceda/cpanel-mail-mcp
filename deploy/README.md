# Deploy — cpanel-mail-mcp as a multi-user HTTP server

The default `cpanel-mail-mcp` package runs over **stdio** — the MCP client
launches it as a subprocess per session. That's the personal-use shape.

`deploy/` gives you the **server** shape: one instance running 24/7 on an
LXC or VPS, HTTPS via Cloudflare Tunnel, and **per-user bearer tokens** so
several people can share the same install with their own mailboxes.

## What you get

* `install.sh` — one-command installer for a fresh Debian/Ubuntu LXC or VM
* `cpanel-mail-mcp.service` — reference systemd unit
* Admin CLI on the server: `cpanel-mail-mcp admin {add-user,list-users,remove-user,rotate-token,add-sso-email,remove-sso-email}`

Once installed, the server listens on `127.0.0.1:8080` (set `MCP_HOST=0.0.0.0`
if `cloudflared` runs on another LXC/VM — and give this LXC a static IP or a
DHCP reservation, or the tunnel breaks when the address changes) and exposes:

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
# The running server re-reads users.json on change: add/rotate/remove apply
# on the next request, no restart needed.

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
| `--trash-folder`    | auto-detect      | SPECIAL-USE `\Trash`, then common names |
| `--no-save-to-sent` | (save enabled)   |                                    |
| `--from-name`       | —                | display name in From:              |
| `--name`            | email            | friendly handle                    |
| `--sso-email`       | —                | SSO login email mapped to this mailbox (repeatable) |

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
# the URL must come before --header (the flag takes several values)
claude mcp add --transport http --scope user cpanel-mail \
  https://mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer <THEIR_TOKEN>"

claude mcp list | grep cpanel     # should show ✔ Connected
```

## Environment variables the server reads

| var                            | default        | purpose                                        |
|--------------------------------|----------------|------------------------------------------------|
| `MCP_TRANSPORT`                | `stdio`        | `stdio` \| `http` \| `streamable-http` \| `sse` |
| `MCP_HOST`                     | `127.0.0.1`    | bind host (HTTP mode)                          |
| `MCP_PORT`                     | `8080`         | bind port                                      |
| `MCP_AUTH_MODE`                | tokens         | `credentials` = callers send their mailbox login per request (no users.json; see the main README) |
| `CPANEL_HOST`                  | —              | credentials mode: IMAP/SMTP server for every login |
| `MCP_ALLOWED_EMAIL_DOMAINS`    | —              | credentials mode: address domains allowed to log in |
| `MCP_API_KEY`                  | —              | credentials mode: extra shared secret required as `X-API-Key` |
| `MCP_CREDENTIAL_CACHE_SECONDS` | `600`          | credentials mode: cache for verified logins    |
| `EMAIL_USERS_FILE`             | —              | path to users.json → **multi-user mode**       |
| `MCP_AUTH_TOKEN`               | —              | single-tenant bearer (ignored if users.json exists) |
| `MCP_ALLOW_NO_AUTH`            | —              | set truthy to disable auth (dev only)          |
| `MCP_ALLOWED_HOSTS`            | —              | comma-list of Host headers to accept (**required behind a reverse proxy** — e.g. `mcp.yourdomain.com`) |
| `MCP_ALLOWED_ORIGINS`          | —              | comma-list of Origin headers to accept (browser clients only) |
| `MCP_DISABLE_DNS_REBINDING_PROTECTION` | —      | set truthy to bypass Host/Origin checks entirely |
| `CF_ACCESS_AUD`                | —              | SaaS OIDC app Client ID (or the self-hosted app's Audience tag) |
| `CF_ACCESS_OIDC_ISSUER`        | —              | SaaS OIDC issuer, `https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<app_uid>` |
| `CF_ACCESS_JWKS_URL`           | `<issuer>/jwks` | override the JWKS URL                         |
| `CF_ACCESS_TEAM_DOMAIN`        | —              | legacy self-hosted Access app (instead of `CF_ACCESS_OIDC_ISSUER`) |
| `MCP_RESOURCE_URL`             | —              | e.g. `https://mcp.yourdomain.com` — public URL of this MCP server (used in `oauth-protected-resource` metadata) |
| `MCP_OAUTH_UPSTREAM_ISSUER`    | —              | enables the DCR proxy (same value as `CF_ACCESS_OIDC_ISSUER`) |
| `MCP_OAUTH_CLIENT_ID` / `MCP_OAUTH_CLIENT_SECRET` | — | SaaS app credentials handed out by `POST /register` |
| `MCP_OAUTH_AUTHORIZATION_SERVERS` | —           | comma-list of AS URLs advertised when the DCR proxy is off |
| `MCP_ATTACHMENT_DIR`           | —              | allow `path` attachments only under this dir (HTTP mode: unset = disabled) |
| `MCP_MAX_ATTACHMENT_MB`        | `25`           | total attachment size cap per message          |
| `MCP_IMAP_TIMEOUT`             | `30`           | seconds before an IMAP socket operation gives up |
| `MCP_DEFAULT_TIMEZONE`         | UTC            | IANA zone for naive `send_invite` times (e.g. `America/Mexico_City`) |
| `MCP_RATE_LIMIT_SEND_PER_MIN`  | `30`           | per-user send/draft calls per minute           |
| `MCP_RATE_LIMIT_READ_PER_MIN`  | `300`          | per-user other calls per minute                |
| `MCP_LOG_LEVEL`                | `INFO`         | stdlib logging level                           |
| `EMAIL_ACCOUNTS_FILE`          | —              | single-tenant accounts JSON                    |
| `EMAIL_ACCOUNTS_JSON`          | —              | single-tenant inline JSON                      |
| `EMAIL_SEND_CONFIRMATION_CODE` | —              | require `params.confirm=<code>` on sends       |

## Upgrade the server

```bash
cd /tmp   # the service user can't read root's home
runuser -u cpanelmcp -- env HOME=/var/lib/cpanelmcp PATH=/var/lib/cpanelmcp/.local/bin:/usr/bin:/bin \
  pipx install --force --pip-args='--no-cache-dir' 'cpanel-mail-mcp==0.7.1'
systemctl restart cpanel-mail-mcp
curl -sf http://127.0.0.1:8080/health   # -> ok
```

Pin the version: plain `pipx upgrade` can resolve a stale cached release.

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

## Cloudflare Access OIDC (optional but recommended for team use)

Turns Cloudflare Access into the OAuth 2.1 authorization server. Users log
in with Google/GitHub/email OTP; the MCP server just verifies the JWT that
CF Access mints and maps `email` → account in `users.json`. This is what
lets you register the server as a Custom Connector on claude.ai (which
requires OAuth, not bearer tokens).

### 1. Configure the Cloudflare Access application

In `one.dash.cloudflare.com` → **Access** → **Applications** → **Add** → **SaaS** → **OIDC**:

* Application name: `cpanel-mail-mcp`
* Redirect URLs (both):
  - `https://claude.ai/api/mcp/auth_callback`
  - `http://127.0.0.1:35333/callback`   (Claude Code CLI default callback)
* Scopes: `openid`, `email`, `profile`
* Policy: `Include` → `Emails` (your team) or `Emails ending in @yourdomain.com`
* Login methods: Google, GitHub, or one-time email PIN

After saving, note:
- **Client ID** and **Client Secret** (used by MCP clients)
- **AUD (Application Audience tag)** — long hex string, from the app's Overview page
- **Team domain** — `<team>.cloudflareaccess.com`, top-left of Zero Trust dashboard
- **Endpoints** — CF shows `/authorization`, `/token`, `/certs` URLs. You can compose them or fetch the full metadata at `https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<app_uid>/.well-known/openid-configuration`

### 2. Update the server systemd unit

Add these env vars to `/etc/systemd/system/cpanel-mail-mcp.service`:

```
Environment=CF_ACCESS_AUD=<client-id>
Environment=CF_ACCESS_OIDC_ISSUER=https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client-id>
Environment=MCP_RESOURCE_URL=https://mcp.yourdomain.com
Environment=MCP_ALLOWED_HOSTS=mcp.yourdomain.com
EnvironmentFile=/etc/cpanel-mail-mcp/oauth-proxy.env
```

and in `/etc/cpanel-mail-mcp/oauth-proxy.env` (mode 0600) the DCR proxy:

```
MCP_OAUTH_UPSTREAM_ISSUER=https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<client-id>
MCP_OAUTH_CLIENT_ID=<client-id>
MCP_OAUTH_CLIENT_SECRET=<client-secret>
```

Note: with the DCR proxy on, `POST /register` hands the client secret to
anyone who asks — that's how DCR-only clients get it. Treat it as public:
what actually protects the server is the Access **policy** (who can log in),
the app's **redirect URL allowlist**, and the JWT `aud` check. Rotating the
secret alone doesn't lock anyone out.

Reload and restart:

```bash
systemctl daemon-reload && systemctl restart cpanel-mail-mcp
journalctl -u cpanel-mail-mcp -n 10 --no-pager | grep -i "CF Access"
# esperado: "CF Access OIDC ENABLED: team=… aud=… (JWKS …)"
```

### 3. Verify metadata is public

```bash
curl -sf https://mcp.yourdomain.com/.well-known/oauth-protected-resource | python3 -m json.tool
```

Should return the resource + authorization_servers JSON.

### 4. Register on the client side

**Claude custom connector (web / mobile):** in claude.ai → Settings →
Connectors → Add custom connector, put the MCP URL. Claude will discover the
CF Access OIDC endpoints via the well-known metadata and drive the OAuth
flow. On first use, the user is redirected to CF Access, logs in with SSO,
and returns with an access token.

**Claude Code CLI:**

```bash
claude mcp add --transport http --scope user cpanel-mail \
  https://mcp.yourdomain.com/mcp \
  --client-id <CF_CLIENT_ID> \
  --client-secret            # prompts for the secret
```

First run opens a browser to CF Access for login; token cached locally.

### 5. Backward compat during migration

The existing bearer token flow **still works** in 0.5.0 — the middleware
tries CF Access JWT first, then falls back to opaque bearer from
`users.json`. You can migrate users one by one without downtime.

## Threat model / notes

* **Bearer tokens are passwords for your mailbox.** Store them in a
  password manager (yours and each user's), share via a secure channel,
  rotate immediately if leaked (`admin rotate-token`).
* **Passwords sit at rest in `/etc/cpanel-mail-mcp/users.json`** (mode 0600,
  owned by `cpanelmcp`). This is fine for a trusted, closed team; NOT
  fine as a public service. For public use you'd want at-rest encryption
  and a signup flow — that's a bigger project.
* **Per-request account isolation is enforced server-side.** Every HTTP
  request is authenticated and mapped to its own account; `params.account`
  is ignored in multi-user mode, and an MCP session can only be used by the
  identity that created it.
* **Remote callers can't read server files.** `path` attachments are
  disabled in HTTP mode unless `MCP_ATTACHMENT_DIR` confines them to one
  directory — never point it at `/etc/cpanel-mail-mcp`.
* **Cloudflare Tunnel** gives you TLS, DDoS protection, and the option to
  layer **Cloudflare Access** in front (email/GitHub SSO). If you add
  Access, use **Service Tokens** (`CF-Access-Client-Id` /
  `CF-Access-Client-Secret` headers) so MCP clients can auth
  non-interactively.
* The server user (`cpanelmcp`) is unprivileged and hardened by
  `ProtectSystem=strict`, `NoNewPrivileges`, `MemoryDenyWriteExecute`,
  restricted address families.
