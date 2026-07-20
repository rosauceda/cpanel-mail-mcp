# cpanel-mail-mcp

MCP server for IMAP/SMTP email accounts — works with cPanel, Gmail
(app passwords), Outlook, Fastmail, iCloud, or any provider that speaks
plain IMAP + SMTP.

## Features

* **Multi-user server mode** — one instance, many users, each with their own bearer token and mailbox (per-request isolation)
* **Multi-account** — manage multiple email accounts from different providers
* **Read, search, list** — full IMAP support with folder browsing
* **Send emails** — plain text, HTML, or both (multipart/alternative)
* **Attachments** — send via file path or base64-encoded inline data
* **Download attachments** — extract attachments from received emails as base64
* **Calendar invites** — send proper ICS invitations with Accept/Decline buttons
* **Save to Sent** — automatically saves sent emails to the Sent folder via IMAP
* **Optional send gate** — configurable confirmation code to prevent accidental sends
* **International folders** — handles UTF-7 encoded folder names (German, etc.)
* **Compact MCP surface** — one `email` tool with lazy action discovery to reduce
  client context use

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

### Loading a dev `.env`

The server auto-loads `.env` from the current working directory. Point it
elsewhere with:

```bash
export EMAIL_ENV_FILE=/absolute/path/to/.env
```

## Usage — the `email` tool

The server exposes **one** MCP tool named `email`. Call it with an `action`
string and a `params` dict. Discover actions with `action='help'` — the
default when `action` is omitted:

```json
{ "action": "help" }
```

### Actions at a glance

| action                | purpose                                          |
|-----------------------|--------------------------------------------------|
| `help`                | list all actions and their signatures            |
| `list_accounts`       | see configured accounts (no secrets returned)    |
| `list_folders`        | list IMAP folders (UTF-7 decoded)                |
| `list_recent`         | last N messages of a folder                      |
| `search`              | IMAP search by FROM/TO/SUBJECT/BODY/TEXT         |
| `read`                | read a message by UID (optionally with attachments) |
| `download_attachments`| fetch attachments as base64                       |
| `send`                | send an email (text/HTML/attachments)             |
| `save_draft`          | append a draft to the account's Drafts folder     |
| `send_invite`         | send an ICS calendar invite                       |

### Send an email with an attachment

```json
{
  "action": "send",
  "params": {
    "to": "someone@example.com",
    "subject": "Report",
    "text": "See attached.",
    "html": "<p>See <b>attached</b>.</p>",
    "attachments": [
      {"path": "/tmp/report.pdf"},
      {"name": "note.txt", "content": "hi from inline"}
    ]
  }
}
```

Attachment shapes accepted:
- `{"path": "/local/file.pdf", "name": "renamed.pdf"?}` — read from disk
- `{"name": "x.bin", "content_base64": "..."}` — inline base64
- `{"name": "x.txt", "content": "hello", "mime": "text/plain"?}` — inline text

### Read a message and its attachments

```json
{
  "action": "read",
  "params": {"uid": "42", "folder": "INBOX", "include_attachments": true}
}
```

### Download only specific attachments

```json
{
  "action": "download_attachments",
  "params": {"uid": "42", "filenames": ["report.pdf"]}
}
```

### Send a calendar invite

```json
{
  "action": "send_invite",
  "params": {
    "to": "guest@example.com",
    "subject": "Kickoff",
    "start": "2026-07-25 09:00",
    "end":   "2026-07-25 10:00",
    "location": "Zoom link here",
    "description": "quarter review"
  }
}
```

Datetimes accept ISO 8601 (`2026-07-25T09:00:00-06:00`) or
`YYYY-MM-DD HH:MM`. Naive times are assumed UTC.

### Save a draft

```json
{"action": "save_draft", "params": {"subject": "todo", "text": "..."}}
```

### Search

```json
{"action": "search", "params": {"query": "invoice", "field": "SUBJECT"}}
```

### Pick an account (multi-account setup)

Any action accepts an optional `account` param; omit it to use the first
configured account:

```json
{"action": "send", "params": {"account": "work", "to": "...", "text": "..."}}
```

## Run as an HTTP server (LXC / VPS / homelab)

By default `cpanel-mail-mcp` runs over **stdio** — your MCP client
launches it per session. To run it 24/7 as a shared HTTPS endpoint,
you have two shapes:

### Multi-user (recommended for a team)

One instance, many users. Each person has their own bearer token, and the
server enforces that they can only touch their own mailbox. Setup:

```bash
# on the server (Debian/Ubuntu LXC as root)
apt install -y curl      # if not present
curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash

# after logging out and back in (so $EMAIL_USERS_FILE is exported):
cpanel-mail-mcp admin add-user --email juan@dominio.com --host mail.dominio.com
# prompts for password → prints juan's bearer token → hand it to juan

systemctl enable --now cpanel-mail-mcp
```

Each user, in their Claude Code:
```bash
claude mcp add --transport http --scope user cpanel-mail \
  --header "Authorization: Bearer <THEIR_TOKEN>" \
  https://mcp.yourdomain.com/mcp
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
