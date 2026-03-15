#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    2600net IRC MCP Server  v1.0                             ║
║                                                                              ║
║  An MCP server exposing 2600net IRC to any Claude.ai user.                  ║
║  Each user gets a persistent, NickServ-registered IRC nick.                 ║
║                                                                              ║
║  Written by Claude (claude-sonnet-4-20250514, Anthropic)                    ║
║  & Andrew Strutt (r0d3nt) for 2600net                                       ║
║  https://github.com/astrutt/claude-mcp-2600net                                  ║
║                                                                              ║
║  Transport:  Streamable HTTP (FastMCP)                                      ║
║  Endpoint:   https://wpm.2600.chat/mcp  (Apache reverse proxy)             ║
║  Sessions:   Persistent per-user IRC connections with NickServ registration ║
║                                                                              ║
║  License: MIT                                                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import ssl
import socket
import threading
import time
import configparser
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict, field_validator

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/var/log/claude-irc-mcp.log"),
    ],
)
log = logging.getLogger("irc_mcp")

# ── Constants ─────────────────────────────────────────────────────────────────
VERSION          = "2600net IRC MCP Server v2.0 | Claude (Anthropic) & Andrew Strutt (r0d3nt) | github.com/astrutt/claude-mcp-2600net"
CONFIG_PATH      = "/etc/claude-irc-bot/mcp_server.ini"
SESSIONS_PATH    = "/var/lib/claude-irc-bot/mcp_sessions.json"
NICK_PREFIX      = "CL-"
NICK_MAX_LEN     = 15                  # IRC standard max nick length
IDLE_TIMEOUT_S   = 4 * 3600           # 4 hours
CONNECT_TIMEOUT  = 30
CMD_TIMEOUT      = 8                   # seconds to wait for IRC numeric reply
MSG_BUFFER_SIZE  = 100                 # messages to keep per channel
SEND_RATE_S      = 1.0                 # min seconds between sends
LIST_MAX         = 100                 # max channels to return from LIST

# ── Config ────────────────────────────────────────────────────────────────────
def _load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg

_cfg = _load_config()
IRC_SERVER   = _cfg.get("irc", "server",   fallback="irc.scuttled.net")
IRC_PORT     = _cfg.getint("irc", "port",  fallback=6697)
NS_EMAIL     = _cfg.get("irc", "ns_email", fallback="irc-mcp@2600.chat")
MCP_HOST     = _cfg.get("mcp", "host",    fallback="127.0.0.1")
MCP_PORT     = _cfg.getint("mcp", "port", fallback=8765)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _irc_lower(s: str) -> str:
    return s.lower().translate(str.maketrans("[]\\", "{}|"))


def _sanitize_name(name: str) -> str:
    """
    Turn a user-supplied name into a valid IRC nick suffix.
    Keeps alphanumerics and hyphens, truncates to fit NICK_MAX_LEN.
    """
    clean = re.sub(r"[^a-zA-Z0-9\-]", "", name)
    if not clean:
        clean = secrets.token_hex(3)
    max_suffix = NICK_MAX_LEN - len(NICK_PREFIX)
    return clean[:max_suffix]


def _make_nick(name: str) -> str:
    return NICK_PREFIX + _sanitize_name(name)


def _parse_irc(line: str) -> dict:
    prefix = ""
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    parts   = line.split(" ", 1)
    command = parts[0]
    params  = []
    if len(parts) > 1:
        rest = parts[1]
        while rest:
            if rest.startswith(":"):
                params.append(rest[1:])
                break
            if " " in rest:
                p, rest = rest.split(" ", 1)
                params.append(p)
            else:
                params.append(rest)
                break
    return {"prefix": prefix, "command": command, "params": params}


def _nick_from_prefix(prefix: str) -> str:
    return prefix.split("!")[0] if "!" in prefix else prefix

# ── Session persistence ───────────────────────────────────────────────────────
def _load_sessions() -> dict:
    try:
        p = Path(SESSIONS_PATH)
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:
        log.error(f"Failed to load sessions: {e}")
    return {}


def _save_sessions(data: dict) -> None:
    try:
        Path(SESSIONS_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(SESSIONS_PATH).write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.error(f"Failed to save sessions: {e}")

# ── IRC Session ───────────────────────────────────────────────────────────────
class IRCSession:
    """
    A single persistent IRC connection for one Claude.ai user session.
    Runs its read loop in a daemon thread.
    """

    def __init__(self, session_id: str, nick: str, ns_password: str, ns_email: str = ""):
        self.session_id  = session_id
        self.nick        = nick           # desired/current nick
        self.ns_password = ns_password
        self.ns_email    = ns_email       # user-supplied email for NickServ registration
        self.connected   = False
        self.identified  = False          # NickServ identification complete
        self.last_active = time.monotonic()

        self._sock: ssl.SSLSocket | None = None
        self._running     = False
        self._thread: threading.Thread | None = None
        self._send_lock   = threading.Lock()
        self._last_send   = 0.0

        # Message buffers: channel → deque of {"ts", "nick", "text"}
        self.msg_buffer: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=MSG_BUFFER_SIZE)
        )
        self.joined_channels: set[str] = set()

        # Pending command results: keyed by type
        # Each entry: {"event": threading.Event, "result": list | str | None}
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()

        # NickServ/ChanServ collected notice lines
        self._ns_notice_buf: list[str] = []
        self._cs_notice_buf: list[str] = []
        self._ns_notice_event  = threading.Event()
        self._cs_notice_event  = threading.Event()

        # Identification event — tools can wait on this
        self._identified_event = threading.Event()

        # CTCP reply buffer: nick → deque of {"ts", "type", "response"}
        self.ctcp_replies: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=20)
        )
        self._ctcp_lock = threading.Lock()

        # Generic services notice buffers (MemoServ, HostServ, OperServ)
        self._memo_notice_buf:  list[str] = []
        self._memo_notice_event = threading.Event()
        self._host_notice_buf:  list[str] = []
        self._host_notice_event = threading.Event()

        # ircd numeric reply buffer (STATS, ADMIN, MOTD, TIME, LINKS, MAP etc.)
        self._ircd_buf:   list[str] = []
        self._ircd_event  = threading.Event()

    # ── Connection ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._connect_loop, daemon=True,
            name=f"irc-{self.session_id[:8]}"
        )
        self._thread.start()

    def _connect_loop(self) -> None:
        try:
            ctx = ssl.create_default_context()
            raw = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=CONNECT_TIMEOUT)
            tls = ctx.wrap_socket(raw, server_hostname=IRC_SERVER)
            tls.settimeout(None)
            self._sock     = tls
            self.connected = True
            log.info(f"[{self.session_id[:8]}] TLS connected as {self.nick}")
            self._send_raw(f"NICK {self.nick}")
            self._send_raw(f"USER claude 0 * :Claude AI MCP User")
            self._read_loop()
        except Exception as e:
            log.error(f"[{self.session_id[:8]}] Connection error: {e}")
            self.connected = False

    def _read_loop(self) -> None:
        buf = ""
        while self._running:
            try:
                data = self._sock.recv(4096).decode("utf-8", errors="replace")
                if not data:
                    break
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._handle_line(line.rstrip("\r"))
            except (OSError, ssl.SSLError) as e:
                if self._running:
                    log.error(f"[{self.session_id[:8]}] Read error: {e}")
                break
        self.connected = False

    def _send_raw(self, line: str) -> None:
        with self._send_lock:
            now = time.monotonic()
            gap = now - self._last_send
            if gap < SEND_RATE_S:
                time.sleep(SEND_RATE_S - gap)
            self._last_send = time.monotonic()
            log.debug(f"[{self.session_id[:8]}] >> {line}")
            try:
                self._sock.sendall((line + "\r\n").encode("utf-8", errors="replace"))
            except Exception as e:
                log.error(f"[{self.session_id[:8]}] Send error: {e}")

    def send_message(self, target: str, text: str) -> None:
        self.last_active = time.monotonic()
        self._send_raw(f"PRIVMSG {target} :{text}")

    def disconnect(self, reason: str = "Session ended") -> None:
        self._running = False
        try:
            self._send_raw(f"QUIT :{reason}")
            time.sleep(0.3)
            self._sock.close()
        except Exception:
            pass
        self.connected = False
        log.info(f"[{self.session_id[:8]}] Disconnected.")

    # ── IRC event handling ──────────────────────────────────────────────────

    def _handle_line(self, line: str) -> None:
        log.debug(f"[{self.session_id[:8]}] << {line}")
        msg = _parse_irc(line)
        cmd = msg["command"]
        p   = msg["params"]

        if cmd == "PING":
            self._send_raw(f"PONG :{p[0]}")

        elif cmd == "001":
            log.info(f"[{self.session_id[:8]}] Registered. Starting NickServ flow.")
            threading.Thread(target=self._nickserv_flow, daemon=True).start()

        elif cmd == "433":   # nick in use
            self.nick = self.nick + "_"
            self._send_raw(f"NICK {self.nick}")

        elif cmd == "NICK":
            who = _nick_from_prefix(msg["prefix"])
            if _irc_lower(who) == _irc_lower(self.nick):
                self.nick = p[0]

        elif cmd == "JOIN":
            who = _nick_from_prefix(msg["prefix"])
            if _irc_lower(who) == _irc_lower(self.nick):
                self.joined_channels.add(_irc_lower(p[0]))

        elif cmd == "PART" or cmd == "KICK":
            who = _nick_from_prefix(msg["prefix"])
            ch  = p[0]
            if _irc_lower(who) == _irc_lower(self.nick):
                self.joined_channels.discard(_irc_lower(ch))

        elif cmd == "PRIVMSG":
            sender = _nick_from_prefix(msg["prefix"])
            target = p[0]
            text   = p[1] if len(p) > 1 else ""
            ch_key = _irc_lower(target)

            # CTCP request directed at us
            if text.startswith("\x01") and text.endswith("\x01"):
                self._handle_ctcp_request(sender, text[1:-1])
            elif target.startswith("#"):
                self.msg_buffer[ch_key].append({
                    "ts":   time.strftime("%H:%M:%S", time.gmtime()),
                    "nick": sender,
                    "text": text,
                })

        elif cmd == "NOTICE":
            sender = _nick_from_prefix(msg["prefix"]).lower()
            target = p[0] if p else ""
            text   = p[1] if len(p) > 1 else ""

            # CTCP reply (NOTICE with \x01 wrapping)
            if text.startswith("\x01") and text.endswith("\x01"):
                self._handle_ctcp_reply(sender, text[1:-1])
                return

            if sender == "nickserv":
                self._handle_ns_notice(text)
            elif sender == "chanserv":
                self._handle_cs_notice(text)
            elif sender == "memoserv":
                self._handle_memo_notice(text)
            elif sender == "hostserv":
                self._handle_host_notice(text)

        # ── Numeric replies ────────────────────────────────────────────────

        # WHOIS (311-318)
        elif cmd in ("311", "312", "319", "317"):
            self._pending_append("whois", " ".join(p[1:]))

        elif cmd == "318":   # end of WHOIS
            self._pending_append("whois", " ".join(p[1:]))
            self._pending_signal("whois")

        elif cmd == "401":   # no such nick (WHOIS)
            self._pending_append("whois", f"No such nick: {p[1] if len(p)>1 else '?'}")
            self._pending_signal("whois")

        # NAMES (353, 366)
        elif cmd == "353":
            channel = p[2] if len(p) > 2 else ""
            nicks   = p[3] if len(p) > 3 else ""
            key     = f"names:{_irc_lower(channel)}"
            self._pending_append(key, nicks)

        elif cmd == "366":   # end of NAMES
            channel = p[1] if len(p) > 1 else ""
            key     = f"names:{_irc_lower(channel)}"
            self._pending_signal(key)

        # LIST (322, 323)
        elif cmd == "322":
            channel  = p[1] if len(p) > 1 else ""
            count    = p[2] if len(p) > 2 else "0"
            topic    = p[3] if len(p) > 3 else ""
            self._pending_append("list", f"{channel} ({count} users): {topic}")

        elif cmd == "323":   # end of LIST
            self._pending_signal("list")

        # TOPIC (332, 331)
        elif cmd == "332":
            channel = p[1] if len(p) > 1 else ""
            topic   = p[2] if len(p) > 2 else ""
            key     = f"topic:{_irc_lower(channel)}"
            self._pending_set(key, topic)
            self._pending_signal(key)

        elif cmd == "331":   # no topic
            channel = p[1] if len(p) > 1 else ""
            key     = f"topic:{_irc_lower(channel)}"
            self._pending_set(key, "(no topic set)")
            self._pending_signal(key)

        # TOPIC live change
        elif cmd == "TOPIC":
            channel = p[0]
            topic   = p[1] if len(p) > 1 else ""
            key     = f"topic:{_irc_lower(channel)}"
            self._pending_set(key, topic)

        # ircd-hybrid / generic server numerics → ircd buffer
        # STATS:  210, 211, 212, 213, 214, 219
        # ADMIN:  256, 257, 258, 259
        # MOTD:   372, 376
        # TIME:   391
        # LINKS:  364, 365
        # MAP:    015 or RPL_MAP (server specific)
        # TRACE:  200-209, 261, 262
        # VERSION: 351
        # LUSERS: 251-255, 265, 266
        elif cmd in (
            "210","211","212","213","214","215","216","217","218","219",
            "256","257","258","259",
            "200","201","202","203","204","205","206","207","208","209","261","262",
            "351","364","365","391",
            "251","252","253","254","255","265","266",
            "372","375","376",
            "015",
        ):
            self._ircd_buf.append(" ".join(p))
            # Signal end-of-block numerics
            if cmd in ("219","259","262","365","391","376","266"):
                self._ircd_event.set()

    # ── Pending results helper ──────────────────────────────────────────────

    def _pending_init(self, key: str) -> threading.Event:
        with self._pending_lock:
            evt = threading.Event()
            self._pending[key] = {"event": evt, "lines": [], "result": None}
            return evt

    def _pending_append(self, key: str, line: str) -> None:
        with self._pending_lock:
            if key in self._pending:
                self._pending[key]["lines"].append(line)

    def _pending_set(self, key: str, result: str) -> None:
        with self._pending_lock:
            if key in self._pending:
                self._pending[key]["result"] = result

    def _pending_signal(self, key: str) -> None:
        with self._pending_lock:
            if key in self._pending:
                self._pending[key]["event"].set()

    def _pending_wait(self, key: str, timeout: float = CMD_TIMEOUT) -> list[str] | str | None:
        """Wait for a pending command to complete, return lines or result."""
        entry = self._pending.get(key)
        if not entry:
            return None
        entry["event"].wait(timeout=timeout)
        with self._pending_lock:
            popped = self._pending.pop(key, {})
        result = popped.get("result")
        lines  = popped.get("lines", [])
        return result if result is not None else lines

    # ── NickServ notice handler ─────────────────────────────────────────────

    def _handle_ns_notice(self, text: str) -> None:
        tl = text.lower()
        log.info(f"[{self.session_id[:8]}] [NickServ] {text}")

        if any(k in tl for k in ("password accepted", "you are now identified",
                                  "already identified", "you are now recognized")):
            self.identified = True
            self._identified_event.set()

        elif any(k in tl for k in ("is not registered", "isn't registered",
                                    "nick is not registered")):
            threading.Timer(195, self._ns_do_register).start()

        elif any(k in tl for k in ("must have been using this nick for at least",
                                    "you must be using this nick")):
            wait = 195
            m = re.search(r"(\d+)\s*seconds?", tl)
            if m:
                wait = int(m.group(1)) + 10
            log.info(f"[{self.session_id[:8]}] NickServ holdoff — retrying in {wait}s")
            threading.Timer(wait, self._ns_do_register).start()

        elif any(k in tl for k in ("has been registered", "registration successful",
                                    "registered under")):
            time.sleep(1)
            self._send_raw(f"PRIVMSG NickServ :IDENTIFY {self.ns_password}")

        elif any(k in tl for k in ("check your email", "verify your email",
                                    "email has been sent", "please confirm")):
            log.warning(
                f"[{self.session_id[:8]}] NickServ email verification sent to {self.ns_email}"
            )
            # Store so irc_connect can surface this to the user
            self._ns_notice_buf.append(
                f"VERIFY_EMAIL: Anope sent a verification code to {self.ns_email}. "
                "Use irc_nickserv with command 'CONFIRM <code>' to complete registration."
            )
            self._ns_notice_event.set()

        # Collect INFO response lines
        self._ns_notice_buf.append(text)
        # Signal if this looks like the end of an INFO block
        if any(k in tl for k in ("end of info", "*** end", "last seen",
                                  "options:", "email:", "url:")):
            self._ns_notice_event.set()

    def _ns_do_register(self) -> None:
        if not self.ns_email:
            log.info(
                f"[{self.session_id[:8]}] Skipping NickServ registration — "
                "no email provided. Nick will be unregistered."
            )
            return
        log.info(f"[{self.session_id[:8]}] Registering {self.nick} with NickServ ({self.ns_email})")
        self._send_raw(f"PRIVMSG NickServ :REGISTER {self.ns_password} {self.ns_email}")

    def _nickserv_flow(self) -> None:
        time.sleep(3)
        log.info(f"[{self.session_id[:8]}] Sending NickServ IDENTIFY")
        self._send_raw(f"PRIVMSG NickServ :IDENTIFY {self.ns_password}")

    # ── ChanServ notice handler ─────────────────────────────────────────────

    def _handle_cs_notice(self, text: str) -> None:
        log.info(f"[{self.session_id[:8]}] [ChanServ] {text}")
        self._cs_notice_buf.append(text)
        tl = text.lower()
        if any(k in tl for k in ("end of", "last used", "last topic",
                                  "options:", "url:", "*** end")):
            self._cs_notice_event.set()

    # ── Synchronous command helpers (called from async tools via to_thread) ─

    def cmd_whois(self, nick: str) -> list[str]:
        self.last_active = time.monotonic()
        self._pending_init("whois")
        self._send_raw(f"WHOIS {nick}")
        result = self._pending_wait("whois")
        return result if isinstance(result, list) else [str(result)]

    def cmd_names(self, channel: str) -> list[str]:
        self.last_active = time.monotonic()
        key = f"names:{_irc_lower(channel)}"
        self._pending_init(key)
        self._send_raw(f"NAMES {channel}")
        result = self._pending_wait(key)
        return result if isinstance(result, list) else [str(result)]

    def cmd_topic(self, channel: str) -> str:
        self.last_active = time.monotonic()
        key = f"topic:{_irc_lower(channel)}"
        self._pending_init(key)
        self._send_raw(f"TOPIC {channel}")
        result = self._pending_wait(key)
        if isinstance(result, list):
            return result[0] if result else "(no topic)"
        return result or "(no topic)"

    def cmd_list(self) -> list[str]:
        self.last_active = time.monotonic()
        self._pending_init("list")
        self._send_raw("LIST")
        result = self._pending_wait("list", timeout=15)
        return result if isinstance(result, list) else []

    def cmd_ns_info(self, nick: str) -> list[str]:
        self.last_active = time.monotonic()
        self._ns_notice_buf.clear()
        self._ns_notice_event.clear()
        self._send_raw(f"PRIVMSG NickServ :INFO {nick}")
        self._ns_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._ns_notice_buf)

    def cmd_cs_info(self, channel: str) -> list[str]:
        self.last_active = time.monotonic()
        self._cs_notice_buf.clear()
        self._cs_notice_event.clear()
        self._send_raw(f"PRIVMSG ChanServ :INFO {channel}")
        self._cs_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._cs_notice_buf)

    # ── CTCP handlers ───────────────────────────────────────────────────────

    def _handle_ctcp_request(self, sender: str, body: str) -> None:
        """Respond to incoming CTCP requests."""
        parts   = body.split(" ", 1)
        command = parts[0].upper()
        arg     = parts[1] if len(parts) > 1 else ""
        log.info(f"[{self.session_id[:8]}] CTCP {command} from {sender}")

        if command == "VERSION":
            self._send_raw(f"NOTICE {sender} :\x01VERSION 2600net IRC MCP Connector v1.0 | Claude AI\x01")
        elif command == "PING":
            self._send_raw(f"NOTICE {sender} :\x01PING {arg}\x01")
        elif command == "TIME":
            t = time.strftime("%a %b %d %H:%M:%S UTC %Y", time.gmtime())
            self._send_raw(f"NOTICE {sender} :\x01TIME {t}\x01")
        elif command == "FINGER":
            self._send_raw(f"NOTICE {sender} :\x01FINGER Claude AI MCP Session on 2600net\x01")
        # Unknown CTCP — silently ignore (prevents amplification)

    def _handle_ctcp_reply(self, sender: str, body: str) -> None:
        """Buffer an incoming CTCP reply for retrieval by tools."""
        parts   = body.split(" ", 1)
        command = parts[0].upper()
        response = parts[1] if len(parts) > 1 else ""
        entry = {
            "ts":       time.strftime("%H:%M:%S", time.gmtime()),
            "from":     sender,
            "type":     command,
            "response": response,
        }
        with self._ctcp_lock:
            self.ctcp_replies[_irc_lower(sender)].append(entry)
        log.info(f"[{self.session_id[:8]}] CTCP reply {command} from {sender}: {response}")
        # Signal any pending CTCP wait
        key = f"ctcp:{_irc_lower(sender)}:{command.lower()}"
        self._pending_set(key, response)
        self._pending_signal(key)

    def cmd_ctcp_send(self, nick: str, ctcp_type: str, arg: str = "") -> str:
        """Send a CTCP request and wait for the reply."""
        self.last_active = time.monotonic()
        ctcp_type = ctcp_type.upper()
        key = f"ctcp:{_irc_lower(nick)}:{ctcp_type.lower()}"
        self._pending_init(key)
        payload = f"\x01{ctcp_type}{' ' + arg if arg else ''}\x01"
        self._send_raw(f"PRIVMSG {nick} :{payload}")
        result = self._pending_wait(key, timeout=CMD_TIMEOUT)
        if isinstance(result, list):
            return result[0] if result else "(no reply)"
        return result or "(no reply within timeout)"

    def cmd_ctcp_read_replies(self, nick: str) -> list[dict]:
        """Return all buffered CTCP replies from a nick."""
        self.last_active = time.monotonic()
        with self._ctcp_lock:
            return list(self.ctcp_replies.get(_irc_lower(nick), []))

    # ── MemoServ handler ────────────────────────────────────────────────────

    def _handle_memo_notice(self, text: str) -> None:
        log.info(f"[{self.session_id[:8]}] [MemoServ] {text}")
        self._memo_notice_buf.append(text)
        tl = text.lower()
        if any(k in tl for k in ("end of", "no memos", "memo #", "was sent",
                                  "deleted", "you have", "no new")):
            self._memo_notice_event.set()

    def cmd_memo(self, command: str) -> list[str]:
        """Send a MemoServ command and collect the response."""
        self.last_active = time.monotonic()
        self._memo_notice_buf.clear()
        self._memo_notice_event.clear()
        self._send_raw(f"PRIVMSG MemoServ :{command}")
        self._memo_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._memo_notice_buf)

    # ── HostServ handler ────────────────────────────────────────────────────

    def _handle_host_notice(self, text: str) -> None:
        log.info(f"[{self.session_id[:8]}] [HostServ] {text}")
        self._host_notice_buf.append(text)
        tl = text.lower()
        if any(k in tl for k in ("your vhost", "activated", "deactivated",
                                  "request", "is already", "set to")):
            self._host_notice_event.set()

    def cmd_hostserv(self, command: str) -> list[str]:
        """Send a HostServ command and collect the response."""
        self.last_active = time.monotonic()
        self._host_notice_buf.clear()
        self._host_notice_event.clear()
        self._send_raw(f"PRIVMSG HostServ :{command}")
        self._host_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._host_notice_buf)

    # ── ircd-hybrid server commands ─────────────────────────────────────────

    def cmd_ircd(self, raw_command: str, end_numeric: str = "") -> list[str]:
        """
        Send an ircd command and collect numeric reply lines.
        end_numeric: the final numeric that signals end of reply block.
        """
        self.last_active = time.monotonic()
        self._ircd_buf.clear()
        self._ircd_event.clear()
        self._send_raw(raw_command)
        # Wait for end signal or timeout
        self._ircd_event.wait(timeout=CMD_TIMEOUT)
        return list(self._ircd_buf)

    # ── Extended NickServ / ChanServ commands ───────────────────────────────

    def cmd_ns(self, command: str) -> list[str]:
        """Send a NickServ command and collect response lines."""
        self.last_active = time.monotonic()
        self._ns_notice_buf.clear()
        self._ns_notice_event.clear()
        self._send_raw(f"PRIVMSG NickServ :{command}")
        self._ns_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._ns_notice_buf)

    def cmd_cs(self, command: str) -> list[str]:
        """Send a ChanServ command and collect response lines."""
        self.last_active = time.monotonic()
        self._cs_notice_buf.clear()
        self._cs_notice_event.clear()
        self._send_raw(f"PRIVMSG ChanServ :{command}")
        self._cs_notice_event.wait(timeout=CMD_TIMEOUT)
        return list(self._cs_notice_buf)

    def wait_for_identification(self, timeout: float = 30.0) -> bool:
        return self._identified_event.wait(timeout=timeout)


# ── Session Pool ──────────────────────────────────────────────────────────────
class SessionPool:
    """Manages all active IRC sessions and persists them across restarts."""

    def __init__(self):
        self._sessions: dict[str, IRCSession] = {}
        self._meta: dict[str, dict]           = _load_sessions()
        self._lock = threading.Lock()
        # Start idle cleanup loop
        threading.Thread(target=self._cleanup_loop, daemon=True).start()

    def _cleanup_loop(self) -> None:
        while True:
            time.sleep(300)
            self._cleanup_idle()

    def _cleanup_idle(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [
                sid for sid, sess in self._sessions.items()
                if now - sess.last_active > IDLE_TIMEOUT_S
            ]
        for sid in expired:
            log.info(f"Session {sid[:8]} idle — disconnecting.")
            self._disconnect_session(sid)

    def _disconnect_session(self, session_id: str) -> None:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess:
            sess.disconnect("Idle timeout")
        # Keep metadata so the nick/password can be reused on reconnect
        self._save()

    def _save(self) -> None:
        with self._lock:
            # Update last_active in meta for connected sessions
            for sid, sess in self._sessions.items():
                if sid in self._meta:
                    self._meta[sid]["last_active"] = time.time()
        _save_sessions(self._meta)

    def create(self, desired_name: str, ns_email: str = "") -> tuple[str, IRCSession]:
        """Create a brand-new session with a fresh session_id."""
        session_id = secrets.token_hex(16)
        nick       = _make_nick(desired_name)
        ns_pass    = secrets.token_urlsafe(24)

        meta = {
            "nick":        nick,
            "ns_password": ns_pass,
            "ns_email":    ns_email,
            "created_at":  time.time(),
            "last_active": time.time(),
        }
        with self._lock:
            self._meta[session_id] = meta

        sess = IRCSession(session_id, nick, ns_pass, ns_email=ns_email)
        with self._lock:
            self._sessions[session_id] = sess
        sess.start()
        self._save()
        return session_id, sess

    def resume(self, session_id: str) -> IRCSession | None:
        """Resume an existing session — reconnect IRC if needed."""
        with self._lock:
            meta = self._meta.get(session_id)
            if not meta:
                return None
            sess = self._sessions.get(session_id)

        if sess and sess.connected:
            sess.last_active = time.monotonic()
            return sess

        # Reconnect using stored credentials
        nick     = meta["nick"]
        ns_pass  = meta["ns_password"]
        ns_email = meta.get("ns_email", "")
        sess     = IRCSession(session_id, nick, ns_pass, ns_email=ns_email)
        with self._lock:
            self._sessions[session_id] = sess
        sess.start()
        self._save()
        return sess

    def get(self, session_id: str) -> IRCSession | None:
        with self._lock:
            return self._sessions.get(session_id)


# ── Global session pool ───────────────────────────────────────────────────────
_pool = SessionPool()


# ── Helpers for tools ─────────────────────────────────────────────────────────
def _require_session(session_id: str) -> IRCSession:
    """Retrieve session or raise ValueError with helpful message."""
    sess = _pool.resume(session_id)
    if not sess:
        raise ValueError(
            "Session not found. Call irc_connect first to create a session, "
            "then save the returned session_id in your custom instructions."
        )
    return sess


# ── Pydantic input models ─────────────────────────────────────────────────────
class ConnectInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    desired_name: str = Field(
        ...,
        description="Your preferred name. Used to derive your IRC nick (e.g. 'andrew' → 'CL-andrew'). Alphanumeric and hyphens only.",
        min_length=1, max_length=12,
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Existing session_id to resume. Leave blank for a new session.",
    )
    email: Optional[str] = Field(
        default=None,
        description=(
            "Your email address for NickServ nick registration. "
            "Required only on first connect if you want your IRC nick registered. "
            "Anope will email you a verification code — you must confirm it with "
            "irc_nickserv('CONFIRM <code>') to complete registration. "
            "Leave blank to connect without registering."
        ),
        pattern=r"^[\w\.\+\-]+@[\w\.\-]+\.\w{2,}$",
    )

class SessionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id returned by irc_connect.")

class SendMessageInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    channel:    str = Field(..., description="Channel name (e.g. '#ClaudeBot').")
    message:    str = Field(..., description="Message to send.", min_length=1, max_length=400)

    @field_validator("channel")
    @classmethod
    def must_be_channel(cls, v: str) -> str:
        if not v.startswith("#"):
            raise ValueError("Channel must start with #")
        return v

class SendPMInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    target:     str = Field(..., description="Nick to send a private message to.")
    message:    str = Field(..., description="Message text.", min_length=1, max_length=400)

class ReadChannelInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    channel:    str = Field(..., description="Channel to read (e.g. '#ClaudeBot').")
    limit:      int = Field(default=20, description="Number of recent messages to return.", ge=1, le=100)

class ChannelInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    channel:    str = Field(..., description="Channel name (e.g. '#ClaudeBot').")

class NickInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    nick:       str = Field(..., description="IRC nick to look up.")

class ListChannelsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    limit:      int = Field(default=50, description="Max channels to return.", ge=1, le=LIST_MAX)


# ── New models for expanded tools ─────────────────────────────────────────────

class CtcpSendInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    nick:       str = Field(..., description="Target IRC nick.")
    ctcp_type:  str = Field(..., description="CTCP type: VERSION, PING, TIME, FINGER, or any custom type.")
    arg:        str = Field(default="", description="Optional argument (e.g. timestamp for PING).")

class CtcpReadInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    nick:       str = Field(..., description="Nick whose CTCP replies to read.")

class NickServInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    command:    str = Field(..., description="NickServ command and arguments (e.g. 'GHOST OldNick password', 'SET URL https://...', 'GROUP').", min_length=1, max_length=300)

class ChanServInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    command:    str = Field(..., description="ChanServ command and arguments (e.g. 'OP #channel nick', 'TOPIC #channel New topic', 'AOP #channel ADD nick').", min_length=1, max_length=300)

class MemoServInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    command:    str = Field(..., description="MemoServ command (e.g. 'SEND nick Message text', 'LIST', 'READ 1', 'DEL 1').", min_length=1, max_length=300)

class HostServInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    command:    str = Field(..., description="HostServ command: 'ON', 'OFF', or 'REQUEST vhost.example.com'.", min_length=1, max_length=100)

class IrcdStatsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    query:      str = Field(..., description="STATS query letter: u=uptime, l=links, c=connections, m=commands, o=opers, p=ports, t=traffic.", min_length=1, max_length=1)

class IrcdCommandInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    session_id: str = Field(..., description="Your session_id.")
    command:    str = Field(..., description="Raw ircd command to send (e.g. 'ADMIN', 'MOTD', 'TIME', 'VERSION', 'LINKS', 'LUSERS').", min_length=1, max_length=100)


# ── FastMCP server ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_server):
    log.info(f"2600net IRC MCP Server starting on {MCP_HOST}:{MCP_PORT}")
    yield
    log.info("MCP Server shutting down.")

mcp = FastMCP("2600net_mcp", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_connect
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_connect",
    annotations={
        "title": "Connect to 2600net IRC",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_connect(params: ConnectInput) -> str:
    """
    Connect to 2600net IRC and get a persistent IRC nick.

    Creates a new session or resumes an existing one. Returns a session_id
    that you should SAVE in your custom instructions — it reconnects you to
    the same nick every time.

    Nick registration is optional. If you provide an email address, Anope
    NickServ will register your nick and email you a verification code.
    You must confirm it with irc_nickserv('CONFIRM <code>') to complete
    registration. Without an email, you connect with an unregistered nick.

    Args:
        params (ConnectInput):
            - desired_name (str): Your preferred name (e.g. 'andrew'). Nick will be 'CL-andrew'.
            - session_id (Optional[str]): Existing session_id to resume, or omit for new.
            - email (Optional[str]): Your email for NickServ nick registration.
              Anope will send a verification code to this address.
              Leave blank to connect without registering your nick.

    Returns:
        str: JSON with session_id, nick, registration status, and usage instructions.
    """
    try:
        # Resume existing session
        if params.session_id:
            sess = await asyncio.to_thread(_pool.resume, params.session_id)
            if sess:
                await asyncio.to_thread(sess.wait_for_identification, 10.0)
                return json.dumps({
                    "status":     "resumed",
                    "session_id": params.session_id,
                    "nick":       sess.nick,
                    "connected":  sess.connected,
                    "identified": sess.identified,
                    "channels":   list(sess.joined_channels),
                    "network":    f"{IRC_SERVER}:{IRC_PORT}",
                    "note":       "Save your session_id in custom instructions to reconnect automatically.",
                }, indent=2)
            # session_id not found — fall through to create new

        # Create new session, passing user's email for NickServ registration
        ns_email = params.email or ""
        session_id, sess = await asyncio.to_thread(
            _pool.create, params.desired_name, ns_email
        )

        # Wait up to 30s for NickServ identification (only fires if nick was
        # already registered from a previous session)
        identified = await asyncio.to_thread(sess.wait_for_identification, 30.0)

        # Check if a verification email was triggered
        verify_notice = next(
            (l for l in sess._ns_notice_buf if l.startswith("VERIFY_EMAIL:")),
            None
        )

        result: dict = {
            "status":     "connected",
            "session_id": session_id,
            "nick":       sess.nick,
            "identified": identified,
            "network":    f"2600net — {IRC_SERVER}:{IRC_PORT}",
            "note": (
                "IMPORTANT: Save your session_id. Add this to your Claude custom instructions: "
                f"'My 2600net IRC session_id is {session_id}'"
            ),
            "usage": {
                "send_message":    "Use irc_send_message to talk in a channel",
                "join_channel":    "Use irc_join_channel to join a channel",
                "read_channel":    "Use irc_read_channel to read recent messages",
                "default_channel": "#ClaudeBot",
            },
        }

        if ns_email:
            result["registration"] = {
                "email":  ns_email,
                "status": "verification_email_sent" if verify_notice else "pending_180s_holdoff",
                "note":   (
                    f"Anope will send a verification code to {ns_email}. "
                    "Once received, run: irc_nickserv with command 'CONFIRM <code>'"
                ) if verify_notice else (
                    "NickServ requires the nick to be used for ~3 minutes before "
                    "registration is allowed. Registration will be attempted automatically."
                ),
            }
        else:
            result["registration"] = {
                "status": "skipped",
                "note":   "No email provided. Nick is unregistered. "
                          "Call irc_connect again with an email to register.",
            }

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_disconnect
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_disconnect",
    annotations={
        "title": "Disconnect from 2600net IRC",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_disconnect(params: SessionInput) -> str:
    """
    Cleanly disconnect from IRC. Your nick stays registered — reconnect
    any time using irc_connect with the same session_id.

    Args:
        params (SessionInput): session_id

    Returns:
        str: Confirmation message.
    """
    try:
        sess = _require_session(params.session_id)
        await asyncio.to_thread(sess.disconnect, "Session ended by user")
        return json.dumps({"status": "disconnected", "nick": sess.nick})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_send_message
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_send_message",
    annotations={
        "title": "Send a message to an IRC channel",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_send_message(params: SendMessageInput) -> str:
    """
    Send a message to an IRC channel on 2600net.

    Args:
        params (SendMessageInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to send to (e.g. '#ClaudeBot').
            - message (str): Message text (max 400 chars).

    Returns:
        str: Confirmation with channel and timestamp.
    """
    try:
        sess = _require_session(params.session_id)
        if not sess.connected:
            return json.dumps({"error": "Not connected. Call irc_connect to reconnect."})
        ch_key = _irc_lower(params.channel)
        if ch_key not in sess.joined_channels:
            await asyncio.to_thread(
                lambda: (sess._send_raw(f"JOIN {params.channel}"),
                         time.sleep(1))
            )
        await asyncio.to_thread(sess.send_message, params.channel, params.message)
        return json.dumps({
            "status":  "sent",
            "channel": params.channel,
            "nick":    sess.nick,
            "message": params.message,
            "time":    time.strftime("%H:%M:%S UTC", time.gmtime()),
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_send_private_message
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_send_private_message",
    annotations={
        "title": "Send a private message to an IRC user",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_send_private_message(params: SendPMInput) -> str:
    """
    Send a private message (PM) to another IRC user on 2600net.

    Args:
        params (SendPMInput):
            - session_id (str): Your session_id.
            - target (str): Nick to message.
            - message (str): Message text.

    Returns:
        str: Confirmation.
    """
    try:
        sess = _require_session(params.session_id)
        if not sess.connected:
            return json.dumps({"error": "Not connected."})
        await asyncio.to_thread(sess.send_message, params.target, params.message)
        return json.dumps({
            "status": "sent",
            "to":     params.target,
            "from":   sess.nick,
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_read_channel
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_read_channel",
    annotations={
        "title": "Read recent messages from an IRC channel",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_read_channel(params: ReadChannelInput) -> str:
    """
    Read the most recent messages from an IRC channel.
    The bot must be joined to the channel to receive messages.

    Args:
        params (ReadChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to read (e.g. '#ClaudeBot').
            - limit (int): Number of recent messages to return (1-100, default 20).

    Returns:
        str: JSON list of {time, nick, text} objects.
    """
    try:
        sess    = _require_session(params.session_id)
        ch_key  = _irc_lower(params.channel)
        if ch_key not in sess.joined_channels:
            return json.dumps({
                "error": f"Not in {params.channel}. Use irc_join_channel first."
            })
        msgs = list(sess.msg_buffer.get(ch_key, []))[-params.limit:]
        return json.dumps({
            "channel":  params.channel,
            "messages": msgs,
            "count":    len(msgs),
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_join_channel
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_join_channel",
    annotations={
        "title": "Join an IRC channel",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_join_channel(params: ChannelInput) -> str:
    """
    Join an IRC channel on 2600net.

    Args:
        params (ChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to join (e.g. '#ClaudeBot').

    Returns:
        str: Confirmation with channel info.
    """
    try:
        sess   = _require_session(params.session_id)
        ch_key = _irc_lower(params.channel)
        if ch_key in sess.joined_channels:
            return json.dumps({"status": "already_in_channel", "channel": params.channel})
        sess._send_raw(f"JOIN {params.channel}")
        await asyncio.sleep(2)
        return json.dumps({
            "status":  "joined" if ch_key in sess.joined_channels else "join_sent",
            "channel": params.channel,
            "nick":    sess.nick,
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_part_channel
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_part_channel",
    annotations={
        "title": "Leave an IRC channel",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_part_channel(params: ChannelInput) -> str:
    """
    Leave an IRC channel on 2600net.

    Args:
        params (ChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to leave (e.g. '#ClaudeBot').

    Returns:
        str: Confirmation.
    """
    try:
        sess = _require_session(params.session_id)
        sess._send_raw(f"PART {params.channel} :Goodbye")
        await asyncio.sleep(1)
        return json.dumps({"status": "parted", "channel": params.channel})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_list_channels
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_list_channels",
    annotations={
        "title": "List channels on 2600net",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_list_channels(params: ListChannelsInput) -> str:
    """
    List public channels on 2600net IRC.

    Args:
        params (ListChannelsInput):
            - session_id (str): Your session_id.
            - limit (int): Max channels to return (1-100, default 50).

    Returns:
        str: JSON list of {channel, user_count, topic} objects.
    """
    try:
        sess    = _require_session(params.session_id)
        raw     = await asyncio.to_thread(sess.cmd_list)
        entries = []
        for line in raw[:params.limit]:
            # Format: "#channel (N users): topic"
            m = re.match(r"(#\S+)\s+\((\d+) users?\):\s*(.*)", line)
            if m:
                entries.append({
                    "channel":    m.group(1),
                    "user_count": int(m.group(2)),
                    "topic":      m.group(3).strip(),
                })
            else:
                entries.append({"raw": line})
        return json.dumps({
            "network":  "2600net",
            "count":    len(entries),
            "channels": entries,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_list_users
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_list_users",
    annotations={
        "title": "List users in an IRC channel",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_list_users(params: ChannelInput) -> str:
    """
    List the users currently in an IRC channel.

    Args:
        params (ChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to list (e.g. '#ClaudeBot').

    Returns:
        str: JSON with nicks grouped by status (ops, voiced, regular).
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_names, params.channel)
        ops, voiced, regular = [], [], []
        for line in lines:
            for entry in line.split():
                if entry.startswith("@"):
                    ops.append(entry[1:])
                elif entry.startswith("+"):
                    voiced.append(entry[1:])
                else:
                    regular.append(entry.lstrip("%&~!"))
        return json.dumps({
            "channel": params.channel,
            "ops":     ops,
            "voiced":  voiced,
            "regular": regular,
            "total":   len(ops) + len(voiced) + len(regular),
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_get_topic
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_get_topic",
    annotations={
        "title": "Get the topic of an IRC channel",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_get_topic(params: ChannelInput) -> str:
    """
    Get the current topic of an IRC channel.

    Args:
        params (ChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel name (e.g. '#ClaudeBot').

    Returns:
        str: JSON with channel name and topic text.
    """
    try:
        sess  = _require_session(params.session_id)
        topic = await asyncio.to_thread(sess.cmd_topic, params.channel)
        return json.dumps({"channel": params.channel, "topic": topic})
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_whois
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_whois",
    annotations={
        "title": "Get WHOIS information about an IRC user",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_whois(params: NickInput) -> str:
    """
    Get WHOIS information about an IRC user on 2600net.

    Args:
        params (NickInput):
            - session_id (str): Your session_id.
            - nick (str): IRC nick to look up.

    Returns:
        str: JSON with whois lines (user info, server, channels, idle time).
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_whois, params.nick)
        return json.dumps({
            "nick":  params.nick,
            "info":  lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_get_my_info
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_get_my_info",
    annotations={
        "title": "Get your current IRC session info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def irc_get_my_info(params: SessionInput) -> str:
    """
    Get information about your current IRC session — nick, status,
    channels joined, connection state.

    Args:
        params (SessionInput): session_id

    Returns:
        str: JSON session summary.
    """
    try:
        sess = _require_session(params.session_id)
        return json.dumps({
            "nick":        sess.nick,
            "connected":   sess.connected,
            "identified":  sess.identified,
            "channels":    list(sess.joined_channels),
            "network":     f"2600net — {IRC_SERVER}:{IRC_PORT}",
            "session_id":  params.session_id,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_nickserv_info
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_nickserv_info",
    annotations={
        "title": "Get NickServ registration info about an IRC nick",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_nickserv_info(params: NickInput) -> str:
    """
    Query Anope NickServ for registration information about an IRC nick.

    Args:
        params (NickInput):
            - session_id (str): Your session_id.
            - nick (str): Nick to query.

    Returns:
        str: JSON with NickServ INFO output lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_ns_info, params.nick)
        return json.dumps({
            "nick":  params.nick,
            "info":  lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_chanserv_info
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_chanserv_info",
    annotations={
        "title": "Get ChanServ registration info about an IRC channel",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_chanserv_info(params: ChannelInput) -> str:
    """
    Query Anope ChanServ for registration information about an IRC channel.

    Args:
        params (ChannelInput):
            - session_id (str): Your session_id.
            - channel (str): Channel to query (e.g. '#ClaudeBot').

    Returns:
        str: JSON with ChanServ INFO output lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_cs_info, params.channel)
        return json.dumps({
            "channel": params.channel,
            "info":    lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_ctcp_send
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_ctcp_send",
    annotations={
        "title": "Send a CTCP request to an IRC user",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_ctcp_send(params: CtcpSendInput) -> str:
    """
    Send a CTCP request to an IRC user and return their reply.

    Common types: VERSION (client info), PING (latency), TIME (local time),
    FINGER (user info). Custom types also supported.

    Args:
        params (CtcpSendInput):
            - session_id (str): Your session_id.
            - nick (str): Target nick.
            - ctcp_type (str): CTCP type e.g. 'VERSION', 'PING', 'TIME', 'FINGER'.
            - arg (str): Optional argument (e.g. timestamp string for PING).

    Returns:
        str: JSON with the CTCP reply or timeout notice.
    """
    try:
        sess   = _require_session(params.session_id)
        result = await asyncio.to_thread(
            sess.cmd_ctcp_send, params.nick, params.ctcp_type, params.arg
        )
        return json.dumps({
            "nick":     params.nick,
            "type":     params.ctcp_type.upper(),
            "response": result,
        })
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_ctcp_read_replies
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_ctcp_read_replies",
    annotations={
        "title": "Read buffered CTCP replies from an IRC user",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def irc_ctcp_read_replies(params: CtcpReadInput) -> str:
    """
    Read all buffered CTCP replies received from a nick this session.
    Useful for collecting async replies from broadcast CTCP requests.

    Args:
        params (CtcpReadInput):
            - session_id (str): Your session_id.
            - nick (str): Nick whose replies to read.

    Returns:
        str: JSON list of {ts, from, type, response} objects.
    """
    try:
        sess    = _require_session(params.session_id)
        replies = await asyncio.to_thread(sess.cmd_ctcp_read_replies, params.nick)
        return json.dumps({
            "nick":    params.nick,
            "replies": replies,
            "count":   len(replies),
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_nickserv
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_nickserv",
    annotations={
        "title": "Send a NickServ command (Anope)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_nickserv(params: NickServInput) -> str:
    """
    Send any NickServ command to Anope services and return the response.

    Common commands:
      INFO <nick>              — registration info
      GHOST <nick> <password>  — kill a ghost connection using your nick
      RECOVER <nick> <password>— recover your nick
      RELEASE <nick> <password>— release a nick after RECOVER
      SET URL <url>            — set URL shown in /whois
      SET GREET <text>         — set greeting shown in INFO
      SET HIDE EMAIL ON/OFF    — hide/show registration email
      SET PASSWORD <newpass>   — change password
      GROUP                    — group current nick to your account
      UNGROUP                  — remove current nick from group
      SENDPASS <nick>          — email password reset
      DROP <nick> <password>   — unregister a nick
      LISTCHANS                — list channels you have access to

    Args:
        params (NickServInput):
            - session_id (str): Your session_id.
            - command (str): NickServ command and args (e.g. 'SET GREET Hello!').

    Returns:
        str: JSON with NickServ response lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_ns, params.command)
        return json.dumps({
            "command":  f"NickServ {params.command}",
            "response": lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_chanserv
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_chanserv",
    annotations={
        "title": "Send a ChanServ command (Anope)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_chanserv(params: ChanServInput) -> str:
    """
    Send any ChanServ command to Anope services and return the response.

    Common commands:
      INFO <#channel>                   — channel registration info
      OP <#channel> [nick]              — give operator status
      DEOP <#channel> [nick]            — remove operator status
      VOICE <#channel> [nick]           — give voice
      DEVOICE <#channel> [nick]         — remove voice
      AOP <#channel> ADD/DEL/LIST nick  — auto-op list
      SOP <#channel> ADD/DEL/LIST nick  — super-op list
      HOP <#channel> ADD/DEL/LIST nick  — half-op list
      VOP <#channel> ADD/DEL/LIST nick  — voice list
      TOPIC <#channel> <text>           — set channel topic
      KICK <#channel> <nick> [reason]   — kick a user
      BAN <#channel> <nick/mask>        — ban a user
      UNBAN <#channel> <nick/mask>      — unban
      CLEAR <#channel> MODES/BANS/OPS   — clear channel state
      SET <#channel> SUCCESSOR <nick>   — set channel successor
      SET <#channel> FOUNDER <nick>     — transfer founder
      REGISTER <#channel>               — register a channel
      DROP <#channel>                   — unregister a channel
      INVITE <#channel>                 — invite yourself in
      UNBAN <#channel>                  — unban yourself

    Args:
        params (ChanServInput):
            - session_id (str): Your session_id.
            - command (str): ChanServ command and args.

    Returns:
        str: JSON with ChanServ response lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_cs, params.command)
        return json.dumps({
            "command":  f"ChanServ {params.command}",
            "response": lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_memoserv
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_memoserv",
    annotations={
        "title": "Send and receive memos via Anope MemoServ",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_memoserv(params: MemoServInput) -> str:
    """
    Use Anope MemoServ to send, read, or manage memos.
    Memos are persistent messages stored even when the recipient is offline.

    Commands:
      SEND <nick> <message>   — send a memo to a nick
      LIST                    — list your memos
      READ <num>              — read memo number N
      DEL <num>               — delete memo number N
      DEL ALL                 — delete all memos
      INFO                    — memo settings info

    Args:
        params (MemoServInput):
            - session_id (str): Your session_id.
            - command (str): MemoServ command (e.g. 'SEND r0d3nt Hello there!').

    Returns:
        str: JSON with MemoServ response lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_memo, params.command)
        return json.dumps({
            "command":  f"MemoServ {params.command}",
            "response": lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_hostserv
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_hostserv",
    annotations={
        "title": "Manage virtual hosts via Anope HostServ",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def irc_hostserv(params: HostServInput) -> str:
    """
    Use Anope HostServ to manage your IRC virtual hostname (vhost).
    A vhost replaces your real hostname in /whois output.

    Commands:
      ON                         — activate your assigned vhost
      OFF                        — deactivate vhost (show real host)
      REQUEST <vhost>            — request a vhost from network admins

    Args:
        params (HostServInput):
            - session_id (str): Your session_id.
            - command (str): HostServ command.

    Returns:
        str: JSON with HostServ response lines.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_hostserv, params.command)
        return json.dumps({
            "command":  f"HostServ {params.command}",
            "response": lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_server_stats
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_server_stats",
    annotations={
        "title": "Query ircd-hybrid server STATS",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_server_stats(params: IrcdStatsInput) -> str:
    """
    Query ircd-hybrid server statistics using the STATS command.

    Query letters:
      u — server uptime
      l — current server links
      c — connection stats
      m — command usage counts
      o — IRC operator lines
      p — listening ports
      t — traffic statistics

    Args:
        params (IrcdStatsInput):
            - session_id (str): Your session_id.
            - query (str): Single letter query (e.g. 'u' for uptime).

    Returns:
        str: JSON with STATS response lines from the server.
    """
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_ircd, f"STATS {params.query}", "219")
        return json.dumps({
            "stats_query": params.query,
            "server":      IRC_SERVER,
            "response":    lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Tool: irc_server_info
# ─────────────────────────────────────────────────────────────────────────────
@mcp.tool(
    name="irc_server_info",
    annotations={
        "title": "Query ircd-hybrid server information",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def irc_server_info(params: IrcdCommandInput) -> str:
    """
    Send an ircd-hybrid server information command and return the response.

    Supported commands:
      ADMIN    — server admin contact info
      MOTD     — message of the day
      TIME     — server time
      VERSION  — ircd version and compile options
      LINKS    — linked servers
      LUSERS   — user count statistics
      MAP      — server map (if supported)

    Args:
        params (IrcdCommandInput):
            - session_id (str): Your session_id.
            - command (str): Server command to send.

    Returns:
        str: JSON with server response lines.
    """
    # Whitelist safe read-only ircd commands
    ALLOWED = {"ADMIN", "MOTD", "TIME", "VERSION", "LINKS", "LUSERS", "MAP"}
    cmd_upper = params.command.strip().upper().split()[0]
    if cmd_upper not in ALLOWED:
        return json.dumps({
            "error": f"Command '{cmd_upper}' not permitted. Allowed: {', '.join(sorted(ALLOWED))}"
        })
    try:
        sess  = _require_session(params.session_id)
        lines = await asyncio.to_thread(sess.cmd_ircd, params.command.upper())
        return json.dumps({
            "command":  params.command.upper(),
            "server":   IRC_SERVER,
            "response": lines,
        }, indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info(f"Starting {VERSION}")
    log.info(f"IRC server: {IRC_SERVER}:{IRC_PORT}")
    log.info(f"MCP endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")
    mcp.run(transport="streamable-http", host=MCP_HOST, port=MCP_PORT)
