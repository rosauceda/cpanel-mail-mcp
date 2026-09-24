# Changelog

## 0.7.1

Safety and correctness release. Upgrading is strongly recommended, especially
for multi-user HTTP deployments.

### Security
- **`path` attachments can no longer read the server's files in HTTP mode.**
  Before, any authenticated user could attach e.g. `users.json` (every
  mailbox password) and mail it to themselves. `path` is now disabled in HTTP
  mode unless `MCP_ATTACHMENT_DIR` confines it to one directory; stdio mode is
  unchanged.
- **Account is resolved per HTTP request**, not from the session that
  `initialize` created, and a session can only be used by the identity that
  created it (others get 404).
- **`users.json` is re-read when it changes**: `add-user`, `rotate-token` and
  `remove-user` apply on the next request. Before, a revoked token kept working
  until the service restarted.
- `users.json` is written atomically, created 0600 and keeps its owner.
- Bearer tokens and the send-gate code are compared in constant time.

### Fixed
- **Message identifiers are real IMAP UIDs.** Before, list/search/read used
  sequence numbers while `move_email`/`delete_email` used `UID MOVE`, so a
  move or soft delete could hit a different message or silently do nothing.
- `uid` arguments accept exactly one numeric UID; ranges like `1:*` are
  rejected (previously `delete_email(uid="1:*", permanent=true)` emptied the
  folder).
- Soft delete never degrades into a permanent delete. The Trash folder comes
  from `trash_folder` (argument or account), else SPECIAL-USE `\Trash`, else
  common names; if none exists the call fails and nothing is deleted.
- Searching with accents (`Reunión`) or quotes no longer crashes (UTF-8
  `SEARCH CHARSET` with a literal, ASCII fallback).
- `reply_email` is threaded again (`In-Reply-To` + full `References` chain),
  replies to `Reply-To` when present, and `reply_all` leaves out your own
  address.
- `get_thread` finds replies, not just ancestors.
- Error hints (`Hint: call list_folders…`) now reach the model.
- Sent messages, drafts and invites carry a `Date` header; invites carry a
  `Message-ID`.
- Recipients with commas in the display name (`"Doe, John" <j@x.com>`) stay
  one recipient.
- Single-part HTML emails land in `body_html`; forwarding one keeps the HTML.
- Unknown charsets and malformed headers no longer crash `read_email`.
- ICS: parameter values quoted, lines folded at 75 octets, display names parsed.
- OAuth proxy reads the upstream `token_endpoint_auth_methods_supported`.

### Added
- **`unread_only` and `since` filters** on `list_recent` and `search_emails`.
  `since` takes an ISO date/datetime or a relative span (`30m`, `2h`, `1d`,
  `1w`) and is exact to the second (IMAP SINCE widened by a day, then filtered
  on INTERNALDATE). Every message now carries `received_at` (server receive
  time) — handy for polling workflows in n8n.
- **Credentials mode for n8n / Docker** (`MCP_AUTH_MODE=credentials`): each
  request carries the mailbox login (`X-Email-User` + `X-Email-Password`, or
  Basic auth) — n8n's MCP Client Tool "Multiple Headers Auth". No users.json,
  no passwords at rest. Logins are verified against IMAP and cached
  (`MCP_CREDENTIAL_CACHE_SECONDS`); failures are throttled per mailbox and
  globally; `MCP_ALLOWED_EMAIL_DOMAINS` and `MCP_API_KEY` narrow access
  further. Mail server fixed by `CPANEL_HOST`.
- **Dockerfile** (non-root, healthcheck) and `docker-compose.yml`, ready for
  Dokploy.
- **`has_attachments` / `attachment_count`** on every message returned by
  `list_recent`, `search_emails` and `get_thread` (read from IMAP
  BODYSTRUCTURE — no bodies downloaded) and on `read_email`. Images embedded
  in the HTML body (signature logos, `cid:`) don't count; `read_email` lists
  them with `embedded: true`. `null` means the server didn't report the
  structure.
- Emails attached to an email (message/rfc822) are listed and downloadable
  as `<subject>.eml` attachments.

### Changed
- Tools are async and run IMAP/SMTP work in threads: a slow mailbox no longer
  blocks other users or `/health`. IMAP has a socket timeout
  (`MCP_IMAP_TIMEOUT`, default 30 s).
- `read_email`, `list_recent`, `search_emails` never change flags (EXAMINE +
  `BODY.PEEK`); `read_email` no longer marks messages as read — call
  `mark_read`.
- `list_recent`/`search_emails` return `flags` per message (no `\Seen` =
  unread) and fetch headers in one round trip.
- `read_email` returns `message_id`, `in_reply_to`, `references`, `reply_to`,
  `flags`, and attachment `size` even without `include_attachments`.
- `move_email`/`copy_email`/`delete_email` return `new_uid` when the server
  reports it (COPYUID).
- `send_invite` takes `timezone`; naive times use it, then
  `MCP_DEFAULT_TIMEZONE`, then UTC.
- New account field `trash_folder` and admin flag `--trash-folder`.
- Bound to a non-loopback `MCP_HOST` without `MCP_ALLOWED_HOSTS`, the server
  no longer answers 421 to proxied / container-name requests (the SDK's
  localhost-only Host check is only applied to loopback binds).
- Multi-user server starts even with an empty `users.json` (users added later
  are picked up automatically).
- Printed `claude mcp add` commands put the URL before `--header`.
- Requires `mcp>=1.28,<2`. **mcp 2.x renamed FastMCP and breaks every earlier
  release on a fresh install** (0.7.0 declared `mcp>=1.2.0` with no upper bound).
