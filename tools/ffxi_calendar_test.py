#!/usr/bin/env python3
"""lsb/ffxi_calendar.py, offline: the rows of openlobby_game_calendar (what the map
module's calendar writes) and openlobby_bonanza_runs become the event list the core's
services/ffxinews.py reads from the live-state store. No database: the store and the kv are
stand-ins, the row shapes are the tables' own.
"""
import calendar
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_calendar as C                                          # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def utc(*a):
    return calendar.timegm(a + (0,) * (6 - len(a)))


CAL_ROWS = [
    {"source": "campaigns", "event_key": "202610101500-202610311500", "name": "Adventurer Campaigns",
     "starts_at": utc(2026, 10, 10, 15), "ends_at": utc(2026, 10, 31, 15),
     "detail": json.dumps({"campaigns": ["Mog Garden Campaign", "Alter Ego Expo"], "plus": False})},
    {"source": "seasonal", "event_key": "harvest-festival-20261020", "name": "Harvest Festival",
     "starts_at": utc(2026, 10, 19, 15), "ends_at": utc(2026, 11, 1, 15),
     "detail": '{"blurb":"Trade a sweet to the townsfolk in San d\'Oria.","openEnded":false}'},
    {"source": "login", "event_key": "2026-10", "name": "Repeat Login Campaign",
     "starts_at": utc(2026, 10, 10, 15), "ends_at": utc(2026, 11, 9, 14), "detail": "not json"},
    {"source": "somethingelse", "event_key": "x", "name": "X", "starts_at": 1, "ends_at": 2, "detail": "{}"},
]
BONANZA_ROWS = [
    {"run_key": "v13-2027", "name": "13th Vana'versary Mog Bonanza", "sales_start": utc(2027, 5, 1, 15),
     "sales_end": utc(2027, 5, 31, 15), "collect_start": utc(2027, 6, 7, 15), "collect_end": utc(2027, 6, 30, 15),
     "price": 2000, "w1": 12345, "w2": 678, "w3": 90, "w4": 1, "w5": 2, "drawn_at": utc(2027, 6, 1)},
    {"run_key": "ny-2027", "name": "", "sales_start": utc(2026, 12, 20), "sales_end": utc(2027, 1, 10),
     "collect_start": utc(2027, 1, 15), "collect_end": utc(2027, 1, 31), "price": None,
     "w1": None, "w2": None, "w3": None, "w4": None, "w5": None, "drawn_at": None},
]


def main():
    print("rows -> events")
    evs = C.events(CAL_ROWS, BONANZA_ROWS)
    check([e["source"] for e in evs] == ["campaigns", "login", "seasonal", "bonanza", "bonanza"],
          "every known source, in start order; an unknown source is left out")
    camp = evs[0]
    check(camp["key"] == "202610101500-202610311500" and camp["detail"]["campaigns"][1] == "Alter Ego Expo"
          and camp["start"] == utc(2026, 10, 10, 15), "a campaign window keeps its key, times and detail")
    check(evs[1]["detail"] == {}, "detail that is not JSON reads as empty")
    check("San d'Oria" in evs[2]["detail"]["blurb"], "a festival's line survives the round trip")
    ny = [e for e in evs if e["key"] == "ny-2027"][0]
    check(ny["name"] == "Mog Bonanza" and ny["detail"]["numbers"] is None and ny["detail"]["drawnAt"] is None
          and ny["detail"]["price"] == 0, "an undrawn Bonanza run: no numbers, a default name")
    v13 = [e for e in evs if e["key"] == "v13-2027"][0]
    check(v13["detail"]["numbers"] == [12345, 678, 90, 1, 2] and v13["end"] == utc(2027, 6, 30, 15)
          and v13["detail"]["salesEnd"] == utc(2027, 5, 31, 15), "a drawn run: its five numbers and its windows")

    print("the feed")

    class Store:
        def __init__(self):
            self.asked = []

        def read(self, now):
            self.asked.append(now)
            return CAL_ROWS, BONANZA_ROWS

    class KV:
        def __init__(self):
            self.v = {}

        def set_json(self, key, value, ttl=None):
            self.v[key] = (json.loads(json.dumps(value)), ttl)

    store, kv, said = Store(), KV(), []
    feed = C.Feed(store, kv, {"FFXI_CALENDAR_PERIOD": "30"}, said.append)
    feed.once(utc(2026, 10, 15))
    value, ttl = kv.v.get(C.FFXI_CALENDAR_KEY, (None, None))
    check(value and value["at"] == utc(2026, 10, 15) and len(value["events"]) == 5,
          "one pass writes the list under ffxi:calendar")
    check(ttl == 6 * 3600, "...kept six hours, so a stopped bridge leaves no calendar for long")
    check(len(said) == 1 and "5 event" in said[0], "the count is logged when it changes")
    feed.once(utc(2026, 10, 15, 0, 1))
    check(len(said) == 1, "...and not again when it does not")

    import openlobby_paths
    services = openlobby_paths.services_dir()
    if services:
        sys.path.insert(0, services)
        import ffxinews
        got = ffxinews.load(utc(2026, 10, 15, 0, 2), types.SimpleNamespace(
            get_json=lambda k: kv.v[k][0] if k in kv.v else None))
        check([e["key"] for e in got] == [e["key"] for e in value["events"]],
              "OpenLobby's ffxinews.load reads exactly what the feed wrote")
    else:
        print("  skip ffxinews.load (OpenLobby's services/ not beside this checkout)")

    print("a table the map has not made yet")

    class Cur:
        def __init__(self, fail):
            self.fail = fail

        def execute(self, sql, args):
            if self.fail:
                raise self.fail

        def fetchall(self):
            return [{"ok": 1}]

    s = C.Store("h", 1, "u", "p", "d")
    check(s._rows(Cur(Exception(1146, "Table 'xidb.openlobby_game_calendar' doesn't exist")), "q", ()) == [],
          "1146 reads as no rows")
    try:
        s._rows(Cur(Exception(2003, "Can't connect")), "q", ())
        check(False, "any other error is raised")
    except Exception:                                # noqa: BLE001
        check(True, "any other error is raised")

    print("the Bonanza's announce column")

    class Db:
        """A cursor over the two tables; the announce column appears once its ALTER runs."""

        def __init__(self, has_column):
            self.has_column, self.ran = has_column, []

        def execute(self, sql, args=()):
            self.ran.append(sql)
            if sql.startswith("ALTER"):
                self.has_column = True
                self.last = []
            elif "openlobby_game_calendar" in sql:
                self.last = CAL_ROWS[:1]
            elif "announce = 1" in sql and not self.has_column:
                raise Exception(1054, "Unknown column 'announce' in 'where clause'")
            else:
                self.last = BONANZA_ROWS[:1]

        def fetchall(self):
            return self.last

    db = Db(False)
    cal, bon = s.read_with(db, utc(2026, 10, 15))
    check(len(cal) == 1 and len(bon) == 1 and sum(q.startswith("ALTER") for q in db.ran) == 2,
          "no announce column yet: the feed adds it (0 for old runs, default 1) and reads again")
    check(all("announce = 1" in q for q in db.ran if "openlobby_bonanza_runs" in q and q.startswith("SELECT")),
          "only announced runs are read")
    db = Db(True)
    s.read_with(db, utc(2026, 10, 15))
    check(not any(q.startswith("ALTER") for q in db.ran), "with the column there, no ALTER")

    print("start")
    logs = []
    bridge = types.SimpleNamespace(log=lambda tag, m: logs.append(m))
    check(C.start(bridge, environ={"FFXI_CALENDAR": "0"}) is None and "OFF" in logs[-1], "FFXI_CALENDAR=0")
    check(C.start(bridge, environ={}) is None and "LSB_DB_PASSWORD" in logs[-1], "no database password: off")

    print()
    if FAILED:
        print("%d FAILED" % len(FAILED))
        return 1
    print("all game calendar feed checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
