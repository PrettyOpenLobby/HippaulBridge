"""The admin panel's FFXI Federation tool, served by the bridge (FFXI_FEDTOOL_PORT).

Manages both directions of FFXI federation from one page, live:

  - WORLDS OUR PLAYERS CAN VISIT (outbound): the FFXI_FED_WORLDS list the lobby reads
    (ffxi_federation.py). A world is added by its server id and key set URL; the key set is
    fetched and checked against that id before anything is saved, and the bridge picks the
    change up at once, no restart.
  - PROVIDERS WHOSE PLAYERS CAN VISIT US (inbound): the trusted key sets our world's
    federation gateway reads (xi_world, network.FEDERATION_TRUST_DIR, re-read every minute).
    Only files named <server id>.keyset are ever written or removed there; the world's own
    identity.key sits in the same directory and is never touched.
  - OUR OWN IDS, to hand to other operators: our provider id and key set, and our world's.

A second page, /notices, is the in-game notices tool (ffxi_notices.py): the lines the login
message carries and the system-chat announcements, posted now, later or as a countdown to a
shutdown. Its page and routes all start with /notices and share nothing with the federation's.

A third, /bonanza, manages the Mog Bonanza runs (ffxi_bonanza.py); its routes start with
/bonanza and it reads and writes the LSB database directly.

A fourth, /chars, looks up and fixes characters (ffxi_chars.py): move one, take off its
equipment, edit its character variables, give it items, copy one onto a test character, and
undo any of it. Its routes start with /chars and it reads and writes the LSB database directly.
/chars/player.json answers the panel's account window: the characters of the POL members it is
given, through the LSB accounts the bridge made for them.

A fifth, /ah, lists and changes the auction house (ffxi_auction.py): prices, taking a listing
down, test listings. Its routes start with /ah and it reads and writes the LSB database directly.

A sixth, /events, switches the seasonal festivals on, off or back to their calendar (ffxi_events.py),
server-wide. Its routes start with /events and it reads and writes the LSB database directly.

The panel (OpenLobby admingames, key "ffxi") proxies this port under /games/ffxi/ behind its
sign-in; this port only checks the panel's token (X-Devtool-Token, or ?t= when opened
directly) and logs the panel's actor (X-Devtool-Actor). Same contract as the FE and FMO tools.

  FFXI_FEDTOOL_PORT    the port (unset = off)
  FFXI_FEDTOOL_BIND    default 127.0.0.1; anything wider REQUIRES a token
  FFXI_FEDTOOL_TOKEN   the token the panel sends (POL_ADMIN_GAME_FFXI_TOKEN there)
  FFXI_FED_TRUST_DIR   our world's trust directory (lsb/federation on the host)
  FFXI_FED_WORLD_KEYSET_URL  our world's gateway or key set URL, shown as ours (optional)
"""
import collections
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ffxi_auction as A
import ffxi_bonanza as B
import ffxi_chars as C
import ffxi_events as E
import ffxi_federation as F
import ffxi_notices as N
import xitoken as X

_LOG = collections.deque(maxlen=200)
_NLOG = collections.deque(maxlen=200)
_LOCK = threading.Lock()
_ID = re.compile(r"xi1\.[A-Za-z0-9_-]{22}\Z")


class ToolError(Exception):
    pass


class Tool:
    """The tool's state and actions. `bridge` is the ffxi_bridge module (FED, reload, the
    local world's name); passed in so this module never imports the bridge."""

    def __init__(self, bridge, environ=os.environ):
        self.bridge = bridge
        self.worlds_path = (environ.get("FFXI_FED_WORLDS") or "").strip()
        self.keys_dir = (environ.get("FFXI_FED_KEYS") or "").strip()
        self.trust_dir = (environ.get("FFXI_FED_TRUST_DIR") or "").strip()
        # Our world's gateway (LSB_FEDERATION_URL), with or without /xi/v1/keyset.
        url = (environ.get("FFXI_FED_WORLD_KEYSET_URL") or "").strip().rstrip("/")
        if url and not url.endswith("/xi/v1/keyset"):
            url += "/xi/v1/keyset"
        self.world_url = url if url.startswith(("https://", "http://")) else ""
        self._provider = None
        self._world = (0.0, None)

    # -- what the page shows -------------------------------------------------------------------

    def provider(self):
        """Our provider id and the key set to hand to world operators (made once per start)."""
        if self._provider is None:
            try:
                issuer = F.load_issuer(self.keys_dir)
                identity = X.SigningKey.from_file("identity", os.path.join(self.keys_dir, "identity.key"))
                self._provider = {"id": issuer.issuer_id, "kid": issuer.key.kid,
                                  "keyset": X.make_keyset(identity, "OpenLobby", [issuer.key])}
            except (OSError, ValueError) as exc:
                return {"error": "provider keys unreadable: %s" % exc}
        return self._provider

    def our_world(self):
        if not self.world_url:
            return None
        at, got = self._world
        if got and time.time() - at < 300:
            return got
        try:
            base = self.world_url[:-len("/xi/v1/keyset")] if self.world_url.endswith("/xi/v1/keyset") else self.world_url
            ks = X.load_keyset(X.WorldGateway(base).keyset())
            got = {"id": ks.server_id, "name": ks.name, "url": self.world_url, "world": ks.world}
        except (OSError, X.XiTokenError, X.GatewayError) as exc:
            got = {"url": self.world_url, "error": str(exc)}
        self._world = (time.time(), got)
        return got

    def worlds(self):
        fed = self.bridge.FED
        live = {w.server_id: w for w in (fed.worlds if fed else [])}
        out = []
        for i, ent in enumerate(F.read_worlds(self.worlds_path) if self.worlds_path else []):
            w = live.get(ent.get("id"))
            ks = w.keyset if w else None
            out.append({"no": w.no if w else None, "id": ent.get("id"), "keyset": ent.get("keyset"),
                        "name": ent.get("name") or "", "pin": ent.get("pin") or "",
                        "shown": w.name if w else None,
                        "gateway": (ks.world or {}).get("gateway") if ks else None,
                        "search": (ks.world or {}).get("search") if ks else None,
                        "expansions": (ks.world or {}).get("expansions") if ks else None,
                        "ok": bool(ks), "error": w.error if w else "not loaded (federation off?)"})
        return out

    def providers(self):
        out = []
        if not self.trust_dir or not os.path.isdir(self.trust_dir):
            return out
        for name in sorted(os.listdir(self.trust_dir)):
            sid = name[:-len(".keyset")] if name.endswith(".keyset") else None
            if not sid or not _ID.match(sid):
                continue
            row = {"id": sid}
            try:
                with open(os.path.join(self.trust_dir, name)) as f:
                    ks = X.load_keyset(f.read().strip(), sid)
                row.update(name=ks.name, keys=sorted(ks.keys), issued=X.format_time(ks.issued), ok=True)
            except (OSError, X.XiTokenError) as exc:
                row.update(ok=False, error=str(exc))
            out.append(row)
        return out

    def state(self):
        fed = self.bridge.FED
        with _LOCK:
            log = list(_LOG)
        return {"provider": self.provider(), "world": self.our_world(),
                "federation": "on" if fed else "off",
                "members": sorted(fed.members) if fed and fed.members else None,
                "local_world": getattr(self.bridge, "_world_name", "") or "",
                "worlds": self.worlds(), "providers": self.providers(),
                "paths": {"worlds": self.worlds_path, "trust": self.trust_dir},
                "log": log}

    # -- actions -------------------------------------------------------------------------------

    def _note(self, actor, line, log=_LOG, tag="fedtool"):
        text = time.strftime("%Y-%m-%d %H:%M:%SZ ", time.gmtime()) + (actor or "someone") + ": " + line
        with _LOCK:
            log.append(text)
        self.bridge.log(tag, text)

    def _fetch_keyset(self, source, pin=None):
        source = (source or "").strip()
        if source.startswith("v4.public."):
            return source
        if not source.startswith(("https://", "http://")):
            raise ToolError("Give a key set URL (https://.../xi/v1/keyset) or paste the key set itself.")
        base = source[:-len("/xi/v1/keyset")] if source.endswith("/xi/v1/keyset") else source
        try:
            return X.WorldGateway(base, pin=pin or None).keyset()
        except (OSError, ValueError, X.GatewayError) as exc:
            raise ToolError("Could not fetch the key set: %s" % exc)

    def _check_id(self, sid):
        sid = (sid or "").strip()
        if not _ID.match(sid):
            raise ToolError("A server id looks like xi1. followed by 22 characters.")
        return sid

    def add_world(self, body, actor):
        if not self.worlds_path:
            raise ToolError("FFXI_FED_WORLDS is not set on the bridge, so there is no world list to edit.")
        sid = self._check_id(body.get("id"))
        source = (body.get("keyset") or "").strip()
        pin = (body.get("pin") or "").strip() or None
        name = (body.get("name") or "").strip()
        if len(name) > F.WORLD_NAME_MAX:
            raise ToolError("Names are at most %d characters (the lobby's limit)." % F.WORLD_NAME_MAX)
        if not source.startswith(("https://", "http://")):
            raise ToolError("Worlds are added by their key set URL, so the bridge can refresh it.")
        try:
            ks = X.load_keyset(self._fetch_keyset(source, pin), sid)
        except X.XiTokenError as exc:
            raise ToolError("That key set does not check out for %s: %s" % (sid, exc))
        if not ks.world or not ks.world.get("gateway"):
            raise ToolError("That key set belongs to %s, but it does not describe a world (no gateway)." % sid)
        shown = (name or ks.name or sid)[:F.WORLD_NAME_MAX]
        local = getattr(self.bridge, "_world_name", "") or ""
        if local and shown.lower() == local.lower():
            raise ToolError("%r is our own world's name; the lobby routes a new character by world "
                            "name, so give this one a different display name." % shown)
        entries = F.read_worlds(self.worlds_path)
        for e in entries:
            if e.get("id") == sid:
                raise ToolError("%s is already listed." % sid)
            if (e.get("name") or "").lower() == shown.lower():
                raise ToolError("Another world is already shown as %r." % shown)
        entry = {"id": sid, "keyset": source}
        if name:
            entry["name"] = name
        if pin:
            entry["pin"] = pin
        entries.append(entry)
        F.write_worlds(self.worlds_path, entries)
        self.bridge.reload_federation()
        self._note(actor, "added world %r (%s, gateway %s)" % (shown, sid, ks.world.get("gateway")))
        return {"ok": True, "msg": "Added %s. It is in the lobby now." % shown}

    def remove_world(self, body, actor):
        sid = self._check_id(body.get("id"))
        entries = F.read_worlds(self.worlds_path)
        keep = [e for e in entries if e.get("id") != sid]
        if len(keep) == len(entries):
            raise ToolError("%s is not listed." % sid)
        F.write_worlds(self.worlds_path, keep)
        self.bridge.reload_federation()
        self._note(actor, "removed world %s" % sid)
        return {"ok": True, "msg": "Removed. Characters there are no longer listed in the lobby "
                                   "(they still exist on that world)."}

    def check_world(self, body, actor):
        sid = self._check_id(body.get("id"))
        fed = self.bridge.FED
        w = next((w for w in (fed.worlds if fed else []) if w.server_id == sid), None)
        if w is None:
            raise ToolError("%s is not loaded." % sid)
        w.refresh(force=True)
        return {"ok": not w.error, "msg": w.error or "The key set checks out."}

    def add_provider(self, body, actor):
        if not self.trust_dir or not os.path.isdir(self.trust_dir):
            raise ToolError("FFXI_FED_TRUST_DIR is not set or not mounted on the bridge.")
        sid = self._check_id(body.get("id"))
        text = self._fetch_keyset(body.get("keyset"))
        try:
            ks = X.load_keyset(text, sid)
        except X.XiTokenError as exc:
            raise ToolError("That key set does not check out for %s: %s" % (sid, exc))
        if not ks.keys:
            raise ToolError("That key set lists no signing keys, so nothing it signs could be accepted.")
        path = os.path.join(self.trust_dir, sid + ".keyset")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text.strip() + "\n")
        os.replace(tmp, path)
        self._note(actor, "now trusting provider %r (%s, keys %s)" % (ks.name, sid, ", ".join(sorted(ks.keys))))
        return {"ok": True, "msg": "Trusted. Our world picks it up within a minute."}

    def remove_provider(self, body, actor):
        sid = self._check_id(body.get("id"))
        path = os.path.join(self.trust_dir, sid + ".keyset")
        if not os.path.isfile(path):
            raise ToolError("%s is not trusted." % sid)
        os.remove(path)
        self._note(actor, "stopped trusting provider %s" % sid)
        return {"ok": True, "msg": "Removed. Our world stops accepting its players within a minute."}

    # -- in-game notices (the /notices page) --------------------------------------------------

    def _store(self):
        store = getattr(self.bridge, "NOTICES", None)
        if store is None:
            why = getattr(self.bridge, "NOTICES_OFF", None) or "not started"
            raise ToolError("In-game notices are off on the bridge (%s)." % why)
        return store

    def notices_state(self, now=None):
        now = int(time.time() if now is None else now)
        sync = getattr(self.bridge, "NOTICES_SYNC", None)
        with _LOCK:
            log = list(_NLOG)
        out = {"now": now, "countdown": list(N.COUNTDOWN_MIN), "motd_max": N.MOTD_MAX,
               "announce_max": N.ANNOUNCE_MAX, "log": log,
               "calendar": {"on": bool(sync), "games": [N.GAME_NAMES.get(g, g) for g in sync.games] if sync else [],
                            "last": sync.last if sync else None, "error": sync.error if sync else None}}
        store = getattr(self.bridge, "NOTICES", None)
        if store is None:
            out["off"] = getattr(self.bridge, "NOTICES_OFF", None) or "not started"
            out["motd"], out["announce"] = [], []
            return out
        try:
            rows = store.rows(now)
        except Exception as exc:
            out["error"] = "Could not read the notices table: %s" % exc
            rows = []
        for r in rows:
            r["state"] = N.describe(r, now)
            r["calendar"] = (r.get("tag") or "").startswith(N.CALENDAR_TAG)
        # Login lines in the order the module puts them in the message; announcements by time.
        out["motd"] = sorted((r for r in rows if r["kind"] == "motd"), key=lambda r: (r.get("sort") or 0, r["id"]))
        out["announce"] = sorted((r for r in rows if r["kind"] == "announce"), key=lambda r: (r["starts_at"], r["id"]))
        return out

    def notices_announce(self, body, actor):
        store, now = self._store(), int(time.time())
        try:
            starts = N.add_announce(store, body.get("text"), body.get("at"), actor, now,
                                    body.get("every") or 0, body.get("times") or 1, bool(body.get("urgent")))
        except N.NoticeError as exc:
            raise ToolError(str(exc))
        later = starts[0] > now + 15
        when = N.utc(starts[0], "%Y-%m-%d %H:%M")
        self._note(actor, "announcement %s%s: %s" % (
            "at " + when if later else "now", " (%d times)" % len(starts) if len(starts) > 1 else "",
            N.clean(body.get("text"), N.ANNOUNCE_MAX)), _NLOG, "notices")
        return {"ok": True, "starts": starts,
                "msg": "Scheduled for %s." % when if later else "Queued. It goes out within 15 seconds."}

    def notices_countdown(self, body, actor):
        store, now = self._store(), int(time.time())
        leads = body.get("leads") or N.COUNTDOWN_MIN
        try:
            leads = [int(x) for x in (leads.replace(",", " ").split() if isinstance(leads, str) else leads)]
            if not leads or any(m < 1 or m > 24 * 60 for m in leads):
                raise ValueError
        except (TypeError, ValueError):
            raise ToolError("Warning times are minutes before the shutdown, like 30, 10, 5, 1.")
        try:
            down, rows, added = N.add_countdown(store, body.get("down_at"), actor, now, leads)
        except N.NoticeError as exc:
            raise ToolError(str(exc))
        when = N.utc(down, "%Y-%m-%d %H:%M")
        self._note(actor, "countdown to %s (%s)" % (when, ", ".join(r["tag"].rsplit(":", 1)[1] for r in rows)
                                                    + " min"), _NLOG, "notices")
        return {"ok": True, "msg": "%d warning%s before the shutdown at %s.%s" % (
            len(rows), "" if len(rows) == 1 else "s", when,
            "" if added == len(rows) else " Some of them were already scheduled.")}

    def notices_motd(self, body, actor):
        store, now = self._store(), int(time.time())
        try:
            end = N.add_motd(store, body.get("text"), body.get("until"), actor, now)
        except N.NoticeError as exc:
            raise ToolError(str(exc))
        self._note(actor, "login line added%s: %s" % (
            " until " + N.utc(end, "%Y-%m-%d %H:%M") if end else "", N.clean(body.get("text"), N.MOTD_MAX)),
            _NLOG, "notices")
        return {"ok": True, "msg": "Added. Players see it at their next login, or with /servmes."}

    def _id(self, body):
        try:
            return int(body.get("id"))
        except (TypeError, ValueError):
            raise ToolError("Which one? The request named no row.")

    def notices_end(self, body, actor):
        rid = self._id(body)
        if not self._store().end_motd(rid, int(time.time())):
            raise ToolError("That line is not showing.")
        self._note(actor, "ended login line %d" % rid, _NLOG, "notices")
        return {"ok": True, "msg": "Ended. New logins no longer see it."}

    def notices_cancel(self, body, actor):
        rid = self._id(body)
        if not self._store().cancel_id(rid):
            raise ToolError("That announcement has already gone out, or is not there.")
        self._note(actor, "cancelled announcement %d" % rid, _NLOG, "notices")
        return {"ok": True, "msg": "Cancelled. It will not be sent."}

    def notices_delete(self, body, actor):
        rid = self._id(body)
        if not self._store().delete_id(rid):
            raise ToolError("Sent announcements stay in the list; only login lines and unsent "
                            "announcements can be removed.")
        self._note(actor, "removed notice %d" % rid, _NLOG, "notices")
        return {"ok": True, "msg": "Removed."}

    ACTIONS = {"/worlds/add": "add_world", "/worlds/remove": "remove_world", "/worlds/check": "check_world",
               "/providers/add": "add_provider", "/providers/remove": "remove_provider",
               "/notices/announce": "notices_announce", "/notices/countdown": "notices_countdown",
               "/notices/motd": "notices_motd", "/notices/end": "notices_end",
               "/notices/cancel": "notices_cancel", "/notices/delete": "notices_delete"}


# -- Mog Bonanza (the /bonanza page, ffxi_bonanza.py) ----------------------------------------------

_BONANZA = []


def _bonanza():
    """The Bonanza page's actions, made on first use; ToolError without the LSB database."""
    if not _BONANZA:
        got, why = B.open_store()
        if got is None:
            raise ToolError("The Bonanza page needs the LSB database (%s)." % why)
        _BONANZA.append(got)
    return _BONANZA[0]


def _bonanza_action(name):
    def action(self, body, actor):
        try:
            return getattr(_bonanza(), name)(body, actor)
        except (ToolError, B.BonanzaError) as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            # pymysql's errors: the LSB database refused or is down.
            raise ToolError("The LSB database said: %s" % exc)
    return action


for _path, _name in B.ACTIONS.items():
    setattr(Tool, "bonanza_" + _name, _bonanza_action(_name))
    Tool.ACTIONS[_path] = "bonanza_" + _name


def bonanza_get(path, query):
    """GET /bonanza.json (the page's state) and /bonanza/winners.json?id=&rank= -> (status, obj)."""
    try:
        if path == "/bonanza.json":
            return 200, _bonanza().state()
        return 200, _bonanza().winners(urllib.parse.parse_qs(query))
    except (ToolError, B.BonanzaError) as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return 503, {"error": "The LSB database said: %s" % exc}


# -- Characters (the /chars page, ffxi_chars.py) ---------------------------------------------------

_CHARS = []


def _chars():
    """The Characters page's actions, made on first use; ToolError without the LSB database."""
    if not _CHARS:
        got, why = C.open_store()
        if got is None:
            raise ToolError("The Characters page needs the LSB database (%s)." % why)
        _CHARS.append(got)
    return _CHARS[0]


def _chars_action(name):
    def action(self, body, actor):
        try:
            return getattr(_chars(), name)(body, actor)
        except (ToolError, C.CharError) as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            # pymysql's errors: the LSB database refused or is down.
            raise ToolError("The LSB database said: %s" % exc)
    return action


for _path, _name in C.ACTIONS.items():
    setattr(Tool, "chars_" + _name, _chars_action(_name))
    Tool.ACTIONS[_path] = "chars_" + _name


def chars_get(path, query):
    """GET /chars.json (the page's state), /chars/search.json?q=, /chars/char.json?id= and
    /chars/arrivals.json?to= (the zones with a zone line into a zone, and its heavy cutscenes),
    /chars/data.json?id= (the character's data summary beside the test character's) and
    /chars/items.json?q= (items to give, by name or id) -> (status, obj)."""
    try:
        if path == "/chars.json":
            return 200, _chars().state()
        args = urllib.parse.parse_qs(query)
        if path == "/chars/search.json":
            return 200, _chars().search(args)
        if path == "/chars/arrivals.json":
            return 200, _chars().arrivals(args)
        if path == "/chars/data.json":
            return 200, _chars().data(args)
        if path == "/chars/items.json":
            return 200, _chars().item_search(args)
        return 200, _chars().detail(args)
    except C.CharError as exc:
        return 400, {"error": str(exc)}
    except ToolError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return 503, {"error": "The LSB database said: %s" % exc}


def player_logins(bridge, members):
    """The LSB account names of these POL members on the primary world: the one the bridge's
    account map (ffxi_lsb_account) records, and pol<member id>, the name the bridge gives every
    account it creates (ffxi_bridge.lsb_account_for). Another world's accounts are not here."""
    acct = getattr(bridge, "_acctmap", None) or {}
    lock = getattr(bridge, "_acctmap_lock", None)
    if lock is not None:
        with lock:
            acct = dict(acct)
    out = []
    for m in members:
        for login in ((acct.get(str(m)) or {}).get("login"), ("pol%d" % m)[:16]):
            if login and login not in out:
                out.append(login)
    return out


def chars_player(tool, query):
    """GET /chars/player.json?members=1,2 (POL member ids): the characters those members have,
    for the panel's account window -> (status, obj); {} when they have none."""
    raw = ",".join(urllib.parse.parse_qs(query).get("members") or [])
    members = sorted({int(x) for x in raw.split(",") if x.strip().isdigit()})[:32]
    if not members:
        return 200, {}
    try:
        return 200, _chars().player(player_logins(tool.bridge, members))
    except ToolError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return 503, {"error": "The LSB database said: %s" % exc.__class__.__name__}


# -- Auction house (the /ah page, ffxi_auction.py) ------------------------------------------------

_AH = []


def _ah():
    """The Auction house page's actions, made on first use; ToolError without the LSB database."""
    if not _AH:
        got, why = A.open_store()
        if got is None:
            raise ToolError("The Auction house page needs the LSB database (%s)." % why)
        _AH.append(got)
    return _AH[0]


def _ah_action(name):
    def action(self, body, actor):
        try:
            return getattr(_ah(), name)(body, actor)
        except (ToolError, A.AuctionError) as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            # pymysql's errors: the LSB database refused or is down.
            raise ToolError("The LSB database said: %s" % exc)
    return action


for _path, _name in A.ACTIONS.items():
    setattr(Tool, "ah_" + _name, _ah_action(_name))
    Tool.ACTIONS[_path] = "ah_" + _name


def ah_get(path, query):
    """GET /ah.json?q= (listings for sale and sold, counts and the log) and /ah/items.json?q=
    (items to post, by name or id) -> (status, obj)."""
    try:
        args = urllib.parse.parse_qs(query)
        if path == "/ah/items.json":
            return 200, _ah().item_search(args)
        return 200, _ah().state(args)
    except A.AuctionError as exc:
        return 400, {"error": str(exc)}
    except ToolError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return 503, {"error": "The LSB database said: %s" % exc}


# -- Seasonal events (the /events page, ffxi_events.py) --------------------------------------------

_EV = []


def _events():
    """The Seasonal events page's actions, made on first use; ToolError without the LSB database."""
    if not _EV:
        got, why = E.open_store()
        if got is None:
            raise ToolError("The Seasonal events page needs the LSB database (%s)." % why)
        _EV.append(got)
    return _EV[0]


def _events_action(name):
    def action(self, body, actor):
        try:
            return getattr(_events(), name)(body, actor)
        except (ToolError, E.EventsError) as exc:
            raise ToolError(str(exc))
        except Exception as exc:
            # pymysql's errors: the LSB database refused or is down.
            raise ToolError("The LSB database said: %s" % exc)
    return action


for _path, _name in E.ACTIONS.items():
    setattr(Tool, "events_" + _name, _events_action(_name))
    Tool.ACTIONS[_path] = "events_" + _name


def events_get():
    """GET /events.json (every festival's switch, season and the log) -> (status, obj)."""
    try:
        return 200, _events().state()
    except ToolError as exc:
        return 503, {"error": str(exc)}
    except Exception as exc:
        return 503, {"error": "The LSB database said: %s" % exc}


def make_handler(tool, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ffxi-fedtool"

        def log_message(self, *a):
            pass

        def _ok_token(self):
            if not token:
                return True
            q = urllib.parse.urlsplit(self.path).query
            got = urllib.parse.parse_qs(q).get("t", [""])[0] or self.headers.get("X-Devtool-Token") or ""
            return hmac.compare_digest(got.encode(), token.encode())

        def _send(self, status, body, ctype):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj), "application/json")

        def do_GET(self):
            if not self._ok_token():
                return self._json(403, {"error": "forbidden"})
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", ""):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/fed.json":
                return self._json(200, tool.state())
            if path == "/notices":
                return self._send(200, N.PAGE, "text/html; charset=utf-8")
            if path == "/notices.json":
                return self._json(200, tool.notices_state())
            if path == "/bonanza":
                return self._send(200, B.PAGE, "text/html; charset=utf-8")
            if path in ("/bonanza.json", "/bonanza/winners.json"):
                return self._json(*bonanza_get(path, urllib.parse.urlsplit(self.path).query))
            if path == "/chars":
                return self._send(200, C.PAGE, "text/html; charset=utf-8")
            if path == "/chars/player.json":
                return self._json(*chars_player(tool, urllib.parse.urlsplit(self.path).query))
            if path in ("/chars.json", "/chars/search.json", "/chars/char.json", "/chars/arrivals.json",
                        "/chars/data.json", "/chars/items.json"):
                return self._json(*chars_get(path, urllib.parse.urlsplit(self.path).query))
            if path == "/ah":
                return self._send(200, A.PAGE, "text/html; charset=utf-8")
            if path in ("/ah.json", "/ah/items.json"):
                return self._json(*ah_get(path, urllib.parse.urlsplit(self.path).query))
            if path == "/events":
                return self._send(200, E.PAGE, "text/html; charset=utf-8")
            if path == "/events.json":
                return self._json(*events_get())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._ok_token():
                return self._json(403, {"error": "forbidden"})
            path = urllib.parse.urlsplit(self.path).path
            action = Tool.ACTIONS.get(path)
            if action is None:
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) if 0 < n <= 65536 else b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "msg": "Bad request."})
            actor = (self.headers.get("X-Devtool-Actor") or "").strip()[:64]
            try:
                return self._json(200, getattr(tool, action)(body, actor))
            except ToolError as exc:
                return self._json(200, {"ok": False, "msg": str(exc)})
            except OSError as exc:
                return self._json(500, {"ok": False, "msg": "Could not write: %s" % exc})
            except Exception as exc:
                # pymysql's errors are not OSErrors: the LSB database refused or is down.
                if not path.startswith("/notices/"):
                    raise
                return self._json(500, {"ok": False, "msg": "The LSB database said: %s" % exc})

    return Handler


def start(bridge, environ=os.environ):
    """Start the tool on its own thread; None when FFXI_FEDTOOL_PORT is unset. A bind wider
    than loopback without a token is refused (logged), never served open."""
    try:
        port = int((environ.get("FFXI_FEDTOOL_PORT") or "0").strip() or 0)
    except ValueError:
        port = 0
    if not port:
        return None
    bind = (environ.get("FFXI_FEDTOOL_BIND") or "").strip() or "127.0.0.1"
    token = (environ.get("FFXI_FEDTOOL_TOKEN") or "").strip()
    if bind not in ("127.0.0.1", "localhost", "::1") and not token:
        bridge.log("fedtool", "REFUSED: FFXI_FEDTOOL_BIND=%s without FFXI_FEDTOOL_TOKEN; the tool is off" % bind)
        return None
    srv = ThreadingHTTPServer((bind, port), make_handler(Tool(bridge, environ), token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    bridge.log("fedtool", "federation tool on http://%s:%d/%s" % (bind, port, "?t=<token>" if token else ""))
    return srv


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFXI federation</title>
<style>
:root{
  --bg:#0f1317;--panel:#161c23;--sunk:#0b0f13;--ink:#e5eaf1;--ink2:#98a4b4;
  --ink3:#66727f;--rule:#232d38;--accent:#74a8ea;--warn:#d59450;--good:#59ab80;
  --m:ui-monospace,"IBM Plex Mono",Consolas,monospace;
}
@media (prefers-color-scheme:light){
  :root{--bg:#edeff3;--panel:#fafbfd;--sunk:#e3e7ee;--ink:#171b21;--ink2:#4b5563;
        --ink3:#7b8798;--rule:#d5dae3;--accent:#2b5ea6;--warn:#96551a;--good:#256b4c;}
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:16px 14px 60px;display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}
h1{font-size:1.1rem;margin:0;font-weight:600}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:5px;padding:13px 15px;min-width:0}
.hd{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:9px;flex-wrap:wrap}
.hd h2{font-size:.95rem;margin:0;font-weight:600}
.sub{font-size:12px;color:var(--ink3)}
.mono{font-family:var(--m);font-size:12.5px;word-break:break-all}
button{font:inherit;font-size:13px;cursor:pointer;color:var(--ink);background:var(--sunk);
       border:1px solid var(--rule);border-radius:4px;padding:4px 10px}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.go{border-color:var(--good);color:var(--good)}
button.rm:hover:not(:disabled){border-color:var(--warn);color:var(--warn)}
input,textarea{font:inherit;font-size:13px;background:var(--sunk);color:var(--ink);
  border:1px solid var(--rule);border-radius:4px;padding:5px 8px;width:100%;min-width:0}
textarea{font-family:var(--m);font-size:12px;min-height:58px;resize:vertical}
label{font-size:12px;color:var(--ink2);display:grid;gap:3px}
.pill{display:inline-block;border:1px solid var(--rule);border-radius:10px;padding:0 8px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill.on{border-color:var(--good);color:var(--good)}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.msg{font-size:13px;border-radius:4px;padding:6px 9px;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good);color:var(--good)}
.msg.bad{border-color:var(--warn);color:var(--warn)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11.5px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
td.a{width:1%;white-space:nowrap}
.acts{display:flex;gap:4px}
.form{display:grid;gap:8px;grid-template-columns:minmax(0,1fr);margin-top:10px;padding-top:10px;border-top:1px solid var(--rule)}
@media(min-width:760px){.form.w{grid-template-columns:minmax(0,2fr) minmax(0,3fr) minmax(0,1.3fr) auto;align-items:end}
                        .form.p{grid-template-columns:minmax(0,2fr) minmax(0,4fr) auto;align-items:end}}
.ids{display:grid;gap:10px;grid-template-columns:minmax(0,1fr)}
@media(min-width:900px){.ids{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.idrow{display:flex;gap:8px;align-items:flex-start;justify-content:space-between}
.why{font-size:12px;color:var(--warn)}
.log{font-family:var(--m);font-size:12px;color:var(--ink2);max-height:200px;overflow:auto;white-space:pre-wrap}
.empty{color:var(--ink3);font-size:13px}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
  td.a{width:auto;padding-top:6px}
}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>FFXI federation</h1><span id="state" class="pill"></span></div>
    <div class="sub" id="facts"></div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Our ids</h2><span class="sub">What other operators need from us</span></div>
    <div class="ids">
      <div>
        <div class="sub">Provider: give this to a world that should let our players in</div>
        <div class="idrow"><span class="mono" id="pid"></span><button data-copy="pid">Copy id</button></div>
        <div class="idrow" style="margin-top:4px"><span class="sub">Key set</span><button data-copy="pks">Copy key set</button></div>
        <div class="mono sub" id="pks" style="max-height:3.2em;overflow:hidden"></div>
      </div>
      <div>
        <div class="sub">World: give this to a provider whose players should come here</div>
        <div class="idrow"><span class="mono" id="wid"></span><button data-copy="wid">Copy id</button></div>
        <div class="idrow" style="margin-top:4px"><span class="mono sub" id="wurl"></span><button data-copy="wurl">Copy URL</button></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Worlds our players can visit</h2><span class="sub" id="wsub"></span></div>
    <div class="tw"><table><thead><tr><th>World</th><th>Server id</th><th>Gateway</th><th>Status</th><th></th></tr></thead>
    <tbody id="worlds"></tbody></table></div>
    <div class="form w">
      <label>Server id<input id="w_id" placeholder="xi1.…" autocomplete="off" spellcheck="false"></label>
      <label>Key set URL<input id="w_ks" placeholder="https://…/xi/v1/keyset" autocomplete="off" spellcheck="false"></label>
      <label>Shown as (optional)<input id="w_name" maxlength="15" autocomplete="off"></label>
      <button class="go" id="w_add">Add world</button>
    </div>
    <p class="sub" style="margin:8px 0 0">The world must trust our provider id for its players to get in. The key set is checked against the server id before it is saved; the lobby lists the world straight away.</p>
  </div>

  <div class="card">
    <div class="hd"><h2>Providers whose players can visit us</h2><span class="sub">Our world's gateway re-reads these every minute</span></div>
    <div class="tw"><table><thead><tr><th>Provider</th><th>Server id</th><th>Keys</th><th>Status</th><th></th></tr></thead>
    <tbody id="providers"></tbody></table></div>
    <div class="form p">
      <label>Server id<input id="p_id" placeholder="xi1.…" autocomplete="off" spellcheck="false"></label>
      <label>Key set URL, or the key set itself<textarea id="p_ks" placeholder="https://…/xi/v1/keyset  or  v4.public.…" spellcheck="false"></textarea></label>
      <button class="go" id="p_add">Trust provider</button>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Changes</h2></div>
    <div class="log" id="log"></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let S = null, BUSY = false;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }

async function load(){
  try{
    const r = await fetch(qs('fed.json'));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || r.statusText);
    S = j; draw();
  }catch(e){ say('Could not load: ' + e.message, false); }
}

function draw(){
  const on = S.federation === 'on';
  const st = q('#state'); st.textContent = on ? 'federation on' : 'federation off'; st.className = 'pill ' + (on ? 'on' : 'warn');
  const bits = [];
  if(S.local_world) bits.push('Our world: ' + S.local_world);
  bits.push(S.members ? 'Remote worlds shown to POL member' + (S.members.length > 1 ? 's ' : ' ') + S.members.join(', ') + ' only'
                      : 'Remote worlds shown to every member');
  q('#facts').textContent = bits.join(' · ');
  const P = S.provider || {};
  q('#pid').textContent = P.id || P.error || '';
  q('#pks').textContent = P.keyset || '';
  const W = S.world || {};
  q('#wid').textContent = W.id || W.error || '(FFXI_FED_WORLD_KEYSET_URL not set)';
  q('#wurl').textContent = W.url || '';
  q('#wsub').textContent = S.worlds.length + ' listed';
  q('#worlds').innerHTML = S.worlds.length ? S.worlds.map(w => `<tr>
      <td>${esc(w.shown || w.name || '?')}${w.no ? ` <span class="sub">0x${w.no.toString(16).toUpperCase()}</span>` : ''}</td>
      <td class="mono">${esc(w.id)}</td>
      <td class="mono">${esc(w.gateway || w.keyset)}${w.search ? `<div class="sub">search ${esc(w.search)}</div>` : ''}</td>
      <td>${w.ok ? '<span class="pill on">checked</span>' : '<span class="pill warn">not usable</span>'}${w.ok ? '' : `<div class="why">${esc(w.error)}</div>`}</td>
      <td class="a"><div class="acts"><button data-wcheck="${esc(w.id)}">Check</button><button class="rm" data-wrm="${esc(w.id)}">Remove</button></div></td>
    </tr>`).join('') : '<tr><td colspan="5" class="empty">No remote worlds yet.</td></tr>';
  q('#providers').innerHTML = S.providers.length ? S.providers.map(p => `<tr>
      <td>${esc(p.name || '?')}</td><td class="mono">${esc(p.id)}</td>
      <td class="mono">${esc((p.keys || []).join(', '))}</td>
      <td>${p.ok ? '<span class="pill on">trusted</span>' : `<span class="pill warn">unreadable</span><div class="why">${esc(p.error)}</div>`}</td>
      <td class="a"><button class="rm" data-prm="${esc(p.id)}">Remove</button></td>
    </tr>`).join('') : '<tr><td colspan="5" class="empty">No providers trusted yet.</td></tr>';
  q('#log').textContent = (S.log || []).slice().reverse().join('\n') || 'Nothing changed since the bridge started.';
}

async function act(path, body, confirmText){
  if(BUSY) return;
  if(confirmText && !confirm(confirmText)) return;
  BUSY = true; document.querySelectorAll('button').forEach(b => b.disabled = true);
  try{
    const r = await fetch(qs(path), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    say(j.msg || (j.ok ? 'Done.' : 'Failed.'), j.ok);
    if(j.ok) return true;
  }catch(e){ say('Failed: ' + e.message, false); }
  finally{ BUSY = false; document.querySelectorAll('button').forEach(b => b.disabled = false); await load(); }
}

q('#w_add').onclick = async () => {
  if(await act('worlds/add', {id: q('#w_id').value, keyset: q('#w_ks').value, name: q('#w_name').value}))
    ['#w_id','#w_ks','#w_name'].forEach(s => q(s).value = '');
};
q('#p_add').onclick = async () => {
  if(await act('providers/add', {id: q('#p_id').value, keyset: q('#p_ks').value}))
    ['#p_id','#p_ks'].forEach(s => q(s).value = '');
};
document.addEventListener('click', e => {
  const b = e.target.closest('button'); if(!b) return;
  if(b.dataset.wrm) act('worlds/remove', {id: b.dataset.wrm}, 'Stop listing this world in the lobby?');
  else if(b.dataset.wcheck) act('worlds/check', {id: b.dataset.wcheck});
  else if(b.dataset.prm) act('providers/remove', {id: b.dataset.prm}, "Stop accepting this provider's players on our world?");
  else if(b.dataset.copy){
    const t = q('#' + b.dataset.copy).textContent;
    navigator.clipboard.writeText(t).then(() => say('Copied.', true), () => say('Copy failed; select the text instead.', false));
  }
});
load();
setInterval(() => { if(!BUSY && document.visibilityState === 'visible') load(); }, 15000);
</script>
</body></html>
"""
