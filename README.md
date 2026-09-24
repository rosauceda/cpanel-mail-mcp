# cpanel-mail-mcp

MCP server for IMAP/SMTP email accounts — works with cPanel, Gmail
(app passwords), Outlook, Fastmail, iCloud, or any provider that speaks
plain IMAP + SMTP.

## Features

* **22 typed MCP tools** — one per operation with Pydantic input/output schemas, tool annotations (`readOnlyHint`/`destructiveHint`/…), and actionable error messages
* **Full mailbox management** — read, search, threading, move, copy, flag, star, delete, reply, forward, drafts, calendar invites, folder create/delete/rename
* **n8n / Docker ready** — `MCP_AUTH_MODE=credentials`: each client sends its own mailbox login per request, the server stores no passwords; Dockerfile for Dokploy
* **Multi-user server mode** — one instance, many users, each with their own bearer token and mailbox (per-request isolation; users.json changes apply without a restart)
* **OAuth 2.1 via Cloudflare Access** — optional SSO (Google / GitHub / Email OTP) instead of shared bearer tokens; server exposes RFC 9728 protected-resource metadata, RFC 8414 AS metadata, and RFC 7591 DCR — proxied over CF Access SaaS OIDC
* **Idempotent sends** — pass `idempotency_key` on `send_email`/`reply_email`/`forward_email`/`send_invite` to safely retry after client timeouts
* **Per-caller rate limiting** — sliding window (send: 30/min, read: 300/min defaults), tunable per bucket
* **Attachment size guard** — configurable cap (default 25 MB total)
* **Multi-account** — manage multiple email accounts from different providers
* **Read, search, list** — full IMAP support with folder browsing
* **Send emails** — plain text, HTML, or both (multipart/alternative)
* **Attachments** — send as base64/inline data, or by file path in local (stdio) mode
* **Download attachments** — extract attachments from received emails as base64
* **Calendar invites** — send proper ICS invitations with Accept/Decline buttons
* **Save to Sent** — automatically saves sent emails to the Sent folder via IMAP
* **Optional send gate** — configurable confirmation code to prevent accidental sends
* **International folders & search** — UTF-7 folder names; accented search terms (`Reunión`) via UTF-8 SEARCH
* **Stable message IDs** — every `uid` is a real IMAP UID, safe to reuse across calls
* **Attachment flag in listings** — `has_attachments` / `attachment_count` per message, without downloading bodies

See [CHANGELOG.md](CHANGELOG.md) for what changed in each release.

## Install

### With `uvx` (recommended, no venv setup)

```bash
claude mcp add cpanel-mail \
  -e CPANEL_USER=you@example.com \
  -e CPANEL_PASS='your_password' \
  -e CPANEL_SMTP_HOST=mail.example.com \
  -e CPANEL_IMAP_HOST=mail.example.com \
  -- uvx cpanel-mail-mcp
```

### With `pipx`

```bash
pipx install cpanel-mail-mcp
claude mcp add cpanel-mail \
  -e CPANEL_USER=you@example.com -e CPANEL_PASS='...' \
  -e CPANEL_SMTP_HOST=mail.example.com -e CPANEL_IMAP_HOST=mail.example.com \
  -- cpanel-mail-mcp
```

### Development install (from a git checkout)

```bash
git clone https://github.com/rosauceda/cpanel-mail-mcp
cd cpanel-mail-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env         # edit
cpanel-mail-mcp              # runs the stdio server
# or: python -m cpanel_mail_mcp
```

## Configuration

Three ways, in priority order:

### 1. `EMAIL_ACCOUNTS_JSON` (best for multi-account)

```bash
export EMAIL_ACCOUNTS_JSON='[
  {"name":"work","user":"me@work.com","password":"...",
   "smtp_host":"mail.work.com","imap_host":"mail.work.com",
   "sent_folder":"INBOX.Sent"},
  {"name":"gmail","user":"me@gmail.com","password":"app_pass",
   "smtp_host":"smtp.gmail.com","smtp_port":587,
   "imap_host":"imap.gmail.com","sent_folder":"[Gmail]/Sent Mail"}
]'
```

### 2. `EMAIL_ACCOUNTS_FILE`

Point to a JSON file with the same shape.

### 3. Legacy single-account (`CPANEL_*`)

Backwards compatible with 0.1.x installs. See [`.env.example`](.env.example).

### Account fields

| field           | required | default   |
|-----------------|----------|-----------|
| `name`          | no       | `user`    |
| `user`          | **yes**  | —         |
| `password`      | **yes**  | —         |
| `smtp_host`     | **yes**  | —         |
| `smtp_port`     | no       | `465`     |
| `imap_host`     | **yes**  | —         |
| `imap_port`     | no       | `993`     |
| `sent_folder`   | no       | `Sent`    |
| `drafts_folder` | no       | `Drafts`  |
| `save_to_sent`  | no       | `true`    |
| `from_name`     | no       | —         |
| `sso_emails`    | no       | `[]`      |
| `trash_folder`  | no       | auto-detect (SPECIAL-USE `\Trash`, then common names) |

`sso_emails` is a list of external identities (e.g. Google login addresses)
that map to this account. Only used in OAuth mode: the JWT `email` claim is
looked up here before falling back to `user`. Handy when the SSO identity
differs from the mailbox address — e.g. logging in with `me@gmail.com` to
access `me@company.com`.

### Loading a dev `.env`

The server auto-loads `.env` from the current working directory. Point it
elsewhere with:

```bash
export EMAIL_ENV_FILE=/absolute/path/to/.env
```

## Tools

Since 0.7.0 the server exposes one tool per operation (was a single
dispatcher tool before). Every tool has a typed Pydantic input schema, an
`outputSchema` for `structuredContent`, and annotations
(`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`)
so clients can render the right approval UI.

Every `uid` is a real IMAP UID (stable across calls and sessions), never a
sequence number. Tools that take a `uid` accept exactly one numeric UID —
ranges like `1:*` are rejected. Read-only tools open folders with EXAMINE,
so `read_email` does **not** mark a message as read (use `mark_read`).

Every message in `list_recent` / `search_emails` / `get_thread` carries
`has_attachments` and `attachment_count`, read from IMAP BODYSTRUCTURE (no
bodies downloaded). Images embedded in the HTML (signature logos) don't
count; `read_email` lists them with `embedded: true`.

| tool                    | annotations                          | purpose                                       |
|-------------------------|--------------------------------------|-----------------------------------------------|
| `list_accounts`         | read-only, idempotent                | Accounts visible to caller (no secrets)       |
| `list_folders`          | read-only, idempotent, openWorld     | IMAP folders (UTF-7 decoded)                  |
| `list_recent`           | read-only, idempotent, openWorld     | Paginated recent messages + flags + `has_attachments`; filters `unread_only`, `since` (`2h`, `1d`, ISO) |
| `search_emails`         | read-only, idempotent, openWorld     | IMAP SEARCH by FROM/TO/SUBJECT/BODY/TEXT      |
| `read_email`            | read-only, idempotent, openWorld     | Headers + body (opt: attachments as base64)   |
| `download_attachments`  | read-only, idempotent, openWorld     | Fetch attachments by name filter              |
| `get_thread`            | read-only, idempotent, openWorld     | Ancestors + replies via Message-ID/References |
| `send_email`            | **destructive**, idempotent          | Send new message (attachments, HTML, save-to-sent) |
| `reply_email`           | **destructive**, idempotent          | Reply to UID, preserves References chain      |
| `forward_email`         | **destructive**, idempotent          | Forward UID (attaches original body + files)  |
| `send_invite`           | **destructive**, idempotent          | ICS calendar invite (RFC 5545, METHOD:REQUEST)|
| `save_draft`            | non-destructive                      | APPEND a draft to Drafts folder               |
| `mark_read`/`mark_unread`| non-destructive, idempotent, openWorld | Toggle \\Seen flag                        |
| `star_email`/`unstar_email`| non-destructive, idempotent, openWorld | Toggle \\Flagged flag                    |
| `move_email`            | non-destructive                      | RFC 6851 MOVE (COPY+EXPUNGE fallback)         |
| `copy_email`            | non-destructive                      | IMAP COPY                                     |
| `delete_email`          | **destructive**                      | Soft-delete → Trash (or hard with `permanent=true`); never hard-deletes on its own |
| `create_folder`         | non-destructive                      | IMAP CREATE + SUBSCRIBE                       |
| `delete_folder`         | **destructive**, idempotent          | IMAP DELETE (must be empty)                   |
| `rename_folder`         | non-destructive                      | IMAP RENAME                                   |

### Example — send with attachment + Save-to-Sent

```json
{
  "tool": "send_email",
  "arguments": {
    "to": "someone@example.com",
    "subject": "Report",
    "text": "See attached.",
    "html": "<p>See <b>attached</b>.</p>",
    "attachments": [
      {"name": "report.pdf", "content_base64": "JVBERi0xLjc..."},
      {"name": "note.txt", "content": "hi from inline"}
    ],
    "idempotency_key": "report-2026-07-20-A"
  }
}
```

Attachment shapes accepted:
- `{"name": "x.bin", "content_base64": "..."}` — inline base64
- `{"name": "x.txt", "content": "hello", "mime": "text/plain"?}` — inline text
- `{"path": "/local/file.pdf", "name": "renamed.pdf"?}` — read from disk. Allowed
  in stdio mode; in HTTP mode **disabled** unless `MCP_ATTACHMENT_DIR` is set, and
  then only for files under that directory (remote callers must never read the
  server's own files).

Total attachment size is capped by `MCP_MAX_ATTACHMENT_MB` (default 25 MB).

### Calendar invite times

`send_invite` accepts ISO 8601 with an offset (`2026-10-01T10:00:00-06:00`)
or a naive `YYYY-MM-DD HH:MM`. Naive times are read in the `timezone` argument
(IANA name, e.g. `America/Mexico_City`), else `MCP_DEFAULT_TIMEZONE`, else UTC.

### Idempotent sends

Any of `send_email`, `reply_email`, `forward_email`, `send_invite` accept
`idempotency_key`. Second call with the same `(caller, key)` within 5
minutes returns the cached first response (`idempotent_replay: true`) — no
duplicate delivery on client retries, even when both calls arrive at once.

### Rate limiting

Per-caller sliding window; defaults:
- **send** bucket (send/reply/forward/invite/draft): 30 requests/min
- **read** bucket (everything else): 300 requests/min

Tune via `MCP_RATE_LIMIT_SEND_PER_MIN` / `MCP_RATE_LIMIT_READ_PER_MIN`.
When exceeded, the tool returns a structured error with `retry_after_seconds`.

### Reply / forward preserve threading

`reply_email` copies the original `Message-ID` into `In-Reply-To` and appends
it to the original `References`, so mail clients thread correctly. It goes to
the original `Reply-To` when present (else `From`); `reply_all` adds the
original To/Cc minus your own address. `forward_email` adds a
standard `Fwd:` prefix and quotes the original headers + body inline; any
attachments on the original are re-attached to the forward.

### Pick an account (multi-account setup)

Every tool accepts an optional `account` param; omit it to use the first
configured account. In multi-user OAuth mode this field is ignored — the
account is chosen by the caller's bearer token or SSO email.

## Run as an HTTP server (LXC / VPS / homelab)

By default `cpanel-mail-mcp` runs over **stdio** — your MCP client
launches it per session. To run it 24/7 as a shared HTTPS endpoint,
you have two shapes:

### Docker / Dokploy + n8n (credentials mode)

The image runs in `MCP_AUTH_MODE=credentials`: every request carries the
mailbox login, so there are no users to register and **no passwords stored
on the server**. Each n8n credential is one mailbox.

```bash
docker build -t cpanel-mail-mcp .
docker run -d -p 8080:8080 \
  -e CPANEL_HOST=mail.tudominio.com \
  -e MCP_ALLOWED_EMAIL_DOMAINS=tudominio.com \
  cpanel-mail-mcp
```

On **Dokploy**: create an *Application* from this repo with build type
**Dockerfile**, set the env vars below, and either keep it internal (n8n on the
same Dokploy reaches it as `http://<app-name>:8080/mcp`) or add a domain.

| env var                     | required | purpose |
|-----------------------------|----------|---------|
| `CPANEL_HOST`               | **yes**  | your IMAP/SMTP server (`CPANEL_IMAP_HOST`/`CPANEL_SMTP_HOST` to split) |
| `MCP_ALLOWED_EMAIL_DOMAINS` | recommended | comma list of address domains allowed to log in |
| `MCP_API_KEY`               | if public | extra shared secret, sent as `X-API-Key` |
| `MCP_CREDENTIAL_CACHE_SECONDS` | no    | how long a verified login is cached (default 600) |
| `CPANEL_IMAP_PORT` / `CPANEL_SMTP_PORT` / `CPANEL_SENT_FOLDER` / `CPANEL_DRAFTS_FOLDER` / `CPANEL_TRASH_FOLDER` | no | same defaults as the cPanel setup |

In n8n: **AI Agent → Tool → MCP Client Tool**

* Endpoint: `http://<app-name>:8080/mcp` (internal) or `https://mcp.tudominio.com/mcp`
* Server Transport: **HTTP Streamable**
* Authentication: **Multiple Headers Auth** → `X-Email-User` = the mailbox,
  `X-Email-Password` = its password (plus `X-API-Key` if you set one)
* Tools to include: **All** (22 tools)

`Authorization: Basic base64(user:password)` works as well. A login is checked
against IMAP once and cached; wrong passwords are throttled per mailbox (5 per
15 min) and globally, so the endpoint can't be used to guess passwords.

### Multi-user (recommended for a team)

One instance, many users. Each person has their own bearer token, and the
server enforces that they can only touch their own mailbox. Setup:

```bash
# on the server (Debian/Ubuntu LXC as root)
apt install -y curl      # if not present
curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash

# after logging out and back in (so $EMAIL_USERS_FILE is exported):
systemctl enable --now cpanel-mail-mcp

cpanel-mail-mcp admin add-user --email juan@dominio.com --host mail.dominio.com
# prompts for password → prints juan's bearer token → hand it to juan
# (add/rotate/remove take effect on the next request — no restart)
```

Each user, in their Claude Code (the URL goes **before** `--header`):
```bash
claude mcp add --transport http --scope user cpanel-mail \
  https://mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer <THEIR_TOKEN>"
```

Full docs, admin CLI, migration, Cloudflare Tunnel example, hardened
systemd unit → [`deploy/`](deploy/).

### Single-tenant (only you)

```bash
export MCP_TRANSPORT=streamable-http
export MCP_HOST=127.0.0.1
export MCP_PORT=8080
export MCP_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
export EMAIL_ACCOUNTS_JSON='[{...one account...}]'   # or EMAIL_ACCOUNTS_FILE
cpanel-mail-mcp
```

Endpoints:
* `POST /mcp` — MCP Streamable HTTP transport (requires bearer token)
* `GET  /health` — plain `ok` for reverse-proxy probes

### OAuth 2.1 via Cloudflare Access (optional, for MCP clients that require OAuth)

Adds SSO on top of the multi-user mode. Cloudflare Access SaaS OIDC does the
actual user login (Google / GitHub / Email OTP); the server verifies the
resulting JWT and maps `email` → account. Also exposes:

* `GET /.well-known/oauth-protected-resource` (RFC 9728) — root and `/mcp` path-suffixed
* `GET /.well-known/oauth-authorization-server` (RFC 8414) — composed metadata that adds a `registration_endpoint`
* `POST /register` (RFC 7591 Dynamic Client Registration) — returns the pre-configured CF client credentials so DCR-only clients can register
* `WWW-Authenticate: Bearer realm="mcp", resource_metadata="..."` on 401 responses

Env vars to enable:

| var                              | purpose                                    |
|----------------------------------|--------------------------------------------|
| `CF_ACCESS_AUD`                  | audience tag / SaaS app Client ID          |
| `CF_ACCESS_OIDC_ISSUER`          | full CF OIDC issuer URL                    |
| `MCP_OAUTH_UPSTREAM_ISSUER`      | same as above (enables the DCR proxy)      |
| `MCP_OAUTH_CLIENT_ID`            | SaaS app Client ID                         |
| `MCP_OAUTH_CLIENT_SECRET`        | SaaS app Client Secret                     |
| `MCP_RESOURCE_URL`               | public URL of this server                  |

Full setup (CF dashboard config, systemd env file, ingress) →
[`deploy/README.md`](deploy/README.md#cloudflare-access-oidc-optional-but-recommended-for-team-use).

## Client compatibility

| Client                                          | Auth mode                       | Status |
|-------------------------------------------------|---------------------------------|--------|
| Claude Code CLI (`claude mcp add --transport http … -H "Authorization: Bearer …"`) | Multi-user bearer                | ✅ Works |
| Claude Code CLI + OAuth (`--client-id/--client-secret`) | OAuth 2.1 with static credentials | ✅ Works |
| Anthropic Messages API (`mcp_servers` + `authorization_token`) | Static bearer, no OAuth flow    | ✅ Works |
| claude.ai Custom Connector (web)                | OAuth 2.1 via CF Access OIDC    | ⚠️ Beta — see below |

### claude.ai Custom Connector — known issue

Custom Connectors is still marked BETA. Earlier versions (≤0.6.x) exposed a
single `email` dispatcher tool with a generic `params: dict`; Anthropic's
frontend rejected the setup with an opaque `ofid_...` reference before
opening the OAuth browser. In 0.7.0 the surface changed to 22 individually
typed tools with `outputSchema` and annotations, which may help.

If setup still fails:

* Fill in the **OAuth Client ID** and **Secreto del cliente OAuth** fields
  from your CF Access SaaS app manually (leaving them empty relies on DCR,
  which we proxy but Claude's frontend may still fail on).
* Contact Anthropic support with the exact `ofid_...` reference from the
  error toast — only they can look up what specifically failed.
* Meanwhile use the Claude Code CLI (works fully) or the Messages API with
  a static `authorization_token`.

## Send gate (recommended when an agent has this tool)

Prevent accidental sends by requiring a shared secret:

```bash
export EMAIL_SEND_CONFIRMATION_CODE=please-send
```

Every `send` / `send_invite` call must include `confirm: "please-send"` in
params, or the server refuses.

## Security notes

* `.env` is git-ignored. Never commit it.
* Prefer a **dedicated mailbox** (e.g. `mcp@yourdomain.com`) with a small quota.
* If your provider supports **app passwords** (Gmail, Fastmail, iCloud), use one
  instead of your primary password.
* MCP env vars end up in `~/.claude.json` on your machine — treat that file
  like a keychain.

## License

MIT — see [LICENSE](LICENSE).
