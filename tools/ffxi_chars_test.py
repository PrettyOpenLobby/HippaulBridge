#!/usr/bin/env python3
"""lsb/ffxi_chars.py and the Characters page of lsb/ffxi_fedtool.py.

Offline (always): the checks on what the page sends, the test-character rules, and the tool-port
contract on the new routes (the account window's /chars/player.json among them), over an
in-memory stand-in for the Store.

Against a real database (--db): every change on real LSB tables. Load LSB's sql/ into an empty
MariaDB first; the run adds two made-up characters (ids 900001 and 900002), checks moving,
taking equipment off, the variable edits, the logged-in refusal, the copy onto the test
character and that Undo puts every row back, then deletes what it added.

  python tools/ffxi_chars_test.py
  python tools/ffxi_chars_test.py --db HOST:PORT:USER:PASSWORD:DATABASE
  python tools/ffxi_chars_test.py --serve N    the page on 127.0.0.1:N over the stand-in
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
import ffxi_chars as C                                             # noqa: E402
import ffxi_fedtool as T                                           # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def refuses(fn, *a, contains=""):
    try:
        fn(*a)
    except C.CharError as exc:
        return contains in str(exc)
    return False


# -- the stand-in ----------------------------------------------------------------------------------

class FakeStore:
    """Two characters, a zone list and a log, with the Store's methods. change() applies nothing; it
    records what it was asked to do, and refuses for a character marked online, as the Store does."""

    def __init__(self):
        self.settings = {}
        self.chars = {
            11: {"charid": 11, "charname": "Arbor", "accid": 1, "nation": 0, "gmlevel": 0, "pos_zone": 246,
                 "pos_prevzone": 225, "pos_x": 1.0, "pos_y": 2.0, "pos_z": 3.0, "pos_rot": 4, "home_zone": 245,
                 "home_x": 5.0, "home_y": 6.0, "home_z": 7.0, "home_rot": 8, "online": 0, "mjob": 16, "mlvl": 67,
                 "sjob": 5, "slvl": 33, "race": 5, "face": 2, "size": 0, "zone_name": "Port_Jeuno"},
            12: {"charid": 12, "charname": "Birch", "accid": 2, "nation": 1, "gmlevel": 0, "pos_zone": 236,
                 "pos_prevzone": 236, "pos_x": 0.0, "pos_y": 0.0, "pos_z": 0.0, "pos_rot": 0, "home_zone": 0,
                 "home_x": 0.0, "home_y": 0.0, "home_z": 0.0, "home_rot": 0, "online": 0, "mjob": 1, "mlvl": 10,
                 "sjob": 0, "slvl": 0, "race": 8, "face": 0, "size": 2, "zone_name": "Port_Bastok"}}
        self.equip = {11: [{"equipslotid": 0, "containerid": 0, "slotid": 3, "itemId": 17000, "name": "test_sword"},
                           {"equipslotid": 2, "containerid": 0, "slotid": 4, "itemId": 17390, "name": "yew_fishing_rod"}],
                      12: []}
        self.vars = {11: [{"varname": "Quest[8][160]Prog", "value": 1, "expiry": 0},
                          {"varname": "Quest[8][160]Other", "value": 2, "expiry": 0},
                          {"varname": "[RoV]ConsoleTest", "value": 1, "expiry": 0}], 12: []}
        self.changes, self.entries = [], {}
        self.off, self.off_log = set(), []

    def setting(self, name):
        return self.settings.get(name)

    def set_setting(self, name, value):
        self.settings[name] = str(value)

    def zones(self):
        return [{"zoneid": 245, "name": "Lower_Jeuno"}, {"zoneid": 246, "name": "Port_Jeuno"}]

    def find(self, text, limit=25):
        return [c for c in self.chars.values() if c["charname"].lower().startswith(text.lower()) or str(c["charid"]) == text]

    def char(self, charid):
        c = self.chars.get(int(charid))
        return dict(c) if c else None

    def equipment(self, charid):
        return list(self.equip.get(int(charid), []))

    def variables(self, charid):
        return list(self.vars.get(int(charid), []))

    def log(self, limit=60):
        return []

    LOGINS = {1: "pol5", 2: "oldname"}

    def of_logins(self, logins, limit=64):
        return sorted((c for c in self.chars.values() if self.LOGINS.get(c["accid"]) in logins),
                      key=lambda c: c["charname"])

    def lost_contact(self, limit=50):
        return [{"id": 1, "at": 1791780000, "charid": 11, "charname": "Arbor", "console": 1, "zone": 246, "prevzone": 246,
                 "trail_zone": 246, "trail_prev": 245, "trail_at": 1791779880, "trail_csid": 324, "silent_s": 61,
                 "c2s": 900, "s2c": 950}]

    def data(self, charid):
        keys = bytearray(1024)
        keys[0], keys[5 * 128] = (0b1011, 0b1) if charid == 11 else (0b11, 0)
        items = [{"location": 0, "slot": 3, "itemId": 17000, "quantity": 1, "extra_len": 0, "extra_hex": "",
                  "name": "test_sword", "equipped": 1}]
        if charid == 11:
            items.append({"location": 5, "slot": 7, "itemId": 15000, "quantity": 1, "extra_len": 24,
                          "extra_hex": "0102" + "00" * 22, "name": "test_ring", "equipped": 0})
        return ({"keyitems": bytes(keys), "titles": b"\x0f" if charid == 11 else b"\x01"},
                {"char_vars": len(self.vars.get(charid, []))}, {"charid": charid, "inventory": 30, "satchel": 0},
                items)

    ITEMS = {880: {"itemid": 880, "name": "bone_chip", "stackSize": 12, "flags": 0, "aH": 19},
             13566: {"itemid": 13566, "name": "defending_ring", "stackSize": 1, "flags": 0x4000 | 0x8000 | 0x40, "aH": 0},
             17000: {"itemid": 17000, "name": "test_sword", "stackSize": 1, "flags": 0x8000, "aH": 1},
             65535: {"itemid": 65535, "name": "gil", "stackSize": 1, "flags": 0, "aH": 0}}

    def find_items(self, text, limit=40):
        return [i for i in self.ITEMS.values() if text.replace(" ", "_") in i["name"] or str(i["itemid"]) == text]

    def item(self, itemid):
        return self.ITEMS.get(int(itemid))

    def gil(self, charid):
        return 500

    def log_entry(self, entry_id):
        return self.entries.get(int(entry_id))

    def cs_off(self):
        return [{"zone": z, "csid": c, "at": None, "who": None} for z, c in sorted(self.off)]

    def set_cs_off(self, zone, csid, off, who, now, detail):
        (self.off.add if off else self.off.discard)((zone, csid))
        self.off_log.append((who, detail))

    def change(self, charid, steps, log):
        if self.chars[charid]["online"]:
            raise C.CharError("%s is logged in." % self.chars[charid]["charname"])
        self.changes.append((charid, steps, log))
        return len(self.changes)

    def copy_rows(self, src, table):
        return [] if table != "char_nonexistent" else None

    def char_columns(self, charid, cols):
        return {c: self.chars[charid].get(c, 0) for c in cols}

    def mark_undone(self, entry_id, who, now):
        self.entries[int(entry_id)]["undone_at"] = now


def offline():
    print("offline")
    st = FakeStore()
    ch = C.Chars(st)

    # moving
    out = ch.move({"id": 11, "home": True}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    check(out["ok"] and cid == 11 and steps[0][0] == "chars", "move home: one chars update")
    check(steps[0][1] == {"pos_zone": 245, "pos_prevzone": 245, "pos_rot": 8, "pos_x": 5.0, "pos_y": 6.0, "pos_z": 7.0},
          "move home: zone, position and facing from the home point, previous zone = the zone")
    check(refuses(ch.move, {"id": 12, "home": True}, "mod", contains="no home point"), "move home refused without one")
    ch.move({"id": 11, "zone": 246, "x": "17.3", "y": 12, "z": "-109.4", "rot": 227}, "mod", now=100)
    check(st.changes[-1][1][0][1]["pos_zone"] == 246 and st.changes[-1][1][0][1]["pos_prevzone"] == 246,
          "move to a zone: previous zone set to the same zone")
    check(refuses(ch.move, {"id": 11, "zone": 999, "x": 0, "y": 0, "z": 0}, "mod", contains="no zone"), "unknown zone refused")
    check(refuses(ch.move, {"id": 11, "zone": 246, "x": "abc", "y": 0, "z": 0}, "mod", contains="number"), "bad coordinate refused")
    check(refuses(ch.move, {"id": 11, "zone": 246, "x": 99999, "y": 0, "z": 0}, "mod", contains="between"), "far coordinate refused")
    check(refuses(ch.move, {"id": 11, "zone": 246, "x": 0, "y": 0, "z": 0, "rot": 300}, "mod", contains="Facing"), "facing over 255 refused")
    check(refuses(ch.move, {"id": 99, "home": True}, "mod", contains="no character"), "unknown character refused")
    check(refuses(ch.move, {}, "mod", contains="Which character"), "no character named")

    # equipment
    ch.unequip({"id": 11, "slot": 2}, "mod", now=100)
    steps = st.changes[-1][1]
    check(steps[0][3] == "char_equip" and steps[0][2] == (11, 2), "unequip one slot: only that slot")
    check(steps[1][3] == "char_look" and "ranged = 0" in steps[1][1], "unequip ranged: its look column cleared")
    ch.unequip({"id": 11, "all": True}, "mod", now=100)
    check(st.changes[-1][1][0][2] == (11, 0, 2), "unequip all: every worn slot")
    check(refuses(ch.unequip, {"id": 11, "slot": 4}, "mod", contains="nothing in Head"), "empty slot refused")
    check(refuses(ch.unequip, {"id": 12, "all": True}, "mod", contains="nothing equipped"), "nothing to take off refused")

    # variables
    ch.set_var({"id": 11, "name": "[RoV]ConsoleTest", "value": "0"}, "mod", now=100)
    check(st.changes[-1][1][0][2] == (11, "[RoV]ConsoleTest", 0), "set a variable")
    check(refuses(ch.set_var, {"id": 11, "name": "has space", "value": 1}, "mod", contains="no spaces"), "name with a space refused")
    check(refuses(ch.set_var, {"id": 11, "name": "x", "value": "1.5"}, "mod", contains="whole number"), "fractional value refused")
    ch.delete_vars({"id": 11, "prefix": "Quest[8][160]"}, "mod", now=100)
    check(sorted(st.changes[-1][1][0][2][1:]) == ["Quest[8][160]Other", "Quest[8][160]Prog"], "delete by prefix takes only matches")
    check(refuses(ch.delete_vars, {"id": 11, "prefix": "Nope"}, "mod", contains="starting Nope"), "prefix matching nothing refused")
    check(refuses(ch.delete_vars, {"id": 11, "name": "Nope"}, "mod", contains="no variable Nope"), "unknown variable refused")

    # logged in
    st.chars[11]["online"] = 1
    check(refuses(ch.move, {"id": 11, "home": True}, "mod", contains="logged in"), "logged-in character refused")
    st.chars[11]["online"] = 0

    # the test character
    check(refuses(ch.copy_to_test, {"id": 11}, "mod", contains="test character first"), "copy refused before a test character is chosen")
    ch.set_test({"id": 12}, "mod")
    check(st.settings["test_charid"] == "12", "test character saved")
    check(refuses(ch.copy_to_test, {"id": 12}, "mod", contains="test character itself"), "copying the test character onto itself refused")
    ch.copy_to_test({"id": 11}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    check(cid == 12, "copy writes the TEST character, never the source")
    tables = [s[1] for s in steps if s[0] == "table"]
    check(set(tables) == set(C.COPY_TABLES), "copy replaces every per-character table")
    check("charname" not in steps[0][1] and "accid" not in steps[0][1], "copy keeps the test character's name and account")
    ch.copy_to_test({"id": 11, "groups": ["story"]}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    check({s[1] for s in steps if s[0] == "table"} == {"char_vars", "char_profile", "char_unlocks", "char_points"}
          and "quests" in steps[0][1] and "pos_zone" not in steps[0][1] and "story" in log[2],
          "partial copy: only the story's tables and columns")
    ch.copy_to_test({"id": 11, "groups": ["items"]}, "mod", now=100)
    steps = st.changes[-1][1]
    check([s[1] for s in steps if s[0] == "table"] == ["char_storage", "char_inventory", "char_equip"]
          and not any(s[0] == "chars" for s in steps), "partial copy: items only, sizes before the items")
    check(refuses(ch.copy_to_test, {"id": 11, "groups": ["nope"]}, "mod", contains="Copy which parts"), "unknown part refused")
    st.chars[12]["online"] = 1
    check(refuses(ch.copy_to_test, {"id": 11}, "mod", contains="logged in"), "copy refused while the test character is logged in")
    st.chars[12]["online"] = 0
    check(ch.state()["test"]["charname"] == "Birch", "state names the test character")

    # undo
    snap = {"chars": {"pos_zone": 246}, "tables": {"char_vars": [{"charid": 11, "varname": "a", "value": 1}]}}
    st.entries[5] = {"id": 5, "charid": 11, "charname": "Arbor", "action": "move", "before_rows": C.dump_snapshot(snap),
                     "undone_at": None}
    ch.undo({"entry": 5}, "mod", now=200)
    steps = st.changes[-1][1]
    check(steps[0] == ("chars", {"pos_zone": 246}) and steps[1] == ("table", "char_vars", snap["tables"]["char_vars"]),
          "undo replays the snapshot")
    check(st.entries[5]["undone_at"] == 200, "undo marks the entry")
    check(refuses(ch.undo, {"entry": 5}, "mod", contains="already undone"), "second undo refused")

    # snapshots survive JSON with blobs and dates
    import datetime
    import decimal
    raw = {"chars": {"missions": b"\x00\x01\xff", "last": datetime.datetime(2026, 10, 12, 3, 4, 5)},
           "tables": {"t": [{"d": decimal.Decimal("1.5"), "b": b"x", "n": None}]}}
    back = C.load_snapshot(C.dump_snapshot(raw))
    check(back["chars"]["missions"] == b"\x00\x01\xff" and back["chars"]["last"] == "2026-10-12 03:04:05"
          and back["tables"]["t"][0] == {"d": decimal.Decimal("1.5"), "b": b"x", "n": None}, "snapshot round-trips blobs, dates, decimals")

    # search
    check([r["charname"] for r in ch.search({"q": ["ar"]})["rows"]] == ["Arbor"], "search by name prefix")
    check(ch.search({"q": [""]}) == {"rows": []}, "empty search finds nothing")
    check(refuses(ch.search, {"q": ["x" * 40]}, contains="longer"), "over-long search refused")

    # the data summary: packed fields decoded at LSB's layouts
    keys = bytearray(C.KEY_TABLES * C.KEY_TABLE_BYTES)
    keys[0] = 0b111                                  # table 0: 3 key items
    keys[64] = 0xFF                                  # table 0's SEEN list: not counted
    keys[5 * C.KEY_TABLE_BYTES + 1] = 0b1            # table 5: 1 key item
    quests = bytearray(C.QUEST_LOGS * C.QUEST_LOG_BYTES)
    quests[8 * 64 + 20] = 0b1                        # Abyssea: 1 current
    quests[8 * 64 + 32] = 0b11                       # Abyssea: 2 complete
    missions = bytearray(C.MISSION_LOGS * C.MISSION_LOG_BYTES)
    missions[11 * 70:11 * 70 + 2] = (3).to_bytes(2, "little")   # ASA current 3
    missions[11 * 70 + 6] = missions[11 * 70 + 7] = 1           # ASA: 2 complete
    rows = dict(C.summarize({"mjob": 16, "mlvl": 67, "sjob": 5, "slvl": 33},
                            {"keyitems": bytes(keys), "titles": b"\x03\x01", "quests": bytes(quests),
                             "missions": bytes(missions)}, {"char_vars": 4}, {"charid": 1, "inventory": 30, "safe": 10},
                            [{"location": 0, "slot": 1, "extra_len": 0}, {"location": 1, "slot": 12, "extra_len": 24}]))
    check(rows["Job"] == "BLU 67 / RDM 33", "summary: job")
    check(rows["Key items"].startswith("4 (by table: 3 0 0 0 0 1 0 0)"), "summary: key items per table, owned list only")
    check(rows["Titles"] == "3", "summary: titles counted from the bitmask")
    check(rows["Quests: Abyssea"] == "1 current, 2 complete", "summary: quest log decoded")
    check(rows["Missions: ASA"] == "current 3, 2 complete", "summary: mission log decoded")
    check(rows["Rows: char_vars"] == "4", "summary: row counts")
    check(rows["Items: safe"] == "1 held, size 10, 1 with extra data", "summary: per container")
    flags = C.item_flags({"inventory": 30, "safe": 10}, [{"location": 0, "slot": 1, "extra_len": 0},
                                                         {"location": 1, "slot": 12, "extra_len": 24},
                                                         {"location": 0, "slot": 40, "extra_len": 0}])
    check([f["slot"] for f in flags] == [12] and "24 bytes" in flags[0]["why"] and "past the container" in flags[0]["why"],
          "item flags: extra data and past the container size (inventory slots are not size-checked)")

    # cutscenes turned off for consoles
    r = ch.cs_off_set({"zone": 245, "csid": 10094, "off": True}, "mod", now=1000)
    check(r["ok"] and st.off == {(245, 10094)} and "Lower Jeuno" in r["msg"], "cutscene off: written for the zone and cutscene")
    check(" 245:10094 " in st.off_log[-1][1] and st.off_log[-1][0] == "mod", "cutscene off: logged with zone:cutscene")
    check(ch.state()["cs_off"] == [{"zone": 245, "csid": 10094, "at": None, "who": None}], "cutscene off: on the page's state")
    ch.cs_off_set({"zone": 245, "csid": 10094, "off": False}, "mod", now=1001)
    check(st.off == set() and st.off_log[-1][1].startswith("turned back on"), "cutscene back on")
    check(refuses(ch.cs_off_set, {"zone": 999, "csid": 1, "off": True}, "mod", contains="no zone"), "cutscene off: unknown zone refused")
    check(refuses(ch.cs_off_set, {"zone": 245, "csid": "x", "off": True}, "mod", contains="Which cutscene"), "cutscene off: bad number refused")
    check(refuses(ch.cs_off_set, {"zone": 245, "csid": 70000, "off": True}, "mod", contains="0 to 65534"), "cutscene off: out of range refused")

    # test scenarios (with a small stand-in for the zone line data)
    C._TESTDATA[:] = [{"zones": {"245": "lower_jeuno", "246": "port_jeuno", "126": "qufim_island"},
                       "lines": [{"from": 245, "to": 246, "line": "a", "x": -150.73, "y": -3.899, "z": -19.9, "rot": 197},
                                 {"from": 246, "to": 245, "line": "b", "x": 27.891, "y": -2.635, "z": 42.423, "rot": 149}],
                       "heavy": [{"zone": 245, "csid": 10094, "file": "f", "conds": "", "loads": [233], "reloads": 1}]}]
    st = FakeStore()
    ch = C.Chars(st)
    check(refuses(ch.arm_scenario, {"id": 12, "to": 246, "csid": 324}, "mod", contains="test character first"),
          "scenario refused before a test character is chosen")
    ch.set_test({"id": 12}, "mod")
    check(refuses(ch.arm_scenario, {"id": 11, "to": 246, "csid": 324}, "mod", contains="only run on the test character"),
          "scenario refused on any other character")
    out = ch.arm_scenario({"id": 12, "to": 246, "from": 245, "csid": 324, "wait": 30, "hide_pcs": True,
                           "prerezone": True}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    sets = {a[1][len("[Scenario]"):]: a[2] for k, sql, a, t in [s for s in steps if s[0] == "sql"] if sql.startswith("REPLACE")}
    check(out["ok"] and cid == 12, "scenario armed on the test character")
    check(sets["From"] == 245 and sets["To"] == 246 and sets["Csid"] == 324 and sets["Wait"] == 30 and sets["Flags"] == 2
          and sets["PreRezone"] == 1 and sets["Quiet"] == 1 and sets["Armed"] == 1 and sets["Stage"] == 0,
          "scenario variables as the form asked")
    check((sets["AtX"], sets["AtY"], sets["AtZ"], sets["AtRot"]) == (-150730, -3899, -19900, 197),
          "arrival spot = where Lower Jeuno's line into Port Jeuno puts a character")
    check(set(sets) == set(C.SCENARIO_VARS), "every variable the module reads is written")
    pos = [s for s in steps if s[0] == "chars"][0][1]
    check(pos["pos_zone"] == 245 and pos["pos_prevzone"] == 245 and pos["pos_x"] == 27.891,
          "the character starts in the zone it arrives from, at a known spot")
    check(steps[0][1].startswith("DELETE") and steps[0][2] == (12, C.SCENARIO_LIKE), "old scenario variables cleared first")
    ch.arm_scenario({"id": 12, "to": 246, "from": 0, "csid": 324}, "mod", now=100)
    pos = [s for s in st.changes[-1][1] if s[0] == "chars"][0][1]
    check(pos["pos_zone"] == 246 and pos["pos_prevzone"] == 246, "fresh login: starts in the destination")
    check(refuses(ch.arm_scenario, {"id": 12, "to": 246, "from": 126, "csid": 324}, "mod", contains="No zone line"),
          "a start zone with no line into the destination refused")
    check(refuses(ch.arm_scenario, {"id": 12, "to": 999, "csid": 324}, "mod", contains="no zone"), "unknown destination refused")
    check(refuses(ch.arm_scenario, {"id": 12, "to": 246, "csid": 70000}, "mod", contains="1 to 65535"), "cutscene id range")
    check(refuses(ch.arm_scenario, {"id": 12, "to": 246, "csid": 324, "wait": 1}, "mod", contains="Wait must be 3"),
          "wait too short refused")
    a = ch.arrivals({"to": ["246"]})
    check([x["from"] for x in a["from"]] == [245] and a["heavy"] == [], "arrivals into a zone")
    check(ch.arrivals({"to": ["245"]})["heavy"][0]["csid"] == 10094, "heavy cutscenes of a zone")
    st.vars[12] = [{"varname": "[Scenario]Armed", "value": 0, "expiry": 0}, {"varname": "[Scenario]Result", "value": 1, "expiry": 0}]
    check("never finished" in ch.detail({"id": ["12"]})["scenario"]["words"], "a run left at 'started' reads as never finished")
    ch.disarm_scenario({"id": 12}, "mod", now=100)
    check(st.changes[-1][1][0][2] == (12, C.SCENARIO_LIKE), "clear removes the scenario variables")
    C._TESTDATA[:] = []

    # giving items and gil (Arbor: inventory 30 with slot 3 taken, no satchel)
    st = FakeStore()
    ch = C.Chars(st)
    out = ch.add_item({"id": 11, "item": 880, "quantity": 30}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    check(out["ok"] and [s[2][2:] for s in steps] == [(1, 880, 12), (2, 880, 12), (4, 880, 6)],
          "give 30 bone chips: stacks of 12, 12 and 6 in free slots 1, 2 and 4 (3 is taken)")
    check(all(s[3] == "char_inventory" and s[2][:2] == (11, 0) for s in steps) and log[1] == "additem",
          "give: inventory rows only, logged as additem (Undo takes them back)")
    check(refuses(ch.add_item, {"id": 11, "item": 880, "quantity": 12 * 29 + 1}, "mod", contains="Give 1 to 348"),
          "give: more than the free slots hold refused")
    check(refuses(ch.add_item, {"id": 11, "item": 880, "location": 5}, "mod", contains="has no satchel"),
          "give: a bag the character lacks refused")
    check(refuses(ch.add_item, {"id": 11, "item": 880, "location": 3}, "mod", contains="only be given"),
          "give: temporary items refused")
    check(refuses(ch.add_item, {"id": 11, "item": 17000}, "mod", contains="already has test_sword"),
          "give: a rare item the character already holds refused")
    check(refuses(ch.add_item, {"id": 12, "item": 13566, "quantity": 2}, "mod", contains="rare"), "give: two of a rare item refused")
    check(ch.add_item({"id": 12, "item": 13566}, "mod", now=100)["ok"], "give: one rare item to a character without it")
    check(refuses(ch.add_item, {"id": 11, "item": 65535}, "mod", contains="no item 65535"), "give: gil is not an item here")
    check(refuses(ch.add_item, {"id": 11, "item": 999}, "mod", contains="no item 999"), "give: unknown item refused")
    check([r["itemid"] for r in ch.item_search({"q": ["bone chip"]})["rows"]] == [880], "item search by name, spaces for underscores")
    check(ch.item_search({"q": ["65535"]})["rows"] == [], "item search leaves gil out")
    out = ch.set_gil({"id": 11, "gil": "250000"}, "mod", now=100)
    cid, steps, log = st.changes[-1]
    check(out["ok"] and steps[0][2] == (11, 65535, 250000) and steps[0][1].startswith("REPLACE")
          and log[2] == "gil 500 -> 250000", "set gil: the slot 0 row, logged with the old amount")
    check(refuses(ch.set_gil, {"id": 11, "gil": 10 ** 9}, "mod", contains="0 to 999999999"), "set gil: over the cap refused")
    st.chars[11]["online"] = 1
    check(refuses(ch.add_item, {"id": 11, "item": 880}, "mod", contains="logged in"), "give: refused while logged in")
    d = ch.data({"id": ["12"]})["this"]
    check(d["gil"] == 500 and d["sizes"][0] == 30 and d["sizes"][5] == 0, "data: gil and bag sizes for the give form")

    # the tool port
    T._CHARS[:] = [C.Chars(FakeStore())]
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
        data = r.read()
        return r.status, data

    s, d = req("GET", "/chars")
    check(s == 200 and b"<title>FFXI characters</title>" in d, "GET /chars serves the page")
    check(req("GET", "/chars", token=None)[0] == 403, "the page needs the token")
    s, d = req("GET", "/chars.json")
    check(s == 200 and "jobs" in json.loads(d), "GET /chars.json")
    s, d = req("GET", "/chars/search.json?q=bi")
    check(s == 200 and json.loads(d)["rows"][0]["charname"] == "Birch", "GET /chars/search.json")
    s, d = req("GET", "/chars/char.json?id=11")
    check(s == 200 and json.loads(d)["char"]["charname"] == "Arbor", "GET /chars/char.json")
    s, d = req("GET", "/chars/char.json?id=99")
    check(s == 400 and "no character" in json.loads(d)["error"], "unknown character is a 400 with the reason")
    s, d = req("POST", "/chars/move", {"id": 11, "home": True})
    check(s == 200 and json.loads(d)["ok"], "POST /chars/move")
    s, d = req("POST", "/chars/move", {"id": 11, "zone": 1})
    check(s == 200 and not json.loads(d)["ok"], "a refused change comes back ok: false with the reason")
    check(req("POST", "/chars/move", {"id": 11, "home": True}, token="bad")[0] == 403, "changes need the token")
    s, d = req("GET", "/chars/items.json?q=bone")
    check(s == 200 and json.loads(d)["rows"][0]["itemid"] == 880, "GET /chars/items.json")
    s, d = req("POST", "/chars/additem", {"id": 11, "item": 880, "quantity": 3})
    check(s == 200 and json.loads(d)["ok"], "POST /chars/additem")
    s, d = req("POST", "/chars/gil", {"id": 11, "gil": 5})
    check(s == 200 and json.loads(d)["ok"], "POST /chars/gil")
    srv.shutdown()

    # one player, for the panel's account window
    st = FakeStore()
    st.chars[12]["online"] = 1
    got = C.Chars(st).player(["pol5"])
    arbor = (got.get("characters") or [{}])[0]
    check([c["name"] for c in got.get("characters", [])] == ["Arbor"]
          and arbor["detail"] == "BLU 67/RDM 33, Port Jeuno" and arbor["online"] is False
          and {f["label"]: f["value"] for f in arbor["facts"]} == {"main job": "BLU 67", "support job": "RDM 33",
                                                                  "zone": "Port Jeuno", "nation": "San d'Oria"},
          "player: a character's job, zone, nation and whether it is logged in")
    check(C.Chars(st).player(["nobody"]) == {}, "player: an account with no characters is nothing")
    bridge = types.SimpleNamespace(log=lambda *a: None, FED=None, _acctmap_lock=threading.Lock(),
                                   _acctmap={"6": {"login": "oldname"}, "alt:5": {"login": "elsewhere"}})
    check(T.player_logins(bridge, [5, 6]) == ["pol5", "oldname", "pol6"],
          "player: a member's LSB accounts are the bridge's map and pol<member>, the primary world only")
    T._CHARS[:] = [C.Chars(st)]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(T.Tool(bridge, environ={}), "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    s, d = req("GET", "/chars/player.json?members=5%2C6")
    doc = json.loads(d)
    check(s == 200 and [(c["name"], c["online"]) for c in doc["characters"]] == [("Arbor", False), ("Birch", True)],
          "GET /chars/player.json: every character of the members, the logged-in one online")
    check(json.loads(req("GET", "/chars/player.json?members=8")[1]) == {}, "GET /chars/player.json: a member with none is {}")
    check(json.loads(req("GET", "/chars/player.json?members=x")[1]) == {}, "GET /chars/player.json: no member id, nothing")
    check(req("GET", "/chars/player.json?members=5", token="bad")[0] == 403, "GET /chars/player.json needs the token")
    text = json.dumps(doc)
    check(not any(k in text for k in ("accid", "charid", "pol5", "oldname", "client_addr", "session", "pos_x")),
          "GET /chars/player.json: no account names, ids, addresses or positions")
    srv.shutdown()


# -- against a real database -------------------------------------------------------------------------

A, B = 900001, 900002


def real(spec):
    print("real database")
    host, port, user, password, database = spec.split(":", 4)
    store = C.Store(host, port, user, password, database)
    ch = C.Chars(store)
    run = store._run

    def cleanup():
        for t in ("chars", "accounts_sessions") + C.COPY_TABLES:
            try:
                run("DELETE FROM %s WHERE charid IN (%%s, %%s)" % t, (A, B))
            except Exception:
                pass

    cleanup()
    run("INSERT INTO chars (charid, accid, charname, pos_zone, pos_prevzone, pos_x, pos_y, pos_z, pos_rot, home_zone, "
        "home_x, home_y, home_z, home_rot, missions, quests) VALUES (%s, 1, 'Arbor', 246, 225, 1, 2, 3, 4, 245, 5, 6, 7, 8, "
        "%s, %s)", (A, b"\x01\x02\x03", b"\x04\x05"))
    run("INSERT INTO chars (charid, accid, charname, pos_zone, pos_prevzone) VALUES (%s, 2, 'Birch', 236, 236)", (B,))
    for cid, race in ((A, 5), (B, 8)):
        run("INSERT INTO char_look (charid, race, face, size, ranged) VALUES (%s, %s, 2, 0, 9)", (cid, race))
        run("INSERT INTO char_stats (charid, mjob, mlvl) VALUES (%s, 16, 67)", (cid,))
    run("INSERT INTO char_inventory (charid, location, slot, itemId, quantity) VALUES (%s, 0, 4, 17390, 1)", (A,))
    run("INSERT INTO char_inventory (charid, location, slot, itemId, quantity) VALUES (%s, 0, 5, 12364, 1)", (A,))
    run("INSERT INTO char_equip (charid, slotid, equipslotid, containerid) VALUES (%s, 4, 2, 0), (%s, 5, 1, 0)", (A, A))
    run("INSERT INTO char_vars (charid, varname, value) VALUES (%s, 'Quest[8][160]Prog', 1), (%s, 'Keep', 7)", (A, A))
    run("INSERT INTO char_inventory (charid, location, slot, itemId, quantity) VALUES (%s, 0, 1, 16534, 1)", (B,))
    run("INSERT INTO char_vars (charid, varname, value) VALUES (%s, 'BirchOnly', 3)", (B,))

    def one(sql, args):
        rows = run(sql, args, fetch=True)
        return rows[0] if rows else None

    try:
        check([r["charid"] for r in store.find("Arbo")] == [A], "find by name")
        d = ch.detail({"id": [str(A)]})
        check([e["equipslotid"] for e in d["equipment"]] == [1, 2] and d["equipment"][1]["itemId"] == 17390,
              "detail lists the equipment with its items")

        e1 = ch.move({"id": A, "home": True}, "mod")["entry"]
        c = one("SELECT pos_zone, pos_prevzone, pos_x, pos_rot FROM chars WHERE charid = %s", (A,))
        check((c["pos_zone"], c["pos_prevzone"], c["pos_x"], c["pos_rot"]) == (245, 245, 5.0, 8), "move home written")

        e2 = ch.unequip({"id": A, "slot": 2}, "mod")["entry"]
        check(not one("SELECT 1 AS x FROM char_equip WHERE charid = %s AND equipslotid = 2", (A,))
              and one("SELECT ranged FROM char_look WHERE charid = %s", (A,))["ranged"] == 0
              and one("SELECT 1 AS x FROM char_inventory WHERE charid = %s AND itemId = 17390", (A,)),
              "unequip: slot cleared, look cleared, item still in the bag")

        e3 = ch.delete_vars({"id": A, "prefix": "Quest[8][160]"}, "mod")["entry"]
        check([v["varname"] for v in store.variables(A)] == ["Keep"], "delete by prefix")

        run("INSERT INTO accounts_sessions (accid, charid, targid, session_key, server_addr, server_port, client_addr, "
            "client_port, version_mismatch) VALUES (1, %s, 0, '', 0, 0, 0, 0, 0)", (A,))
        check(refuses(ch.set_var, {"id": A, "name": "x", "value": 1}, "mod", contains="logged in"), "logged in: refused")
        check(not one("SELECT 1 AS x FROM char_vars WHERE charid = %s AND varname = 'x'", (A,)), "logged in: nothing written")
        run("DELETE FROM accounts_sessions WHERE charid = %s", (A,))

        ch.undo({"entry": e3}, "mod")
        check(sorted(v["varname"] for v in store.variables(A)) == ["Keep", "Quest[8][160]Prog"], "undo puts the variables back")
        ch.undo({"entry": e2}, "mod")
        check(one("SELECT 1 AS x FROM char_equip WHERE charid = %s AND equipslotid = 2", (A,))
              and one("SELECT ranged FROM char_look WHERE charid = %s", (A,))["ranged"] == 9, "undo puts the rod back on")
        ch.undo({"entry": e1}, "mod")
        c = one("SELECT pos_zone, pos_prevzone, pos_x FROM chars WHERE charid = %s", (A,))
        check((c["pos_zone"], c["pos_prevzone"], c["pos_x"]) == (246, 225, 1.0), "undo puts the position back")

        ch.set_test({"id": B}, "mod")
        e4 = ch.copy_to_test({"id": A}, "mod")["entry"]
        b = one("SELECT charname, accid, pos_zone, missions, quests FROM chars WHERE charid = %s", (B,))
        check((b["charname"], b["accid"], b["pos_zone"]) == ("Birch", 2, 246) and bytes(b["missions"]) == b"\x01\x02\x03",
              "copy: position and story copied, name and account kept")
        check(sorted(r["itemId"] for r in run("SELECT itemId FROM char_inventory WHERE charid = %s", (B,), fetch=True))
              == [12364, 17390], "copy: the source's items replace the test character's")
        check(one("SELECT race FROM char_look WHERE charid = %s", (B,))["race"] == 5, "copy: look copied")
        check([v["varname"] for v in store.variables(B)] == ["Keep", "Quest[8][160]Prog"], "copy: variables copied")
        check(sorted(r["varname"] for r in store.variables(A)) == ["Keep", "Quest[8][160]Prog"], "copy: the source is untouched")

        ch.undo({"entry": e4}, "mod")
        b = one("SELECT pos_zone, missions FROM chars WHERE charid = %s", (B,))
        check(b["pos_zone"] == 236 and b["missions"] in (None, b"") and
              [r["itemId"] for r in run("SELECT itemId FROM char_inventory WHERE charid = %s", (B,), fetch=True)] == [16534]
              and [v["varname"] for v in store.variables(B)] == ["BirchOnly"]
              and one("SELECT race FROM char_look WHERE charid = %s", (B,))["race"] == 8,
              "undo of the copy restores the test character exactly")
        check(refuses(ch.undo, {"entry": e4}, "mod", contains="already undone"), "second undo refused")
        # the data summary and the item tools, on real tables
        run("UPDATE chars SET keyitems = %s WHERE charid = %s", (b"\x07" + bytes(1023), A))
        run("UPDATE char_inventory SET extra = %s WHERE charid = %s AND slot = 5", (b"\x01\x02" + bytes(22), A))
        d = ch.data({"id": [str(A)]})
        rows = dict(d["this"]["rows"])
        check(rows["Key items"].startswith("3 ") and d["test"]["charname"] == "Birch", "data: decoded, beside the test character")
        check([(f["slot"], "24 bytes" in f["why"]) for f in d["this"]["flags"]] == [(5, True)], "data: the item with extra data flagged")
        e6 = ch.remove_item({"id": A, "location": 0, "slot": 5}, "mod")["entry"]
        check(not one("SELECT 1 AS x FROM char_inventory WHERE charid = %s AND slot = 5", (A,))
              and not one("SELECT 1 AS x FROM char_equip WHERE charid = %s AND slotid = 5", (A,)), "remove item: bag and worn rows gone")
        ch.undo({"entry": e6}, "mod")
        it = one("SELECT itemId, extra FROM char_inventory WHERE charid = %s AND slot = 5", (A,))
        check(it and it["itemId"] == 12364 and bytes(it["extra"])[:2] == b"\x01\x02"
              and one("SELECT 1 AS x FROM char_equip WHERE charid = %s AND slotid = 5", (A,)), "undo puts the item back, worn, with its extra data")
        e7 = ch.copy_to_test({"id": A, "groups": ["story"]}, "mod")["entry"]
        b = one("SELECT pos_zone, keyitems FROM chars WHERE charid = %s", (B,))
        check(b["pos_zone"] == 236 and bytes(b["keyitems"])[:1] == b"\x07"
              and [r["itemId"] for r in run("SELECT itemId FROM char_inventory WHERE charid = %s", (B,), fetch=True)] == [16534],
              "partial copy of the story: key items copied, position and items untouched")
        ch.undo({"entry": e7}, "mod")
        check(bytes(one("SELECT keyitems FROM chars WHERE charid = %s", (B,))["keyitems"] or b"")[:1] != b"\x07",
              "undo of the partial copy")
        # emptying for a test: every bag and what is worn, and the key items; gil stays; undo brings it all back
        run("INSERT INTO char_inventory (charid, location, slot, itemId, quantity) VALUES (%s, 0, 0, 65535, 1234)", (A,))
        before = run("SELECT location, slot, itemId FROM char_inventory WHERE charid = %s ORDER BY location, slot", (A,), fetch=True)
        worn = run("SELECT equipslotid FROM char_equip WHERE charid = %s", (A,), fetch=True)
        e8 = ch.empty({"id": A, "parts": ["items", "keyitems"]}, "mod")["entry"]
        left = run("SELECT itemId FROM char_inventory WHERE charid = %s", (A,), fetch=True)
        check([r["itemId"] for r in left] == [65535] and not run("SELECT 1 FROM char_equip WHERE charid = %s", (A,), fetch=True)
              and not any(bytes(one("SELECT keyitems FROM chars WHERE charid = %s", (A,))["keyitems"]))
              and one("SELECT body FROM char_look WHERE charid = %s", (A,))["body"] == 0,
              "empty: only gil left, nothing worn, no key items, bare look")
        check(refuses(ch.empty, {"id": A, "parts": ["items", "keyitems"]}, "mod", contains="nothing to empty"),
              "empty: refused when there is nothing left")
        check(refuses(ch.empty, {"id": A, "parts": ["spells"]}, "mod", contains="Empty what"), "empty: unknown part refused")
        ch.undo({"entry": e8}, "mod")
        check(run("SELECT location, slot, itemId FROM char_inventory WHERE charid = %s ORDER BY location, slot", (A,), fetch=True) == before
              and run("SELECT equipslotid FROM char_equip WHERE charid = %s", (A,), fetch=True) == worn
              and bytes(one("SELECT keyitems FROM chars WHERE charid = %s", (A,))["keyitems"])[:1] == b"",
              "undo of the emptying: items, worn gear and key items back")
        run("DELETE FROM char_inventory WHERE charid = %s AND itemId = 65535", (A,))

        # lost contact: the bridge's record, read back by the page
        check(store.lost_contact() == [], "lost contact: empty before the bridge's first row")
        sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
        import pymysql
        import ffxi_bridge as BR
        gone, st = "no accounts_sessions row", [10, 0, 12, 0, 1000.0, 1030.0]
        check(BR.lost_contact_silence("ab12", gone, st, 1061.0) == 61.0, "watcher: row gone after 61 s of client silence = lost")
        check(BR.lost_contact_silence("ab12", gone, st, 1003.0) is None, "watcher: row gone 3 s after the last datagram = logout")
        check(BR.lost_contact_silence(None, gone, st, 1061.0) is None, "watcher: no row from the start is not a loss")
        check(BR.lost_contact_silence(gone, gone, st, 1061.0) is None, "watcher: an absent row is reported once")
        check(BR.lost_contact_silence("query failed: x", gone, st, 1061.0) is None, "watcher: after a failed read, no guess")
        check(BR.lost_contact_silence("ab12", "cd34", st, 1061.0) is None, "watcher: a changed key is not a loss")
        conn = pymysql.connect(host=host, port=int(port), user=user, password=password, database=database, autocommit=True)
        for k, v in (("Console", 1), ("Zone", 246), ("Prev", 245), ("At", 1791779880), ("Csid", 324)):
            run("REPLACE INTO char_vars (charid, varname, value) VALUES (%s, %s, %s)", (A, "[Trail]" + k, v))
        BR.record_lost_contact(conn, A, 61.4, [900, 0, 950, 0, 0.0, 0.0], now=1791780000)
        conn.close()
        lost = store.lost_contact()
        check(len(lost) == 1 and lost[0]["charname"] == "Arbor" and lost[0]["console"] == 1 and lost[0]["trail_csid"] == 324
              and lost[0]["trail_prev"] == 245 and lost[0]["silent_s"] == 61 and lost[0]["c2s"] == 900,
              "lost contact: the bridge writes who, where, the arrival cutscene and the silence")
        check(ch.state()["lost"][0]["charid"] == A, "lost contact: on the page's state")
        run("DELETE FROM char_vars WHERE charid = %s AND varname LIKE %s", (A, "[Trail]%"))
        run("DELETE FROM openlobby_crash_log WHERE charid = %s", (A,))

        # cutscenes turned off for consoles: server_variables rows the map reads on each arrival
        run("DELETE FROM server_variables WHERE name LIKE %s", (C.CS_OFF_PREFIX + "%",))
        ch.cs_off_set({"zone": 245, "csid": 10094, "off": True}, "mod", now=1791780100)
        got = run("SELECT name, value FROM server_variables WHERE name LIKE %s", (C.CS_OFF_PREFIX + "%",), fetch=True)
        check([(g["name"], g["value"]) for g in got] == [("[ConsoleCsOff]245:10094", 1)], "cutscene off: the server variable the map reads")
        off = store.cs_off()
        check(off == [{"zone": 245, "csid": 10094, "at": 1791780100, "who": "mod"}], "cutscene off: listed with when and who")
        ch.cs_off_set({"zone": 245, "csid": 10094, "off": False}, "mod", now=1791780200)
        check(not run("SELECT 1 FROM server_variables WHERE name LIKE %s", (C.CS_OFF_PREFIX + "%",), fetch=True)
              and store.cs_off() == [], "cutscene back on: the variable is gone")
        run("DELETE FROM openlobby_chartool_log WHERE charid = 0 AND action IN ('csoff', 'cson')")

        # a scenario on the test character, with the real zone line data
        e5 = ch.arm_scenario({"id": B, "to": 246, "from": 245, "csid": 324, "hide_npcs": True}, "mod")["entry"]
        got = {v["varname"]: v["value"] for v in store.variables(B) if v["varname"].startswith("[Scenario]")}
        b = one("SELECT pos_zone, pos_prevzone FROM chars WHERE charid = %s", (B,))
        check(len(got) == len(C.SCENARIO_VARS) and got["[Scenario]Flags"] == 0x10 and got["[Scenario]To"] == 246
              and (b["pos_zone"], b["pos_prevzone"]) == (245, 245), "scenario armed: variables and start position written")
        ch.disarm_scenario({"id": B}, "mod")
        check(not any(v["varname"].startswith("[Scenario]") for v in store.variables(B)), "scenario cleared")
        ch.undo({"entry": e5}, "mod")
        check([v["varname"] for v in store.variables(B)] == ["BirchOnly"]
              and one("SELECT pos_zone FROM chars WHERE charid = %s", (B,))["pos_zone"] == 236, "undo of the arming")

        log = [e for e in store.log() if e["charid"] in (A, B)]
        check([e["action"] for e in reversed(log)] == ["move", "unequip", "delvar", "undo", "undo", "undo", "copy", "undo",
                                                       "rmitem", "undo", "copy", "undo", "empty", "undo", "scenario", "scenario",
                                                       "undo"],
              "everything logged, in order")
        check(sorted(e["id"] for e in log if e["undone_at"]) == sorted([e1, e2, e3, e4, e6, e7, e8, e5]),
              "the undone entries are marked")

        # giving items and gil, on LSB's real item_basic
        run("DELETE FROM char_storage WHERE charid = %s", (A,))
        check(refuses(ch.add_item, {"id": A, "item": 880}, "mod", contains="has no inventory"),
              "give: a character with no char_storage row has no bags")
        run("INSERT INTO char_storage (charid) VALUES (%s)", (A,))
        before = run("SELECT location, slot, itemId, quantity FROM char_inventory WHERE charid = %s ORDER BY location, slot",
                     (A,), fetch=True)
        e9 = ch.add_item({"id": A, "item": 880, "quantity": 25}, "mod")["entry"]
        got = run("SELECT slot, itemId, quantity FROM char_inventory WHERE charid = %s AND itemId = 880 ORDER BY slot",
                  (A,), fetch=True)
        check([(g["slot"], g["quantity"]) for g in got] == [(1, 12), (2, 12), (3, 1)], "give: bone chips in stacks of 12 in free slots")
        check(refuses(ch.add_item, {"id": A, "item": 13566, "quantity": 2}, "mod", contains="rare"), "give: rare item twice refused")
        e10 = ch.add_item({"id": A, "item": 13566, "location": 1}, "mod")["entry"]
        check(one("SELECT slot FROM char_inventory WHERE charid = %s AND location = 1 AND itemId = 13566", (A,))["slot"] == 1,
              "give: a rare item into the mog safe")
        check(refuses(ch.add_item, {"id": A, "item": 13566}, "mod", contains="already has"), "give: second copy of a rare item refused")
        e11 = ch.set_gil({"id": A, "gil": 123456}, "mod")["entry"]
        check(store.gil(A) == 123456 and ch.data({"id": [str(A)]})["this"]["gil"] == 123456, "set gil: written and read back")
        for e in (e11, e10, e9):
            ch.undo({"entry": e}, "mod")
        check(run("SELECT location, slot, itemId, quantity FROM char_inventory WHERE charid = %s ORDER BY location, slot",
                  (A,), fetch=True) == before and store.gil(A) == 0, "undo of the gifts and the gil: the bags as they were")
    finally:
        cleanup()
        run("DELETE FROM openlobby_chartool_log WHERE charid IN (%s, %s)", (A, B))
        run("DELETE FROM openlobby_chartool_settings WHERE name = 'test_charid'")


def serve(port):
    T._CHARS[:] = [C.Chars(FakeStore())]
    tool = T.Tool(types.SimpleNamespace(log=lambda *a: None, FED=None), environ={})
    srv = ThreadingHTTPServer(("127.0.0.1", port), T.make_handler(tool, ""))
    print("http://127.0.0.1:%d/chars" % port)
    srv.serve_forever()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve(int(sys.argv[sys.argv.index("--serve") + 1]))
    offline()
    if "--db" in sys.argv:
        real(sys.argv[sys.argv.index("--db") + 1])
    print("%d failed" % len(FAILED) if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)
