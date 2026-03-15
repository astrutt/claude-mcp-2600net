# 2600net IRC MCP Server

A remote MCP (Model Context Protocol) server that connects Claude.ai users to **2600net IRC** (`irc.scuttled.net`). Each user gets a persistent, NickServ-registered IRC nick. Built on Anthropic's FastMCP framework with Streamable HTTP transport.

**Written by Claude (Anthropic) & Andrew Strutt (r0d3nt) for 2600net**
**Repo:** https://github.com/astrutt/claude-mcp-2600net

---

## What it does

Add `https://wpm.2600.chat/mcp` as a connector in Claude.ai and Claude can:

- Connect to 2600net with a personal persistent IRC nick (`CL-yourname`)
- Send and read messages in any channel
- Register and manage their nick with Anope NickServ
- Manage channels via Anope ChanServ
- Send and receive memos via MemoServ
- Manage virtual hosts via HostServ
- Send and receive CTCP requests (VERSION, PING, TIME, FINGER)
- Query ircd-hybrid server information (STATS, MOTD, ADMIN, LINKS, etc.)

---

## Architecture

```
Claude.ai user
      ↓  HTTPS
wpm.2600.chat/mcp  (Apache reverse proxy, Let's Encrypt TLS)
      ↓  HTTP localhost:8765
IRC MCP Server  (FastMCP, Streamable HTTP, Python)
      ↓  TLS port 6697
irc.scuttled.net  (2600net IRC, ircd-hybrid + Anope services)
```

The MCP server is completely independent of the Claude IRC Bot — separate install directory, separate Python venv, separate system user.

---

## Requirements

- Ubuntu 22.04+ or Debian 11+ with Python 3.10+
- Apache with `mod_proxy`, `mod_proxy_http`, `mod_headers`, `mod_rewrite`
- Let's Encrypt SSL certificate for your domain
- An IRC network running ircd-hybrid with Anope services

---

## Install

```bash
git clone https://github.com/astrutt/claude-mcp-2600net.git
cd claude-mcp-2600net
sudo bash install_mcp.sh
```

The installer:
1. Creates a dedicated system user (`claudemcp`)
2. Installs to `/opt/claude-irc-mcp/` with its own Python venv
3. Installs Python packages (`mcp[cli]`)
4. Writes config to `/etc/claude-irc-mcp/mcp_server.ini`
5. Creates session storage at `/var/lib/claude-irc-mcp/`
6. Installs and enables a systemd service

### Apache configuration

Add the contents of `mcp_apache.conf` inside your existing HTTPS VirtualHost, then:

```bash
sudo a2enmod proxy proxy_http headers rewrite
sudo apache2ctl configtest
sudo systemctl reload apache2
```

### Start the service

```bash
sudo systemctl start claude-irc-mcp
sudo journalctl -u claude-irc-mcp -f
```

### Test (foreground)

```bash
/opt/claude-irc-mcp/venv/bin/python3 /opt/claude-irc-mcp/irc_mcp_server.py
```

---

## Connecting from Claude.ai

1. Go to **Settings → Connectors → Add custom connector**
2. Enter: `https://wpm.2600.chat/mcp`
3. Save — Claude now has access to all 22 IRC tools

### First use

Ask Claude to connect:

> "Connect me to 2600net IRC with the name 'yourname' and email 'you@example.com'"

Claude will call `irc_connect` and return a `session_id`. **Save this in your custom instructions** — it reconnects you to the same nick every time:

```
My 2600net IRC session_id is abc123def456...
```

### Nick registration

Providing an email triggers Anope NickServ registration. Anope will email you a verification code. Confirm it with:

> "Confirm my NickServ registration with code ABC123"

Claude will call `irc_nickserv('CONFIRM ABC123')`.

You can also connect without an email — your nick will be unregistered but fully functional.

---

## Tools reference

### Session management
| Tool | Description |
|---|---|
| `irc_connect` | Create or resume a session, get a persistent nick |
| `irc_disconnect` | Clean disconnect (nick stays registered) |
| `irc_get_my_info` | Show current nick, channels, connection state |

### Messaging
| Tool | Description |
|---|---|
| `irc_send_message` | Send a message to a channel |
| `irc_send_private_message` | Send a PM to a user |
| `irc_read_channel` | Read recent messages from a channel |

### Channel management
| Tool | Description |
|---|---|
| `irc_join_channel` | Join a channel |
| `irc_part_channel` | Leave a channel |
| `irc_list_channels` | List public channels on the network |
| `irc_list_users` | List users in a channel (ops, voiced, regular) |
| `irc_get_topic` | Get a channel's topic |
| `irc_whois` | WHOIS a nick |

### CTCP
| Tool | Description |
|---|---|
| `irc_ctcp_send` | Send CTCP request (VERSION, PING, TIME, FINGER, custom) |
| `irc_ctcp_read_replies` | Read buffered CTCP replies from a nick |

The server also auto-responds to incoming CTCP requests from other users.

### Anope services
| Tool | Description |
|---|---|
| `irc_nickserv` | Any NickServ command (INFO, GHOST, RECOVER, SET, GROUP, DROP, ...) |
| `irc_chanserv` | Any ChanServ command (OP, TOPIC, AOP/SOP/VOP/HOP, KICK, BAN, ...) |
| `irc_memoserv` | MemoServ (SEND, LIST, READ, DEL) |
| `irc_hostserv` | HostServ (ON, OFF, REQUEST) |

### ircd-hybrid server queries
| Tool | Description |
|---|---|
| `irc_server_stats` | STATS query (uptime, links, connections, commands, opers, ports, traffic) |
| `irc_server_info` | ADMIN, MOTD, TIME, VERSION, LINKS, LUSERS, MAP |

---

## Session behaviour

- Each Claude.ai user gets a unique `session_id` and IRC nick (`CL-yourname`)
- Sessions persist across MCP server restarts (stored in `mcp_sessions.json`)
- Idle sessions disconnect after 4 hours; nick stays registered
- Reconnecting with the same `session_id` reclaims the same nick
- NickServ passwords are auto-generated per session — users never see them

---

## Security notes

- The MCP server listens on `127.0.0.1` only — Apache handles all public TLS
- No authentication required — any Claude.ai user can connect (open by design)
- Each session is isolated — users can only act as their own nick
- ircd-hybrid server commands are whitelisted to read-only queries only (STATS, MOTD, etc.)
- CTCP unknown types are silently ignored to prevent flood amplification
- Rate limiting: 1 second minimum between IRC sends per session

---

## Files

| File | Purpose |
|---|---|
| `irc_mcp_server.py` | MCP server (22 tools) |
| `install_mcp.sh` | Interactive installer |
| `mcp_server.ini` | Configuration template |
| `mcp_apache.conf` | Apache reverse proxy snippet |
| `claude-irc-mcp.service` | systemd unit file |
| `README_MCP.md` | This file |
| `CHANGELOG_MCP.md` | Version history |

---

## Default network: 2600net

- Server: `irc.scuttled.net:6697` (TLS)
- Services: Anope (NickServ, ChanServ, MemoServ, HostServ, OperServ)
- IRCd: ircd-hybrid

To use with a different IRC network, edit `/etc/claude-irc-mcp/mcp_server.ini`.

---

## License

MIT — see `LICENSE`.
Contributions welcome via pull request.
