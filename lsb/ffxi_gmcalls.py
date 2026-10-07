"""In-game GM calls: FINAL FANTASY XI's Help Desk to the OpenLobby GM desk, and back.

A player who opens the Help Desk in the game and sends a GM call never touches
the PlayOnline Viewer. The client sends packet 0x0D3 to the map server, and
LandSandBoat (the hippaul-ps2 fork, src/map/gmcall_container.cpp) stores it
as a row of its own `help_desk` table (charid, message) and logs it on
xi_world, where routing to anything outside is still a TODO. Nothing reached
the GM desk, nobody was alerted, and the player waited on a call no GM could
see.

This module is the missing route, run as a thread inside the bridge:

  * Every `period` seconds it reads `help_desk` rows newer than the last one
    it handled and files each as a ticket in the directory the core's `gmd`
    writes and the admin panel's GM desk reads (POL_GMD_TICKET_DIR, the same
    JSON record gmd files for a Viewer call). From there the usual desk
    machinery applies unchanged: the Discord/Web Push alert, the no-GM-on-duty
    auto-reply, knock, invite, close, the transcript mail. The caller is
    named by the handle their character's Content ID is linked to, with the
    handle's client id as the ticket's `guid`, so the ticket is also the
    player's own if they open the Viewer's GM Call screen later, and nobody
    else's (gmd matches held tickets on that id alone).

  * In the other direction it watches the desk's per-ticket state
    (gm-tickets.json) for the tickets it filed and writes the desk's answer
    back into `help_desk.response`, which LandSandBoat sends to the character
    as a GM message (0x0B6) on their next zone or login and clears once the
    player acknowledges it (0x0D5). Three events are delivered: the auto-reply
    when no GM was on duty, a knock (the GM is ready in the Viewer's GM chat),
    and the close, with the GM's resolution note when one was written.

State lives next to the tickets: `ffxi-helpdesk.json` holds the last
`help_desk` id handled, and each ticket records its `help_desk` row and what
was delivered under its `ffxi` key, so a restart neither re-files a call nor
repeats an answer. The bridge needs write access to the ticket directory for
this (docker-compose.yml mounts it); without it the thread says so and keeps
trying.

Everything that decides something is a plain function over dicts, driven by
tools/ffxi_gmcalls_test.py without a database.
"""
import json
import os
import re
import socket
import struct
import time

#: gmd's ticket name. The number is the request number.
TICKET_RE = re.compile(r"gm-\d{8}T\d{6}-(\d+)\.json")
#: gmd's request counter and the desk's per-ticket state, in the ticket dir.
STATE_NAME = "gm-state.txt"
TICKET_STATE_NAME = "gm-tickets.json"
#: This module's own watermark, in the ticket dir.
OWN_STATE_NAME = "ffxi-helpdesk.json"
#: The core's content code for FINAL FANTASY XI (accounts.py).
CONTENT_FFXI = 1
SOURCE = "ffxi-helpdesk"
#: A `help_desk` row this module INSERTS to carry a reply when the call's own
#: row already holds one. Its message starts with this, so the poller skips it.
REPLY_MARK = "[PlayOnline GM desk]"
#: LandSandBoat sends at most this much of a response (ipc_server.cpp).
RESPONSE_MAX = 1024
#: Rows read per poll. More arrive on the next one.
BATCH = 50

SUBJECT = "GM Call from the game"
#: What the player is told in the game. {n} is the request number.
AWAY_TEXT = ("Your GM call #{n} has been received. No GM is on duty at the "
             "moment. A GM will reply as soon as one is available.")
KNOCK_TEXT = ("A GM is ready to talk about your call #{n}. Open the PlayOnline "
              "Viewer, choose Service & Support, then GM Call, and press Start.")
CLOSE_TEXT = "Your GM call #{n} has been closed."
REPLY_TEXT = "GM reply to your call #{n}: {text}"


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _write_json(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------- request numbers
def highest_request_no(ticket_dir):
    """The largest request number any ticket on disk carries, or 0."""
    best = 0
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return 0
    for n in names:
        m = TICKET_RE.fullmatch(n)
        if m:
            best = max(best, int(m.group(1)))
    return best


def next_request_no(ticket_dir):
    """Allocate a request number the way gmd does (read gm-state.txt, write
    the next one back), with two guards gmd does not need because it is the
    only writer of its own file: the number is never at or below one already
    on a ticket, and a counter that cannot be read (gmd writes it in place, so
    a read can land on an empty file) does not restart the numbering at 1.
    The write is atomic so gmd's own read never sees a torn file."""
    path = os.path.join(ticket_dir, STATE_NAME)
    n = None
    try:
        with open(path) as f:
            n = int(f.read().strip())
    except (OSError, ValueError):
        n = None
    floor = highest_request_no(ticket_dir) + 1
    if n is None or n < 1:
        n = floor
    n = max(n, floor)
    os.makedirs(ticket_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n + 1) + "\n")
    os.replace(tmp, path)
    return n


def room_name(req_no, env=os.environ):
    """The GM chat room gmd will name for this request (gmd.room_for): the
    per-request prefix plus the number, if gmd runs with a room at all."""
    base = env.get("POL_GMD_CHAT_ROOM", "#gmchat001")
    per = env.get("POL_GMD_ROOM_PER_REQUEST", "#gmcall")
    if per and base and req_no:
        return (per + "%03d" % req_no)[:31]
    return base or None


# --------------------------------------------------------------------- tickets
def _ip(value):
    """accounts_sessions.client_addr is an IPv4 as a host-order integer."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, str):
        return value
    try:
        return socket.inet_ntoa(struct.pack("<I", int(value) & 0xFFFFFFFF))
    except (struct.error, OSError, ValueError):
        return ""


def build_ticket(row, handle, req_no, room, now=None):
    """The ticket record gmd would have filed, from a `help_desk` row joined
    with the character, its zone, account and session (poll_once's query),
    and the handle the character's Content ID is linked to (or None).

    gmd's own fields come first and mean the same: `guid` is the handle's
    client id, which is how gmd knows the ticket is this player's; `handle`
    the name the desk shows; `peer` the player's address as ip:0 (the desk
    compares the ip part); `room` the chat room a knock sends them to.
    `source` and `ffxi` are this module's: which row the call came from, and
    what has been delivered back."""
    now = time.time() if now is None else now
    name = (row.get("charname") or "").strip() or f"charid {row.get('charid')}"
    zone = (row.get("zone_name") or "").strip()
    where = f" in {zone}" if zone else ""
    message = (row.get("message") or "").strip()
    who = f"Sent from the Help Desk in FINAL FANTASY XI by {name}{where}."
    if handle is None:
        who += (" This character's Content ID is not linked to a handle, so "
                "the caller could not be named; see the ffxi block.")
    body = f"{message}\n\n{who}" if message else who
    created = row.get("created_at")
    if hasattr(created, "strftime"):
        created = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    ip = _ip(row.get("client_addr"))
    return {
        "received_at": iso(now),
        "request_no": int(req_no),
        "guid": int((handle or {}).get("client_guid") or 0),
        "handle": (handle or {}).get("handle_name") or "",
        "content_id": CONTENT_FFXI,
        "issue": 0,
        "subject": SUBJECT,
        "body": body,
        "peer": f"{ip}:0" if ip else "",
        "room": room,
        "raw": "",
        "source": SOURCE,
        "ffxi": {
            "help_desk_id": int(row["id"]),
            "charid": int(row.get("charid") or 0),
            "name": row.get("charname") or "",
            "zone_id": int(row.get("pos_zone") or 0),
            "zone": zone,
            "lsb_login": row.get("login") or "",
            "handle_id": int((handle or {}).get("id") or 0),
            "member_id": int((handle or {}).get("member_id") or 0),
            "filed_in_game_at": str(created or ""),
            "delivered": {},
        },
    }


def ticket_name(req_no, now=None):
    now = time.time() if now is None else now
    return time.strftime("gm-%Y%m%dT%H%M%S", time.gmtime(now)) + f"-{req_no}.json"


def own_tickets(ticket_dir):
    """{help_desk id: ticket id} for every ticket this module filed."""
    out = {}
    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return out
    for n in names:
        if not TICKET_RE.fullmatch(n):
            continue
        rec = _read_json(os.path.join(ticket_dir, n), {})
        if rec.get("source") != SOURCE:
            continue
        hid = (rec.get("ffxi") or {}).get("help_desk_id")
        if hid is not None:
            out[int(hid)] = n[:-len(".json")]
    return out


# ------------------------------------------------------------------ the poll
ROW_SQL = ("SELECT h.id, h.charid, h.message, h.created_at, c.charname, c.accid, "
           "c.pos_zone, z.name, a.login, s.client_addr "
           "FROM help_desk h "
           "LEFT JOIN chars c ON c.charid = h.charid "
           "LEFT JOIN zone_settings z ON z.zoneid = c.pos_zone "
           "LEFT JOIN accounts a ON a.id = c.accid "
           "LEFT JOIN accounts_sessions s ON s.charid = h.charid "
           "WHERE h.id > %s ORDER BY h.id LIMIT %s")
ROW_COLS = ("id", "charid", "message", "created_at", "charname", "accid",
            "pos_zone", "zone_name", "login", "client_addr")


def fetch_rows(conn, since_id, limit=BATCH):
    with conn.cursor() as cur:
        cur.execute(ROW_SQL, (int(since_id), int(limit)))
        rows = cur.fetchall()
    return [dict(zip(ROW_COLS, r)) for r in rows]


def max_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM help_desk")
        row = cur.fetchone()
    return int(row[0] if row else 0)


class Relay:
    """One direction each way, over one LSB connection the caller manages.

    `content_id_for(charid)` -> the POL Content ID the bridge paired the
    character with, or None; `handle_for(content_id)` -> the handle row
    (dict with handle_name, client_guid, id, member_id) or None; `log(msg)`.
    `since_id` on first run: None = start at the newest row now (older calls
    are history, not news), a number = start after that row."""

    def __init__(self, ticket_dir, content_id_for, handle_for, log=print,
                 since_id=None, env=os.environ):
        self.ticket_dir = ticket_dir
        self.content_id_for = content_id_for
        self.handle_for = handle_for
        self.log = log
        self.since_id = since_id
        self.env = env
        self.own_path = os.path.join(ticket_dir, OWN_STATE_NAME)
        self.last_id = None
        self._own = None

    # -- state
    def load(self, conn):
        st = _read_json(self.own_path, {})
        if isinstance(st.get("last_id"), int):
            self.last_id = st["last_id"]
        elif self.since_id is not None:
            self.last_id = int(self.since_id)
        else:
            self.last_id = max_id(conn)
            self.log(f"starting after help_desk row {self.last_id} (older calls "
                     f"are not re-filed; FFXI_GMCALL_SINCE_ID=0 would file them all)")
        self._own = own_tickets(self.ticket_dir)
        return self.last_id

    def save(self):
        _write_json(self.own_path, {"last_id": self.last_id, "updated": iso()})

    def writable(self):
        try:
            os.makedirs(self.ticket_dir, exist_ok=True)
        except OSError:
            return False
        return os.access(self.ticket_dir, os.W_OK)

    # -- game -> desk
    def file_calls(self, conn, now=None):
        """File every new help_desk row as a ticket. Returns the records filed."""
        if self.last_id is None:
            self.load(conn)
        filed = []
        for row in fetch_rows(conn, self.last_id):
            hid = int(row["id"])
            if (row.get("message") or "").startswith(REPLY_MARK):
                pass                      # our own reply carrier, not a call
            elif hid in self._own:
                pass                      # filed before the watermark was saved
            else:
                rec = self.file_one(row, now)
                if rec is not None:
                    filed.append(rec)
            self.last_id = max(self.last_id, hid)
        if filed or self.last_id != _read_json(self.own_path, {}).get("last_id"):
            self.save()
        return filed

    def file_one(self, row, now=None):
        handle = None
        cid = self.content_id_for(row.get("charid"))
        if cid is not None:
            try:
                h = self.handle_for(cid)
                handle = dict(h) if h is not None else None
            except Exception as exc:
                self.log(f"handle lookup for Content ID {cid} failed ({exc!r}); "
                         f"filing the call unnamed")
        req_no = next_request_no(self.ticket_dir)
        rec = build_ticket(row, handle, req_no, room_name(req_no, self.env), now)
        name = ticket_name(req_no, now)
        _write_json(os.path.join(self.ticket_dir, name), rec)
        self._own[int(row["id"])] = name[:-len(".json")]
        self.log(f"GM call #{req_no} filed as {name}: help_desk row {row['id']}, "
                 f"{rec['ffxi']['name'] or 'charid %s' % rec['ffxi']['charid']}"
                 f" -> handle {rec['handle'] or '(unknown)'}")
        return rec

    # -- desk -> game
    def answer_calls(self, conn):
        """Deliver what the desk did with our tickets. Returns [(ticket id,
        kind, text)] for what was written."""
        if self._own is None:
            self._own = own_tickets(self.ticket_dir)
        if not self._own:
            return []
        state = _read_json(os.path.join(self.ticket_dir, TICKET_STATE_NAME), {})
        out = []
        for hid, tid in sorted(self._own.items()):
            path = os.path.join(self.ticket_dir, tid + ".json")
            rec = _read_json(path, {})
            if rec.get("source") != SOURCE:
                continue
            due = deliveries(rec, state.get(tid) or {})
            if not due:
                continue
            charid = int((rec.get("ffxi") or {}).get("charid") or 0)
            for kind, marker, text in due:
                try:
                    deliver(conn, charid, hid, text)
                except Exception as exc:
                    self.log(f"could not deliver the {kind} for {tid} in game "
                             f"({exc!r}); will retry")
                    break
                # Re-read before writing: gmd may have marked it meanwhile.
                rec = _read_json(path, rec)
                rec.setdefault("ffxi", {}).setdefault("delivered", {})[kind] = marker
                _write_json(path, rec)
                out.append((tid, kind, text))
                self.log(f"{tid}: {kind} delivered to charid {charid} in game")
        return out


def deliveries(rec, state):
    """What `rec` (a ticket this module filed) still owes the player, from the
    desk's state for it: [(kind, marker, text)]. The marker is the desk event's
    own timestamp, so a withdrawn-and-repeated knock or a reopened-and-closed
    request is delivered again, and the same event only once."""
    n = rec.get("request_no") or "?"
    done = (rec.get("ffxi") or {}).get("delivered") or {}
    out = []
    away = state.get("auto_reply_at")
    if away and done.get("away") != away:
        out.append(("away", away, AWAY_TEXT.format(n=n)))
    knock = state.get("knocked_at")
    if knock and done.get("knock") != knock:
        out.append(("knock", knock, KNOCK_TEXT.format(n=n)))
    if state.get("status") == "closed":
        at = state.get("at") or True
        if done.get("closed") != at:
            note = str(state.get("resolution") or "").strip()
            text = REPLY_TEXT.format(n=n, text=note) if note else CLOSE_TEXT.format(n=n)
            out.append(("closed", at, text))
    return out


def deliver(conn, charid, help_desk_id, text):
    """Hand `text` to the character as a GM message: into the call's own row
    while it has no response yet, else a new row that carries only the reply
    (LandSandBoat sends every unacknowledged response in id order)."""
    text = str(text or "")[:RESPONSE_MAX]
    with conn.cursor() as cur:
        cur.execute("UPDATE help_desk SET response = %s, responded_at = NOW() "
                    "WHERE id = %s AND response IS NULL AND deleted_at IS NULL",
                    (text, int(help_desk_id)))
        if cur.rowcount:
            return "updated"
        cur.execute("INSERT INTO help_desk (charid, message, response, responded_at) "
                    "VALUES (%s, %s, %s, NOW())",
                    (int(charid), f"{REPLY_MARK} reply to help_desk row {int(help_desk_id)}",
                     text))
    return "inserted"


def run(relay, connect, period, log=print, sleep=time.sleep, once=False):
    """The thread body: file new calls and answer old ones, every `period`
    seconds, over one connection `connect()` -> (conn, error) returns."""
    conn, err = None, None
    said = None
    while True:
        try:
            if not relay.writable():
                msg = (f"{relay.ticket_dir} is not writable: mount the core's "
                       f"gm-calls directory read-write in the bridge (see "
                       f"docker-compose.yml); calls stay in help_desk until then")
                if msg != said:
                    log(msg)
                    said = msg
            else:
                said = None
                if conn is None:
                    conn, err = connect()
                if conn is not None:
                    conn.ping(reconnect=True)
                    relay.file_calls(conn)
                    relay.answer_calls(conn)
                elif err:
                    log(f"not polling: {err}")
                    err = None
        except Exception as exc:
            log(f"poll failed ({exc!r}); reconnecting")
            conn = None
        if once:
            return
        sleep(period)
