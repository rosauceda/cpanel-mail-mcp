#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Install cpanel-mail-mcp as a persistent HTTP MCP server.
# Target: Debian 12 / Ubuntu 22.04+ LXC or VM. Run as root.
#
#   curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash
#
# What it does:
#   • installs pipx + cpanel-mail-mcp from PyPI
#   • creates a dedicated system user (cpanelmcp)
#   • drops an /etc/cpanel-mail-mcp/accounts.json.example
#   • generates a random bearer token in /etc/cpanel-mail-mcp/token
#   • installs a systemd unit listening on 127.0.0.1:8080
#
# What you still do:
#   • edit /etc/cpanel-mail-mcp/accounts.json with real credentials
#   • run:  systemctl enable --now cpanel-mail-mcp
#   • point cloudflared (or nginx) at http://127.0.0.1:8080
# ─────────────────────────────────────────────────────────────
set -euo pipefail

USER_NAME="${CPANEL_MCP_USER:-cpanelmcp}"
CONFIG_DIR="${CPANEL_MCP_CONFIG_DIR:-/etc/cpanel-mail-mcp}"
STATE_DIR="/var/lib/${USER_NAME}"
BIND_HOST="${MCP_BIND_HOST:-127.0.0.1}"
BIND_PORT="${MCP_BIND_PORT:-8080}"

if [[ $EUID -ne 0 ]]; then
  echo "Este script debe correr como root." >&2
  exit 1
fi

echo "==> apt update + deps"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y --no-install-recommends python3 python3-venv pipx ca-certificates >/dev/null

echo "==> system user ($USER_NAME)"
if ! id "$USER_NAME" &>/dev/null; then
  useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
install -d -m 750 -o "$USER_NAME" -g "$USER_NAME" "$CONFIG_DIR"

echo "==> pipx install cpanel-mail-mcp (as $USER_NAME)"
sudo -u "$USER_NAME" -H env HOME="$STATE_DIR" PATH="$STATE_DIR/.local/bin:/usr/bin:/bin" bash -lc '
  set -e
  pipx install --force cpanel-mail-mcp
'
BIN="$STATE_DIR/.local/bin/cpanel-mail-mcp"
[[ -x "$BIN" ]] || { echo "no encontré el binario en $BIN" >&2; exit 1; }

echo "==> config: accounts.json template"
if [[ ! -f "$CONFIG_DIR/accounts.json" ]]; then
  cat > "$CONFIG_DIR/accounts.json" <<'JSON'
[
  {
    "name": "default",
    "user": "you@example.com",
    "password": "REPLACE_ME",
    "smtp_host": "mail.example.com",
    "smtp_port": 465,
    "imap_host": "mail.example.com",
    "imap_port": 993,
    "sent_folder": "INBOX.Sent",
    "drafts_folder": "INBOX.Drafts",
    "save_to_sent": true
  }
]
JSON
  chown "$USER_NAME:$USER_NAME" "$CONFIG_DIR/accounts.json"
  chmod 600 "$CONFIG_DIR/accounts.json"
  ACCOUNTS_NEW=1
fi

echo "==> bearer token"
TOKEN_FILE="$CONFIG_DIR/token"
if [[ ! -s "$TOKEN_FILE" ]]; then
  TOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')
  echo "$TOK" > "$TOKEN_FILE"
  chown "$USER_NAME:$USER_NAME" "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  TOKEN_NEW=1
fi

# EnvironmentFile keeps the token out of `systemctl show`
if [[ ! -f "$CONFIG_DIR/env" ]]; then
  {
    echo "MCP_AUTH_TOKEN=$(cat "$TOKEN_FILE")"
  } > "$CONFIG_DIR/env"
  chown "$USER_NAME:$USER_NAME" "$CONFIG_DIR/env"
  chmod 600 "$CONFIG_DIR/env"
fi

echo "==> systemd unit"
cat > /etc/systemd/system/cpanel-mail-mcp.service <<UNIT
[Unit]
Description=cpanel-mail-mcp — HTTP MCP server for IMAP/SMTP mailboxes
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_HOST=$BIND_HOST
Environment=MCP_PORT=$BIND_PORT
Environment=EMAIL_ACCOUNTS_FILE=$CONFIG_DIR/accounts.json
EnvironmentFile=$CONFIG_DIR/env
ExecStart=$BIN
Restart=on-failure
RestartSec=3

# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE_DIR
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
LockPersonality=true
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

echo
echo "══════════════════════════════════════════════════════════════"
echo " cpanel-mail-mcp instalado."
echo "══════════════════════════════════════════════════════════════"
echo
echo " Siguiente:"
echo "  1) Edita:   $CONFIG_DIR/accounts.json   (pon tu(s) cuenta(s) reales)"
echo "  2) Arranca: systemctl enable --now cpanel-mail-mcp"
echo "  3) Status:  systemctl status cpanel-mail-mcp"
echo "  4) Logs:    journalctl -u cpanel-mail-mcp -f"
echo "  5) Salud:   curl -sf http://$BIND_HOST:$BIND_PORT/health   # -> ok"
echo
echo " Cloudflared: apunta tu hostname a  http://$BIND_HOST:$BIND_PORT"
echo " Ejemplo ingress (~/.cloudflared/config.yml):"
echo "   ingress:"
echo "     - hostname: mcp.tudominio.com"
echo "       service:  http://$BIND_HOST:$BIND_PORT"
echo "     - service:  http_status:404"
echo
echo " Bearer token para el cliente MCP:"
echo "   cat $TOKEN_FILE"
echo
echo " Registro en Claude Code (desde tu Mac):"
echo "   TOKEN=\$(ssh root@LXC cat $TOKEN_FILE)"
echo "   claude mcp add --transport http --scope user cpanel-mail \\"
echo "     --header \"Authorization: Bearer \$TOKEN\" \\"
echo "     https://mcp.tudominio.com/mcp"
echo
echo "══════════════════════════════════════════════════════════════"
