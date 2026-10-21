#!/usr/bin/env python3
"""lsb/ffxi_events.py and the Seasonal events page of lsb/ffxi_fedtool.py.

Offline (always): the switches the page reads and writes, the running/season logic over the
game calendar's rows, and the tool-port contract on the /events routes, over an in-memory
stand-in for the Store.

Against a real database (--db): sets and clears one switch in LSB's real server_variables and
checks the row the map would read, then puts it back as it was.

  python tools/ffxi_events_test.py
  python tools/ffxi_events_test.py --db HOST:PORT:USER:PASSWORD:DATABASE
  python tools/ffxi_events_test.py --serve N    the page on 127.0.0.1:N over the stand-in
"""
import http.client
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_events as E                                            # noqa: E402
import ffxi_fedtool as T                                           # noqa: E402

FAILED = []
DAY = 86400
# 2026-10-17 03:00 UTC (12:00 JST)
NOW = 1792206000


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def refuses(fn, *a, contains=""):
    try:
        fn(*a)
    except E.EventsError as exc:
        return contains in str(exc)
    return False


class FakeStore:
    def __init__(self, modes=None, windows=None):
        self.vars = dict(modes or {})
        self.rows = list(windows or [])
        self.logged = []

    def modes(self):
        return dict(self.vars)

    def windows(self, now):
        return self.rows

    def log(self, limit=60):
        return []

    def set_mode(self, var, value, festival, who, now, detail):
        if value == E.MODE_CALENDAR:
            self.vars.pop(var, None)
        else:
            self.vars[var] = value
        self.logged.append((var, value, festival, who, now, detail))


def win(key, start, days, open_ended=False):
    return {"event_key": key, "name": "?", "starts_at": start, "ends_at": start + days * DAY,
            "detail": json.dumps({"openEnded": open_ended})}


def by_id(state):
    return {f["id"]: f for f in state["festivals"]}


def offline():
    print("offline")
    check(len({f["var"] for f in E.FESTIVALS}) == len(E.FESTIVALS), "every festival has its own variable")
    check(all(f["var"].endswith("]Mode") for f in E.FESTIVALS), "every variable is a module's [X]Mode switch")

    # Harvest Festival running now (Lilies follows it), Starlight next, Doll Festival last spring only.
    rows = [win("harvest-festival-20261020", NOW - DAY, 13), win("lsb-StarlightCelebration-20261201", NOW + 45 * DAY, 31),
            win("doll-festival-20260219", NOW - 200 * DAY, 14)]
    st = FakeStore({"[Buffalo]Mode": E.MODE_ON, "[Lilies]Mode": E.MODE_OFF}, rows)
    ev = E.Events(st)
    s = by_id(ev.state(now=NOW))
    check(s["blazing-buffaloes"]["mode"] == "on" and s["blazing-buffaloes"]["running"] is True, "forced on runs")
    check(s["lilies"]["mode"] == "off" and s["lilies"]["running"] is False and s["lilies"]["in_season"] is True,
          "forced off stops a festival that is in season, and says it is in season")
    check(s["starlight"]["mode"] == "auto" and s["starlight"]["running"] is False and
          s["starlight"]["window"]["starts"] == NOW + 45 * DAY, "auto out of season: not running, next window shown")
    check(s["smilebringer"]["window"] == s["starlight"]["window"], "Smilebringer Boot Camp follows Starlight's dates")
    check(s["doll-festival"]["window"] is None and s["doll-festival"]["running"] is False,
          "a window that ended is neither shown nor running")
    check(s["valentione"]["running"] is None and s["valentione"]["window"] is None,
          "a festival the calendar does not list: running unknown on auto")
    st.rows = [win("lsb-StarlightCelebration-20261201", NOW + 45 * DAY, 2, open_ended=True)]
    check(by_id(ev.state(now=NOW))["starlight"]["window"] == {"starts": NOW + 45 * DAY, "ends": None},
          "a window cut at the calendar's scan edge has no end")
    st.rows = [win("harvest-festival-20261020", NOW - DAY, 13)]
    st.vars = {}
    check(by_id(ev.state(now=NOW))["lilies"]["running"] is True, "auto in season runs")
    st.rows = []
    check(by_id(ev.state(now=NOW))["lilies"]["running"] is None, "an empty calendar (map not published yet) says nothing")

    # set
    st = FakeStore()
    ev = E.Events(st)
    out = ev.set({"id": "starlight", "mode": "on"}, "mod", now=NOW)
    check(out["ok"] and st.vars == {"[Starlight]Mode": 1}, "on writes 1, the value !starlight on writes")
    check(st.logged[-1][2:] == ("starlight", "mod", NOW, "Starlight Celebration: auto -> on"), "on is logged old -> new")
    ev.set({"id": "starlight", "mode": "off"}, "mod", now=NOW)
    check(st.vars == {"[Starlight]Mode": 2}, "off writes 2")
    ev.set({"id": "starlight", "mode": "auto"}, "mod", now=NOW)
    check(st.vars == {} and st.logged[-1][1] == 0, "auto removes the row, as the map does for 0")
    n = len(st.logged)
    out = ev.set({"id": "starlight", "mode": "auto"}, "mod", now=NOW)
    check(out["ok"] and "already" in out["msg"] and len(st.logged) == n, "no change: nothing written or logged")
    check(refuses(ev.set, {"id": "harvest-festival", "mode": "on"}, "mod", contains="no festival"),
          "a festival with no switch refused")
    check(refuses(ev.set, {"id": "starlight", "mode": "1"}, "mod", contains="on, off or auto"), "a bad mode refused")

    # the real Store's SQL, over a recording connection
    sql = []

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, q, args=()):
            sql.append((q, args))

    class Conn:
        def cursor(self):
            return Cur()

        def commit(self):
            sql.append(("COMMIT", ()))

        def rollback(self):
            sql.append(("ROLLBACK", ()))

        def close(self):
            pass

    store = E.Store("h", 1, "u", "p", "d")
    store._ready = True
    store._connect = lambda autocommit=True: Conn()
    store.set_mode("[FoS]Mode", 1, "feast-of-swords", "mod", NOW, "x")
    check(sql[0] == ("INSERT INTO server_variables (name, value, expiry) VALUES (%s, %s, 0) "
                     "ON DUPLICATE KEY UPDATE value = %s, expiry = 0", ("[FoS]Mode", 1, 1)) and
          "openlobby_eventtool_log" in sql[1][0] and sql[2][0] == "COMMIT", "on: upsert with no expiry, logged, one transaction")
    sql[:] = []
    store.set_mode("[FoS]Mode", 0, "feast-of-swords", "mod", NOW, "x")
    check(sql[0] == ("DELETE FROM server_variables WHERE name = %s", ("[FoS]Mode",)), "auto: the row deleted")
    store._run = lambda q, args=(), fetch=False: [{"name": "[FoS]Mode", "value": 1, "expiry": 0},
                                                  {"name": "[VD]Mode", "value": 1, "expiry": 5}]
    check(store.modes() == {"[FoS]Mode": 1}, "an expired row reads as the calendar, as in the map")

    # the tool port
    T._EV[:] = [E.Events(FakeStore({"[Doll]Mode": 1}))]
    tool = T.Tool(types.SimpleNamespace(log=lambda *a: None, FED=None), environ={})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(tool, "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, body=None, token="tok"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdr = {"X-Devtool-Token": token, "X-Devtool-Actor": "mod"} if token else {}
        if body is not None:
            hdr["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdr)
        r = c.getresponse()
        return r.status, r.read()

    s, d = req("GET", "/events")
    check(s == 200 and b"<title>FFXI seasonal events</title>" in d, "GET /events serves the page")
    check(req("GET", "/events", token=None)[0] == 403, "the page needs the token")
    s, d = req("GET", "/events.json")
    check(s == 200 and by_id(json.loads(d))["doll-festival"]["mode"] == "on", "GET /events.json")
    s, d = req("POST", "/events/set", {"id": "doll-festival", "mode": "auto"})
    check(s == 200 and json.loads(d)["ok"] and T._EV[0].store.vars == {}, "POST /events/set")
    s, d = req("POST", "/events/set", {"id": "nope", "mode": "on"})
    check(s == 200 and not json.loads(d)["ok"] and "no festival" in json.loads(d)["msg"],
          "a refused change comes back ok: false with the reason")
    check(req("POST", "/events/set", {"id": "doll-festival", "mode": "on"}, token="bad")[0] == 403, "changes need the token")
    srv.shutdown()


def real(spec):
    print("real database")
    host, port, user, password, database = spec.split(":", 4)
    store = E.Store(host, port, user, password, database)
    ev = E.Events(store)
    var = "[Starlight]Mode"
    before = store.modes().get(var, 0)
    try:
        ev.set({"id": "starlight", "mode": "on"}, "mod-evtest")
        rows = store._run("SELECT value, expiry FROM server_variables WHERE name = %s", (var,), fetch=True)
        check(len(rows) == 1 and rows[0]["value"] == 1 and rows[0]["expiry"] == 0, "on: the row the map reads is 1")
        check(by_id(ev.state())["starlight"]["running"] is True, "on: the page says running")
        ev.set({"id": "starlight", "mode": "auto"}, "mod-evtest")
        rows = store._run("SELECT value FROM server_variables WHERE name = %s", (var,), fetch=True)
        check(len(rows) == 0, "auto: no row")
        check(any(e["who"] == "mod-evtest" for e in store.log()), "changes logged")
    finally:
        if before:
            store.set_mode(var, before, "starlight", "mod-evtest", 0, "test: put back")
        else:
            store._run("DELETE FROM server_variables WHERE name = %s", (var,))
        store._run("DELETE FROM openlobby_eventtool_log WHERE who = 'mod-evtest'")


def serve(port):
    rows = [win("harvest-festival-20261020", NOW - DAY, 13), win("lsb-StarlightCelebration-20261201", NOW + 45 * DAY, 31)]
    T._EV[:] = [E.Events(FakeStore({"[Buffalo]Mode": 1}, rows))]
    tool = T.Tool(types.SimpleNamespace(log=lambda *a: None, FED=None), environ={})
    srv = ThreadingHTTPServer(("127.0.0.1", port), T.make_handler(tool, ""))
    print("http://127.0.0.1:%d/events" % port)
    srv.serve_forever()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve(int(sys.argv[sys.argv.index("--serve") + 1]))
    offline()
    if "--db" in sys.argv:
        real(sys.argv[sys.argv.index("--db") + 1])
    print("%d failed" % len(FAILED) if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)
