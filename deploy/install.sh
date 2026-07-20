#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Install cpanel-mail-mcp as a multi-user HTTP MCP server.
# Target: Debian 12 / Ubuntu 22.04+ LXC or VM. Run as root.
#
#   apt install -y curl        # if curl isn't in your base image
#   curl -fsSL https://raw.githubusercontent.com/rosauceda/cpanel-mail-mcp/main/deploy/install.sh | bash
#
# What it does:
#   • installs pipx + git + cpanel-mail-mcp from PyPI
#   • creates a dedicated system user (cpanelmcp)
#   • creates an empty /etc/cpanel-mail-mcp/users.json
#   • installs a systemd unit listening on 127.0.0.1:8080 in multi-user mode
#
# What you still do:
#   • add each user:  cpanel-mail-mcp admin add-user --email … --host …
#     (each user gets their own bearer token; only they can act on their mailbox)
#   • start service:  systemctl enable --now cpanel-mail-mcp
#   • put cloudflared (or nginx/caddy) in front for HTTPS
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
apt-get install -y --no-install-recommends python3 python3-venv pipx git ca-certificates >/dev/null

echo "==> system user ($USER_NAME)"
if ! id "$USER_NAME" &>/dev/null; then
  useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
install -d -m 750 -o "$USER_NAME" -g "$USER_NAME" "$CONFIG_DIR"

echo "==> pipx install cpanel-mail-mcp (as $USER_NAME)"
# runuser (util-linux) works on minimal images that don't ship sudo
runuser -u "$USER_NAME" -- env HOME="$STATE_DIR" PATH="$STATE_DIR/.local/bin:/usr/bin:/bin" bash -lc '
  set -e
  pipx install --force cpanel-mail-mcp
'
BIN="$STATE_DIR/.local/bin/cpanel-mail-mcp"
[[ -x "$BIN" ]] || { echo "no encontré el binario en $BIN" >&2; exit 1; }

echo "==> users.json (empty — you fill it with the admin CLI)"
USERS_FILE="$CONFIG_DIR/users.json"
if [[ ! -f "$USERS_FILE" ]]; then
  echo "[]" > "$USERS_FILE"
  chown "$USER_NAME:$USER_NAME" "$USERS_FILE"
  chmod 600 "$USERS_FILE"
fi

# convenience symlink so operators can run `cpanel-mail-mcp admin …` from root
ln -sf "$BIN" /usr/local/bin/cpanel-mail-mcp

# expose EMAIL_USERS_FILE to root's shell so the admin CLI knows where to write
if ! grep -q 'EMAIL_USERS_FILE' /etc/profile.d/cpanel-mail-mcp.sh 2>/dev/null; then
  cat > /etc/profile.d/cpanel-mail-mcp.sh <<PROFILE
# Point the admin CLI at the deploy's users.json
export EMAIL_USERS_FILE=$USERS_FILE
PROFILE
fi

echo "==> systemd unit"
cat > /etc/systemd/system/cpanel-mail-mcp.service <<UNIT
[Unit]
Description=cpanel-mail-mcp — HTTP MCP server for IMAP/SMTP mailboxes (multi-user)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=MCP_TRANSPORT=streamable-http
Environment=MCP_HOST=$BIND_HOST
Environment=MCP_PORT=$BIND_PORT
Environment=EMAIL_USERS_FILE=$USERS_FILE
# Behind a reverse proxy (Cloudflare Tunnel, nginx, caddy)? Add your public
# hostname(s) here or requests come back as 421 Misdirected Request.
# Environment=MCP_ALLOWED_HOSTS=mcp.yourdomain.com

# ── Cloudflare Access OIDC (optional; enables OAuth 2.1 for Claude Custom
#    Connector). Set these once you've created a SaaS OIDC app in CF Access.
# Environment=CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
# Environment=CF_ACCESS_AUD=<application-audience-tag>
# Environment=MCP_RESOURCE_URL=https://mcp.yourdomain.com
# Environment=MCP_OAUTH_AUTHORIZATION_SERVERS=https://<team>.cloudflareaccess.com/cdn-cgi/access/sso/oidc/<app_uid>
ExecStart=$BIN
Restart=on-failure
RestartSec=3

# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE_DIR $CONFIG_DIR
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
echo " cpanel-mail-mcp instalado (multi-user)."
echo "══════════════════════════════════════════════════════════════"
echo
echo " 1) Da de alta usuarios (uno por persona):"
echo
echo "     source /etc/profile.d/cpanel-mail-mcp.sh   # o cierra y abre la sesión SSH"
echo "     cpanel-mail-mcp admin add-user \\"
echo "       --email juan@dominio.com \\"
echo "       --host mail.dominio.com"
echo
echo "    Cada 'add-user' imprime el bearer token del usuario. Anótalo y compártelo"
echo "    con esa persona por un canal seguro (Signal, 1Password Send, etc.)."
echo
echo " 2) Arranca el servicio:  systemctl enable --now cpanel-mail-mcp"
echo " 3) Status:              systemctl status cpanel-mail-mcp"
echo " 4) Logs:                journalctl -u cpanel-mail-mcp -f"
echo " 5) Salud:               curl -sf http://$BIND_HOST:$BIND_PORT/health   # -> ok"
echo
echo " Cloudflared: apunta tu hostname a  http://$BIND_HOST:$BIND_PORT"
echo " Ejemplo ingress (~/.cloudflared/config.yml):"
echo "   ingress:"
echo "     - hostname: mcp.tudominio.com"
echo "       service:  http://$BIND_HOST:$BIND_PORT"
echo "     - service:  http_status:404"
echo
echo " Cada usuario, en su Claude Code:"
echo "   claude mcp add --transport http --scope user cpanel-mail \\"
echo "     --header \"Authorization: Bearer <SU_TOKEN>\" \\"
echo "     https://mcp.tudominio.com/mcp"
echo
echo " Admin CLI extra:"
echo "   cpanel-mail-mcp admin list-users"
echo "   cpanel-mail-mcp admin rotate-token --email juan@dominio.com"
echo "   cpanel-mail-mcp admin remove-user  --email juan@dominio.com"
echo
echo "══════════════════════════════════════════════════════════════"
