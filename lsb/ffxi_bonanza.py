"""Mog Bonanza runs for the admin panel (Games > FFXI > Bonanza), served by ffxi_fedtool.py.

The Bonanza itself runs in a map-server module that stock LandSandBoat does not include and
this repository does not ship; without it this page has no tables to show. A run is one
row of openlobby_bonanza_runs: sales, the draw at the end of sales, a wait, then collection.
The map follows whichever row is current, makes the retail calendar's rows itself (New Year,
Vana'versary, and a short first run) and draws every run whose sales have ended.

This page reads those tables and writes to them:

  - make a run by hand (windows, prize set, marble title, price, caps), edit it, delete one
    that has sold nothing;
  - start a run now or stop its sales now (the map draws within a minute);
  - draw, redraw (only before any prize is claimed) and cancel (refunds every unclaimed
    marble by delivery box). These are REQUESTS: the page writes request = 'draw' etc. into
    the row and the map does the work on its next tick, so the draw and the refunds have one
    implementation. request_result says what happened;
  - the winners of a run and its sales;
  - whether a run is announced: with announce = 1 (the default for every new run, the map's
    calendar runs included) the news posts (ffxi_calendar.py, then OpenLobby's ffxinews.py) tell
    the Viewer and the Discord news channel when its marbles go on sale and what the winning
    numbers are. A test run is unticked so nobody hears of it.

The same checks as the map's: one run at a time (windows of runs that are not cancelled never
overlap; the 2016 moogle has one menu), sales start < sales end <= collection start <
collection end, title 64-76 (the Bonanza names the 2016 client has), a prize set the map knows.

Times are unix seconds, shown and entered as UTC. The calendar's boundaries are 00:00 JST,
which is 15:00 UTC the day before.

  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for the notices tool
"""
import calendar
import os
import re
import time

DAY = 86400
JST = 9 * 3600

#: The marble's exdata title -> the name the 2016 client shows for it.
TITLES = {64: "6th Anniversary", 65: "7th Anniversary", 66: "Moggy New Year", 67: "8th Vana'versary",
          68: "Moggy New Year", 69: "9th Vana'versary", 70: "Homecoming", 71: "11th Vana'versary",
          72: "I Dream of Mog Bonanza 2014", 73: "12th Vana'versary", 74: "I Dream of Mog Bonanza 2015",
          75: "13th Vana'versary", 76: "Rhapsodies"}
#: What the 2016 moogle says whatever the title is, so it is the default.
DEFAULT_TITLE = 76

#: The prize sets the map module knows.
PRIZE_SETS = {"idream2014": "I Dream of Mog Bonanza 2014", "v12": "12th Vana'versary",
              "idream2015": "I Dream of Mog Bonanza 2015", "v13": "13th Vana'versary"}

#: The 2016 event says 2,000 gil whatever we charge.
CLIENT_PRICE = 2000
DEFAULTS = {"price": CLIENT_PRICE, "max_held": 10, "max_per_account": 30, "gil_seed": 0}

_KEY = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
REQUESTS = ("draw", "redraw", "cancel")

#: The news toggle. Added here and by the map module's own schema, whichever runs
#: first, both idempotent: the column comes in at 0, so the runs made before it existed are not
#: announced after the fact, and new rows get 1.
ANNOUNCE_DDL = ("ALTER TABLE openlobby_bonanza_runs ADD COLUMN IF NOT EXISTS announce TINYINT(1) NOT NULL DEFAULT 0",
                "ALTER TABLE openlobby_bonanza_runs ALTER COLUMN announce SET DEFAULT 1")

RUN_COLUMNS = ("run_id, run_key, name, title_id, prize_set, sales_start, sales_end, collect_start, collect_end, "
               "price, max_held, max_per_account, gil_seed, w1, w2, w3, w4, w5, drawn_at, drawn_by, pool_gil, "
               "gil1, gil2, gil3, winners1, winners2, winners3, winners4, winners5, winners6, cancelled_at, "
               "cancelled_by, request, request_by, request_at, request_result, origin, created_at, created_by, note, announce")


class BonanzaError(Exception):
    """A request the panel should show back as it is."""


def phase(run, now):
    """'cancelled', 'upcoming', 'sales', 'wait', 'collect' or 'closed' (the map's phaseOf)."""
    if run.get("cancelled_at") is not None:
        return "cancelled"
    if now < run["sales_start"]:
        return "upcoming"
    if now < run["sales_end"]:
        return "sales"
    if now < run["collect_start"]:
        return "wait"
    if now < run["collect_end"]:
        return "collect"
    return "closed"


def parse_time(value, label):
    """Unix seconds from the panel: digits, or 'YYYY-MM-DDTHH:MM' in UTC (a datetime-local)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    s = str(value or "").strip()
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return calendar.timegm(time.strptime(s, fmt))
        except ValueError:
            pass
    raise BonanzaError("%s: give the time as YYYY-MM-DD HH:MM (UTC)." % label)


def _int(value, label, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BonanzaError("%s must be a whole number." % label)
    if not lo <= n <= hi:
        raise BonanzaError("%s must be %d to %d." % (label, lo, hi))
    return n


def next_jst_midnight(now):
    """The next 00:00 JST after `now`."""
    return (int(now) + JST) // DAY * DAY + DAY - JST


def suggested(now):
    """The create form's defaults: a short run from the next 00:00 JST, ten days of sales,
    one day's wait, a week of collection (the first run the map's calendar makes)."""
    start = next_jst_midnight(now)
    return {"sales_start": start, "sales_end": start + 10 * DAY, "collect_start": start + 11 * DAY,
            "collect_end": start + 18 * DAY, "title_id": DEFAULT_TITLE, "prize_set": "idream2015",
            **DEFAULTS}


def check_windows(row, others, run_id=None):
    """The window rules, and no overlap with another run that is not cancelled."""
    if not (row["sales_start"] < row["sales_end"] <= row["collect_start"] < row["collect_end"]):
        raise BonanzaError("The windows must run: sales start, sales end, collection start, collection end "
                           "(sales end may equal collection start).")
    for o in others:
        if o["run_id"] == run_id or o.get("cancelled_at") is not None:
            continue
        if o["sales_start"] < row["collect_end"] and o["collect_end"] > row["sales_start"]:
            raise BonanzaError("That overlaps run %s (%s). One run at a time: the 2016 moogle has one menu."
                               % (o["run_key"], o.get("name") or ""))


def clean_run(body, now):
    """A new run from the form, checked. -> the row to insert (without its overlap check)."""
    key = str(body.get("run_key") or "").strip()
    if not _KEY.match(key):
        raise BonanzaError("The key is 1-32 letters, digits, - or _ (for example 2027-spring).")
    title = _int(body.get("title_id", DEFAULT_TITLE), "Title", 64, 76)
    prize_set = str(body.get("prize_set") or "")
    if prize_set not in PRIZE_SETS:
        raise BonanzaError("Pick one of the prize sets.")
    row = {"run_key": key, "name": re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:64],
           "title_id": title, "prize_set": prize_set,
           "sales_start": parse_time(body.get("sales_start"), "Sales start"),
           "sales_end": parse_time(body.get("sales_end"), "Sales end"),
           "collect_start": parse_time(body.get("collect_start"), "Collection start"),
           "collect_end": parse_time(body.get("collect_end"), "Collection end"),
           "price": _int(body.get("price", CLIENT_PRICE), "Price", 1, 99999999),
           "max_held": _int(body.get("max_held", DEFAULTS["max_held"]), "Marbles held", 1, 99),
           "max_per_account": _int(body.get("max_per_account", DEFAULTS["max_per_account"]), "Marbles per account", 0, 65535),
           "gil_seed": _int(body.get("gil_seed", 0), "Gil seed", 0, 999999999),
           "note": str(body.get("note") or "").strip()[:255] or None,
           "announce": 0 if body.get("announce", True) in (False, 0, "0", "false", "off") else 1}
    if not (row["sales_start"] < row["sales_end"] <= row["collect_start"] < row["collect_end"]):
        raise BonanzaError("The windows must run: sales start, sales end, collection start, collection end "
                           "(sales end may equal collection start).")
    if row["collect_end"] <= now:
        raise BonanzaError("That run would already be over.")
    return row


# -- the tables ----------------------------------------------------------------------------------

class Store:
    """The Bonanza tables over pymysql, a connection per call (as the notices Store)."""

    def __init__(self, host, port, user, password, database):
        self._args = dict(host=host, port=int(port), user=user, password=password,
                          database=database, autocommit=True, connect_timeout=5, charset="utf8mb4")

    @classmethod
    def from_env(cls, environ=os.environ):
        """A Store, or (None, why)."""
        if not (environ.get("LSB_DB_PASSWORD") or "").strip():
            return None, "LSB_DB_PASSWORD is not set"
        try:
            import pymysql  # noqa: F401
        except ImportError:
            return None, "pymysql is not in this image"
        return cls(environ.get("LSB_DB_HOST") or "db", environ.get("LSB_DB_PORT") or 3306,
                   environ.get("LSB_DB_USER") or "xiadmin", environ.get("LSB_DB_PASSWORD"),
                   environ.get("LSB_DB_NAME") or "xidb"), None

    _announce_ready = False

    def _ensure_announce(self, cur):
        """The announce column, once per process. Before the map has made the table there is
        nothing to alter, and the next call tries again."""
        if Store._announce_ready:
            return
        try:
            for ddl in ANNOUNCE_DDL:
                cur.execute(ddl)
        except Exception as exc:                     # noqa: BLE001 -- pymysql's error classes
            if (getattr(exc, "args", None) or (None,))[0] == 1146:
                return
            raise
        Store._announce_ready = True

    def _run(self, sql, args=(), fetch=False):
        import pymysql
        conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._args)
        try:
            with conn.cursor() as cur:
                self._ensure_announce(cur)
                n = cur.execute(sql, args)
                rows = cur.fetchall() if fetch else n
        finally:
            conn.close()
        if fetch:
            # SUM() comes back as Decimal; the page wants plain numbers.
            return [{k: int(v) if v is not None and type(v).__name__ == "Decimal" else v for k, v in r.items()}
                    for r in rows]
        return rows

    def ready(self):
        return bool(self._run("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() "
                              "AND table_name IN ('openlobby_bonanza_runs', 'openlobby_bonanza_sales', "
                              "'openlobby_bonanza_log')", fetch=True)[0]["n"] == 3)

    def runs(self, limit=40):
        return self._run("SELECT %s FROM openlobby_bonanza_runs ORDER BY sales_start DESC LIMIT %%s" % RUN_COLUMNS,
                         (int(limit),), fetch=True)

    def all_windows(self):
        return self._run("SELECT run_id, run_key, name, sales_start, collect_end, cancelled_at "
                         "FROM openlobby_bonanza_runs WHERE cancelled_at IS NULL", fetch=True)

    def run(self, run_id):
        rows = self._run("SELECT %s FROM openlobby_bonanza_runs WHERE run_id = %%s" % RUN_COLUMNS,
                         (int(run_id),), fetch=True)
        return rows[0] if rows else None

    def stats(self, run_ids):
        if not run_ids:
            return {}
        marks = ", ".join(["%s"] * len(run_ids))
        rows = self._run("SELECT run_id, COUNT(*) AS sold, COUNT(DISTINCT charid) AS chars, "
                         "COUNT(DISTINCT accid) AS accounts, SUM(price) AS gross, SUM(gm) AS gm, "
                         "SUM(claimed_at IS NOT NULL) AS claimed, SUM(refunded_at IS NOT NULL) AS refunded, "
                         "SUM(COALESCE(prize_gil, 0)) AS gil_paid FROM openlobby_bonanza_sales "
                         "WHERE run_id IN (%s) GROUP BY run_id" % marks, tuple(int(i) for i in run_ids), fetch=True)
        return {r["run_id"]: r for r in rows}

    def insert_run(self, row, actor, now):
        cols = list(row) + ["origin", "created_at", "created_by"]
        vals = list(row.values()) + ["manual", int(now), actor or None]
        return self._run("INSERT IGNORE INTO openlobby_bonanza_runs (%s) VALUES (%s)"
                         % (", ".join(cols), ", ".join(["%s"] * len(cols))), tuple(vals))

    def update_run(self, run_id, fields):
        sets = ", ".join("%s = %%s" % k for k in fields)
        return self._run("UPDATE openlobby_bonanza_runs SET %s WHERE run_id = %%s AND cancelled_at IS NULL" % sets,
                         tuple(fields.values()) + (int(run_id),))

    def delete_run(self, run_id):
        return self._run("DELETE FROM openlobby_bonanza_runs WHERE run_id = %s AND NOT EXISTS "
                         "(SELECT 1 FROM openlobby_bonanza_sales WHERE run_id = %s)", (int(run_id), int(run_id)))

    def set_request(self, run_id, what, actor, now):
        return self._run("UPDATE openlobby_bonanza_runs SET request = %s, request_by = %s, request_at = %s, "
                         "request_result = NULL WHERE run_id = %s", (what, actor or "admin", int(now), int(run_id)))

    def add_log(self, run_id, who, action, detail, now):
        self._run("INSERT INTO openlobby_bonanza_log (run_id, at, who, action, detail) VALUES (%s, %s, %s, %s, %s)",
                  (int(run_id), int(now), (who or "admin")[:64], action, detail[:512]))

    def log(self, limit=40):
        return self._run("SELECT l.run_id, r.run_key, l.at, l.who, l.action, l.detail FROM openlobby_bonanza_log l "
                         "LEFT JOIN openlobby_bonanza_runs r ON r.run_id = l.run_id ORDER BY l.id DESC LIMIT %s",
                         (int(limit),), fetch=True)

    def winners(self, run_id, rank):
        return self._run("SELECT s.number, s.prize_rank, s.claimed_at, s.prize_row, s.prize_item, s.prize_gil, s.gm, "
                         "s.refunded_at, COALESCE(c.charname, '?') AS name FROM openlobby_bonanza_sales s "
                         "LEFT JOIN chars c ON c.charid = s.charid WHERE s.run_id = %s AND s.prize_rank IS NOT NULL "
                         "AND ((%s = 0 AND s.prize_rank < 6) OR s.prize_rank = %s) "
                         "ORDER BY s.prize_rank, c.charname LIMIT 500", (int(run_id), int(rank), int(rank)), fetch=True)


# -- the page's actions --------------------------------------------------------------------------

class Bonanza:
    """What the page shows and does, over a Store (or a test's stand-in with the same methods)."""

    def __init__(self, store):
        self.store = store

    def _run(self, body):
        try:
            run_id = int(body.get("id"))
        except (TypeError, ValueError):
            raise BonanzaError("Which run? The request named none.")
        run = self.store.run(run_id)
        if run is None:
            raise BonanzaError("There is no such run.")
        return run

    def state(self, now=None):
        now = int(time.time() if now is None else now)
        out = {"now": now, "titles": TITLES, "prize_sets": PRIZE_SETS, "client_price": CLIENT_PRICE,
               "suggested": suggested(now)}
        if not self.store.ready():
            out["off"] = ("The Bonanza tables are not in the LSB database yet. They come with the map-server "
                          "Bonanza module, which stock LandSandBoat does not include.")
            out["runs"], out["log"] = [], []
            return out
        runs = self.store.runs()
        stats = self.store.stats([r["run_id"] for r in runs])
        for r in runs:
            r["phase"] = phase(r, now)
            r["stats"] = stats.get(r["run_id"]) or {"sold": 0, "chars": 0, "accounts": 0, "gross": 0, "gm": 0,
                                                    "claimed": 0, "refunded": 0, "gil_paid": 0}
            r["title_name"] = TITLES.get(r["title_id"], "?")
            r["set_name"] = PRIZE_SETS.get(r["prize_set"], r["prize_set"] + " (unknown to this page)")
        out["runs"] = runs
        cur = next((r for r in runs if r["phase"] in ("sales", "wait", "collect")), None)
        out["current"] = cur["run_id"] if cur else None
        out["log"] = self.store.log()
        return out

    def create(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        row = clean_run(body, now)
        check_windows(row, self.store.all_windows())
        if not self.store.insert_run(row, actor, now):
            raise BonanzaError("A run with the key %s already exists." % row["run_key"])
        made = next((r for r in self.store.runs(200) if r["run_key"] == row["run_key"]), None)
        if made:
            day = lambda t: time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))
            self.store.add_log(made["run_id"], actor, "create", "manual %s set %s title %d sales %s to %s, collection %s to %s UTC" % (
                row["run_key"], row["prize_set"], row["title_id"], day(row["sales_start"]), day(row["sales_end"]),
                day(row["collect_start"]), day(row["collect_end"])), now)
        return {"ok": True, "msg": "Run %s made. The moogles open for it at %s." % (
            row["run_key"], time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(row["sales_start"])))}

    def update(self, body, actor, now=None):
        """Edit a run. Windows follow the same rules; once a marble is sold the title, prize set
        and price are fixed (a claim checks the marble's title), and once drawn the sales window
        is fixed too."""
        now = int(time.time() if now is None else now)
        run = self._run(body)
        if run.get("cancelled_at") is not None:
            raise BonanzaError("That run is cancelled.")
        merged = dict(run)
        merged.update({k: body[k] for k in ("name", "title_id", "prize_set", "sales_start", "sales_end",
                                             "collect_start", "collect_end", "price", "max_held",
                                             "max_per_account", "gil_seed", "note", "announce") if k in body})
        merged["run_key"] = run["run_key"]
        new = clean_run(merged, now)
        sold = (self.store.stats([run["run_id"]]).get(run["run_id"]) or {}).get("sold", 0)
        fields = {}
        for k in ("name", "max_held", "max_per_account", "gil_seed", "note", "collect_start", "collect_end",
                  "title_id", "prize_set", "price", "sales_start", "sales_end", "announce"):
            if new[k] != run.get(k):
                fields[k] = new[k]
        if sold and any(k in fields for k in ("title_id", "prize_set", "price")):
            raise BonanzaError("Marbles are sold: the title, prize set and price can no longer change.")
        if run.get("drawn_at") is not None and any(k in fields for k in ("sales_start", "sales_end")):
            raise BonanzaError("The numbers are drawn: only the collection window can change.")
        if not fields:
            return {"ok": True, "msg": "Nothing changed."}
        check_windows(new, self.store.all_windows(), run["run_id"])
        self.store.update_run(run["run_id"], fields)
        self.store.add_log(run["run_id"], actor, "edit", ", ".join("%s=%s" % kv for kv in fields.items()), now)
        return {"ok": True, "msg": "Saved."}

    def start(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        run = self._run(body)
        if phase(run, now) != "upcoming":
            raise BonanzaError("Only a run that has not started can be started now.")
        if run["sales_end"] <= now + 60:
            raise BonanzaError("Its sales end is too close; edit the windows instead.")
        start = now - now % 60
        check_windows(dict(run, sales_start=start), self.store.all_windows(), run["run_id"])
        self.store.update_run(run["run_id"], {"sales_start": start})
        self.store.add_log(run["run_id"], actor, "start", "sales opened at %d" % start, now)
        return {"ok": True, "msg": "Sales are open. The moogles appear within a minute."}

    def stop(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        run = self._run(body)
        if phase(run, now) != "sales":
            raise BonanzaError("That run is not selling.")
        fields = {"sales_end": max(now, run["sales_start"] + 1)}
        fields["collect_start"] = max(run["collect_start"], fields["sales_end"])
        if fields["collect_start"] >= run["collect_end"]:
            raise BonanzaError("Its collection would be empty; edit the windows instead.")
        self.store.update_run(run["run_id"], fields)
        self.store.add_log(run["run_id"], actor, "stop", "sales ended at %d" % fields["sales_end"], now)
        return {"ok": True, "msg": "Sales ended. The map draws the numbers within a minute."}

    def request(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        run = self._run(body)
        what = str(body.get("what") or "")
        if what not in REQUESTS:
            raise BonanzaError("Ask for draw, redraw or cancel.")
        st = self.store.stats([run["run_id"]]).get(run["run_id"]) or {}
        if run.get("cancelled_at") is not None:
            raise BonanzaError("That run is cancelled.")
        if what == "draw":
            if run.get("drawn_at") is not None:
                raise BonanzaError("Already drawn. Redraw instead (only while nothing is claimed).")
            if run["sales_end"] > now:
                raise BonanzaError("Sales are still on. Stop them first.")
        if what == "redraw":
            if run.get("drawn_at") is None:
                raise BonanzaError("Not drawn yet.")
            if st.get("claimed"):
                raise BonanzaError("A prize has been claimed: the numbers can no longer change.")
        self.store.set_request(run["run_id"], what, actor, now)
        self.store.add_log(run["run_id"], actor, "asked", what, now)
        msg = {"draw": "The map draws the numbers within a minute.",
               "redraw": "The map draws new numbers within a minute; the old ones stay in the log.",
               "cancel": "The map cancels the run within a minute and refunds every unclaimed marble "
                         "by delivery box."}[what]
        return {"ok": True, "msg": msg}

    def delete(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        run = self._run(body)
        if run.get("origin") == "calendar":
            raise BonanzaError("The calendar would make this run again. Cancel it instead.")
        if not self.store.delete_run(run["run_id"]):
            raise BonanzaError("Marbles were sold for this run; cancel it instead (that refunds them).")
        self.store.add_log(run["run_id"], actor, "delete", run["run_key"], now)
        return {"ok": True, "msg": "Deleted."}

    def winners(self, query):
        try:
            run_id = int((query.get("id") or ["0"])[0])
            rank = int((query.get("rank") or ["0"])[0])
        except ValueError:
            raise BonanzaError("Bad request.")
        return {"rows": self.store.winners(run_id, rank)}


#: POST routes -> Bonanza methods (body, actor).
ACTIONS = {"/bonanza/create": "create", "/bonanza/update": "update", "/bonanza/start": "start",
           "/bonanza/stop": "stop", "/bonanza/request": "request", "/bonanza/delete": "delete"}


def open_store(environ=os.environ):
    """(Bonanza, None) or (None, why)."""
    store, why = Store.from_env(environ)
    return (Bonanza(store), None) if store else (None, why)


#: The panel page, served by ffxi_fedtool.py at /bonanza (the panel's Games > FFXI > Bonanza).
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mog Bonanza</title>
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
button{font:inherit;font-size:13px;cursor:pointer;color:var(--ink);background:var(--sunk);
       border:1px solid var(--rule);border-radius:4px;padding:4px 10px;white-space:nowrap}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.go{border-color:var(--good);color:var(--good)}
button.rm:hover:not(:disabled){border-color:var(--warn);color:var(--warn)}
input,select,textarea{font:inherit;font-size:13px;background:var(--sunk);color:var(--ink);
  border:1px solid var(--rule);border-radius:4px;padding:5px 8px;width:100%;min-width:0}
label{font-size:12px;color:var(--ink2);display:grid;gap:3px}
label.chk{display:flex;gap:7px;align-items:center;align-self:end;min-height:31px}
label.chk input{width:auto}
.pill{display:inline-block;border:1px solid var(--rule);border-radius:10px;padding:0 8px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill.on{border-color:var(--good);color:var(--good)}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.pill.acc{border-color:var(--accent);color:var(--accent)}
.msg{font-size:13px;border-radius:4px;padding:6px 9px;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good);color:var(--good)}
.msg.bad{border-color:var(--warn);color:var(--warn)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11.5px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
td.t{white-space:nowrap;font-size:12.5px}
td.n{font-family:var(--m);font-size:12.5px;white-space:nowrap}
.acts{display:flex;gap:4px;flex-wrap:wrap}
.form{display:grid;gap:8px;grid-template-columns:minmax(0,1fr)}
@media(min-width:760px){.form{grid-template-columns:repeat(4,minmax(0,1fr))}.form .w2{grid-column:span 2}}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.log{font-family:var(--m);font-size:12px;color:var(--ink2);max-height:220px;overflow:auto;white-space:pre-wrap}
.empty{color:var(--ink3);font-size:13px}
.k{font-weight:600}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>Mog Bonanza</h1><span id="state" class="pill"></span></div>
    <div class="sub" id="facts"></div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Runs</h2><span class="sub">Newest first. The map follows the run whose sales or collection is on.</span></div>
    <div class="tw"><table><thead><tr><th>Run</th><th>Status</th><th>Sales (UTC)</th><th>Collection (UTC)</th><th>Prizes</th><th>Sold</th><th>Winning numbers</th><th></th></tr></thead>
    <tbody id="runs"></tbody></table></div>
  </div>

  <div class="card" id="wcard" hidden>
    <div class="hd"><h2 id="wtitle">Winners</h2>
      <span class="acts"><select id="wrank" style="width:auto"><option value="0">Ranks 1-5</option><option>1</option><option>2</option><option>3</option><option>4</option><option>5</option><option>6</option></select>
      <button id="wclose">Close</button></span></div>
    <div class="tw"><table><thead><tr><th>Rank</th><th>Number</th><th>Character</th><th>Prize</th></tr></thead><tbody id="wrows"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2 id="ftitle">New run</h2><span class="sub">Times in UTC. The calendar's runs change at 00:00 JST, which is 15:00 UTC the day before.</span></div>
    <div class="form">
      <label>Key<input id="f_key" maxlength="32" autocomplete="off" placeholder="2026-autumn"></label>
      <label class="w2">Name in announcements<input id="f_name" maxlength="64" autocomplete="off" placeholder="Autumn Mog Bonanza"></label>
      <label>Prize set<select id="f_set"></select></label>
      <label>Sales start<input id="f_ss" type="datetime-local"></label>
      <label>Sales end (the draw)<input id="f_se" type="datetime-local"></label>
      <label>Collection start<input id="f_cs" type="datetime-local"></label>
      <label>Collection end<input id="f_ce" type="datetime-local"></label>
      <label>Marble title<select id="f_title"></select></label>
      <label>Price (gil)<input id="f_price" type="number" min="1"></label>
      <label>Marbles held per character<input id="f_held" type="number" min="1" max="99"></label>
      <label>Marbles per account, 0 = no cap<input id="f_acct" type="number" min="0"></label>
      <label>Gil added to the pool<input id="f_seed" type="number" min="0"></label>
      <label class="w2">Note<input id="f_note" maxlength="255" autocomplete="off"></label>
      <label class="w2 chk"><input id="f_announce" type="checkbox" checked>Announce in the Viewer news and on Discord</label>
    </div>
    <div class="row">
      <button class="go" id="f_go">Make run</button>
      <button id="f_cancel" hidden>Stop editing</button>
      <span class="sub" id="f_hint"></span>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Changes</h2><span class="sub">From this page, the map and the calendar</span></div>
    <div class="log" id="log"></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = t => t == null ? '' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');
const local = t => new Date(t * 1000).toISOString().slice(0, 16);
const pad = (n, w) => String(n).padStart(w, '0');
const num = n => Number(n || 0).toLocaleString('en-US');
const TONE = {sales:'on', collect:'on', wait:'acc', upcoming:'', closed:'', cancelled:'warn'};
const WORDS = {sales:'selling', wait:'waiting for collection', collect:'collecting', upcoming:'scheduled', closed:'over', cancelled:'cancelled'};
let S = null, BUSY = false, EDIT = null, WRUN = null;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }

async function load(){
  try{
    const r = await fetch(qs('bonanza.json'));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || r.statusText);
    const first = !S; S = j; draw(first);
  }catch(e){ say('Could not load: ' + e.message, false); }
}

function numbers(r){
  if(r.w1 == null) return '<span class="sub">' + (r.phase === 'cancelled' ? '' : 'not drawn') + '</span>';
  return `<span class="n">${pad(r.w1,5)} ${pad(r.w2,4)} ${pad(r.w3,3)} ${pad(r.w4,2)} ${r.w5}</span>
    <div class="sub">winners ${[1,2,3,4,5,6].map(i => num(r['winners' + i])).join(' / ')}</div>
    <div class="sub">pool ${num(r.pool_gil)} gil; ${num(r.gil1)} / ${num(r.gil2)} / ${num(r.gil3)}</div>`;
}

function actions(r){
  const b = [], st = r.stats || {};
  if(r.phase === 'upcoming') b.push(`<button data-act="start" data-id="${r.run_id}">Start now</button>`);
  if(r.phase === 'sales') b.push(`<button data-act="stop" data-id="${r.run_id}">Stop sales</button>`);
  if(r.phase !== 'cancelled' && r.w1 == null && (r.phase === 'wait' || r.phase === 'collect' || r.phase === 'closed'))
    b.push(`<button data-req="draw" data-id="${r.run_id}">Draw</button>`);
  if(r.phase !== 'cancelled' && r.w1 != null && !st.claimed) b.push(`<button data-req="redraw" data-id="${r.run_id}">Redraw</button>`);
  if(r.w1 != null) b.push(`<button data-win="${r.run_id}">Winners</button>`);
  if(r.phase !== 'cancelled' && r.phase !== 'closed') b.push(`<button data-edit="${r.run_id}">Edit</button>`);
  if(r.phase !== 'cancelled' && r.phase !== 'closed') b.push(`<button class="rm" data-req="cancel" data-id="${r.run_id}">Cancel</button>`);
  if(!st.sold && r.origin !== 'calendar') b.push(`<button class="rm" data-act="delete" data-id="${r.run_id}">Delete</button>`);
  return b.join('');
}

function draw(first){
  const st = q('#state');
  const cur = S.runs.find(r => r.run_id === S.current);
  if(S.off){ st.textContent = 'tables missing'; st.className = 'pill warn'; }
  else if(cur){ st.textContent = cur.run_key + ': ' + WORDS[cur.phase]; st.className = 'pill on'; }
  else { st.textContent = 'no run on'; st.className = 'pill'; }
  const bits = [];
  if(S.off) bits.push(S.off);
  else bits.push('Marbles cost ' + num(S.client_price) + ' gil in the 2016 client\'s text whatever the price here. Draw, redraw and cancel are done by the map within a minute.');
  q('#facts').textContent = bits.join(' ');
  q('#runs').innerHTML = S.runs.length ? S.runs.map(r => {
    const s = r.stats || {};
    const req = r.request ? `<div class="sub">asked: ${esc(r.request)}</div>` : (r.request_result ? `<div class="sub">${esc(r.request_result)}</div>` : '');
    return `<tr>
      <td><span class="k">${esc(r.run_key)}</span><div class="sub">${esc(r.name || '')}${r.origin === 'calendar' ? ' · calendar' : ''}${r.announce ? '' : ' · not announced'}</div></td>
      <td><span class="pill ${TONE[r.phase] || ''}">${esc(WORDS[r.phase] || r.phase)}</span>${req}</td>
      <td class="t">${esc(when(r.sales_start))}<br>${esc(when(r.sales_end))}</td>
      <td class="t">${esc(when(r.collect_start))}<br>${esc(when(r.collect_end))}</td>
      <td>${esc(r.set_name)}<div class="sub">title ${r.title_id} ${esc(r.title_name)}; ${num(r.price)} gil, hold ${r.max_held}, account ${r.max_per_account || 'no cap'}</div></td>
      <td>${num(s.sold)}<div class="sub">${num(s.accounts)} accounts, ${num(s.gross)} gil${s.claimed ? ', ' + num(s.claimed) + ' claimed' : ''}${s.refunded ? ', ' + num(s.refunded) + ' refunded' : ''}</div></td>
      <td>${numbers(r)}</td>
      <td><div class="acts">${actions(r)}</div></td></tr>`;
  }).join('') : '<tr><td colspan="8" class="empty">No runs yet. The map makes the first one at its first start.</td></tr>';
  q('#log').textContent = (S.log || []).map(l => when(l.at) + ' ' + (l.run_key || l.run_id) + ' ' + (l.who || '') + ' ' + l.action + (l.detail ? ': ' + l.detail : '')).join('\n') || 'Nothing yet.';
  if(first){
    q('#f_set').innerHTML = Object.entries(S.prize_sets).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join('');
    q('#f_title').innerHTML = Object.entries(S.titles).map(([k, v]) => `<option value="${k}">${k} ${esc(v)}</option>`).join('');
    fill(S.suggested);
  }
}

function fill(r){
  q('#f_key').value = r.run_key || ''; q('#f_key').disabled = !!EDIT;
  q('#f_name').value = r.name || ''; q('#f_set').value = r.prize_set; q('#f_title').value = String(r.title_id);
  q('#f_ss').value = local(r.sales_start); q('#f_se').value = local(r.sales_end);
  q('#f_cs').value = local(r.collect_start); q('#f_ce').value = local(r.collect_end);
  q('#f_price').value = r.price; q('#f_held').value = r.max_held; q('#f_acct').value = r.max_per_account;
  q('#f_seed').value = r.gil_seed; q('#f_note').value = r.note || '';
  q('#f_announce').checked = r.announce === undefined ? true : !!r.announce;
  q('#ftitle').textContent = EDIT ? 'Edit run ' + r.run_key : 'New run';
  q('#f_go').textContent = EDIT ? 'Save' : 'Make run'; q('#f_cancel').hidden = !EDIT;
  q('#f_hint').textContent = EDIT ? 'Once marbles are sold the title, prize set and price stay as they are.' : 'Suggested: the next 00:00 JST, 10 days of sales, 1 day wait, 7 days collection.';
}

function form(){
  return {run_key: q('#f_key').value.trim(), name: q('#f_name').value, prize_set: q('#f_set').value, title_id: +q('#f_title').value,
          sales_start: q('#f_ss').value, sales_end: q('#f_se').value, collect_start: q('#f_cs').value, collect_end: q('#f_ce').value,
          price: q('#f_price').value, max_held: q('#f_held').value, max_per_account: q('#f_acct').value, gil_seed: q('#f_seed').value,
          note: q('#f_note').value, announce: q('#f_announce').checked};
}

async function act(path, body, confirmText){
  if(BUSY) return;
  if(confirmText && !confirm(confirmText)) return;
  BUSY = true; document.querySelectorAll('button').forEach(b => b.disabled = true);
  try{
    const r = await fetch(qs(path), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    say(j.msg || (j.ok ? 'Done.' : 'Failed.'), j.ok);
    return !!j.ok;
  }catch(e){ say('Failed: ' + e.message, false); }
  finally{ BUSY = false; document.querySelectorAll('button').forEach(b => b.disabled = false); await load(); }
}

async function winners(){
  if(!WRUN) return;
  const r = S.runs.find(x => x.run_id === WRUN);
  q('#wcard').hidden = false; q('#wtitle').textContent = 'Winners of ' + (r ? r.run_key : WRUN);
  try{
    const res = await fetch(qs('bonanza/winners.json?id=' + WRUN + '&rank=' + q('#wrank').value));
    const j = await res.json();
    q('#wrows').innerHTML = (j.rows || []).length ? j.rows.map(w => `<tr><td>${w.prize_rank}</td><td class="n">${pad(w.number,5)}</td>
      <td>${esc(w.name)}${w.gm ? ' <span class="pill">GM</span>' : ''}</td>
      <td>${w.refunded_at ? 'refunded' : w.claimed_at == null ? '<span class="sub">not claimed</span>' : w.prize_gil ? num(w.prize_gil) + ' gil' : w.prize_row === 99 ? 'Bonanza Ball' : 'item ' + w.prize_item}</td></tr>`).join('')
      : '<tr><td colspan="4" class="empty">Nobody in this rank.</td></tr>';
  }catch(e){ say('Could not load the winners: ' + e.message, false); }
}

q('#f_go').onclick = async () => {
  const body = form();
  if(EDIT){ body.id = EDIT; if(await act('bonanza/update', body)){ EDIT = null; fill(S.suggested); } }
  else if(await act('bonanza/create', body, 'Make run ' + body.run_key + '?')) fill(S.suggested);
};
q('#f_cancel').onclick = () => { EDIT = null; fill(S.suggested); };
q('#wclose').onclick = () => { WRUN = null; q('#wcard').hidden = true; };
q('#wrank').onchange = winners;
document.addEventListener('click', e => {
  const b = e.target.closest('button'); if(!b) return;
  const id = +b.dataset.id;
  const r = S && S.runs.find(x => x.run_id === (id || +b.dataset.edit || +b.dataset.win));
  if(b.dataset.act === 'start') act('bonanza/start', {id}, 'Open sales for ' + r.run_key + ' now?');
  else if(b.dataset.act === 'stop') act('bonanza/stop', {id}, 'End sales for ' + r.run_key + ' now? The map draws the numbers within a minute.');
  else if(b.dataset.act === 'delete') act('bonanza/delete', {id}, 'Delete run ' + r.run_key + '?');
  else if(b.dataset.req === 'cancel') act('bonanza/request', {id, what: 'cancel'}, 'Cancel ' + r.run_key + '? Every unclaimed marble is refunded by delivery box. This cannot be undone.');
  else if(b.dataset.req === 'redraw') act('bonanza/request', {id, what: 'redraw'}, 'Draw new numbers for ' + r.run_key + '? The old ones stay in the log.');
  else if(b.dataset.req) act('bonanza/request', {id, what: b.dataset.req}, 'Draw the numbers for ' + r.run_key + ' now?');
  else if(b.dataset.edit){ EDIT = r.run_id; fill(r); q('#ftitle').scrollIntoView({behavior: 'smooth'}); }
  else if(b.dataset.win){ WRUN = r.run_id; winners(); }
});
load();
setInterval(() => { if(!BUSY && document.visibilityState === 'visible') load(); }, 15000);
</script>
</body></html>
"""
