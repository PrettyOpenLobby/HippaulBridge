"""FFXI's own calendar, handed to the PlayOnline side for the news posts.

The map server knows what FFXI has scheduled. A map-server module (not part of stock LandSandBoat)
publishes it to openlobby_game_calendar (Adventurer Campaign windows, the Repeat Login Campaign,
the seasonal festivals, each worked out by the game's own code), and the Mog Bonanza keeps its
runs in openlobby_bonanza_runs; only a run with announce = 1 (the Bonanza page's toggle, on for a
new run) is handed over. This module reads both and writes one list of events to the core's
live-state store (polcore.kv, key FFXI_CALENDAR_KEY). OpenLobby's services/ffxinews.py turns the
list into the Viewer's news items and the posts in the Discord news channel, so nothing here
decides what is posted or when.

Each event is {"source", "key", "name", "start", "end", "detail"}: source is the calendar it came
from (campaigns, login, seasonal, bonanza), key is stable for the life of the event, start and end
are unix seconds and detail is that source's extra fields. The value written is
{"at": <when>, "events": [...]}, kept for FFXI_CALENDAR_TTL seconds so a bridge that stops leaves
no stale calendar behind for long.

  FFXI_CALENDAR=0            off
  FFXI_CALENDAR_PERIOD       seconds between passes (60)
  FFXI_CALENDAR_TTL          seconds the published list lives (21600)
  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for ffxi_notices.py
"""
import json
import os
import threading
import time

FFXI_CALENDAR_KEY = "ffxi:calendar"
DAY = 86400
#: Events whose end is this far back are still handed over (a post may stay up for a while).
BEHIND = 2 * DAY
#: And events starting this far ahead.
AHEAD = 45 * DAY
SOURCES = ("campaigns", "login", "seasonal")

CALENDAR_SQL = ("SELECT source, event_key, name, starts_at, ends_at, detail FROM openlobby_game_calendar "
                "WHERE ends_at > %s AND starts_at < %s ORDER BY starts_at, source, event_key")
BONANZA_SQL = ("SELECT run_key, name, sales_start, sales_end, collect_start, collect_end, price, "
               "w1, w2, w3, w4, w5, drawn_at FROM openlobby_bonanza_runs "
               "WHERE cancelled_at IS NULL AND announce = 1 AND collect_end > %s AND sales_start < %s "
               "ORDER BY sales_start")
#: The Bonanza page's announce column (ffxi_bonanza.ANNOUNCE_DDL), added here too when the page
#: has not been opened since it came in. Idempotent.
try:
    from ffxi_bonanza import ANNOUNCE_DDL
except ImportError:                                  # pragma: no cover
    ANNOUNCE_DDL = ()


def _num(environ, name, default):
    try:
        return float((environ.get(name) or "").strip() or default)
    except ValueError:
        return float(default)


def _code(exc):
    args = getattr(exc, "args", ())
    return args[0] if args else None


def missing_table(exc):
    """True for MariaDB's 'table does not exist' (1146): the map has not made it yet."""
    return _code(exc) == 1146


class Store:
    """The two tables over pymysql, a connection per pass."""

    def __init__(self, host, port, user, password, database):
        self._args = dict(host=host, port=int(port), user=user, password=password,
                          database=database, autocommit=True, connect_timeout=5,
                          charset="utf8mb4")

    @classmethod
    def from_env(cls, environ=os.environ):
        """A Store, or (None, why) when there is nothing to connect with."""
        if not (environ.get("LSB_DB_PASSWORD") or "").strip():
            return None, "LSB_DB_PASSWORD is not set"
        try:
            import pymysql  # noqa: F401
        except ImportError:
            return None, "pymysql is not in this image"
        return cls(environ.get("LSB_DB_HOST") or "db", environ.get("LSB_DB_PORT") or 3306,
                   environ.get("LSB_DB_USER") or "xiadmin", environ.get("LSB_DB_PASSWORD"),
                   environ.get("LSB_DB_NAME") or "xidb"), None

    def _rows(self, cur, sql, args):
        try:
            cur.execute(sql, args)
        except Exception as exc:                 # noqa: BLE001 -- pymysql's error classes
            if missing_table(exc):
                return []
            raise
        return list(cur.fetchall())

    def read(self, now):
        """(calendar rows, bonanza rows) around `now`. A table the map has not made yet reads
        as empty."""
        import pymysql
        conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._args)
        try:
            with conn.cursor() as cur:
                return self.read_with(cur, now)
        finally:
            conn.close()

    def read_with(self, cur, now):
        lo, hi = int(now) - BEHIND, int(now) + AHEAD
        cal = self._rows(cur, CALENDAR_SQL, (lo, hi))
        try:
            bonanza = self._rows(cur, BONANZA_SQL, (lo, hi))
        except Exception as exc:                     # noqa: BLE001
            if _code(exc) != 1054 or not ANNOUNCE_DDL:   # 1054: no announce column yet
                raise
            for ddl in ANNOUNCE_DDL:
                cur.execute(ddl)
            bonanza = self._rows(cur, BONANZA_SQL, (lo, hi))
        return cal, bonanza


def events(calendar_rows, bonanza_rows):
    """The event list from the two tables' rows. Pure, for the test."""
    out = []
    for r in calendar_rows:
        if r.get("source") not in SOURCES:
            continue
        try:
            detail = json.loads(r.get("detail") or "{}")
        except ValueError:
            detail = {}
        out.append({"source": r["source"], "key": str(r["event_key"]), "name": str(r.get("name") or ""),
                    "start": int(r["starts_at"]), "end": int(r["ends_at"]),
                    "detail": detail if isinstance(detail, dict) else {}})
    for r in bonanza_rows:
        numbers = [r.get("w%d" % i) for i in range(1, 6)]
        out.append({"source": "bonanza", "key": str(r["run_key"]), "name": str(r.get("name") or "Mog Bonanza"),
                    "start": int(r["sales_start"]), "end": int(r["collect_end"]),
                    "detail": {"salesEnd": int(r["sales_end"]), "collectStart": int(r["collect_start"]),
                               "price": int(r.get("price") or 0),
                               "drawnAt": int(r["drawn_at"]) if r.get("drawn_at") is not None else None,
                               "numbers": [int(n) for n in numbers] if None not in numbers else None}})
    out.sort(key=lambda e: (e["start"], e["source"], e["key"]))
    return out


class Feed:
    def __init__(self, store, kv, environ=os.environ, log=print):
        self.store, self.kv, self.log = store, kv, log
        self.period = max(10.0, _num(environ, "FFXI_CALENDAR_PERIOD", 60))
        self.ttl = max(self.period * 3, _num(environ, "FFXI_CALENDAR_TTL", 6 * 3600))
        self.last, self.error, self._said, self._count = None, None, None, None

    def once(self, now=None):
        now = int(time.time() if now is None else now)
        cal, bonanza = self.store.read(now)
        evs = events(cal, bonanza)
        self.kv.set_json(FFXI_CALENDAR_KEY, {"at": now, "events": evs}, ttl=self.ttl)
        if len(evs) != self._count:
            self.log("game calendar: %d event(s) handed to the news posts" % len(evs))
            self._count = len(evs)
        self.last, self.error = now, None
        return evs

    def run(self, sleep=time.sleep):
        while True:
            try:
                self.once()
                self._said = None
            except Exception as exc:                 # noqa: BLE001 -- never die
                self.error = "%s: %s" % (type(exc).__name__, exc)
                if self.error != self._said:
                    self.log("game calendar pass failed (%s); retrying every %ds" % (self.error, self.period))
                    self._said = self.error
            sleep(self.period)


def start(bridge, kv=None, environ=os.environ):
    """Start the feed thread. Never raises: the news posts are an extra."""
    log = lambda m: bridge.log("calendar", m)      # noqa: E731
    if (environ.get("FFXI_CALENDAR") or "").strip() == "0":
        log("game calendar feed OFF (FFXI_CALENDAR=0)")
        return None
    store, why = Store.from_env(environ)
    if store is None:
        log("game calendar feed OFF: %s" % why)
        return None
    try:
        kv = kv or bridge.ffxidb.kv()
    except Exception as exc:                         # noqa: BLE001
        log("game calendar feed OFF: no live-state store (%s)" % exc)
        return None
    feed = Feed(store, kv, environ, log)
    threading.Thread(target=feed.run, name="ffxi-calendar", daemon=True).start()
    log("game calendar feed on: kv %s every %ds" % (FFXI_CALENDAR_KEY, feed.period))
    return feed
