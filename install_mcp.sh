#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# 2600net IRC MCP Server — Installer
# Requires the Claude IRC Bot to already be installed (shares the venv).
# Run as root: sudo bash install_mcp.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[*]${RESET} $*"; }
success() { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*"; exit 1; }
ask()     { echo -e "${BOLD}${CYAN}[?]${RESET} ${BOLD}$*${RESET}"; }

[[ $EUID -ne 0 ]] && error "Please run as root: sudo bash install_mcp.sh"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║       2600net IRC MCP Server — Installer             ║${RESET}"
echo -e "${BOLD}║   Claude.ai connector for 2600net IRC                ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Check prerequisites ───────────────────────────────────────────────────────
[[ -d /opt/claude-irc-bot/venv ]] || \
    error "Claude IRC Bot venv not found. Run install.sh first."

[[ -d /etc/claude-irc-bot ]] || \
    error "Claude IRC Bot config directory not found. Run install.sh first."

# ── Collect config ────────────────────────────────────────────────────────────
echo -e "${BOLD}── Configuration ───────────────────────────────────────${RESET}"
echo ""

ask "Email for NickServ user registrations [irc-mcp@2600.chat]:"
read -r NS_EMAIL; NS_EMAIL="${NS_EMAIL:-irc-mcp@2600.chat}"

ask "MCP server port (localhost only) [8765]:"
read -r MCP_PORT; MCP_PORT="${MCP_PORT:-8765}"

ask "System user to run as [claudebot]:"
read -r SYS_USER; SYS_USER="${SYS_USER:-claudebot}"

echo ""

# ── Install MCP server script ─────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "${SCRIPT_DIR}/irc_mcp_server.py" /opt/claude-irc-bot/
chown "${SYS_USER}:${SYS_USER}" /opt/claude-irc-bot/irc_mcp_server.py
chmod 750 /opt/claude-irc-bot/irc_mcp_server.py
success "Installed irc_mcp_server.py"

# ── Install MCP Python dependencies ──────────────────────────────────────────
info "Installing MCP package into venv ..."
/opt/claude-irc-bot/venv/bin/pip install --quiet --upgrade "mcp[cli]"
success "MCP package installed."

# ── Write config ──────────────────────────────────────────────────────────────
cat > /etc/claude-irc-bot/mcp_server.ini <<EOCONF
[irc]
server    = irc.scuttled.net
port      = 6697
ns_email  = ${NS_EMAIL}

[mcp]
host      = 127.0.0.1
port      = ${MCP_PORT}

[sessions]
idle_timeout_hours = 4
EOCONF

chown "root:${SYS_USER}" /etc/claude-irc-bot/mcp_server.ini
chmod 640 /etc/claude-irc-bot/mcp_server.ini
success "Written config: /etc/claude-irc-bot/mcp_server.ini"

# ── Create session storage directory ─────────────────────────────────────────
mkdir -p /var/lib/claude-irc-bot
chown "${SYS_USER}:${SYS_USER}" /var/lib/claude-irc-bot
chmod 750 /var/lib/claude-irc-bot
success "Created session storage: /var/lib/claude-irc-bot"

# ── Log file ──────────────────────────────────────────────────────────────────
touch /var/log/claude-irc-mcp.log
chown "${SYS_USER}:${SYS_USER}" /var/log/claude-irc-mcp.log
success "Created log: /var/log/claude-irc-mcp.log"

# ── systemd service ───────────────────────────────────────────────────────────
cp "${SCRIPT_DIR}/claude-irc-mcp.service" /etc/systemd/system/
# Patch the user/group in case a non-default user was chosen
sed -i "s/^User=claudebot/User=${SYS_USER}/" /etc/systemd/system/claude-irc-mcp.service
sed -i "s/^Group=claudebot/Group=${SYS_USER}/" /etc/systemd/system/claude-irc-mcp.service
systemctl daemon-reload
systemctl enable claude-irc-mcp
success "Installed and enabled systemd service."

# ── Apache config hint ────────────────────────────────────────────────────────
echo ""
echo -e "${YELLOW}── Apache configuration needed ─────────────────────────${RESET}"
echo ""
echo -e "Add the following to your HTTPS VirtualHost for wpm.2600.chat:"
echo ""
cat "${SCRIPT_DIR}/mcp_apache.conf"
echo ""
echo -e "Then run:"
echo -e "  ${BOLD}sudo a2enmod proxy proxy_http headers rewrite${RESET}"
echo -e "  ${BOLD}sudo apache2ctl configtest${RESET}"
echo -e "  ${BOLD}sudo systemctl reload apache2${RESET}"
echo ""

# ── Done ──────────────────────────────────────────────────────────────────────
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║            MCP Server install complete!              ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Test run:   ${BOLD}/opt/claude-irc-bot/venv/bin/python3 /opt/claude-irc-bot/irc_mcp_server.py${RESET}"
echo -e "  Start:      ${BOLD}sudo systemctl start claude-irc-mcp${RESET}"
echo -e "  Logs:       ${BOLD}sudo journalctl -u claude-irc-mcp -f${RESET}"
echo ""
echo -e "  MCP URL:    ${CYAN}https://wpm.2600.chat/mcp${RESET}"
echo -e "  Add to Claude.ai: Settings → Connectors → Add → paste the URL above"
echo ""
