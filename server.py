"""ei-chat: a tiny group chat for humans and their AI agents. Stdlib only.

People join from a browser. Agents join over the same HTTP API with curl
(or anything that can POST JSON). One Python file, no dependencies.

Run:
    python3 server.py                 start the chat (prints the URL)
    python3 server.py key <name>      mint a key so <name> is verified
    python3 server.py revoke <name>   remove <name>'s key
    python3 server.py keys            list names that have keys

API (all under /api/<TOKEN>/):
    POST send          {"name": "...", "text": "..."}   -> the stored message
    GET  messages      ?since=<id>&wait=<secs>          -> messages after <id>
                       (wait up to 30s for new ones: long-poll for agents)
    GET  participants                                   -> who is around
    POST heartbeat     {"name": "..."}                  -> presence ping only
A name that has a key can only be used with  Authorization: Bearer <key>.

Optional Echo bridge: set EI_CHAT_TARGET to an Echo session id and every
message is also forwarded as a DM to that session, DMs to the bridge are
posted into the chat, and the server shuts down when that session leaves.
See README.md.
"""
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode

BASE = os.path.dirname(os.path.abspath(__file__))

HOST = os.environ.get("EI_CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("EI_CHAT_PORT", "8747"))
DATA = os.environ.get("EI_CHAT_DATA", os.path.join(BASE, "data"))
STORE = os.path.join(DATA, "messages.json")
KEYS_FILE = os.path.join(DATA, "participants.json")
TOKEN_FILE = os.path.join(DATA, "token")
TITLE = os.environ.get("EI_CHAT_TITLE", "ei-chat")
SUBTITLE = os.environ.get("EI_CHAT_SUBTITLE", "humans and agents, one room")

# Echo bridge (optional, off unless EI_CHAT_TARGET is set)
TARGET = os.environ.get("EI_CHAT_TARGET", "")
ECHO = os.environ.get("EI_URL", "http://127.0.0.1:6767")
ECHO_ID = os.environ.get("EI_CHAT_ECHO_ID", "eichat-bridge")
AGENT_NAME = os.environ.get("EI_CHAT_AGENT_NAME", "Agent")

MAX_WAIT = 30  # seconds a long-poll may hold a request open


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def load_token():
    """EI_CHAT_TOKEN wins; otherwise a random token kept in data/token so the
    URL stays the same across restarts."""
    env = os.environ.get("EI_CHAT_TOKEN")
    if env:
        return env
    try:
        with open(TOKEN_FILE) as f:
            tok = f.read().strip()
        if tok:
            return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(9)
    os.makedirs(DATA, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(tok + "\n")
    return tok


TOKEN = load_token()


# --- identity --------------------------------------------------------------
# data/participants.json maps secret key -> display name. Mint keys with
# `python3 server.py key <name>` and hand each one over privately.

def load_keys():
    return _read_json(KEYS_FILE, {})


def resolve_identity(headers, claimed_name):
    """Return (name, verified). Raise ValueError when someone tries to use a
    name that has a key without sending that key."""
    keys = load_keys()
    auth = headers.get("Authorization", "")
    key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if key:
        if key in keys:
            return keys[key], True  # the server picks the name, not the client
        raise ValueError("unknown key")
    if claimed_name in set(keys.values()):
        raise ValueError(f"'{claimed_name}' has a key - send it as "
                         "Authorization: Bearer <key> to use this name")
    return claimed_name, False


# --- message store -----------------------------------------------------------

lock = threading.Lock()
new_message = threading.Condition(lock)
MESSAGES = []  # loaded in main(); write-through to STORE


def save_message(name, text, verified=False):
    with new_message:
        msg = {
            "id": (MESSAGES[-1]["id"] + 1) if MESSAGES else 1,
            "name": name[:40],
            "text": text[:4000],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "verified": bool(verified),
        }
        MESSAGES.append(msg)
        _write_json(STORE, MESSAGES)
        new_message.notify_all()
    return msg


def messages_since(since, wait=0.0):
    deadline = time.time() + min(max(wait, 0.0), MAX_WAIT)
    with new_message:
        while True:
            out = [m for m in MESSAGES if m["id"] > since]
            left = deadline - time.time()
            if out or left <= 0:
                return out
            new_message.wait(left)


# --- presence (in memory, a live "who's around" view) ------------------------

PRESENCE = {}  # name -> {"last_ts": epoch, "verified": bool}
ONLINE_WINDOW = 30


def touch_presence(name, verified):
    with lock:
        PRESENCE[name] = {"last_ts": time.time(), "verified": bool(verified)}


def seed_presence_from_history():
    for m in MESSAGES:
        if m["name"] == "System":
            continue
        try:
            ts = time.mktime(time.strptime(m["ts"], "%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            continue
        cur = PRESENCE.get(m["name"])
        if cur is None or cur["last_ts"] < ts:
            PRESENCE[m["name"]] = {"last_ts": ts,
                                   "verified": bool(m.get("verified"))}


def presence_snapshot():
    now = time.time()
    with lock:
        items = list(PRESENCE.items())
    out = [{"name": n, "verified": i["verified"],
            "online": now - i["last_ts"] <= ONLINE_WINDOW,
            "last_seen_secs_ago": int(now - i["last_ts"])} for n, i in items]
    out.sort(key=lambda p: (not p["online"], p["last_seen_secs_ago"]))
    return out


# --- Echo bridge (only runs when EI_CHAT_TARGET is set) ----------------------

def echo_get(path, timeout=5):
    with urllib.request.urlopen(ECHO + path, timeout=timeout) as r:
        return json.loads(r.read())


def echo_post(path, params, timeout=5):
    req = urllib.request.Request(ECHO + path + "?" + urlencode(params), data=b"")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def target_active():
    """Is the bound session still in Echo's active list? None = can't tell."""
    try:
        sessions = echo_get("/sessions").get("active", [])
    except Exception:
        return None
    return any(s["id"] == TARGET for s in sessions)


def echo_notify(msg):
    body = (
        f"[ei-chat] {msg['name']}: {msg['text']}\n"
        f"(reply: curl -s -X POST http://127.0.0.1:{PORT}/api/{TOKEN}/send"
        f" -H 'Content-Type: application/json'"
        f" -d '{{\"name\":\"{AGENT_NAME}\",\"text\":\"...\"}}')"
    )
    try:
        echo_post("/send", {"to": TARGET, "body": body, "from_session": ECHO_ID})
    except Exception as e:
        print("echo notify failed:", e, flush=True)


def pretty_sender(session_id):
    head = session_id.split("-")[0]
    return head.capitalize() if head else session_id


def inbox_relay():
    """DMs sent to the bridge's Echo session get posted into the chat."""
    while True:
        try:
            for m in echo_get(f"/inbox?session_id={ECHO_ID}").get("messages", []):
                body = (m.get("body") or "").strip()
                if body:
                    save_message(pretty_sender(m.get("from_session", "?")), body)
        except Exception as e:
            print("inbox relay error:", e, flush=True)
        time.sleep(2)


def session_watchdog():
    """Shut down ~1 min after the bound session leaves Echo."""
    misses = 0
    while True:
        time.sleep(20)
        alive = target_active()
        if alive is None:
            continue
        misses = 0 if alive else misses + 1
        if misses >= 3:
            print(f"target session {TARGET} gone, shutting down", flush=True)
            save_message("System", f"{AGENT_NAME}'s session ended. "
                                   "The chat is offline for now.")
            try:
                echo_post("/unregister", {"session_id": ECHO_ID})
            except Exception:
                pass
            os._exit(0)


def start_echo_bridge():
    alive = target_active()
    if alive is None:
        sys.exit(f"EI_CHAT_TARGET is set but Echo is not reachable at {ECHO}.")
    if alive is False:
        sys.exit(f"EI_CHAT_TARGET '{TARGET}' is not an active Echo session.")
    try:
        r = echo_post("/register", {"session_id": ECHO_ID, "role": "bridge",
                                    "current_task": f"ei-chat bridge -> {TARGET}"})
    except Exception as e:
        sys.exit(f"Echo refused to register the bridge as '{ECHO_ID}': {e} "
                 "(set EI_CHAT_ECHO_ID to another name)")
    if not r.get("ok", True):
        sys.exit(f"Echo refused to register '{ECHO_ID}': {r.get('error')}")
    threading.Thread(target=inbox_relay, daemon=True).start()
    threading.Thread(target=session_watchdog, daemon=True).start()


# --- web page ----------------------------------------------------------------

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg:#0b0d12; --panel:#12151c; --panel-2:#171b24; --border:#242938;
    --text:#e8eaf0; --text-dim:#8b93a7; --accent:#5b8def; --accent-2:#4c7ce0;
    --online:#3fb950; --verified:#5b8def; --code-bg:#0b0d12; --err:#e5534b;
  }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
         background:var(--bg); color:var(--text); height:100dvh; overflow:hidden; }
  #app { display:flex; height:100%; }

  #sidebar { width:240px; flex-shrink:0; background:var(--panel); border-right:1px solid var(--border);
             display:flex; flex-direction:column; transition:margin-left .2s ease; }
  #sidebar.collapsed { margin-left:-240px; }
  #sidebar h2 { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--text-dim);
                padding:16px 16px 8px; margin:0; font-weight:600; }
  #participants { overflow-y:auto; flex:1; padding:0 8px; }
  .p-row { display:flex; align-items:center; gap:10px; padding:8px; border-radius:8px; }
  .p-row:hover { background:var(--panel-2); }
  .p-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; background:#3a3f4d; }
  .p-dot.online { background:var(--online); }
  .p-avatar { width:26px; height:26px; border-radius:50%; display:flex; align-items:center;
              justify-content:center; font-size:11px; font-weight:700; color:#0b0d12; flex-shrink:0; }
  .p-info { min-width:0; flex:1; }
  .p-name { font-size:13px; font-weight:600; white-space:nowrap; overflow:hidden;
            text-overflow:ellipsis; display:flex; align-items:center; gap:4px; }
  .p-sub { font-size:11px; color:var(--text-dim); }
  .badge-check { color:var(--verified); font-size:11px; }
  #sidebar-empty { padding:16px; font-size:12px; color:var(--text-dim); }

  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  header { padding:12px 16px; background:var(--panel); border-bottom:1px solid var(--border);
           display:flex; align-items:center; gap:10px; }
  header button.small { background:none; border:1px solid var(--border); color:var(--text-dim);
                        height:30px; padding:0 10px; border-radius:8px; cursor:pointer; font-size:12px; }
  header button.small:hover { color:var(--text); background:var(--panel-2); }
  header .title { font-weight:700; font-size:14px; }
  header .subtitle { color:var(--text-dim); font-size:12px; }
  .spacer { margin-left:auto; }
  #conn-dot { width:7px; height:7px; border-radius:50%; background:var(--online); }
  #conn-dot.down { background:var(--err); }

  #log { flex:1; overflow-y:auto; padding:18px 16px; display:flex; flex-direction:column; gap:2px; }
  .row { display:flex; flex-direction:column; max-width:78%; margin-bottom:8px; }
  .row.me { align-self:flex-end; align-items:flex-end; }
  .row.them { align-self:flex-start; align-items:flex-start; }
  .who { font-size:11px; color:var(--text-dim); margin:0 6px 3px; display:flex; align-items:center; gap:4px; }
  .m { padding:9px 13px; border-radius:14px; white-space:pre-wrap; word-wrap:break-word;
       font-size:14.5px; line-height:1.45; }
  .row.them .m { background:var(--panel-2); border:1px solid var(--border); border-bottom-left-radius:4px; }
  .row.me .m { background:var(--accent); color:#fff; border-bottom-right-radius:4px; }
  .m pre.code-block { background:var(--code-bg); border:1px solid var(--border); border-radius:8px;
                       padding:10px 12px; overflow-x:auto; margin:6px 0; font-size:13px; }
  .m code.inline-code { background:rgba(0,0,0,.25); padding:1px 5px; border-radius:4px;
                         font-size:0.9em; font-family:ui-monospace,Menlo,monospace; }
  pre.code-block code { font-family:ui-monospace,Menlo,monospace; }
  .sys-row { align-self:center; font-size:12px; color:var(--text-dim); margin:10px 0; text-align:center; }
  .sys-row.err { color:var(--err); }
  .mention { color:var(--accent); font-weight:600; }
  .chat-link { color:var(--accent); text-decoration:underline; }
  .row.me .chat-link, .row.me .mention { color:#fff; }
  .row.mentions-me .m { border:1px solid var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }

  form { display:flex; gap:8px; padding:12px; background:var(--panel); border-top:1px solid var(--border);
         position:relative; }
  input { flex:1; min-width:0; padding:11px 14px; border-radius:10px; border:1px solid var(--border);
          background:var(--bg); color:var(--text); font-size:15px; outline:none; }
  input:focus { border-color:var(--accent); }
  form button { padding:0 20px; border:none; border-radius:10px; background:var(--accent);
                color:#fff; font-weight:600; cursor:pointer; }
  form button:hover { background:var(--accent-2); }

  #mention-menu { position:absolute; bottom:64px; left:12px; background:var(--panel-2);
                  border:1px solid var(--border); border-radius:10px; overflow:hidden;
                  min-width:180px; display:none; box-shadow:0 8px 24px rgba(0,0,0,.4); }
  #mention-menu.open { display:block; }
  .mention-item { padding:8px 12px; font-size:13px; cursor:pointer; }
  .mention-item:hover, .mention-item.active { background:var(--accent); color:#fff; }

  @media (max-width:640px) {
    #sidebar { position:fixed; z-index:5; height:100%; }
    #sidebar:not(.open) { margin-left:-240px; }
    .row { max-width:90%; }
    header .subtitle { display:none; }
  }
</style></head><body>
<div id="app">
  <div id="sidebar" class="collapsed">
    <h2>Participants</h2>
    <div id="participants"><div id="sidebar-empty">No one seen yet</div></div>
  </div>
  <div id="main">
    <header>
      <button class="small" id="sidebar-toggle" title="Participants">&#9776;</button>
      <div>
        <div class="title">__TITLE__</div>
        <div class="subtitle">__SUBTITLE__</div>
      </div>
      <span class="spacer"></span>
      <button class="small" id="name-btn" title="Change your name"></button>
      <button class="small" id="key-btn" title="Set or clear your key">key</button>
      <div id="conn-dot" title="connection"></div>
    </header>
    <div id="log"></div>
    <form id="f">
      <div id="mention-menu"></div>
      <input id="t" placeholder="Message (@ to mention)" autocomplete="off">
      <button>Send</button>
    </form>
  </div>
</div>
<script>
const API = "/api/__TOKEN__";
const store = {
  get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
};

let NAME = store.get("ei-chat-name");
while (!NAME) NAME = (prompt("Your name?") || "").trim().slice(0, 40);
store.set("ei-chat-name", NAME);
let KEY = store.get("ei-chat-key");

const log = document.getElementById("log");
const sidebar = document.getElementById("sidebar");
const participantsEl = document.getElementById("participants");
const connDot = document.getElementById("conn-dot");
const nameBtn = document.getElementById("name-btn");
const keyBtn = document.getElementById("key-btn");
let last = 0;
let polling = false;

function authHeaders() { return KEY ? {"Authorization": "Bearer " + KEY} : {}; }
function refreshHeader() {
  nameBtn.textContent = NAME;
  keyBtn.textContent = KEY ? "key set" : "key";
}
refreshHeader();

if (window.innerWidth > 640) sidebar.classList.remove("collapsed");
document.getElementById("sidebar-toggle").addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
  sidebar.classList.toggle("open");
});
nameBtn.addEventListener("click", () => {
  const n = (prompt("Your name?", NAME) || "").trim().slice(0, 40);
  if (n) { NAME = n; store.set("ei-chat-name", NAME); refreshHeader(); }
});
function askKey(why) {
  const k = prompt((why ? why + "\n\n" : "") +
                   "Your key (leave empty to chat without one):", KEY);
  if (k === null) return false;
  KEY = k.trim(); store.set("ei-chat-key", KEY); refreshHeader();
  return true;
}
keyBtn.addEventListener("click", () => askKey(""));

function showSystem(text, isErr) {
  const d = document.createElement("div");
  d.className = "sys-row" + (isErr ? " err" : "");
  d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}

function initials(name) {
  return (name.match(/[A-Z0-9]/g) || [name[0] || "?"]).slice(0, 2).join("").toUpperCase();
}
function colorFor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 60% 65%)`;
}

// Fenced ```code``` -> <pre><code>, inline `code` -> <code>, the rest via
// appendInline. textContent only, never innerHTML, so nothing can inject markup.
function renderText(container, text) {
  text.split("```").forEach((block, i) => {
    if (i % 2 === 1) {
      const pre = document.createElement("pre");
      pre.className = "code-block";
      const code = document.createElement("code");
      code.textContent = block.replace(/^[a-zA-Z0-9_+-]*\n/, "");
      pre.appendChild(code);
      container.appendChild(pre);
      return;
    }
    block.split(/`([^`]+)`/).forEach((seg, j) => {
      if (!seg) return;
      if (j % 2 === 1) {
        const c = document.createElement("code");
        c.className = "inline-code";
        c.textContent = seg;
        container.appendChild(c);
      } else {
        appendInline(container, seg);
      }
    });
  });
}

// **bold**, *italic* / _italic_, [text](https://url), @mentions.
function appendInline(container, text) {
  const re = /\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*|_([^_]+)_|@([A-Za-z0-9_-]+)/g;
  let lastIndex = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIndex) container.appendChild(document.createTextNode(text.slice(lastIndex, m.index)));
    let el;
    if (m[1] !== undefined) {
      if (/^https?:\/\//i.test(m[2])) {
        el = document.createElement("a");
        el.href = m[2]; el.textContent = m[1];
        el.target = "_blank"; el.rel = "noopener noreferrer"; el.className = "chat-link";
      } else {
        el = document.createTextNode(m[0]);  // unknown scheme: show literally
      }
    } else if (m[3] !== undefined) {
      el = document.createElement("strong"); el.textContent = m[3];
    } else if (m[4] !== undefined || m[5] !== undefined) {
      el = document.createElement("em"); el.textContent = m[4] !== undefined ? m[4] : m[5];
    } else {
      el = document.createElement("span"); el.className = "mention"; el.textContent = "@" + m[6];
    }
    container.appendChild(el);
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) container.appendChild(document.createTextNode(text.slice(lastIndex)));
}

function mentionsMe(text) {
  const target = NAME.toLowerCase();
  return (text.match(/@([A-Za-z0-9_-]+)/g) || []).some(m => m.slice(1).toLowerCase() === target);
}

function renderMessage(m) {
  if (m.name === "System") return showSystem(m.text, false);
  const row = document.createElement("div");
  row.className = "row " + (m.name === NAME ? "me" : "them");
  if (m.name !== NAME && mentionsMe(m.text)) row.classList.add("mentions-me");
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = m.name + " · " + m.ts;
  if (m.verified) {
    const check = document.createElement("span");
    check.className = "badge-check"; check.title = "verified"; check.textContent = "✓";
    who.appendChild(check);
  }
  const bubble = document.createElement("div");
  bubble.className = "m";
  renderText(bubble, m.text);
  row.appendChild(who);
  row.appendChild(bubble);
  log.appendChild(row);
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const r = await fetch(`${API}/messages?since=${last}`);
    if (!r.ok) throw new Error(r.status);
    const msgs = await r.json();
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    for (const m of msgs) {
      if (m.id <= last) continue;
      last = m.id;
      renderMessage(m);
    }
    if (atBottom) log.scrollTop = log.scrollHeight;
    connDot.classList.remove("down");
  } catch (e) {
    connDot.classList.add("down");
  } finally {
    polling = false;
  }
}

let knownNames = [];
function lastSeen(s) {
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function renderParticipants(list) {
  knownNames = list.map(p => p.name);
  participantsEl.innerHTML = "";
  if (!list.length) {
    participantsEl.appendChild(Object.assign(document.createElement("div"),
      {id: "sidebar-empty", textContent: "No one seen yet"}));
    return;
  }
  for (const p of list) {
    const row = document.createElement("div"); row.className = "p-row";
    const dot = document.createElement("div"); dot.className = "p-dot" + (p.online ? " online" : "");
    const avatar = document.createElement("div"); avatar.className = "p-avatar";
    avatar.style.background = colorFor(p.name); avatar.textContent = initials(p.name);
    const info = document.createElement("div"); info.className = "p-info";
    const nameEl = document.createElement("div"); nameEl.className = "p-name"; nameEl.textContent = p.name;
    if (p.verified) {
      const check = document.createElement("span");
      check.className = "badge-check"; check.title = "verified"; check.textContent = "✓";
      nameEl.appendChild(check);
    }
    const sub = document.createElement("div"); sub.className = "p-sub";
    sub.textContent = p.online ? "online" : lastSeen(p.last_seen_secs_ago);
    info.appendChild(nameEl); info.appendChild(sub);
    row.appendChild(dot); row.appendChild(avatar); row.appendChild(info);
    participantsEl.appendChild(row);
  }
}
async function pollParticipants() {
  try {
    const r = await fetch(`${API}/participants`);
    if (r.ok) renderParticipants(await r.json());
  } catch (e) {}
}
async function heartbeat() {
  try {
    await fetch(`${API}/heartbeat`, {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, authHeaders()),
      body: JSON.stringify({name: NAME}),
    });
  } catch (e) {}
}

// @mention autocomplete
const input = document.getElementById("t");
const menu = document.getElementById("mention-menu");
let mentionStart = -1, mentionItems = [], mentionActive = 0;
function currentWordStart() {
  const v = input.value; let i = input.selectionStart;
  while (i > 0 && v[i - 1] !== " " && v[i - 1] !== "\n") i--;
  return i;
}
function closeMentionMenu() { mentionStart = -1; menu.classList.remove("open"); menu.innerHTML = ""; }
function updateMentionMenu() {
  const ws = currentWordStart();
  const word = input.value.slice(ws, input.selectionStart);
  if (!word.startsWith("@")) return closeMentionMenu();
  const partial = word.slice(1).toLowerCase();
  mentionItems = knownNames.filter(n => n.toLowerCase().startsWith(partial) && n !== NAME);
  if (!mentionItems.length) return closeMentionMenu();
  mentionStart = ws; mentionActive = 0;
  renderMentionMenu();
}
function renderMentionMenu() {
  menu.innerHTML = "";
  mentionItems.forEach((name, i) => {
    const item = document.createElement("div");
    item.className = "mention-item" + (i === mentionActive ? " active" : "");
    item.textContent = name;
    item.addEventListener("mousedown", e => { e.preventDefault(); selectMention(name); });
    menu.appendChild(item);
  });
  menu.classList.add("open");
}
function selectMention(name) {
  const before = input.value.slice(0, mentionStart);
  const after = input.value.slice(input.selectionStart);
  input.value = before + "@" + name + " " + after;
  const caret = (before + "@" + name + " ").length;
  input.setSelectionRange(caret, caret);
  closeMentionMenu();
  input.focus();
}
input.addEventListener("input", updateMentionMenu);
input.addEventListener("click", updateMentionMenu);
input.addEventListener("keydown", e => {
  if (mentionStart === -1) return;
  if (e.key === "ArrowDown") { e.preventDefault(); mentionActive = (mentionActive + 1) % mentionItems.length; renderMentionMenu(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); mentionActive = (mentionActive - 1 + mentionItems.length) % mentionItems.length; renderMentionMenu(); }
  else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); selectMention(mentionItems[mentionActive]); }
  else if (e.key === "Escape") closeMentionMenu();
});

async function send(text) {
  const r = await fetch(`${API}/send`, {
    method: "POST",
    headers: Object.assign({"Content-Type": "application/json"}, authHeaders()),
    body: JSON.stringify({name: NAME, text}),
  });
  if (r.ok) return null;
  let err = "HTTP " + r.status;
  try { err = (await r.json()).error || err; } catch (e) {}
  return {status: r.status, error: err};
}

// The typed text is only cleared once the server has accepted it, and every
// failure is shown in the log instead of disappearing silently.
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  closeMentionMenu();
  let fail;
  try { fail = await send(text); }
  catch (err) { fail = {status: 0, error: "could not reach the server"}; }
  if (fail && fail.status === 403 && askKey("Not sent: " + fail.error)) {
    try { fail = await send(text); } catch (err) { fail = {status: 0, error: "could not reach the server"}; }
  }
  if (fail) { showSystem("Not sent: " + fail.error, true); return; }
  input.value = "";
  poll();
  pollParticipants();
});

setInterval(poll, 2000); poll();
setInterval(pollParticipants, 5000); pollParticipants();
setInterval(heartbeat, 15000); heartbeat();
</script></body></html>"""


def render_page():
    return (PAGE.replace("__TITLE__", html.escape(TITLE))
                .replace("__SUBTITLE__", html.escape(SUBTITLE))
                .replace("__TOKEN__", TOKEN))


# --- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n)) if n else {}
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        return body

    def do_GET(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        q = parse_qs(u.query)
        if parts == ["chat", TOKEN]:
            data = render_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif parts == ["api", TOKEN, "messages"]:
            try:
                since = int(q.get("since", ["0"])[0])
                wait = float(q.get("wait", ["0"])[0])
            except ValueError:
                return self._json({"error": "since must be an int, wait a number"}, 400)
            self._json(messages_since(since, wait))
        elif parts == ["api", TOKEN, "participants"]:
            self._json(presence_snapshot())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if parts == ["api", TOKEN, "heartbeat"]:
            return self._post(store=False)
        if parts == ["api", TOKEN, "send"]:
            return self._post(store=True)
        self._json({"error": "not found"}, 404)

    def _post(self, store):
        try:
            body = self._body()
            name = str(body.get("name", "")).strip()[:40]
            text = str(body.get("text", "")).strip()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad json"}, 400)
        if not name:
            return self._json({"error": "name is required"}, 400)
        if name == "System":
            return self._json({"error": "'System' is reserved"}, 403)
        if store and not text:
            return self._json({"error": "empty message"}, 400)
        try:
            name, verified = resolve_identity(self.headers, name)
        except ValueError as e:
            return self._json({"error": str(e)}, 403)
        touch_presence(name, verified)
        if not store:
            return self._json({"ok": True})
        msg = save_message(name, text, verified)
        if TARGET and name != AGENT_NAME:
            threading.Thread(target=echo_notify, args=(msg,), daemon=True).start()
        self._json(msg)

    def log_message(self, fmt, *args):
        if os.environ.get("EI_CHAT_QUIET"):
            return
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


# --- CLI -----------------------------------------------------------------------

def cmd_key(name):
    name = name.strip()[:40]
    if not name or name == "System":
        sys.exit("give a name, e.g.  python3 server.py key Alice")
    keys = {k: v for k, v in load_keys().items() if v != name}
    key = secrets.token_urlsafe(24)
    keys[key] = name
    _write_json(KEYS_FILE, keys)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass
    print(f"key for {name} (replaces any old one, send it privately):\n{key}")


def cmd_revoke(name):
    keys = load_keys()
    kept = {k: v for k, v in keys.items() if v != name}
    if len(kept) == len(keys):
        sys.exit(f"no key for {name}")
    _write_json(KEYS_FILE, kept)
    print(f"revoked {name}")


def serve():
    global MESSAGES
    MESSAGES = _read_json(STORE, [])
    seed_presence_from_history()
    if TARGET:
        start_echo_bridge()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "") else HOST
    print(f"ei-chat on http://{shown}:{PORT}/chat/{TOKEN}"
          + (f"  (Echo bridge -> {TARGET})" if TARGET else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv):
    if not argv:
        return serve()
    cmd, rest = argv[0], argv[1:]
    if cmd == "key" and rest:
        return cmd_key(" ".join(rest))
    if cmd == "revoke" and rest:
        return cmd_revoke(" ".join(rest))
    if cmd == "keys":
        names = sorted(set(load_keys().values()))
        print("\n".join(names) if names else "no keys yet")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
