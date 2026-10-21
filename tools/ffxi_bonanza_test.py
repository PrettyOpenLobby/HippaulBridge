#!/usr/bin/env python3
"""lsb/ffxi_bonanza.py and the Bonanza page of lsb/ffxi_fedtool.py, offline.

The LSB tables are a stand-in with the same rules as the SQL (a run key is unique, a run with
sales cannot be deleted). Checks the window and overlap rules, what an edit may change once
marbles are sold or the numbers drawn, start and stop, the draw / redraw / cancel requests
the map carries out, and the tool-port contract on the new routes.

  python tools/ffxi_bonanza_test.py            the checks
  python tools/ffxi_bonanza_test.py --serve N  serve the page on 127.0.0.1:N with sample runs
                                               (for a look in a browser)
"""
import calendar
import http.client
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_bonanza as B                                           # noqa: E402
import ffxi_fedtool as T                                           # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def utc(*a):
    return calendar.timegm(a + (0,) * (6 - len(a)))


class FakeStore:
    """The Bonanza tables in memory."""

    def __init__(self):
        self.runs_, self.sales, self.logs, self.next_id = [], {}, [], 1

    def ready(self):
        return True

    def runs(self, limit=40):
        return [dict(r) for r in sorted(self.runs_, key=lambda r: -r["sales_start"])][:limit]

    def all_windows(self):
        return [dict(r) for r in self.runs_ if r.get("cancelled_at") is None]

    def run(self, run_id):
        return next((dict(r) for r in self.runs_ if r["run_id"] == run_id), None)

    def stats(self, ids):
        out = {}
        for i in ids:
            s = self.sales.get(i)
            if s:
                out[i] = dict(s)
        return out

    def insert_run(self, row, actor, now):
        if any(r["run_key"] == row["run_key"] for r in self.runs_):
            return 0
        r = {k: None for k in B.RUN_COLUMNS.replace(" ", "").split(",")}
        r.update(row, run_id=self.next_id, origin="manual", created_at=now, created_by=actor)
        self.next_id += 1
        self.runs_.append(r)
        return 1

    def update_run(self, run_id, fields):
        for r in self.runs_:
            if r["run_id"] == run_id and r.get("cancelled_at") is None:
                r.update(fields)
                return 1
        return 0

    def delete_run(self, run_id):
        if self.sales.get(run_id, {}).get("sold"):
            return 0
        before = len(self.runs_)
        self.runs_ = [r for r in self.runs_ if r["run_id"] != run_id]
        return before - len(self.runs_)

    def set_request(self, run_id, what, actor, now):
        return self.update_run(run_id, {"request": what, "request_by": actor, "request_at": now})

    def add_log(self, run_id, who, action, detail, now):
        self.logs.append({"run_id": run_id, "run_key": None, "at": now, "who": who, "action": action, "detail": detail})

    def log(self, limit=40):
        return list(reversed(self.logs))[:limit]

    def winners(self, run_id, rank):
        return [{"number": 612, "prize_rank": 2, "claimed_at": None, "prize_row": None, "prize_item": None,
                 "prize_gil": None, "gm": 0, "refunded_at": None, "name": "Testchar"}]


def form(key, ss, days=10, wait=1, collect=7, **kw):
    out = {"run_key": key, "name": "Test", "prize_set": "v13", "title_id": 76,
           "sales_start": ss, "sales_end": ss + days * B.DAY, "collect_start": ss + (days + wait) * B.DAY,
           "collect_end": ss + (days + wait + collect) * B.DAY}
    out.update(kw)
    return out


def rules():
    print("rules")
    now = utc(2026, 10, 10, 12)
    check(B.next_jst_midnight(now) == utc(2026, 10, 10, 15), "next 00:00 JST from 12:00 UTC is 15:00 UTC")
    check(B.next_jst_midnight(utc(2026, 10, 10, 16)) == utc(2026, 10, 11, 15), "after 15:00 UTC it is the next day")
    s = B.suggested(now)
    check(s["collect_end"] - s["sales_start"] == 18 * B.DAY and s["title_id"] == 76, "suggested: 18 days, title 76")
    run = {"sales_start": 10, "sales_end": 20, "collect_start": 30, "collect_end": 40}
    check([B.phase(run, t) for t in (5, 10, 20, 30, 40)] == ["upcoming", "sales", "wait", "collect", "closed"],
          "phases")
    check(B.phase(dict(run, cancelled_at=1), 15) == "cancelled", "a cancelled run")
    check(B.parse_time("2026-10-11T15:00", "x") == utc(2026, 10, 11, 15), "datetime-local is UTC")
    for bad, label in ((dict(run_key="a b"), "a key with a space"), (dict(title_id=77), "title 77"),
                       (dict(prize_set="nomad"), "an unknown set"), (dict(sales_end=now), "sales end before start"),
                       (dict(collect_end=now - 1), "a run already over")):
        try:
            B.clean_run(dict(form("ok", now + B.DAY), **bad), now)
            check(False, "refuses " + label)
        except B.BonanzaError:
            check(True, "refuses " + label)


def actions():
    print("actions")
    now = utc(2026, 10, 10, 12)
    st = FakeStore()
    b = B.Bonanza(st)
    start = B.next_jst_midnight(now)
    check(b.create(form("short", start), "admin", now)["ok"], "makes a run")
    try:
        b.create(form("clash", start + 5 * B.DAY), "admin", now)
        check(False, "refuses an overlapping run")
    except B.BonanzaError as exc:
        check("overlaps" in str(exc), "refuses an overlapping run")
    try:
        b.create(form("short", start + 40 * B.DAY), "admin", now)
        check(False, "refuses a second run with the same key")
    except B.BonanzaError:
        check(True, "refuses a second run with the same key")
    rid = st.runs_[0]["run_id"]

    check(b.start({"id": rid}, "admin", now)["ok"], "starts an upcoming run now")
    check(B.phase(st.run(rid), now) == "sales", "it is selling")
    st.sales[rid] = {"run_id": rid, "sold": 3, "chars": 2, "accounts": 2, "gross": 6000, "gm": 0, "claimed": 0,
                     "refunded": 0, "gil_paid": 0}
    try:
        b.update({"id": rid, "title_id": 72}, "admin", now)
        check(False, "a sold run keeps its title")
    except B.BonanzaError:
        check(True, "a sold run keeps its title")
    check(b.update({"id": rid, "max_held": 5}, "admin", now)["ok"] and st.run(rid)["max_held"] == 5, "caps can change")
    try:
        b.request({"id": rid, "what": "draw"}, "admin", now)
        check(False, "no draw while selling")
    except B.BonanzaError:
        check(True, "no draw while selling")
    check(b.stop({"id": rid}, "admin", now + 60)["ok"] and st.run(rid)["sales_end"] == now + 60, "stops sales now")
    check(b.request({"id": rid, "what": "draw"}, "admin", now + 120)["ok"] and st.run(rid)["request"] == "draw",
          "asks the map to draw")
    st.update_run(rid, {"drawn_at": now + 130, "w1": 1, "request": None})
    try:
        b.update({"id": rid, "sales_end": now + 3 * B.DAY}, "admin", now + 200)
        check(False, "a drawn run keeps its sales window")
    except B.BonanzaError:
        check(True, "a drawn run keeps its sales window")
    st.sales[rid]["claimed"] = 1
    try:
        b.request({"id": rid, "what": "redraw"}, "admin", now + 200)
        check(False, "no redraw after a claim")
    except B.BonanzaError:
        check(True, "no redraw after a claim")
    try:
        b.delete({"id": rid}, "admin", now + 200)
        check(False, "a run with sales cannot be deleted")
    except B.BonanzaError:
        check(True, "a run with sales cannot be deleted")
    check(b.request({"id": rid, "what": "cancel"}, "admin", now + 200)["ok"], "asks the map to cancel")

    check(st.run(rid)["announce"] == 1, "a run made by hand is announced unless unticked")
    check(b.update({"id": rid, "announce": False}, "admin", now + 200)["ok"] and st.run(rid)["announce"] == 0,
          "...and the toggle can be turned off on a running run")
    check(b.create(form("quiet", start + 120 * B.DAY, announce=False), "admin", now)["ok"]
          and next(r for r in st.runs_ if r["run_key"] == "quiet")["announce"] == 0,
          "a test run made unticked is saved as not announced")
    b.delete({"id": next(r for r in st.runs_ if r["run_key"] == "quiet")["run_id"]}, "admin", now)
    check(B.ANNOUNCE_DDL[0].endswith("DEFAULT 0") and B.ANNOUNCE_DDL[1].endswith("SET DEFAULT 1")
          and "announce" in B.RUN_COLUMNS,
          "the column comes in at 0 for the runs already there, then defaults to 1")

    check(b.create(form("later", start + 60 * B.DAY), "admin", now)["ok"], "makes a later run")
    later = next(r for r in st.runs_ if r["run_key"] == "later")["run_id"]
    check(b.delete({"id": later}, "admin", now)["ok"] and st.run(later) is None, "deletes a run with no sales")
    state = b.state(now + 200)
    check(state["runs"][0]["phase"] == "wait" and state["current"] == rid, "state: the current run")


def port():
    print("tool port")
    now = utc(2026, 10, 10, 12)
    st = FakeStore()
    T._BONANZA[:] = [B.Bonanza(st)]
    bridge = types.SimpleNamespace(FED=None, log=lambda *a: None, NOTICES=None, NOTICES_SYNC=None, NOTICES_OFF="test")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(T.Tool(bridge, {}), "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, p = srv.server_address

    def call(method, path, body=None, token="tok"):
        c = http.client.HTTPConnection(host, p, timeout=5)
        h = {"Content-Type": "application/json", "X-Devtool-Actor": "tester"}
        if token:
            h["X-Devtool-Token"] = token
        c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
        r = c.getresponse()
        data = r.read()
        return r.status, data

    s, data = call("GET", "/bonanza")
    check(s == 200 and b"Mog Bonanza" in data, "GET /bonanza serves the page")
    s, _ = call("GET", "/bonanza.json", token="")
    check(s == 403, "the token is required")
    s, data = call("POST", "/bonanza/create", form("web", B.next_jst_midnight(now) + 400 * B.DAY))
    got = json.loads(data)
    check(s == 200 and got.get("ok"), "POST /bonanza/create makes a run")
    s, data = call("POST", "/bonanza/create", form("web", B.next_jst_midnight(now) + 500 * B.DAY))
    got = json.loads(data)
    check(s == 200 and not got.get("ok") and "already" in got.get("msg", ""), "a refusal comes back as ok false")
    s, data = call("GET", "/bonanza.json")
    got = json.loads(data)
    check(s == 200 and got["runs"][0]["run_key"] == "web" and st.logs[0]["who"] == "tester", "state and the actor")
    s, data = call("GET", "/bonanza/winners.json?id=1&rank=2")
    check(s == 200 and json.loads(data)["rows"][0]["number"] == 612, "winners")
    srv.shutdown()
    T._BONANZA[:] = []


def serve(port_no):
    """The page on a sample state, to look at in a browser."""
    now = int(__import__("time").time())
    st = FakeStore()
    b = B.Bonanza(st)
    start = B.next_jst_midnight(now)
    b.create(form("first-short", start - 12 * B.DAY, name="Mog Bonanza"), "calendar", start - 13 * B.DAY)
    first = st.runs_[0]
    first.update(origin="calendar", w1=53412, w2=2474, w3=336, w4=89, w5=6, drawn_at=start - B.DAY, pool_gil=1234000,
                 gil1=0, gil2=283820, gil3=111060, winners1=0, winners2=1, winners3=3, winners4=6, winners5=61, winners6=546)
    st.sales[first["run_id"]] = {"sold": 617, "chars": 70, "accounts": 52, "gross": 1234000, "gm": 0, "claimed": 12,
                                 "refunded": 0, "gil_paid": 283820}
    b.create(form("2026-newyear", start + 70 * B.DAY, days=35, wait=21, collect=15, prize_set="idream2015",
                  name="New Year Mog Bonanza 2027"), "calendar", now)
    st.runs_[1]["origin"] = "calendar"
    st.add_log(first["run_id"], "schedule", "draw", "53412 2474 336 89 6; winners 0/1/3/6/61/546", start - B.DAY)
    T._BONANZA[:] = [b]
    bridge = types.SimpleNamespace(FED=None, log=lambda *a: None, NOTICES=None, NOTICES_SYNC=None, NOTICES_OFF="test")
    srv = ThreadingHTTPServer(("127.0.0.1", port_no), T.make_handler(T.Tool(bridge, {}), ""))
    print("http://127.0.0.1:%d/bonanza" % port_no)
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]))
    rules()
    actions()
    port()
    print("FAILED: %d" % len(FAILED) if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)
