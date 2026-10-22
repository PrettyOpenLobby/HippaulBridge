#!/usr/bin/env python3
"""lsb/ffxi_notices.py and the Notices page of lsb/ffxi_fedtool.py, offline.

The LSB table is a stand-in with the same rules as the SQL (a tag is unique, INSERT IGNORE
keeps the first row, a sent announcement is never removed). Checks what the calendar sync
writes for a fixed clock and calendar, that a second pass changes nothing, that a sent or
cancelled announcement is not put back, FFXI's own calendar, the countdown's rows, and the tool-port contract on
the new routes. Needs OpenLobby's eventcal.py (an OpenLobby checkout beside this repository).
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
import ffxi_fedtool as T                                           # noqa: E402
import ffxi_notices as N                                           # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def utc(*a):
    return calendar.timegm(a + (0,) * (6 - len(a)))


class FakeStore:
    """openlobby_notices in memory, with the SQL's semantics."""

    def __init__(self):
        self.t, self.next_id = [], 1

    def rows(self, now):
        return [dict(r) for r in self.t
                if (r["kind"] == "motd" and (r["ends_at"] is None or r["ends_at"] > now - N.DAY))
                or (r["kind"] == "announce" and (r["sent_at"] is None or r["sent_at"] > now - 2 * N.DAY))]

    def _by_tag(self, tag):
        return next((r for r in self.t if tag is not None and r["tag"] == tag), None)

    def insert(self, row):
        if self._by_tag(row.get("tag")):
            return 0
        r = {"id": self.next_id, "kind": row["kind"], "tag": row.get("tag"), "message": row["message"],
             "starts_at": int(row["starts_at"]), "ends_at": row.get("ends_at"), "sent_at": None,
             "created_by": row.get("created_by"), "created_at": row.get("created_at") or 0,
             "sort": row.get("sort") or 0}
        self.next_id += 1
        self.t.append(r)
        return 1

    def put_motd(self, row):
        r = self._by_tag(row["tag"])
        if r is None:
            return self.insert(dict(row, kind="motd"))
        new = {k: row.get(k) for k in ("message", "starts_at", "ends_at", "sort")}
        if all(r[k] == v for k, v in new.items()):
            return 0
        r.update(new)
        return 2

    def delete_tag(self, tag):
        r = self._by_tag(tag)
        if r and (r["kind"] == "motd" or r["sent_at"] is None):
            self.t.remove(r)
            return 1
        return 0

    def pending_calendar(self, now):
        return [(r["id"], r["tag"]) for r in self.t if r["kind"] == "announce" and r["sent_at"] is None
                and r["starts_at"] > now and (r["tag"] or "").startswith("calendar:")]

    def delete_id(self, rid):
        r = next((r for r in self.t if r["id"] == rid), None)
        if r and (r["kind"] == "motd" or r["sent_at"] is None):
            self.t.remove(r)
            return 1
        return 0

    def cancel_id(self, rid):
        r = next((r for r in self.t if r["id"] == rid and r["kind"] == "announce" and r["sent_at"] is None), None)
        if r:
            r["ends_at"] = r["starts_at"]
            return 1
        return 0

    def end_motd(self, rid, now):
        r = next((r for r in self.t if r["id"] == rid and r["kind"] == "motd"
                  and (r["ends_at"] is None or r["ends_at"] > now)), None)
        if r:
            r["ends_at"] = now
            return 1
        return 0

    # what the map module does every 15 s
    def module_tick(self, now):
        for r in sorted(self.t, key=lambda r: (r["starts_at"], r["id"])):
            if r["kind"] == "announce" and r["sent_at"] is None and r["starts_at"] <= now:
                r["sent_at"] = now

    def tag(self, tag):
        return self._by_tag(tag)

    def motd_tags(self, prefix):
        return [r["tag"] for r in self.t if r["kind"] == "motd" and (r["tag"] or "").startswith(prefix)]


ec = N.find_eventcal()
check(ec is not None, "eventcal.py found through the OpenLobby candidates")
if ec is None:
    sys.exit(1)

CAL = {"tm": {"weekly": {"key": "chocobo-cup", "name": "Chocobo Cup", "dow": 4, "hour": 0, "hours": 72,
                         "sessions": {"hours": [19, 2], "minutes": 60, "from": "2026-09-28",
                                      "season_hour": 12, "season_hours": 72}},
              "events": []},
       "jan": {"weekly": {"key": "weekend-cup", "name": "Weekend Cup, week {week}", "dow": 4, "hour": 0,
                          "hours": 72},
               "events": [{"key": "quiet-cup", "name": "Quiet Cup", "date": "2026-10-21", "days": 1,
                           "remind": False}]}}
GAMES = ["tm", "jan"]
REMIND = [180, 15]

# ================================================================================================
print("1. The calendar sync, Friday 2026-10-09 15:00 UTC")
NOW = utc(2026, 10, 9, 15)
store = FakeStore()
p = N.plan(NOW, CAL, GAMES, ec, REMIND)
got = N.apply(store, p, NOW)
tm, jan = store.tag("calendar:motd:tm"), store.tag("calendar:motd:jan")
check(jan and jan["message"] == "Current Event: JongHoLow Weekend Cup, until Mon 00:00 UTC. "
                                "Check community channels for details.", "JongHoLow's cup is on: %r" % (jan or {}).get("message"))
check(jan and (jan["starts_at"], jan["ends_at"]) == (utc(2026, 10, 9), utc(2026, 10, 12)),
      "...and the line ends when the cup does, bridge or no bridge")
check(tm and tm["message"] == "Next Event: Tetra Master Chocobo Cup, 19:00 UTC. Check community channels for details.",
      "Tetra Master's season is on, no session yet: %r" % (tm or {}).get("message"))
check(tm and tm["ends_at"] == utc(2026, 10, 9, 19), "...until the session starts")
check(all(len(r["message"]) <= N.MOTD_MAX for r in (tm, jan) if r), "the login lines fit")
ann = sorted((r["starts_at"], r["tag"], r["message"], r["ends_at"]) for r in store.t if r["kind"] == "announce")
want = [(utc(2026, 10, 9, 16), "calendar:remind:chocobo-cup-20261009-1900:180",
         "Tetra Master: the Chocobo Cup starts in 3 hours, at 19:00 UTC.", utc(2026, 10, 9, 16, 10)),
        (utc(2026, 10, 9, 18, 45), "calendar:remind:chocobo-cup-20261009-1900:15",
         "Tetra Master: the Chocobo Cup starts in 15 minutes, at 19:00 UTC.", utc(2026, 10, 9, 18, 55)),
        (utc(2026, 10, 9, 19), "calendar:start:chocobo-cup-20261009-1900",
         "Tetra Master: the Chocobo Cup starts now! Join from PlayOnline.", utc(2026, 10, 9, 19, 10))]
check(ann[:3] == want, "tonight's 19:00 session: two reminders and the start, in order, each lapsing in time")
check(all(r[1] != "calendar:start:weekend-cup-20261009" for r in ann),
      "a start that was hours ago is not announced late")
check(any(r[1] == "calendar:start:chocobo-cup-20261010-0200" for r in ann)
      and any(r[1] == "calendar:remind:chocobo-cup-20261010-0200:180" for r in ann),
      "the 02:00 session's rows are queued ahead too")
check(all(len(r[2]) <= 140 for r in ann), "every announcement is one chat line")
check(got["motd"] == 2 and got["queued"] == len(ann) and not got["dropped"], "counts: %r" % got)

print("2. A second pass changes nothing")
before = json.dumps(store.t, sort_keys=True)
got = N.apply(store, N.plan(NOW + 60, CAL, GAMES, ec, REMIND), NOW + 60)
check(not any(got.values()) and json.dumps(store.t, sort_keys=True) == before, "idempotent: %r" % got)

print("3. Time passes")
store.module_tick(utc(2026, 10, 9, 16))
sent = store.tag("calendar:remind:chocobo-cup-20261009-1900:180")
check(sent["sent_at"] == utc(2026, 10, 9, 16), "the module sends the 3-hour reminder")
cancel = store.tag("calendar:remind:chocobo-cup-20261009-1900:15")
store.cancel_id(cancel["id"])
check(N.describe(cancel, utc(2026, 10, 9, 17)) == "cancelled", "a reminder cancelled in the panel shows as cancelled")
T1 = utc(2026, 10, 9, 19, 30)
got = N.apply(store, N.plan(T1, CAL, GAMES, ec, REMIND), T1)
check(store.tag("calendar:motd:tm")["message"]
      == "Current Event: Tetra Master Chocobo Cup, until 20:00 UTC. Check community channels for details.",
      "during the session the line says it is on")
check(sum(1 for r in store.t if r["tag"] == sent["tag"]) == 1 and store.tag(sent["tag"])["sent_at"],
      "a sent announcement is not queued again")
check(store.tag(cancel["tag"])["ends_at"] == store.tag(cancel["tag"])["starts_at"],
      "a cancelled one stays cancelled")
T2 = utc(2026, 10, 9, 21)
N.apply(store, N.plan(T2, CAL, GAMES, ec, REMIND), T2)
check(store.tag("calendar:motd:tm")["message"].startswith("Next Event: Tetra Master Chocobo Cup, Sat 02:00 UTC."),
      "between sessions the next one is named with its day")
T3 = utc(2026, 10, 12, 3)
got = N.apply(store, N.plan(T3, CAL, GAMES, ec, REMIND), T3)
check(store.tag("calendar:motd:tm") is None and store.tag("calendar:motd:jan") is None,
      "Monday after the cups: both lines are gone (%r)" % got)
T4 = utc(2026, 10, 15, 3)
N.apply(store, N.plan(T4, CAL, GAMES, ec, REMIND), T4)
check(store.tag("calendar:motd:jan")["message"].startswith("Next Event: JongHoLow Weekend Cup, Fri 00:00 UTC."),
      "a day before the next cup the line names it")

print("4. A window that leaves the calendar takes its pending rows with it")
cal2 = {"jan": CAL["jan"]}
pend = [t for _i, t in store.pending_calendar(T4) if t.startswith("calendar:remind:chocobo") or
        t.startswith("calendar:start:chocobo")]
got = N.apply(store, N.plan(T4, cal2, GAMES, ec, REMIND), T4)
check(pend and got["dropped"] == len(pend) and not any(t.startswith("calendar:start:chocobo")
                                                    for _i, t in store.pending_calendar(T4)),
      "%d pending Tetra Master rows dropped" % len(pend))
check(store.tag("calendar:motd:tm") is None, "and its line")
check(store.tag(sent["tag"]) is not None, "an announcement already sent stays as history")

print("5. Calendar details")
q = N.plan(utc(2026, 10, 21, 1), CAL, ["jan"], ec, REMIND)
check(not any("quiet-cup" in r["tag"] for r in q["announce"]) and q["motd"]["jan"]["message"].startswith(
    "Current Event: JongHoLow Quiet Cup"), "remind: false: a login line, no announcements")
check(q["next"] == utc(2026, 10, 22), "the next pass is set for the cup's end")
check(N.plan(NOW, {}, GAMES, ec, REMIND) == {"motd": {"tm": None, "jan": None}, "announce": [], "next": None},
      "an empty calendar: nothing")

# ================================================================================================
print("5b. FFXI's own calendar (openlobby_game_calendar)")
FX = N.ffxi_events([
    {"source": "campaigns", "event_key": "c1", "name": "Adventurer Campaigns",
     "starts_at": utc(2026, 10, 10, 15), "ends_at": utc(2026, 10, 20, 15),
     "detail": '{"campaigns": ["Dedication", "Commitment", "Synthesis"], "plus": true, "fishing": false}'},
    {"source": "login", "event_key": "2026-10", "name": "Repeat Login Campaign",
     "starts_at": utc(2026, 10, 10, 15), "ends_at": utc(2026, 11, 1, 15),
     "detail": '{"month": "October", "earnEnd": %d}' % utc(2026, 10, 25, 15)},
    {"source": "seasonal", "event_key": "harvest-festival-2026", "name": "Harvest Festival",
     "starts_at": utc(2026, 10, 20, 15), "ends_at": utc(2026, 11, 1, 15),
     "detail": '{"blurb": "Trade a sweet to the townsfolk for a costume.", "openEnded": false}'},
    {"source": "bonanza", "event_key": "b1", "name": "Mog Bonanza", "starts_at": 0, "ends_at": 1, "detail": "{}"},
    {"source": "seasonal", "event_key": "odd", "name": "Odd", "starts_at": 0, "ends_at": 1, "detail": "not json"},
])
check([e["source"] for e in FX] == ["campaigns", "login", "seasonal", "seasonal"] and FX[3]["detail"] == {},
      "rows to events: the Bonanza left to its own module, bad detail JSON read as none")
FX = FX[:3]
T0 = utc(2026, 10, 14, 12)
f = N.plan_ffxi(T0, FX)
check([r["message"] for r in f["motd"]] == [
    "Now on: Adventurer Campaigns, until Oct 20 15:00 UTC.",
    "Now on: The October Login Campaign. Earn points until Oct 25 15:00 UTC."],
      "the login lines of what runs now, the login campaign naming its points' end")
tags = [r["tag"] for r in f["announce"]]
check("calendar:ffxi:start:campaigns:c1" not in tags and "calendar:ffxi:start:seasonal:harvest-festival-2026" in tags,
      "a start already past is not shouted late; one to come is")
check([t for t in tags if ":digest:" in t] == ["calendar:ffxi:digest:" + x for x in
                                               ("2026101414", "2026101502", "2026101514", "2026101602")],
      "the 'Now in Vana'diel' shout at 02 and 14 UTC over the next two days")
dig = next(r for r in f["announce"] if r["tag"].endswith("2026101414"))
check(dig["message"] == "Now in Vana'diel: Adventurer Campaigns (until Oct 20 15:00 UTC) and the October Login "
                        "Campaign (points until Oct 25 15:00 UTC). Type /servmes to see them again.", dig["message"])
close = {r["tag"]: r for r in f["announce"] if ":closing:" in r["tag"]}
check(close["calendar:ffxi:closing:campaigns:c1"]["starts_at"] == utc(2026, 10, 19, 15)
      and close["calendar:ffxi:closing:campaigns:c1"]["message"]
      == "Adventurer Campaigns end in one day, at Tue 15:00 UTC.", "the campaigns' closing shout, a day ahead")
check(close["calendar:ffxi:closing:login:2026-10"]["starts_at"] == utc(2026, 10, 24, 15)
      and close["calendar:ffxi:closing:login:2026-10"]["message"].startswith(
          "The October Login Campaign: one day left to earn login points, until Sun 15:00 UTC."),
      "the login campaign's closing shout is for its points, not its exchange")
hf = next(r for r in f["announce"] if r["tag"] == "calendar:ffxi:start:seasonal:harvest-festival-2026")
check(hf["message"] == "The Harvest Festival has begun! It runs until Nov 1 15:00 UTC. Trade a sweet to the "
                       "townsfolk for a costume." and hf["ends_at"] == hf["starts_at"] + N.ANNOUNCE_TTL,
      "a festival's start shout carries its blurb")
check(f["next"] == utc(2026, 10, 14, 14), "the next pass is at the next shout")
check(all(len(r["message"]) <= N.ANNOUNCE_MAX for r in f["announce"])
      and all(len(r["message"]) <= N.MOTD_MAX for r in f["motd"])
      and all(r["message"].isascii() for r in f["announce"] + f["motd"]), "every line fits and is ASCII")

fs = FakeStore()
got = N.apply(fs, dict(N.plan(T0, {}, [], ec), motd_ffxi=f["motd"], announce=f["announce"]), T0)
check(got["motd"] == 2 and got["queued"] == len([r for r in f["announce"] if r["starts_at"] <= T0 + 2 * N.DAY]),
      "applied: two login lines and the shouts of the next two days")
got = N.apply(fs, dict(N.plan(T0, {}, [], ec), motd_ffxi=f["motd"], announce=f["announce"]), T0)
check(not any(got.values()), "a second pass changes nothing")
T1 = utc(2026, 10, 21, 3)
f1 = N.plan_ffxi(T1, FX)
got = N.apply(fs, dict(N.plan(T1, {}, [], ec), motd_ffxi=f1["motd"], announce=f1["announce"]), T1)
lines = sorted(r["message"] for r in fs.t if r["kind"] == "motd")
check(lines == ["Now on: The Harvest Festival, until Nov 1 15:00 UTC.",
                "Now on: The October Login Campaign. Earn points until Sun 15:00 UTC."]
      and got["motd_off"] == 1, "a week on: the campaigns' line is gone, the festival's is up")
T2 = utc(2026, 10, 27, 3)
check([r["message"] for r in N.plan_ffxi(T2, FX)["motd"]][0]
      == "Now on: The October Login Campaign. Trade your points until Sun 15:00 UTC.",
      "after the points stop, the login line is about trading them")

late = N.plan_ffxi(utc(2026, 10, 20, 15, 5), FX, digest_hours=(15,))
check(not any(r["tag"] == "calendar:ffxi:digest:2026102015" for r in late["announce"])
      and any(r["tag"] == "calendar:ffxi:digest:2026102115" for r in late["announce"]),
      "no digest within the hour after a start shout; the next day's goes out")
many = dict(FX[0], detail={"campaigns": ["Campaign number %d with a long name" % i for i in range(30)]})
msg = N._ffxi_start(many)
check(len(msg) <= N.ANNOUNCE_MAX and msg.endswith(" more."), "a long campaign list is cut to fit: " + msg[-30:])
check(N.plan_ffxi(T0, []) == {"motd": [], "announce": [], "next": None}, "no FFXI events: nothing")

# ================================================================================================
print("6. The countdown")
down = utc(2026, 10, 10, 18)
rows = N.countdown_rows(down, utc(2026, 10, 10, 17))
check([(r["starts_at"], r["ends_at"]) for r in rows] == [(down - m * 60, down) for m in (30, 15, 10, 5, 4, 3, 2, 1)],
      "warnings at T-30, T-15, T-10, then every minute from T-5, each lapsing at the shutdown")
check(rows[2]["message"] == "The FFXI server goes down for maintenance in 10 minutes. Please log out safely."
      and rows[-1]["message"].endswith("in 1 minute. Please log out safely."), "the wording, singular at 1")
check([r["tag"] for r in N.countdown_rows(down, down - 20 * 60)] == ["admin:down:%d:%d" % (down, m) for m in (15, 10, 5, 4, 3, 2, 1)],
      "twenty minutes out: the 30-minute warning is left out")
cd = FakeStore()
d, r1, a1 = N.add_countdown(cd, "2026-10-10T18:00", "mod1", utc(2026, 10, 10, 17))
d, r2, a2 = N.add_countdown(cd, "2026-10-10T18:00", "mod1", utc(2026, 10, 10, 17))
check(d == down and a1 == 8 and a2 == 0 and len(cd.t) == 8, "the same countdown twice queues it once")

# ================================================================================================
print("7. The Notices page on the tool port")
LOGS = []
bridge = types.SimpleNamespace(FED=None, _world_name="OpenLobby", log=lambda tag, msg: LOGS.append(msg),
                               NOTICES=None, NOTICES_SYNC=None, NOTICES_OFF="LSB_DB_PASSWORD is not set")
srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(T.Tool(bridge, {}), "s3cret"))
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None, token="s3cret", actor="mod1"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = {"X-Devtool-Actor": actor}
    if token:
        h["X-Devtool-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


for m, path in (("GET", "/notices"), ("GET", "/notices.json"), ("POST", "/notices/announce"),
                ("POST", "/notices/countdown"), ("POST", "/notices/motd"), ("POST", "/notices/cancel")):
    # No body: the port answers 403 before reading one, and Windows aborts a socket that
    # still has unread bytes in it.
    check(call(m, path, token=None)[0] == 403, "%s %s without the token: refused" % (m, path))
    check(call(m, path, token="wrong")[0] == 403, "%s %s, wrong token: refused" % (m, path))
st, page = call("GET", "/notices")
check(st == 200 and "<title>FFXI notices</title>" in page and "<script>" in page, "the page, with the token")
DASH = chr(0x2014)
check(DASH not in page and DASH not in open(N.__file__, encoding="utf-8").read(), "no em dash in the page or the module")
st, page = call("GET", "/")
check(st == 200 and "<title>FFXI federation</title>" in page, "/ is still the federation page")
check(call("GET", "/notices.json?t=s3cret", token=None)[0] == 200, "?t= works when opened directly")
st, s = call("GET", "/notices.json")
check(s["off"] == "LSB_DB_PASSWORD is not set" and s["motd"] == [], "off on the bridge: the page says why")
r = call("POST", "/notices/announce", {"text": "hello"})[1]
check(not r["ok"] and "off on the bridge" in r["msg"], "...and an action says so instead of failing")

bridge.NOTICES, bridge.NOTICES_OFF = FakeStore(), None
r = call("POST", "/notices/announce", {"text": "  Server  restart\nsoon  "})[1]
row = bridge.NOTICES.t[-1]
check(r["ok"] and row["message"] == "Server restart soon" and row["created_by"] == "mod1",
      "announce now: one line, the panel's actor")
check(row["ends_at"] - row["starts_at"] == N.ANNOUNCE_TTL, "...lapsing after the module's default")
check(row["tag"].startswith("admin:announce:"), "...as news (not an urgent tag)")
r = call("POST", "/notices/announce", {"text": "Log out now", "urgent": True})[1]
check(r["ok"] and bridge.NOTICES.t[-1]["tag"].startswith(N.URGENT_TAG), "urgent: tagged for the maintenance colour")
r = call("POST", "/notices/announce", {"text": "x" * 500})[1]
check(not r["ok"] and "440" in r["msg"], "too long: refused")
r = call("POST", "/notices/announce", {"text": "later", "at": "2099-01-01T10:00", "every": 30, "times": 3})[1]
later = [x for x in bridge.NOTICES.t if x["message"] == "later"]
check(r["ok"] and [x["starts_at"] for x in later] == [utc(2099, 1, 1, 10), utc(2099, 1, 1, 10, 30), utc(2099, 1, 1, 11)],
      "scheduled with a repeat: three rows half an hour apart")
r = call("POST", "/notices/announce", {"text": "past", "at": "2020-01-01T10:00"})[1]
check(not r["ok"], "a time that has passed: refused")
r = call("POST", "/notices/countdown", {"down_at": "2099-01-01T10:00"})[1]
check(not r["ok"] and "week" in r["msg"], "a countdown more than a week out: refused")
r = call("POST", "/notices/countdown", {"down_at": "2020-01-01T10:00"})[1]
check(not r["ok"], "a countdown in the past: refused")
r = call("POST", "/notices/countdown", {"down_at": "2026-01-01T10:00", "leads": "ten"})[1]
check(not r["ok"] and "minutes" in r["msg"], "warning times that are not numbers: refused")
r = call("POST", "/notices/motd", {"text": "Double EXP weekend!", "until": "2099-01-01T00:00"})[1]
motd = bridge.NOTICES.t[-1]
check(r["ok"] and motd["kind"] == "motd" and motd["ends_at"] == utc(2099, 1, 1), "a login line with an end")
st, s = call("GET", "/notices.json")
check([x["message"] for x in s["motd"]] == ["Double EXP weekend!"] and s["motd"][0]["state"] == "showing",
      "the page lists it as showing")
check(len([x for x in s["announce"] if x["state"] in ("scheduled", "due")]) == 5, "and the five pending announcements")
check(call("POST", "/notices/end", {"id": motd["id"]})[1]["ok"], "end the line")
check(not call("POST", "/notices/end", {"id": motd["id"]})[1]["ok"], "ending it twice: refused")
check(call("POST", "/notices/delete", {"id": motd["id"]})[1]["ok"] and motd not in bridge.NOTICES.t, "remove it")
check(call("POST", "/notices/cancel", {"id": later[0]["id"]})[1]["ok"]
      and N.describe(later[0], utc(2098, 1, 1)) == "cancelled", "cancel a pending announcement")
bridge.NOTICES.module_tick(utc(2099, 1, 1, 12))
check(not call("POST", "/notices/cancel", {"id": later[1]["id"]})[1]["ok"], "a sent one cannot be cancelled")
check(not call("POST", "/notices/delete", {"id": later[1]["id"]})[1]["ok"] and later[1] in bridge.NOTICES.t,
      "nor removed")
check(not call("POST", "/notices/cancel", {"id": "x"})[1]["ok"], "no id: refused")
st, s = call("GET", "/notices.json")
check(any("mod1" in line and "countdown" not in line and "announcement" in line for line in s["log"]),
      "changes are logged with the panel's actor")
check(call("POST", "/notices/nope", {})[0] == 404 and call("GET", "/notices/x")[0] == 404, "unknown routes: 404")

print("8. The panel's side (admingames): the Notices tool and the maintenance warning's call")
sys.path.insert(0, os.path.join(HERE, os.pardir, os.pardir, "openlobby", "services"))
import admingames as G                                             # noqa: E402
import adminmodules as M                                           # noqa: E402
check([t.path for t in M.load()["ffxi"].tools][:2] == ["", "notices"] and G._is_page("ffxi", "notices"),
      "FFXI lists Federation and Notices; an unreachable Notices page is shown as a page")
for k in ("POL_ADMIN_GAME_FFXI_URL", "POL_ADMIN_GAME_FFXI_TOKEN"):
    os.environ.pop(k, None)
ok, msg = G.call("ffxi", "notices/announce", {"text": "x"}, "op")
check(not ok and "no tools set up" in msg, "no FFXI tool configured: reported, not raised (%r)" % msg)
os.environ["POL_ADMIN_GAME_FFXI_URL"] = "http://127.0.0.1:%d" % srv.server_port
os.environ["POL_ADMIN_GAME_FFXI_TOKEN"] = "wrong"
ok, msg = G.call("ffxi", "notices/announce", {"text": "x"}, "op")
check(not ok and "refused" in msg, "a wrong token: reported as the panel's fault (%r)" % msg)
os.environ["POL_ADMIN_GAME_FFXI_TOKEN"] = "s3cret"
ok, msg = G.call("ffxi", "notices/announce", {"text": "All Worlds Maintenance: from 10:00 UTC.", "urgent": True}, "op")
row = bridge.NOTICES.t[-1]
check(ok and row["message"] == "All Worlds Maintenance: from 10:00 UTC." and row["created_by"] == "op"
      and row["tag"].startswith(N.URGENT_TAG),
      "the maintenance warning lands as an urgent announcement, by the panel's user (%r)" % msg)
check('"urgent": True}, user)' in open(os.path.join(HERE, os.pardir, os.pardir, "openlobby", "services", "admin.py"),
                                       encoding="utf-8").read(), "admin.py sends its maintenance warning as urgent")

print("9. Starting on the bridge")
b = types.SimpleNamespace(log=lambda tag, msg: LOGS.append(msg))
check(N.start(b, {}) is None and b.NOTICES is None and b.NOTICES_OFF == "LSB_DB_PASSWORD is not set",
      "no LSB database password: off, with the reason, no crash")
check(N.start(b, {"FFXI_NOTICES": "0", "LSB_DB_PASSWORD": "x"}) is None and b.NOTICES_OFF == "FFXI_NOTICES=0",
      "FFXI_NOTICES=0: off")
try:
    import pymysql  # noqa: F401
    have = True
except ImportError:
    have = False
if have:
    s2 = N.start(b, {"LSB_DB_PASSWORD": "x", "FFXI_NOTICES_CALENDAR": "0"})
    check(s2 is not None and b.NOTICES_SYNC is None, "FFXI_NOTICES_CALENDAR=0: the panel tool works, no sync")
else:
    print("  skip FFXI_NOTICES_CALENDAR=0 (no pymysql here)")

print()
print("RESULT: %s" % ("%d FAILURE(S)" % len(FAILED) if FAILED else "all passed"))
for f in FAILED:
    print("  - " + f)
sys.exit(1 if FAILED else 0)
