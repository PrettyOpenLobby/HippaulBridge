#!/usr/bin/env python3
"""lsb/ffxi_gmcalls.py, driven offline: in-game GM calls filed on the core's
GM desk, and the desk's answers written back for the game.

No database and no LSB: the `help_desk` table is a fake connection that
records every statement, the ticket directory is a temp dir, and the desk's
state file is written by hand the way admin.py writes it.

Run from tools/: `python ffxi_gmcalls_test.py`. Exits non-zero on failure.
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_gmcalls as G  # noqa: E402

FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.statements.append((sql, tuple(params)))
        s = " ".join(sql.split())
        if s.startswith("SELECT COALESCE(MAX(id)"):
            self._rows = [(max([r[0] for r in self.conn.rows] or [0]),)]
        elif s.startswith("SELECT h.id"):
            since, limit = params
            self._rows = [r for r in sorted(self.conn.rows) if r[0] > since][:limit]
        elif s.startswith("UPDATE help_desk"):
            text, hid = params
            self.rowcount = 0
            for r in self.conn.rows:
                if r[0] == hid and r[0] not in self.conn.responded:
                    self.conn.responded[r[0]] = text
                    self.rowcount = 1
        elif s.startswith("INSERT INTO help_desk"):
            charid, message, text = params
            nid = max([r[0] for r in self.conn.rows] or [0]) + 1
            self.conn.rows.append((nid, charid, message, None, "", 0, 0, "", "", 0))
            self.conn.responded[nid] = text
            self.conn.inserted.append((nid, charid, message, text))
            self.rowcount = 1
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`rows` are tuples in ffxi_gmcalls.ROW_COLS order."""
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []
        self.responded = {}
        self.inserted = []

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=True):
        pass


def row(hid, charid, message, name="Bluebell", zone="Southern San d'Oria",
        login="pol7", addr=0x0100007F):
    return (hid, charid, message, "2026-10-02 03:00:00", name, 7, 230, zone,
            login, addr)


HANDLES = {
    4201: {"id": 11, "handle_name": "Bluebell", "client_guid": 0x253953FBA4,
           "member_id": 7},
}
IDMAP = {"1001": 4201}


def logs():
    out = []
    return out, lambda m: out.append(m)


def relay_for(d, since_id=None, env=None):
    _l, log = logs()
    return G.Relay(d, lambda c: IDMAP.get(str(c)),
                   lambda cid: HANDLES.get(cid), log=log, since_id=since_id,
                   env=env if env is not None else {}), _l


def section(title):
    print(f"\n[{title}]")


def test_request_numbers():
    section("request numbers follow gmd's counter and never collide")
    d = tempfile.mkdtemp(prefix="gmcalls-")
    try:
        check(G.next_request_no(d) == 1, "empty dir, no counter: 1")
        with open(os.path.join(d, G.STATE_NAME)) as f:
            check(f.read().strip() == "2", "counter written as 2 (gmd's convention)")
        with open(os.path.join(d, G.STATE_NAME), "w") as f:
            f.write("7\n")
        check(G.next_request_no(d) == 7, "counter 7 -> request 7")
        with open(os.path.join(d, G.STATE_NAME)) as f:
            check(f.read().strip() == "8", "and the counter is now 8")
        # Tickets on disk above the counter (gmd restarted on an older file).
        for n in (9, 12):
            with open(os.path.join(d, f"gm-20261002T030000-{n}.json"), "w") as f:
                json.dump({"request_no": n}, f)
        with open(os.path.join(d, G.STATE_NAME), "w") as f:
            f.write("3\n")
        check(G.next_request_no(d) == 13, "counter 3 but ticket 12 exists -> 13")
        # A torn (empty) counter file must not restart at 1.
        with open(os.path.join(d, G.STATE_NAME), "w") as f:
            f.write("")
        check(G.next_request_no(d) == 13, "empty (torn) counter: tickets reach 12, "
                                          "so 13, never 1")
        check(G.room_name(5, {}) == "#gmcall005", "room follows gmd's per-request naming")
        check(G.room_name(5, {"POL_GMD_ROOM_PER_REQUEST": "", "POL_GMD_CHAT_ROOM": "#x"})
              == "#x", "no per-request rooms -> the shared room")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_build_ticket():
    section("a help_desk row becomes the ticket gmd would have filed")
    r = dict(zip(G.ROW_COLS, row(5, 1001, "I am stuck in a wall")))
    rec = G.build_ticket(r, HANDLES[4201], 42, "#gmcall042", now=0)
    check(rec["request_no"] == 42 and rec["content_id"] == 1, "request number and FFXI content code")
    check(rec["guid"] == 0x253953FBA4, "guid is the handle's client id (how gmd knows the caller)")
    check(rec["handle"] == "Bluebell", "handle name for the desk")
    check(rec["body"].startswith("I am stuck in a wall"), "the player's text leads the body")
    check("Bluebell in Southern San d'Oria" in rec["body"], "character and zone named")
    check(rec["peer"] == "127.0.0.1:0", "client_addr (host-order int) -> ip:0")
    check(rec["room"] == "#gmcall042" and rec["source"] == G.SOURCE, "room and source")
    check(rec["ffxi"]["help_desk_id"] == 5 and rec["ffxi"]["charid"] == 1001
          and rec["ffxi"]["delivered"] == {}, "ffxi block: row, charid, nothing delivered")
    check(rec["raw"] == "" and rec["issue"] == 0, "no raw 0x102, issue 0 (like FMO's in-game calls)")
    unk = G.build_ticket(r, None, 43, None, now=0)
    check(unk["guid"] == 0 and unk["handle"] == "" and "not linked to a handle" in unk["body"],
          "no handle: guid 0 (gmd hands it to nobody), the desk is told why")


def test_file_calls():
    section("new help_desk rows are filed once, old ones are not raised")
    d = tempfile.mkdtemp(prefix="gmcalls-")
    try:
        conn = FakeConn([row(1, 1001, "old call before the relay existed")])
        relay, log = relay_for(d)
        filed = relay.file_calls(conn, now=0)
        check(filed == [] and relay.last_id == 1, "first run starts at the newest row")
        check(os.path.exists(os.path.join(d, G.OWN_STATE_NAME)), "watermark saved")
        conn.rows.append(row(2, 1001, "help, stuck"))
        conn.rows.append(row(3, 2002, "no handle for me", name="Nobody", login="poltest"))
        filed = relay.file_calls(conn, now=0)
        names = sorted(n for n in os.listdir(d) if G.TICKET_RE.fullmatch(n))
        check(len(filed) == 2 and len(names) == 2, "two calls -> two tickets")
        check([f["request_no"] for f in filed] == [1, 2], "numbered 1, 2 from gmd's counter")
        check(filed[0]["handle"] == "Bluebell" and filed[1]["handle"] == "",
              "paired character named, unpaired one filed unnamed")
        check(relay.file_calls(conn, now=0) == [], "nothing new -> nothing filed")
        with open(os.path.join(d, G.OWN_STATE_NAME)) as f:
            check(json.load(f)["last_id"] == 3, "watermark advanced to 3")
        conn.rows.append(row(4, 1001, G.REPLY_MARK + " reply to help_desk row 2"))
        check(relay.file_calls(conn, now=0) == [] and relay.last_id == 4,
              "our own reply carrier row is skipped, watermark still advances")
        # A restart: the watermark file is gone but the tickets are there.
        os.remove(os.path.join(d, G.OWN_STATE_NAME))
        relay2, _ = relay_for(d, since_id=0)
        filed = relay2.file_calls(conn, now=0)
        check([f["ffxi"]["help_desk_id"] for f in filed] == [1],
              "restart with SINCE_ID=0: the never-filed row 1 is filed, rows 2 and 3 "
              "are recognised by their tickets, the carrier row 4 is skipped")
        check(len([n for n in os.listdir(d) if G.TICKET_RE.fullmatch(n)]) == 3,
              "three tickets, none twice")
        own = G.own_tickets(d)
        check(sorted(own) == [1, 2, 3], "own_tickets maps help_desk rows to tickets")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_deliveries():
    section("the desk's events reach the game once each")
    rec = {"request_no": 9, "ffxi": {"delivered": {}}}
    check(G.deliveries(rec, {}) == [], "open, untouched: nothing owed")
    st = {"status": "open", "auto_reply_at": 100.0}
    due = G.deliveries(rec, st)
    check([k for k, _m, _t in due] == ["away"] and "#9" in due[0][2],
          "no GM on duty -> the away text with the request number")
    rec["ffxi"]["delivered"]["away"] = 100.0
    check(G.deliveries(rec, st) == [], "delivered once, not again")
    st.update(knocked_at=200.0)
    due = G.deliveries(rec, st)
    check([k for k, _m, _t in due] == ["knock"] and "GM Call" in due[0][2],
          "knock -> told where to meet the GM")
    rec["ffxi"]["delivered"]["knock"] = 200.0
    st.pop("knocked_at"); st.update(knocked_at=250.0)
    check([k for k, _m, _t in G.deliveries(rec, st)] == ["knock"],
          "a withdrawn and repeated knock is delivered again")
    rec["ffxi"]["delivered"]["knock"] = 250.0
    st.update(status="closed", at=300.0, resolution="Moved you to the gate.")
    due = G.deliveries(rec, st)
    check(due == [("closed", 300.0, "GM reply to your call #9: Moved you to the gate.")],
          "close with a resolution -> the GM's note")
    rec["ffxi"]["delivered"]["closed"] = 300.0
    st.update(status="open"); st.pop("resolution")
    check(G.deliveries(rec, st) == [], "reopened: nothing to say")
    st.update(status="closed", at=400.0)
    check(G.deliveries(rec, st) == [("closed", 400.0, G.CLOSE_TEXT.format(n=9))],
          "closed again without a note -> the plain close text")


def test_answer_calls():
    section("answers are written into help_desk for the game to show")
    d = tempfile.mkdtemp(prefix="gmcalls-")
    try:
        conn = FakeConn([row(1, 1001, "baseline")])
        relay, log = relay_for(d)
        relay.file_calls(conn, now=0)
        conn.rows.append(row(2, 1001, "stuck"))
        [rec] = relay.file_calls(conn, now=0)
        tid = G.own_tickets(d)[2]
        check(relay.answer_calls(conn) == [], "no desk state yet: nothing written")
        state = {tid: {"status": "open", "auto_reply_at": 1.5}}
        with open(os.path.join(d, G.TICKET_STATE_NAME), "w") as f:
            json.dump(state, f)
        out = relay.answer_calls(conn)
        check([(t, k) for t, k, _ in out] == [(tid, "away")], "away delivered")
        check(conn.responded.get(2, "").startswith("Your GM call #1 has been received"),
              "written into the call's own row (UPDATE, response was NULL)")
        check(conn.inserted == [], "no carrier row needed")
        with open(os.path.join(d, tid + ".json")) as f:
            check(json.load(f)["ffxi"]["delivered"] == {"away": 1.5},
                  "ticket records what was delivered")
        check(relay.answer_calls(conn) == [], "same state again: nothing repeated")
        state[tid].update(status="closed", at=9.0, resolution="Done. " * 300)
        with open(os.path.join(d, G.TICKET_STATE_NAME), "w") as f:
            json.dump(state, f)
        out = relay.answer_calls(conn)
        check([(t, k) for t, k, _ in out] == [(tid, "closed")], "close delivered")
        check(len(conn.inserted) == 1 and conn.inserted[0][1] == 1001
              and conn.inserted[0][2].startswith(G.REPLY_MARK),
              "the row already held a response, so a carrier row was inserted")
        check(len(conn.responded[conn.inserted[0][0]]) == G.RESPONSE_MAX,
              "a long note is cut to what the client can read (1024)")
        # The carrier row must not come back as a new call.
        check(relay.file_calls(conn, now=0) == [], "the carrier row is not filed as a call")
        # A failing write is retried next time, not marked delivered.
        state[tid].update(knocked_at=20.0)
        with open(os.path.join(d, G.TICKET_STATE_NAME), "w") as f:
            json.dump(state, f)

        class Broken(FakeConn):
            def cursor(self):
                raise RuntimeError("db gone")
        check(relay.answer_calls(Broken()) == [], "database down: nothing claimed delivered")
        with open(os.path.join(d, tid + ".json")) as f:
            check("knock" not in json.load(f)["ffxi"]["delivered"],
                  "and the knock stays owed")
        out = relay.answer_calls(conn)
        check([(t, k) for t, k, _ in out] == [(tid, "knock")], "delivered on the next poll")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_run_without_write_access():
    section("an unwritable ticket dir is reported, not fatal")
    d = tempfile.mkdtemp(prefix="gmcalls-")
    try:
        missing = os.path.join(d, "no", "such", "dir")
        relay, said = relay_for(missing)
        relay.writable = lambda: False          # os.access is unreliable on Windows
        calls = []
        G.run(relay, lambda: (FakeConn(), None), 0, log=said.append,
              sleep=lambda s: calls.append(s), once=True)
        check(any("not writable" in m for m in said), "says the mount is missing")
        check(not os.path.exists(os.path.join(missing, G.OWN_STATE_NAME)), "touched nothing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    t0 = time.time()
    test_request_numbers()
    test_build_ticket()
    test_file_calls()
    test_deliveries()
    test_answer_calls()
    test_run_without_write_access()
    print(f"\n{len(FAILS)} failure(s) in {time.time() - t0:.1f}s")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
