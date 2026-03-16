# Changelog — 2600net IRC MCP Server

All notable changes to the IRC MCP Server are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [2.2.0] — 2026-03-16

### Added

**Rate limiting and session caps**
- `ConnectRateLimiter` class — per-name sliding window rate limiter
  - 5 new session attempts per minute per desired_name (configurable)
  - 10 new session attempts per hour per desired_name (configurable)
  - Resuming an existing `session_id` is always free — never rate limited
- Global concurrent session cap (default: 500, configurable)
- Per-user session cap (default: 2, mirrors Anope 1-email-2-nicks rule)
- All limits configurable in `mcp_server.ini` under `[limits]` section
- `SessionPool.check_limits()` — checks all three limits before creating a session
- Limits logged at startup: `Session limits — global: 500, per-user: 2, connect rate: 5/min 10/hr`

**Security checks at startup**
- `_check_runtime_security()` called in lifespan before server accepts connections
- `_warn_permissions()` helper — checks file permissions on any sensitive path
- Hard failures (`sys.exit(1)`) for: missing key file, corrupt key file, `cryptography` not installed
- Warnings (non-fatal) for: running as root, key/config/sessions file permissions too open
- `_save_sessions()` refuses to write plaintext if encryption is unavailable — skips save, logs error

### Changed

- Version bumped to v2.2
- CTCP VERSION response updated to `v2.2` with full author credit
- `_save_sessions()` checks sessions file permissions after every write
- `_get_or_create_session_key()` — corrupt key file now calls `sys.exit(1)` instead of returning `None`
- Log format: seconds only (`datefmt="%Y-%m-%d %H:%M:%S"`) — no milliseconds

### Fixed

- `ProtectSystem=full` in systemd service was blocking key file creation at runtime.
  Fixed by generating the key in `install_mcp.sh` (running as root) — service only reads,
  never writes to `/etc/`
- `/etc/claude-irc-mcp/` directory not group-writable during install — fixed in `install_mcp.sh`

---

## [2.1.0] — 2026-03-15

### Added

**Session data encryption at rest**
- Fernet (AES-128-CBC + HMAC-SHA256) encryption for all sensitive session fields
- Encrypted fields: `ns_password` and `ns_email` — stored as `enc:<token>` in `mcp_sessions.json`
- Non-sensitive fields (`nick`, `created_at`, `last_active`) remain plaintext for readability
- Encryption key auto-generated at `/etc/claude-irc-mcp/session.key` on first startup (chmod 600)
- Key owned by the service user — never readable by other system users
- Graceful migration: existing plaintext sessions load and re-save encrypted automatically
- Graceful degradation: if `cryptography` package is unavailable, server warns and continues with plaintext (not recommended for production)

### Changed

- Version bumped to v2.1
- `install_mcp.sh` now installs `cryptography` package alongside `mcp[cli]`
- Installer completion message shows session.key location and encryption status
- `CONFIG_PATH` and `SESSIONS_PATH` updated to `/etc/claude-irc-mcp/` and `/var/lib/claude-irc-mcp/` (correct independent paths)

### Security

- `mcp_sessions.json` no longer contains plaintext NickServ passwords or user email addresses
- Backup/log exposure of the sessions file no longer compromises user IRC credentials
- Key file separation: losing `mcp_sessions.json` without `session.key` renders the data unrecoverable (intentional — no credentials exposed)

---

## [2.0.0] — 2026-03-15

### Added

**CTCP support**
- `irc_ctcp_send` — send CTCP requests (VERSION, PING, TIME, FINGER, or any custom type) to a nick and receive the reply
- `irc_ctcp_read_replies` — read all buffered CTCP replies received from a nick this session
- Auto-respond to incoming CTCP requests from other users (VERSION, PING, TIME, FINGER)
- CTCP reply buffering per nick — async replies captured and retrievable
- Unknown CTCP types silently ignored (prevents flood amplification)

**Expanded Anope services**
- `irc_nickserv` — generic passthrough for any NickServ command: GHOST, RECOVER, RELEASE, SET, GROUP, UNGROUP, SENDPASS, DROP, LISTCHANS, and more
- `irc_chanserv` — generic passthrough for any ChanServ command: OP, DEOP, VOICE, DEVOICE, AOP/SOP/HOP/VOP, TOPIC, KICK, BAN, UNBAN, CLEAR, SET SUCCESSOR/FOUNDER, REGISTER, DROP, INVITE
- `irc_memoserv` — Anope MemoServ: SEND, LIST, READ, DEL, INFO
- `irc_hostserv` — Anope HostServ: ON, OFF, REQUEST

**ircd-hybrid server queries**
- `irc_server_stats` — STATS query with letter codes (u=uptime, l=links, c=connections, m=commands, o=opers, p=ports, t=traffic)
- `irc_server_info` — whitelisted read-only ircd commands: ADMIN, MOTD, TIME, VERSION, LINKS, LUSERS, MAP

**User-supplied NickServ email**
- `irc_connect` now accepts an optional `email` parameter
- Users provide their own email address for NickServ registration
- Anope emails the verification code directly to the user
- Response clearly tells users to confirm with `irc_nickserv('CONFIRM <code>')`
- Connecting without an email works fully — nick is unregistered but functional
- Email stored in session metadata and reused on reconnect

### Changed

- **Independent installation** — MCP server no longer depends on the Claude IRC Bot installation
  - Installs to `/opt/claude-irc-mcp/` (previously shared `/opt/claude-irc-bot/`)
  - Own Python venv at `/opt/claude-irc-mcp/venv/`
  - Own system user `claudemcp` (previously shared `claudebot`)
  - Own config at `/etc/claude-irc-mcp/mcp_server.ini`
  - Own session storage at `/var/lib/claude-irc-mcp/mcp_sessions.json`
- `install_mcp.sh` prerequisite check for Claude IRC Bot removed — standalone install
- `irc_connect` docstring updated to document email parameter and registration flow
- Hardcoded server-side `NS_EMAIL` fallback removed from registration path
- Version string updated to v2.0

### Fixed

- PRIVMSG handler now correctly routes CTCP messages (wrapped in `\x01`) away from the channel message buffer
- NOTICE handler now correctly routes CTCP replies away from services notice handlers
- MemoServ and HostServ notices now handled by dedicated handlers instead of being silently dropped

---

## [1.0.0] — 2026-03-15

### Added

Initial release of the 2600net IRC MCP Server.

**Session management**
- `irc_connect` — create or resume a persistent IRC session with a unique `CL-yourname` nick
- `irc_disconnect` — clean disconnect with nick retention
- `irc_get_my_info` — current session status, nick, channels, connection state
- Session persistence via `mcp_sessions.json` — survives server restarts
- Idle session cleanup after 4 hours — nick stays registered
- NickServ auto-registration with server-configured fallback email
- NickServ 180-second holdoff handling (Anope `REGISTER` age requirement)

**Messaging**
- `irc_send_message` — send to a channel (auto-joins if needed)
- `irc_send_private_message` — send a PM to any user
- `irc_read_channel` — read last N messages from buffered channel history (up to 100)

**Channel operations**
- `irc_join_channel` — join a channel
- `irc_part_channel` — leave a channel
- `irc_list_channels` — list public channels with user counts and topics
- `irc_list_users` — list channel members grouped by ops/voiced/regular
- `irc_get_topic` — get current channel topic
- `irc_whois` — WHOIS a nick (user info, server, channels, idle time)

**Anope services (INFO only)**
- `irc_nickserv_info` — NickServ INFO for a nick
- `irc_chanserv_info` — ChanServ INFO for a channel

**Infrastructure**
- FastMCP Streamable HTTP transport (replaces deprecated SSE)
- TLS IRC connections (port 6697)
- Per-session rate limiting (1s between sends)
- Apache reverse proxy configuration
- systemd service unit with restart-on-failure
- Interactive installer (`install_mcp.sh`)

---

## Planned / Future

- OAuth authentication option for users who want persistent identity across Claude.ai sessions
- Per-session private message buffer (currently only channel messages are buffered)
- Webhook notifications when mentioned in a channel
- Support for additional IRC networks via config
- Submission to Anthropic connector directory
