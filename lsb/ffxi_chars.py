"""FFXI characters for the admin panel (Games > FFXI > Characters), served by ffxi_fedtool.py.

For unsticking a player and for reproducing a player's problem on a test character. It reads
and writes the LSB database directly:

  - find a character by name or id and see where it is (zone, position, home point), its jobs,
    its look, what it has equipped and its character variables;
  - move it to its home point or to a zone and position, take off one piece of equipment or
    all of it, set or delete a character variable, or delete every variable with a prefix
    (a quest's or mission's progress, e.g. Quest[8][160]);
  - empty a character for a test: every bag and what it wears (gil stays), its key items, or
    both, so a problem can be tried without them and Undo brings them back;
  - give a character items for a test, into free slots of a bag it has, split into stacks, and
    set its gil;
  - turn an arrival cutscene off for consoles, and back on: a PS2 or Xbox player arriving to it is let
    through as if they had watched it and told to watch it from a Melody Minstrel (the server variable
    [ConsoleCsOff]<zone>:<csid>, read by a map-server module that stock LandSandBoat does not include);
  - copy a character onto the test character: look, jobs and levels, skills, spells, items and
    what is equipped, position and home point, missions, quests, key items, titles and
    character variables, so its situation can be played on an emulator. The test character is
    chosen on the page and kept in openlobby_chartool_settings.

Every change refuses while the character it writes to is logged in (an accounts_sessions row),
because the map server holds the character in memory and would write its own copy back. Every
change records the rows it replaces in openlobby_chartool_log first, and Undo puts those rows
back. Undo does not merge: anything changed in those tables after the entry is overwritten.

  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for the notices tool
"""
import base64
import datetime
import decimal
import json
import os
import re
import time

#: char_equip.equipslotid -> the name the page shows
SLOTS = {0: "Main", 1: "Sub", 2: "Ranged", 3: "Ammo", 4: "Head", 5: "Body", 6: "Hands", 7: "Legs",
         8: "Feet", 9: "Neck", 10: "Waist", 11: "Ear 1", 12: "Ear 2", 13: "Ring 1", 14: "Ring 2", 15: "Back"}
#: equip slots that show on the model -> their char_look column
LOOK_COLUMNS = {0: "main", 1: "sub", 2: "ranged", 4: "head", 5: "body", 6: "hands", 7: "legs", 8: "feet"}
JOBS = ["", "WAR", "MNK", "WHM", "BLM", "RDM", "THF", "PLD", "DRK", "BST", "BRD", "RNG", "SAM", "NIN",
        "DRG", "SMN", "BLU", "COR", "PUP", "DNC", "SCH", "GEO", "RUN"]

#: The chars columns a copy takes. Name, account, GM level, play time and creation stay the test character's.
COPY_CHAR_COLUMNS = ("nation", "pos_zone", "pos_prevzone", "pos_rot", "pos_x", "pos_y", "pos_z", "moghouse", "boundary",
                     "home_zone", "home_rot", "home_x", "home_y", "home_z", "missions", "assault", "campaign",
                     "eminence", "quests", "keyitems", "set_blue_spells", "abilities", "weaponskills", "titles",
                     "zones", "unlocked_weapons", "job_master", "campaign_allegiance", "isstylelocked", "settings",
                     "moghancement", "languages", "mentor")
#: Per-character tables (charid rows) a copy replaces wholesale. A table this database lacks is skipped.
COPY_TABLES = ("char_look", "char_stats", "char_jobs", "char_exp", "char_skills", "char_spells", "char_profile",
               "char_vars", "char_storage", "char_inventory", "char_equip", "char_unlocks", "char_merit",
               "char_job_points", "char_points", "char_effects", "char_style")

#: What "Copy onto the test character" can take, part by part. Together they are COPY_CHAR_COLUMNS and COPY_TABLES.
COPY_GROUPS = {
    "items": {"label": "Items and gear", "tables": ("char_storage", "char_inventory", "char_equip"), "cols": ()},
    "jobs": {"label": "Jobs and abilities", "tables": ("char_stats", "char_jobs", "char_exp", "char_skills", "char_spells",
                                                      "char_merit", "char_job_points"),
             "cols": ("abilities", "weaponskills", "set_blue_spells", "unlocked_weapons", "job_master")},
    "look": {"label": "Look", "tables": ("char_look", "char_style"), "cols": ("isstylelocked",)},
    "story": {"label": "Story", "tables": ("char_vars", "char_profile", "char_unlocks", "char_points"),
              "cols": ("missions", "assault", "campaign", "eminence", "quests", "keyitems", "titles", "zones",
                       "campaign_allegiance", "moghancement")},
    "place": {"label": "Position", "tables": (), "cols": ("pos_zone", "pos_prevzone", "pos_rot", "pos_x", "pos_y", "pos_z",
                                                          "home_zone", "home_rot", "home_x", "home_y", "home_z",
                                                          "nation", "moghouse", "boundary")},
    "other": {"label": "Other", "tables": ("char_effects",), "cols": ("settings", "languages", "mentor")},
}

#: chars.nation -> its name
NATIONS = ["San d'Oria", "Bastok", "Windurst"]
#: char_inventory.location -> its char_storage column (LSB CONTAINER_ID order)
CONTAINERS = {0: "inventory", 1: "safe", 2: "storage", 3: "tempitems", 4: "locker", 5: "satchel", 6: "sack", 7: "case",
              8: "wardrobe", 9: "safe2", 10: "wardrobe2", 11: "wardrobe3", 12: "wardrobe4", 13: "wardrobe5",
              14: "wardrobe6", 15: "wardrobe7", 16: "wardrobe8", 17: "recycle"}
#: Containers items can be given into: those with a size in char_storage (LSB sizes Storage from
#: its furnishings and Temporary items from the zone, and Recycle is the game's own)
GIVE_CONTAINERS = (0, 1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16)
#: item_basic.flags, LSB src/map/enums/item_flag.h
FLAG_NO_AUCTION, FLAG_EXCLUSIVE, FLAG_RARE = 0x40, 0x4000, 0x8000
GIL_ITEM, MAX_GIL = 65535, 999999999
#: blob layouts, LSB src/common/mmo.h and char_entity.h
KEY_TABLES, KEY_TABLE_BYTES = 8, 128   # 512-bit keyList then 512-bit seenList per table
QUEST_LOGS, QUEST_LOG_BYTES = 11, 64   # current[32] then complete[32]
MISSION_LOGS, MISSION_LOG_BYTES = 15, 70  # u16 current, u16 statusUpper, u16 statusLower, bool complete[64]
MISSION_LOG_NAMES = ("San d'Oria", "Bastok", "Windurst", "Zilart", "ToAU", "WotG", "CoP", "Assault", "Campaign",
                     "ACP", "AMK", "ASA", "SoA", "RoV", "TVR")
QUEST_LOG_NAMES = ("San d'Oria", "Bastok", "Windurst", "Jeuno", "Other areas", "Outlands", "Aht Urhgan", "Crystal War",
                   "Abyssea", "Adoulin", "Coalition")


def _bits(b):
    return sum(bin(x).count("1") for x in (b or b""))


def summarize(char, blobs, counts, storage, items):
    """Rows of (part, value) describing a character, comparable between two characters."""
    def job(j, lvl):
        j = j or 0
        return "%s %s" % (JOBS[j] if j < len(JOBS) else "#%d" % j, lvl)

    main = job(char.get("mjob"), char.get("mlvl"))
    rows = [("Job", main + (" / " + job(char.get("sjob"), char.get("slvl")) if char.get("sjob") else ""))]
    keys = bytes(blobs.get("keyitems") or b"")
    per = [_bits(keys[t * KEY_TABLE_BYTES:t * KEY_TABLE_BYTES + 64]) for t in range(KEY_TABLES)]
    rows.append(("Key items", "%d (by table: %s)" % (sum(per), " ".join(str(n) for n in per))))
    rows.append(("Titles", str(_bits(blobs.get("titles")))))
    quests = bytes(blobs.get("quests") or b"")
    for i, name in enumerate(QUEST_LOG_NAMES):
        chunk = quests[i * QUEST_LOG_BYTES:(i + 1) * QUEST_LOG_BYTES]
        cur, done = _bits(chunk[:32]), _bits(chunk[32:])
        if cur or done:
            rows.append(("Quests: " + name, "%d current, %d complete" % (cur, done)))
    missions = bytes(blobs.get("missions") or b"")
    for i, name in enumerate(MISSION_LOG_NAMES):
        chunk = missions[i * MISSION_LOG_BYTES:(i + 1) * MISSION_LOG_BYTES]
        if len(chunk) < 6:
            continue
        cur = int.from_bytes(chunk[0:2], "little")
        done = sum(1 for x in chunk[6:] if x)
        rows.append(("Missions: " + name, "current %d, %d complete" % (cur, done)))
    for col in ("zones", "abilities", "weaponskills", "set_blue_spells", "unlocked_weapons", "assault", "campaign",
                "eminence"):
        b = blobs.get(col)
        rows.append(("Packed: " + col, "%d bytes, %d bits set" % (len(b or b""), _bits(b))))
    for table, n in sorted(counts.items()):
        rows.append(("Rows: " + table, str(n)))
    sizes = {k: v for k, v in (storage or {}).items() if k != "charid"}
    by_loc = {}
    for it in items:
        by_loc.setdefault(it["location"], []).append(it)
    for loc, name in CONTAINERS.items():
        held, size = by_loc.get(loc, []), sizes.get(name)
        if held or size:
            rows.append(("Items: " + name, "%d held, size %s, %d with extra data" % (
                len(held), "?" if size is None else size, sum(1 for i in held if i["extra_len"]))))
    return rows


def item_flags(storage, items):
    """Items worth a look on an imported character: extra data, or a slot past the container's size."""
    sizes = {k: v for k, v in (storage or {}).items() if k != "charid"}
    out = []
    for it in items:
        why = []
        if it["extra_len"]:
            why.append("%d bytes of extra data" % it["extra_len"])
        size = sizes.get(CONTAINERS.get(it["location"], ""))
        if it["location"] != 0 and size is not None and it["slot"] > size:
            why.append("slot %d past the container's size %d" % (it["slot"], size))
        if why:
            out.append(dict(it, why=", ".join(why)))
    return out


#: chars columns a move changes
MOVE_COLUMNS = ("pos_zone", "pos_prevzone", "pos_rot", "pos_x", "pos_y", "pos_z")

_VARNAME = re.compile(r"[\x21-\x7e]{1,64}\Z")
LOG_DDL = ("CREATE TABLE IF NOT EXISTS openlobby_chartool_log (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, "
           "at INT UNSIGNED NOT NULL, who VARCHAR(64) NOT NULL, charid INT UNSIGNED NOT NULL, "
           "charname VARCHAR(32) NOT NULL DEFAULT '', action VARCHAR(16) NOT NULL, detail VARCHAR(512) NOT NULL, "
           "before_rows LONGTEXT NULL, undone_at INT UNSIGNED NULL, undone_by VARCHAR(64) NULL, "
           "KEY (charid)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
SETTINGS_DDL = ("CREATE TABLE IF NOT EXISTS openlobby_chartool_settings (name VARCHAR(32) NOT NULL PRIMARY KEY, "
                "value VARCHAR(64) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")


class CharError(Exception):
    """A request the panel should show back as it is."""


# -- test scenarios (run by a map-server module, not part of stock LandSandBoat) ------------------

#: Zone lines and the heavy-cutscene catalog, built by tools/gen_ffxi_testdata.py from the LSB tree.
TESTDATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffxi_testdata.json")
_TESTDATA = []

#: Cutscene flags the scenario form offers (the client's event flags, LSB xi.cutsceneFlag)
FLAG_NO_PCS, FLAG_NO_NPCS = 0x02, 0x10
#: The [Scenario]* variables the module reads; arming replaces all of them.
SCENARIO_VARS = ("Armed", "Stage", "Result", "Rezoned", "From", "To", "AtX", "AtY", "AtZ", "AtRot", "Wait", "Csid",
                 "Flags", "Delay", "PreRezone", "Quiet")
SCENARIO_LIKE = "[Scenario]%"  # MariaDB LIKE: only % and _ are special
#: server_variables rows that turn an arrival cutscene off for consoles: [ConsoleCsOff]<zone>:<csid> = 1
CS_OFF_PREFIX = "[ConsoleCsOff]"


def testdata():
    """{"zones": {id: name}, "lines": [...], "heavy": [...]}; empty when the file is missing."""
    if not _TESTDATA:
        try:
            with open(TESTDATA_PATH, encoding="utf-8") as fh:
                _TESTDATA.append(json.load(fh))
        except (OSError, ValueError):
            _TESTDATA.append({"zones": {}, "lines": [], "heavy": []})
    return _TESTDATA[0]


def arrivals(to):
    """The zone lines into zone `to`, one per zone they come from."""
    seen, out = set(), []
    for line in testdata()["lines"]:
        if line["to"] == to and line["from"] != to and line["from"] not in seen:
            seen.add(line["from"])
            out.append(line)
    return out


def standing_spot(zone):
    """Somewhere a character can stand in `zone`: where a zone line into it puts one, or None."""
    for line in testdata()["lines"]:
        if line["to"] == zone:
            return line
    return None


def scenario_status(variables):
    """What the [Scenario]* variables say about the last arming, or None."""
    v = {r["varname"][len("[Scenario]"):]: r["value"] for r in variables if r["varname"].startswith("[Scenario]")}
    if not v:
        return None
    stage, result = v.get("Stage", 0), v.get("Result", 0)
    if result == 2:
        words = "finished: the cutscene played to its end"
    elif result == 1:
        words = "started but never finished: the client died in the cutscene, or it is still playing"
    elif v.get("Armed") == 1:
        words = {0: "armed: log the test character in to start", 1: "travelling to the destination",
                 2: "zoning in place before the cutscene"}.get(stage, "armed")
    else:
        words = "not run"
    return dict(v, words=words)


# -- row snapshots (what Undo puts back) ---------------------------------------------------------

def _enc(v):
    if isinstance(v, (bytes, bytearray)):
        return {"b64": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, (datetime.datetime, datetime.date)):
        return {"dt": v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime.datetime) else v.isoformat()}
    if isinstance(v, decimal.Decimal):
        return {"dec": str(v)}
    return v


def _dec(v):
    if isinstance(v, dict):
        if "b64" in v:
            return base64.b64decode(v["b64"])
        if "dt" in v:
            return v["dt"]
        if "dec" in v:
            return decimal.Decimal(v["dec"])
    return v


def dump_snapshot(snap):
    """{"chars": {col: value} or None, "tables": {table: [row, ...]}} -> JSON text."""
    return json.dumps({"chars": None if snap.get("chars") is None else {k: _enc(v) for k, v in snap["chars"].items()},
                       "tables": {t: [{k: _enc(v) for k, v in r.items()} for r in rows]
                                  for t, rows in (snap.get("tables") or {}).items()}})


def load_snapshot(text):
    raw = json.loads(text)
    return {"chars": None if raw.get("chars") is None else {k: _dec(v) for k, v in raw["chars"].items()},
            "tables": {t: [{k: _dec(v) for k, v in r.items()} for r in rows] for t, rows in raw["tables"].items()}}


# -- the database ---------------------------------------------------------------------------------

class Store:
    """The LSB tables over pymysql: a connection per read, one transaction per change."""

    def __init__(self, host, port, user, password, database):
        self._args = dict(host=host, port=int(port), user=user, password=password,
                          database=database, autocommit=True, connect_timeout=5, charset="utf8mb4")
        self._ready = False

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

    def _connect(self, autocommit=True):
        import pymysql
        args = dict(self._args, autocommit=autocommit)
        return pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **args)

    def _run(self, sql, args=(), fetch=False):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                n = cur.execute(sql, args)
                return cur.fetchall() if fetch else n
        finally:
            conn.close()

    def _setup(self):
        if not self._ready:
            self._run(LOG_DDL)
            self._run(SETTINGS_DDL)
            self._ready = True

    # reads

    def setting(self, name):
        self._setup()
        rows = self._run("SELECT value FROM openlobby_chartool_settings WHERE name = %s", (name,), fetch=True)
        return rows[0]["value"] if rows else None

    def set_setting(self, name, value):
        self._setup()
        self._run("REPLACE INTO openlobby_chartool_settings (name, value) VALUES (%s, %s)", (name, str(value)))

    def zones(self):
        return self._run("SELECT zoneid, name FROM zone_settings ORDER BY name", fetch=True)

    def find(self, text, limit=25):
        text = text.strip()
        if text.isdigit():
            where, args = "c.charid = %s", (int(text),)
        else:
            where, args = "c.charname LIKE %s", (text.replace("\\", "").replace("%", "").replace("_", "\\_") + "%",)
        return self._run("SELECT c.charid, c.charname, c.accid, c.pos_zone, z.name AS zone_name, s.mjob, s.mlvl, "
                         "s.sjob, s.slvl, (SELECT COUNT(*) FROM accounts_sessions a WHERE a.charid = c.charid) AS online "
                         "FROM chars c LEFT JOIN char_stats s ON s.charid = c.charid "
                         "LEFT JOIN zone_settings z ON z.zoneid = c.pos_zone WHERE " + where +
                         " ORDER BY c.charname LIMIT %s", args + (int(limit),), fetch=True)

    def char(self, charid):
        rows = self._run("SELECT c.charid, c.charname, c.accid, c.nation, c.gmlevel, c.pos_zone, c.pos_prevzone, "
                         "c.pos_x, c.pos_y, c.pos_z, c.pos_rot, c.home_zone, c.home_x, c.home_y, c.home_z, c.home_rot, "
                         "c.playtime, c.last_logout, z.name AS zone_name, pz.name AS prevzone_name, "
                         "hz.name AS home_zone_name, s.mjob, s.mlvl, s.sjob, s.slvl, l.race, l.face, l.size, "
                         "(SELECT COUNT(*) FROM accounts_sessions a WHERE a.charid = c.charid) AS online "
                         "FROM chars c LEFT JOIN char_stats s ON s.charid = c.charid "
                         "LEFT JOIN char_look l ON l.charid = c.charid "
                         "LEFT JOIN zone_settings z ON z.zoneid = c.pos_zone "
                         "LEFT JOIN zone_settings pz ON pz.zoneid = c.pos_prevzone "
                         "LEFT JOIN zone_settings hz ON hz.zoneid = c.home_zone WHERE c.charid = %s",
                         (int(charid),), fetch=True)
        if not rows:
            return None
        row = rows[0]
        if isinstance(row.get("last_logout"), datetime.datetime):
            row["last_logout"] = row["last_logout"].strftime("%Y-%m-%d %H:%M:%S")
        return row

    def equipment(self, charid):
        return self._run("SELECT e.equipslotid, e.containerid, e.slotid, i.itemId, COALESCE(b.name, '?') AS name "
                         "FROM char_equip e LEFT JOIN char_inventory i ON i.charid = e.charid "
                         "AND i.location = e.containerid AND i.slot = e.slotid "
                         "LEFT JOIN item_basic b ON b.itemid = i.itemId WHERE e.charid = %s ORDER BY e.equipslotid",
                         (int(charid),), fetch=True)

    def variables(self, charid):
        return self._run("SELECT varname, value, expiry FROM char_vars WHERE charid = %s ORDER BY varname",
                         (int(charid),), fetch=True)

    def find_items(self, text, limit=40):
        """item_basic rows by id or by part of the name (spaces match the names' underscores)."""
        text = text.strip()
        if text.isdigit():
            where, args = "itemid = %s", (int(text),)
        else:
            like = text.lower().replace("\\", "").replace("%", "").replace("_", " ").replace(" ", "\\_")
            where, args = "name LIKE %s", ("%" + like + "%",)
        return self._run("SELECT itemid, name, stackSize, flags, aH FROM item_basic WHERE " + where +
                         " ORDER BY LENGTH(name), name LIMIT %s", args + (int(limit),), fetch=True)

    def item(self, itemid):
        rows = self._run("SELECT itemid, name, stackSize, flags, aH FROM item_basic WHERE itemid = %s", (int(itemid),),
                         fetch=True)
        return rows[0] if rows else None

    def gil(self, charid):
        rows = self._run("SELECT quantity FROM char_inventory WHERE charid = %s AND location = 0 AND slot = 0 "
                         "AND itemId = %s", (int(charid), GIL_ITEM), fetch=True)
        return int(rows[0]["quantity"]) if rows else 0

    def data(self, charid):
        """(blobs {col: bytes}, row counts {table: n}, char_storage row, items [...]) for the summary."""
        blobs = self.char_columns(charid, ("keyitems", "titles", "quests", "missions", "zones", "abilities",
                                           "weaponskills", "set_blue_spells", "unlocked_weapons", "assault",
                                           "campaign", "eminence")) or {}
        counts = {}
        for table in COPY_TABLES:
            rows = self._run("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                             "AND table_name = %s AND column_name = 'charid'", (table,), fetch=True)
            if rows[0]["n"]:
                counts[table] = self._run("SELECT COUNT(*) AS n FROM %s WHERE charid = %%s" % table, (int(charid),),
                                          fetch=True)[0]["n"]
        storage = self._run("SELECT * FROM char_storage WHERE charid = %s", (int(charid),), fetch=True)
        items = self._run("SELECT i.location, i.slot, i.itemId, i.quantity, COALESCE(LENGTH(i.extra), 0) AS extra_len, "
                          "HEX(i.extra) AS extra_hex, COALESCE(b.name, '?') AS name, "
                          "EXISTS (SELECT 1 FROM char_equip e WHERE e.charid = i.charid AND e.containerid = i.location "
                          "AND e.slotid = i.slot) AS equipped FROM char_inventory i "
                          "LEFT JOIN item_basic b ON b.itemid = i.itemId WHERE i.charid = %s AND i.itemId <> 65535 "
                          "ORDER BY i.location, i.slot", (int(charid),), fetch=True)
        for it in items:
            # an all-zero extra is how LSB stores "none"
            if it["extra_hex"] and set(it["extra_hex"]) == {"0"}:
                it["extra_len"] = 0
        return blobs, counts, storage[0] if storage else None, items

    def of_logins(self, logins, limit=64):
        """The characters on these LSB accounts (accounts.login), with job, zone and whether each
        is logged in. A deleted character has accid 0 and is not among them."""
        logins = [str(x) for x in logins if x][:64]
        if not logins:
            return []
        return self._run("SELECT c.charid, c.charname, c.nation, c.pos_zone, z.name AS zone_name, s.mjob, s.mlvl, "
                         "s.sjob, s.slvl, (SELECT COUNT(*) FROM accounts_sessions x WHERE x.charid = c.charid) AS online "
                         "FROM chars c JOIN accounts a ON a.id = c.accid LEFT JOIN char_stats s ON s.charid = c.charid "
                         "LEFT JOIN zone_settings z ON z.zoneid = c.pos_zone WHERE a.login IN (" +
                         ", ".join(["%s"] * len(logins)) + ") ORDER BY c.charname LIMIT %s",
                         tuple(logins) + (int(limit),), fetch=True)

    def lost_contact(self, limit=50):
        """openlobby_crash_log (written by the bridge, ffxi_bridge.py record_lost_contact); [] before its first row."""
        have = self._run("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema = DATABASE() "
                         "AND table_name = 'openlobby_crash_log'", fetch=True)[0]["n"]
        if not have:
            return []
        return self._run("SELECT id, at, charid, charname, console, zone, prevzone, trail_zone, trail_prev, trail_at, "
                         "trail_csid, silent_s, c2s, s2c FROM openlobby_crash_log ORDER BY id DESC LIMIT %s",
                         (int(limit),), fetch=True)

    def cs_off(self):
        """[{zone, csid, at, who}] for every arrival cutscene turned off for consoles, by zone and cutscene."""
        self._setup()
        rows = self._run("SELECT name FROM server_variables WHERE name LIKE %s AND value = 1", (CS_OFF_PREFIX + "%",),
                         fetch=True)
        out = []
        for r in rows:
            try:
                zone, csid = (int(x) for x in r["name"][len(CS_OFF_PREFIX):].split(":"))
            except ValueError:
                continue
            last = self._run("SELECT at, who FROM openlobby_chartool_log WHERE action = 'csoff' AND charid = 0 "
                             "AND detail LIKE %s ORDER BY id DESC LIMIT 1", ("%% %d:%d %%" % (zone, csid),), fetch=True)
            out.append({"zone": zone, "csid": csid, "at": last[0]["at"] if last else None,
                        "who": last[0]["who"] if last else None})
        return sorted(out, key=lambda x: (x["zone"], x["csid"]))

    def set_cs_off(self, zone, csid, off, who, now, detail):
        """Turn one arrival cutscene off for consoles (off=True) or back on, and log it under no character."""
        self._setup()
        name = "%s%d:%d" % (CS_OFF_PREFIX, int(zone), int(csid))
        if off:
            self._run("REPLACE INTO server_variables (name, value) VALUES (%s, 1)", (name,))
        else:
            self._run("DELETE FROM server_variables WHERE name = %s", (name,))
        self._run("INSERT INTO openlobby_chartool_log (at, who, charid, charname, action, detail) "
                  "VALUES (%s, %s, 0, %s, %s, %s)",
                  (int(now), (who or "admin")[:64], "(all consoles)", "csoff" if off else "cson", detail[:512]))

    def log(self, limit=60):
        self._setup()
        return self._run("SELECT id, at, who, charid, charname, action, detail, before_rows IS NOT NULL AS has_rows, "
                         "undone_at, undone_by FROM openlobby_chartool_log ORDER BY id DESC LIMIT %s",
                         (int(limit),), fetch=True)

    def log_entry(self, entry_id):
        self._setup()
        rows = self._run("SELECT * FROM openlobby_chartool_log WHERE id = %s", (int(entry_id),), fetch=True)
        return rows[0] if rows else None

    # one change, in one transaction

    def change(self, charid, steps, log):
        """Snapshot what `steps` will touch, apply them and log, all in one transaction, refusing if the
        character is logged in. `steps` is a list of ("chars", {col: value}) / ("table", table, [rows]) /
        ("sql", sql, args, table). `log` is (who, action, detail, now). -> the log entry's id."""
        self._setup()
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT charname FROM chars WHERE charid = %s FOR UPDATE", (int(charid),))
                got = cur.fetchone()
                if not got:
                    raise CharError("There is no character %d." % int(charid))
                cur.execute("SELECT COUNT(*) AS n FROM accounts_sessions WHERE charid = %s", (int(charid),))
                if cur.fetchone()["n"]:
                    raise CharError("%s is logged in. Changes only take while the character is logged out "
                                    "(the map server keeps its own copy until then)." % got["charname"])
                snap = {"chars": None, "tables": {}}
                for step in steps:
                    if step[0] == "chars":
                        cols = list(step[1])
                        cur.execute("SELECT %s FROM chars WHERE charid = %%s" % ", ".join(cols), (int(charid),))
                        snap["chars"] = dict(snap["chars"] or {}, **cur.fetchone())
                    else:
                        # ("table", table, rows) replaces `table`; ("sql", sql, args, table) changes `table`
                        table = step[1] if step[0] == "table" else step[3]
                        if table not in snap["tables"]:
                            cur.execute("SELECT * FROM %s WHERE charid = %%s" % table, (int(charid),))
                            snap["tables"][table] = list(cur.fetchall())
                for step in steps:
                    if step[0] == "chars":
                        sets = ", ".join("%s = %%s" % c for c in step[1])
                        cur.execute("UPDATE chars SET %s WHERE charid = %%s" % sets, tuple(step[1].values()) + (int(charid),))
                    elif step[0] == "table":
                        _replace_rows(cur, step[1], int(charid), step[2])
                    else:
                        cur.execute(step[1], step[2])
                who, action, detail, now = log
                cur.execute("INSERT INTO openlobby_chartool_log (at, who, charid, charname, action, detail, before_rows) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            (int(now), (who or "admin")[:64], int(charid), got["charname"][:32], action, detail[:512],
                             dump_snapshot(snap)))
                entry = cur.lastrowid
            conn.commit()
            return entry
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def copy_rows(self, src, table):
        """`table`'s rows for charid `src`; [] when this database has no such table."""
        if not self._run("SELECT COUNT(*) AS n FROM information_schema.columns WHERE table_schema = DATABASE() "
                         "AND table_name = %s AND column_name = 'charid'", (table,), fetch=True)[0]["n"]:
            return None
        return self._run("SELECT * FROM %s WHERE charid = %%s" % table, (int(src),), fetch=True)

    def char_columns(self, charid, cols):
        have = {r["column_name"] for r in self._run(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'chars'",
            fetch=True)}
        cols = [c for c in cols if c in have]
        rows = self._run("SELECT %s FROM chars WHERE charid = %%s" % ", ".join(cols), (int(charid),), fetch=True)
        return rows[0] if rows else None

    def mark_undone(self, entry_id, who, now):
        self._run("UPDATE openlobby_chartool_log SET undone_at = %s, undone_by = %s WHERE id = %s",
                  (int(now), (who or "admin")[:64], int(entry_id)))


def _replace_rows(cur, table, charid, rows):
    """`table`'s rows for `charid` become `rows` (their charid set to `charid`)."""
    cur.execute("DELETE FROM %s WHERE charid = %%s" % table, (charid,))
    for r in rows:
        r = dict(r, charid=charid)
        cols = list(r)
        cur.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join("`%s`" % c for c in cols),
                                                       ", ".join(["%s"] * len(cols))), tuple(r.values()))


# -- the page's actions --------------------------------------------------------------------------

def _id(body, key="id", label="character"):
    try:
        n = int(body.get(key))
    except (TypeError, ValueError):
        raise CharError("Which %s? The request named none." % label)
    if n <= 0:
        raise CharError("Which %s? The request named none." % label)
    return n


def _float(body, key, label):
    try:
        v = float(body.get(key))
    except (TypeError, ValueError):
        raise CharError("%s must be a number." % label)
    if not -5000 <= v <= 5000:
        raise CharError("%s must be between -5000 and 5000." % label)
    return v


class Chars:
    """What the page shows and does, over a Store (or a test's stand-in with the same methods)."""

    def __init__(self, store):
        self.store = store

    def _char(self, charid):
        c = self.store.char(charid)
        if c is None:
            raise CharError("There is no character %d." % charid)
        return c

    def _test_id(self):
        v = self.store.setting("test_charid")
        return int(v) if v and str(v).isdigit() else None

    def state(self, now=None):
        test = self._test_id()
        tc = self.store.char(test) if test else None
        data = testdata()
        return {"jobs": JOBS, "slots": SLOTS, "test": tc and {"charid": tc["charid"], "charname": tc["charname"],
                                                                "online": tc["online"]},
                "log": self.store.log(), "heavy": data.get("heavy", []), "zone_names": data.get("zones", {}),
                "lost": self.store.lost_contact(), "cs_off": self.store.cs_off()}

    def search(self, query):
        text = (query.get("q") or [""])[0].strip()
        if not text:
            return {"rows": []}
        if len(text) > 32:
            raise CharError("That is longer than any character name.")
        return {"rows": self.store.find(text)}

    def detail(self, query):
        charid = _id({"id": (query.get("id") or [""])[0]})
        c = self._char(charid)
        variables = self.store.variables(charid)
        return {"char": c, "equipment": self.store.equipment(charid), "variables": variables,
                "zones": self.store.zones(), "is_test": charid == self._test_id(),
                "scenario": scenario_status(variables)}

    def player(self, logins):
        """The account window's answer for the characters on these LSB accounts: {"characters":
        [{name, detail, online, facts}], "note"}, or {} when there are none."""
        chars = []
        for c in self.store.of_logins(logins):
            def job(j, lvl):
                j = int(j or 0)
                return "%s %s" % (JOBS[j] if 0 < j < len(JOBS) else "#%d" % j, lvl or 0)
            main = job(c.get("mjob"), c.get("mlvl")) if c.get("mjob") else ""
            sub = job(c.get("sjob"), c.get("slvl")) if c.get("sjob") else ""
            zone = str(c.get("zone_name") or "").replace("_", " ")
            nation = NATIONS[c["nation"]] if c.get("nation") in (0, 1, 2) else ""
            facts = [{"label": "main job", "value": main or "-"}]
            if sub:
                facts.append({"label": "support job", "value": sub})
            facts.append({"label": "zone", "value": zone or "zone %s" % c.get("pos_zone")})
            if nation:
                facts.append({"label": "nation", "value": nation})
            chars.append({"name": c["charname"], "detail": ", ".join(x for x in (main + ("/" + sub if sub else ""), zone) if x),
                          "online": bool(c.get("online")), "facts": facts})
        return {"characters": chars, "note": ""} if chars else {}

    def summary(self, charid):
        c = self._char(charid)
        blobs, counts, storage, items = self.store.data(charid)
        return {"charid": charid, "charname": c["charname"], "rows": summarize(c, blobs, counts, storage, items),
                "items": items, "flags": item_flags(storage, items), "gil": self.store.gil(charid),
                "sizes": {loc: (storage or {}).get(CONTAINERS[loc]) or 0 for loc in GIVE_CONTAINERS}}

    def data(self, query):
        """The character's data summary, and the test character's beside it when there is one."""
        charid = _id({"id": (query.get("id") or [""])[0]})
        out = {"this": self.summary(charid), "groups": {k: g["label"] for k, g in COPY_GROUPS.items()}}
        test = self._test_id()
        if test and test != charid and self.store.char(test):
            out["test"] = self.summary(test)
        return out

    def remove_item(self, body, actor, now=None):
        """Take one item off a character entirely (bag row and, if worn, the equipment row)."""
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        try:
            loc, slot = int(body.get("location")), int(body.get("slot"))
        except (TypeError, ValueError):
            raise CharError("Which item? Give its container and slot.")
        _, _, _, items = self.store.data(charid)
        it = next((i for i in items if i["location"] == loc and i["slot"] == slot), None)
        if it is None:
            raise CharError("%s has nothing in container %d slot %d." % (c["charname"], loc, slot))
        steps = [("sql", "DELETE FROM char_equip WHERE charid = %s AND containerid = %s AND slotid = %s",
                  (charid, loc, slot), "char_equip"),
                 ("sql", "DELETE FROM char_inventory WHERE charid = %s AND location = %s AND slot = %s",
                  (charid, loc, slot), "char_inventory")]
        what = "%s x%d (item %d) from %s slot %d" % (it["name"], it["quantity"], it["itemId"],
                                                    CONTAINERS.get(loc, loc), slot)
        entry = self.store.change(charid, steps, (actor, "rmitem", "removed " + what, now))
        return {"ok": True, "msg": "Removed %s. Undo puts it back." % what, "entry": entry}

    def item_search(self, query):
        text = (query.get("q") or [""])[0].strip()
        if not text:
            return {"rows": []}
        if len(text) > 40:
            raise CharError("That is longer than any item name.")
        return {"rows": [r for r in self.store.find_items(text) if r["itemid"] != GIL_ITEM]}

    def add_item(self, body, actor, now=None):
        """Give a character an item for a test: `quantity` of it into free slots of bag `location`, in stacks."""
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        itemid = _id(body, "item", "item")
        it = self.store.item(itemid) if itemid != GIL_ITEM else None
        if it is None:
            raise CharError("There is no item %d." % itemid)
        try:
            loc, qty = int(body.get("location") or 0), int(body.get("quantity") or 1)
        except (TypeError, ValueError):
            raise CharError("The bag and the quantity must be whole numbers.")
        if loc not in GIVE_CONTAINERS:
            raise CharError("Items can only be given into the inventory, safe, locker, satchel, sack, case or a wardrobe.")
        _, _, storage, items = self.store.data(charid)
        size = min((storage or {}).get(CONTAINERS[loc]) or 0, 80)
        if not size:
            raise CharError("%s has no %s." % (c["charname"], CONTAINERS[loc]))
        stack = max(int(it["stackSize"] or 1), 1)
        if it["flags"] & FLAG_RARE:
            if qty > 1:
                raise CharError("%s is rare: a character can only hold one." % it["name"])
            if any(i["itemId"] == itemid for i in items):
                raise CharError("%s already has %s, and it is rare." % (c["charname"], it["name"]))
        held = {i["slot"] for i in items if i["location"] == loc}
        free = [s for s in range(1, size + 1) if s not in held]
        if not 1 <= qty <= stack * len(free):
            raise CharError("Give 1 to %d: %s stacks to %d and %s has %d free slot%s in its %s." % (
                stack * len(free), it["name"], stack, c["charname"], len(free), "" if len(free) == 1 else "s",
                CONTAINERS[loc]))
        steps, left = [], qty
        for slot in free:
            n = min(left, stack)
            steps.append(("sql", "INSERT INTO char_inventory (charid, location, slot, itemId, quantity) "
                                 "VALUES (%s, %s, %s, %s, %s)", (charid, loc, slot, itemid, n), "char_inventory"))
            left -= n
            if not left:
                break
        what = "%s x%d (item %d) to %s" % (it["name"], qty, itemid, CONTAINERS[loc])
        entry = self.store.change(charid, steps, (actor, "additem", "gave " + what, now))
        return {"ok": True, "entry": entry,
                "msg": "Gave %s %s, in %d slot%s. Undo takes it back." % (c["charname"], what, len(steps),
                                                                        "" if len(steps) == 1 else "s")}

    def set_gil(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        try:
            gil = int(body.get("gil"))
        except (TypeError, ValueError):
            raise CharError("Gil must be a whole number.")
        if not 0 <= gil <= MAX_GIL:
            raise CharError("Gil must be 0 to %d." % MAX_GIL)
        was = self.store.gil(charid)
        steps = [("sql", "REPLACE INTO char_inventory (charid, location, slot, itemId, quantity) VALUES (%s, 0, 0, %s, %s)",
                  (charid, GIL_ITEM, gil), "char_inventory")]
        entry = self.store.change(charid, steps, (actor, "gil", "gil %d -> %d" % (was, gil), now))
        return {"ok": True, "msg": "%s has %d gil (was %d)." % (c["charname"], gil, was), "entry": entry}

    def empty(self, body, actor, now=None):
        """Empty a character for a test: every bag and what it wears (gil stays), its key items, or both."""
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        parts = body.get("parts")
        if not isinstance(parts, list) or not parts or any(p not in ("items", "keyitems") for p in parts):
            raise CharError("Empty what? Choose items, key items or both.")
        steps, said = [], []
        if "items" in parts:
            _, _, _, items = self.store.data(charid)
            if items:
                steps += [("sql", "DELETE FROM char_equip WHERE charid = %s", (charid,), "char_equip"),
                          ("sql", "DELETE FROM char_inventory WHERE charid = %s AND itemId <> 65535", (charid,),
                           "char_inventory"),
                          ("sql", "UPDATE char_look SET %s WHERE charid = %%s"
                           % ", ".join("%s = 0" % k for k in LOOK_COLUMNS.values()), (charid,), "char_look")]
                said.append("%d item%s" % (len(items), "" if len(items) == 1 else "s"))
        if "keyitems" in parts:
            blob = bytes((self.store.char_columns(charid, ("keyitems",)) or {}).get("keyitems") or b"")
            held = sum(_bits(blob[t * KEY_TABLE_BYTES:t * KEY_TABLE_BYTES + 64]) for t in range(KEY_TABLES))
            if any(blob):
                steps.append(("chars", {"keyitems": bytes(len(blob))}))
                said.append("%d key item%s" % (held, "" if held == 1 else "s"))
        if not steps:
            raise CharError("%s has nothing to empty there." % c["charname"])
        what = " and ".join(said)
        entry = self.store.change(charid, steps, (actor, "empty", "emptied for a test: " + what, now))
        return {"ok": True, "msg": "Took %s off %s. Undo on this change puts them all back." % (what, c["charname"]),
                "entry": entry}

    def cs_off_set(self, body, actor, now=None):
        """Turn an arrival cutscene off for consoles ("off": true) or back on."""
        now = int(time.time() if now is None else now)
        zone = _id(body, "zone", "zone")
        if not any(r["zoneid"] == zone for r in self.store.zones()):
            raise CharError("There is no zone %d." % zone)
        try:
            csid = int(body.get("csid"))
        except (TypeError, ValueError):
            raise CharError("Which cutscene? Give its number.")
        if not 0 <= csid <= 65534:
            raise CharError("A cutscene number is from 0 to 65534.")
        off = bool(body.get("off"))
        names = testdata().get("zones", {})
        zname = " ".join(w.capitalize() for w in (names.get(str(zone)) or names.get(zone) or "zone %d" % zone).split("_"))
        where = "%s %d:%d " % (zname, zone, csid)
        detail = ("turned off for consoles: " if off else "turned back on for consoles: ") + where.strip()
        self.store.set_cs_off(zone, csid, off, actor, now, detail + " ")
        if off:
            return {"ok": True, "msg": "Cutscene %d in %s is off for consoles. A PS2 or Xbox player arriving to it is "
                                       "let through and pointed to a Melody Minstrel." % (csid, zname)}
        return {"ok": True, "msg": "Cutscene %d in %s plays on consoles again." % (csid, zname)}

    def arrivals(self, query):
        to = _id({"id": (query.get("to") or [""])[0]}, label="zone")
        return {"to": to, "from": [{"from": a["from"], "line": a["line"]} for a in arrivals(to)],
                "heavy": [h for h in testdata().get("heavy", []) if h["zone"] == to]}

    # changes

    def move(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        if body.get("home"):
            if not c.get("home_zone"):
                raise CharError("%s has no home point set." % c["charname"])
            zone, x, y, z, rot = c["home_zone"], c["home_x"], c["home_y"], c["home_z"], c["home_rot"]
            where = "its home point"
        else:
            zone = _id(body, "zone", "zone")
            if not any(r["zoneid"] == zone for r in self.store.zones()):
                raise CharError("There is no zone %d." % zone)
            x, y, z = _float(body, "x", "X"), _float(body, "y", "Y"), _float(body, "z", "Z")
            try:
                rot = int(body.get("rot") or 0)
            except (TypeError, ValueError):
                raise CharError("Facing must be a whole number from 0 to 255.")
            if not 0 <= rot <= 255:
                raise CharError("Facing must be a whole number from 0 to 255.")
            where = "zone %d (%.1f, %.1f, %.1f)" % (zone, x, y, z)
        # The previous zone is set to the zone itself, so nothing keyed on "arriving from" another zone plays.
        cols = {"pos_zone": zone, "pos_prevzone": zone, "pos_rot": rot, "pos_x": x, "pos_y": y, "pos_z": z}
        entry = self.store.change(charid, [("chars", cols)], (actor, "move", "moved to " + where, now))
        return {"ok": True, "msg": "%s will be in %s at its next login." % (c["charname"], where), "entry": entry}

    def unequip(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        worn = {e["equipslotid"]: e for e in self.store.equipment(charid)}
        if body.get("all"):
            slots = sorted(worn)
        else:
            try:
                slots = [int(body.get("slot"))]
            except (TypeError, ValueError):
                raise CharError("Which slot?")
            if slots[0] not in worn:
                raise CharError("%s has nothing in %s." % (c["charname"], SLOTS.get(slots[0], "that slot")))
        if not slots:
            raise CharError("%s has nothing equipped." % c["charname"])
        marks = ", ".join(["%s"] * len(slots))
        steps = [("sql", "DELETE FROM char_equip WHERE charid = %%s AND equipslotid IN (%s)" % marks,
                  (charid,) + tuple(slots), "char_equip")]
        look = [LOOK_COLUMNS[s] for s in slots if s in LOOK_COLUMNS]
        if look:
            steps.append(("sql", "UPDATE char_look SET %s WHERE charid = %%s" % ", ".join("%s = 0" % k for k in look),
                          (charid,), "char_look"))
        names = ", ".join("%s (%s)" % (SLOTS.get(s, s), worn[s]["name"]) for s in slots)
        entry = self.store.change(charid, steps, (actor, "unequip", "took off " + names, now))
        return {"ok": True, "msg": "Took off %s. The items stay in %s's bags." % (names, c["charname"]), "entry": entry}

    def set_var(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        name = str(body.get("name") or "").strip()
        if not _VARNAME.match(name):
            raise CharError("A variable name is 1-64 printable characters with no spaces.")
        try:
            value = int(body.get("value"))
        except (TypeError, ValueError):
            raise CharError("The value must be a whole number.")
        if not -2147483648 <= value <= 2147483647:
            raise CharError("The value must fit in 32 bits.")
        steps = [("sql", "REPLACE INTO char_vars (charid, varname, value) VALUES (%s, %s, %s)",
                  (charid, name, value), "char_vars")]
        entry = self.store.change(charid, steps, (actor, "setvar", "%s = %d" % (name, value), now))
        return {"ok": True, "msg": "%s: %s = %d." % (c["charname"], name, value), "entry": entry}

    def delete_vars(self, body, actor, now=None):
        """One variable (name) or every variable starting with a prefix (prefix)."""
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        have = [v["varname"] for v in self.store.variables(charid)]
        if body.get("prefix"):
            prefix = str(body["prefix"])
            if not _VARNAME.match(prefix):
                raise CharError("A prefix is 1-64 printable characters with no spaces.")
            names = [n for n in have if n.startswith(prefix)]
            what = "every variable starting %s" % prefix
        else:
            name = str(body.get("name") or "")
            names = [name] if name in have else []
            what = name
        if not names:
            raise CharError("%s has no variable %s." % (c["charname"], what if not body.get("prefix") else
                                                         "starting " + str(body["prefix"])))
        marks = ", ".join(["%s"] * len(names))
        steps = [("sql", "DELETE FROM char_vars WHERE charid = %%s AND varname IN (%s)" % marks,
                  (charid,) + tuple(names), "char_vars")]
        entry = self.store.change(charid, steps, (actor, "delvar", "deleted " + ", ".join(names)[:400], now))
        return {"ok": True, "msg": "Deleted %d variable%s from %s." % (len(names), "" if len(names) == 1 else "s",
                                                                      c["charname"]), "entry": entry}

    def set_test(self, body, actor, now=None):
        charid = _id(body)
        c = self._char(charid)
        self.store.set_setting("test_charid", charid)
        return {"ok": True, "msg": "%s is the test character now. Copies overwrite it." % c["charname"]}

    def copy_to_test(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        src = _id(body)
        test = self._test_id()
        if not test:
            raise CharError("Choose the test character first (Use as test character, on its page).")
        if src == test:
            raise CharError("That is the test character itself.")
        s = self._char(src)
        t = self._char(test)
        groups = body.get("groups") or list(COPY_GROUPS)
        if not isinstance(groups, list) or not groups or any(g not in COPY_GROUPS for g in groups):
            raise CharError("Copy which parts? Choose at least one of: %s." % ", ".join(
                g["label"] for g in COPY_GROUPS.values()))
        want_cols = [c for g in groups for c in COPY_GROUPS[g]["cols"]]
        want_tables = [t_ for g in groups for t_ in COPY_GROUPS[g]["tables"]]
        cols = self.store.char_columns(src, want_cols) if want_cols else {}
        steps = [("chars", cols)] if cols else []
        copied = []
        for table in [t_ for t_ in COPY_TABLES if t_ in want_tables]:
            rows = self.store.copy_rows(src, table)
            if rows is None:
                continue
            steps.append(("table", table, rows))
            copied.append(table)
        parts = ", ".join(COPY_GROUPS[g]["label"].lower() for g in groups)
        detail = "copied %s (%d) onto the test character: %s" % (s["charname"], src, parts)
        entry = self.store.change(test, steps, (actor, "copy", detail, now))
        note = " %s was logged in, so the copy has what it last saved." % s["charname"] if s["online"] else ""
        return {"ok": True, "msg": "%s now has %s's %s.%s Undo puts %s back."
                % (t["charname"], s["charname"], parts, note, t["charname"]), "entry": entry}

    def arm_scenario(self, body, actor, now=None):
        """Arm a test scenario on the test character and put it at the start (the map module runs it)."""
        now = int(time.time() if now is None else now)
        test = self._test_id()
        if not test:
            raise CharError("Choose the test character first (Use as test character, on its page).")
        if _id(body) != test:
            raise CharError("Scenarios only run on the test character.")
        t = self._char(test)
        to = _id(body, "to", "destination zone")
        names = testdata()["zones"]
        if str(to) not in names:
            raise CharError("There is no zone %d." % to)
        try:
            src = int(body.get("from") or 0)
        except (TypeError, ValueError):
            raise CharError("Arrive from which zone?")
        if src:
            line = next((a for a in arrivals(to) if a["from"] == src), None)
            if line is None:
                raise CharError("No zone line leads from zone %d into zone %d." % (src, to))
            start = standing_spot(src)
            if start is None:
                raise CharError("There is nowhere known to stand in zone %d." % src)
        else:
            line = standing_spot(to)
            if line is None:
                raise CharError("There is nowhere known to arrive in zone %d." % to)
            start = line
        csid = _id(body, "csid", "cutscene")
        if csid > 65535:
            raise CharError("A cutscene id is 1 to 65535.")
        nums = {}
        for key, label, lo, hi, default in (("wait", "Wait", 3, 600, 20), ("delay", "Delay", 0, 60000, 0)):
            raw = body.get(key)
            try:
                nums[key] = default if raw in (None, "") else int(raw)
            except (TypeError, ValueError):
                raise CharError("%s must be a whole number." % label)
            if not lo <= nums[key] <= hi:
                raise CharError("%s must be %d to %d." % (label, lo, hi))
        flags = (FLAG_NO_PCS if body.get("hide_pcs") else 0) | (FLAG_NO_NPCS if body.get("hide_npcs") else 0)
        values = {"Armed": 1, "Stage": 0, "Result": 0, "Rezoned": 0, "From": src, "To": to,
                  "AtX": int(round(line["x"] * 1000)), "AtY": int(round(line["y"] * 1000)),
                  "AtZ": int(round(line["z"] * 1000)), "AtRot": int(line["rot"]), "Wait": nums["wait"], "Csid": csid,
                  "Flags": flags, "Delay": nums["delay"], "PreRezone": 1 if body.get("prerezone") else 0,
                  "Quiet": 1 if body.get("quiet", True) else 0}
        zone = src or to
        steps = [("sql", "DELETE FROM char_vars WHERE charid = %s AND varname LIKE %s", (test, SCENARIO_LIKE), "char_vars")]
        for k in SCENARIO_VARS:
            steps.append(("sql", "REPLACE INTO char_vars (charid, varname, value) VALUES (%s, %s, %s)",
                          (test, "[Scenario]" + k, values[k]), "char_vars"))
        steps.append(("chars", {"pos_zone": zone, "pos_prevzone": zone, "pos_x": start["x"], "pos_y": start["y"],
                                "pos_z": start["z"], "pos_rot": start["rot"]}))
        if src:
            how = "from %s into %s after %d s" % (names.get(str(src), src), names.get(str(to), to), nums["wait"])
        else:
            how = "fresh login into %s" % names.get(str(to), to)
        opts = [o for o, on in (("hide players", flags & FLAG_NO_PCS), ("hide NPCs", flags & FLAG_NO_NPCS),
                                ("re-zone first", values["PreRezone"]), ("delay %d ms" % nums["delay"], nums["delay"]),
                                ("start zone not quiet", not values["Quiet"])) if on]
        what = "cutscene %d, %s%s" % (csid, how, (", " + ", ".join(opts)) if opts else "")
        entry = self.store.change(test, steps, (actor, "scenario", "scenario: " + what, now))
        return {"ok": True, "entry": entry,
                "msg": "Armed on %s: %s. Log it in to run it; each arming runs once." % (t["charname"], what)}

    def disarm_scenario(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        charid = _id(body)
        c = self._char(charid)
        steps = [("sql", "DELETE FROM char_vars WHERE charid = %s AND varname LIKE %s", (charid, SCENARIO_LIKE), "char_vars")]
        entry = self.store.change(charid, steps, (actor, "scenario", "cleared the test scenario", now))
        return {"ok": True, "msg": "Cleared the scenario on %s." % c["charname"], "entry": entry}

    def undo(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        entry_id = _id(body, "entry", "change")
        e = self.store.log_entry(entry_id)
        if e is None:
            raise CharError("There is no such change.")
        if e.get("undone_at"):
            raise CharError("That change was already undone.")
        if not e.get("before_rows"):
            raise CharError("That change kept nothing to put back.")
        snap = load_snapshot(e["before_rows"])
        steps = []
        if snap["chars"]:
            steps.append(("chars", snap["chars"]))
        for table, rows in snap["tables"].items():
            steps.append(("table", table, rows))
        new = self.store.change(e["charid"], steps, (actor, "undo", "undid #%d (%s)" % (entry_id, e["action"]), now))
        self.store.mark_undone(entry_id, actor, now)
        return {"ok": True, "msg": "Put %s back as it was before #%d." % (e["charname"], entry_id), "entry": new}


#: POST routes -> Chars methods (body, actor).
ACTIONS = {"/chars/move": "move", "/chars/unequip": "unequip", "/chars/setvar": "set_var",
           "/chars/delvars": "delete_vars", "/chars/settest": "set_test", "/chars/copy": "copy_to_test",
           "/chars/undo": "undo", "/chars/scenario": "arm_scenario", "/chars/unscenario": "disarm_scenario",
           "/chars/rmitem": "remove_item", "/chars/empty": "empty", "/chars/csoff": "cs_off_set",
           "/chars/additem": "add_item", "/chars/gil": "set_gil"}


def open_store(environ=os.environ):
    """(Chars, None) or (None, why)."""
    store, why = Store.from_env(environ)
    return (Chars(store), None) if store else (None, why)


#: The panel page, served by ffxi_fedtool.py at /chars (the panel's Games > FFXI > Characters).
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFXI characters</title>
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
input,select{font:inherit;font-size:13px;background:var(--sunk);color:var(--ink);
  border:1px solid var(--rule);border-radius:4px;padding:5px 8px;width:100%;min-width:0}
label{font-size:12px;color:var(--ink2);display:grid;gap:3px}
.pill{display:inline-block;border:1px solid var(--rule);border-radius:10px;padding:0 8px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill.on{border-color:var(--good);color:var(--good)}
.pill.acc{border-color:var(--accent);color:var(--accent)}
#sstate{white-space:normal;max-width:100%}
.msg{font-size:13px;border-radius:4px;padding:6px 9px;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good);color:var(--good)}
.msg.bad{border-color:var(--warn);color:var(--warn)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11.5px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
td.n{font-family:var(--m);font-size:12.5px;white-space:nowrap}
tr.pick{cursor:pointer}
tr.pick:hover td{background:var(--sunk)}
.acts{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.grid{display:grid;gap:8px;grid-template-columns:minmax(0,1fr)}
@media(min-width:760px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}.grid .w2{grid-column:span 2}}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:end}
.row label{flex:1 1 140px}
.facts{display:grid;gap:6px 18px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));font-size:13px}
.facts b{font-weight:600;color:var(--ink2);font-size:12px;display:block}
.empty{color:var(--ink3);font-size:13px}
.vars,.heavy{max-height:320px;overflow:auto}
.checks{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px}
label.ck{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink)}
label.ck input{width:auto}
.flags{display:grid;gap:8px}
.flag{display:flex;gap:10px;align-items:flex-start}
.flag input{width:auto;margin-top:4px}
.flag b{font-weight:600;font-size:13px}
tr.diff td{background:color-mix(in srgb, var(--warn) 12%, transparent)}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>Characters</h1><span id="testpill" class="pill"></span></div>
    <div class="sub">Changes only take while the character is logged out, and each one can be undone from the log below.
      Moving a character also sets its previous zone to the new zone, so cutscenes that play on arriving from another zone stay quiet.
      A zone's own arrival cutscenes still play.</div>
    <div class="row">
      <label>Find a character (name or id)<input id="q" autocomplete="off" maxlength="32" placeholder="Name"></label>
      <button id="find">Find</button>
    </div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
    <div class="tw" style="margin-top:8px"><table id="rtable" hidden><thead><tr><th>Name</th><th>Id</th><th>Job</th><th>Zone</th><th></th></tr></thead>
      <tbody id="results"></tbody></table></div>
  </div>

  <div class="card" id="ccard" hidden>
    <div class="hd"><h2 id="cname"></h2><span class="acts" id="cacts"></span></div>
    <div class="facts" id="cfacts"></div>
    <div id="copyrow" hidden>
      <div class="checks" id="cgroups"></div>
      <div class="row"><button id="ccopy">Copy these parts onto the test character</button></div>
    </div>
  </div>

  <div class="card" id="dcard" hidden>
    <div class="hd"><h2>Character data</h2>
      <label class="ck"><input type="checkbox" id="donly"> Only what differs from the test character</label></div>
    <div class="tw"><table><thead><tr><th>Part</th><th id="dthis">This character</th><th id="dtest">Test character</th></tr></thead><tbody id="drows"></tbody></table></div>
  </div>

  <div class="card" id="icard" hidden>
    <div class="hd"><h2>Items</h2>
      <label class="ck"><input type="checkbox" id="ionly" checked> Only items worth a look</label></div>
    <div class="sub">Worth a look: items with extra data (an imported character carries it as retail wrote it) and items past their container's size.
      Give puts an item into free slots of the bag you choose, split into stacks. Remove takes an item off the character entirely.
      Undo in the log reverses either.</div>
    <div class="row">
      <label>Give an item (name or id)<input id="g_q" autocomplete="off" maxlength="40" placeholder="Hi-Potion, 4116"></label>
      <button id="g_find">Find</button>
      <label style="flex:0 1 180px">Gil<input id="g_gil" inputmode="numeric"></label>
      <button id="g_gilset" class="go">Set gil</button>
    </div>
    <div class="row" id="g_pick" hidden>
      <label style="flex:2 1 260px">Item<select id="g_item"></select></label>
      <label style="flex:0 1 110px">Quantity<input id="g_qty" inputmode="numeric" value="1"></label>
      <label style="flex:0 1 190px">Into<select id="g_loc"></select></label>
      <button id="g_go" class="go">Give</button>
    </div>
    <div class="tw vars" style="margin-top:8px"><table><thead><tr><th>Where</th><th>Item</th><th>Why</th><th></th></tr></thead><tbody id="items"></tbody></table></div>
  </div>

  <div class="card" id="scard" hidden>
    <div class="hd"><h2>Test scenario</h2><span id="sstate" class="pill"></span></div>
    <div class="sub">Plays one cutscene under conditions you choose, the next time the test character logs in. It starts in the zone it
      arrives from, waits, walks in through that zone's real zone line, then plays the cutscene. Each arming runs once; arm again to repeat.
      The cutscene's own quest or mission does not advance.</div>
    <div class="grid" style="margin-top:10px">
      <label class="w2">Arrive in<select id="s_to"></select></label>
      <label class="w2">Arrive from<select id="s_from"></select></label>
      <label>Cutscene id<input id="s_csid" inputmode="numeric"></label>
      <label class="w2">Or pick a heavy cutscene in this zone<select id="s_heavy"></select></label>
      <label>Wait before travelling (s)<input id="s_wait" inputmode="numeric" value="20"></label>
      <label>Delay the cutscene (ms)<input id="s_delay" inputmode="numeric" value="0"></label>
    </div>
    <div class="checks">
      <label class="ck"><input type="checkbox" id="s_pcs"> Hide other players</label>
      <label class="ck"><input type="checkbox" id="s_npcs"> Hide NPCs not in the cutscene</label>
      <label class="ck"><input type="checkbox" id="s_rezone"> Zone in place once before the cutscene</label>
      <label class="ck"><input type="checkbox" id="s_quiet" checked> No other arrival cutscenes in the start zone</label>
    </div>
    <div class="row"><button id="s_arm" class="go">Arm</button><button id="s_clear" class="rm">Clear</button></div>
  </div>

  <div class="card" id="fcard" hidden>
    <div class="hd"><h2>Console test flags</h2><span class="sub">Each is a character variable; switching one is logged like any change.</span></div>
    <div id="flags" class="flags"></div>
  </div>

  <div class="card" id="mcard" hidden>
    <div class="hd"><h2>Move</h2><span class="sub">Takes effect at the next login.</span></div>
    <div class="row"><button id="mhome">To its home point</button></div>
    <div class="grid" style="margin-top:10px">
      <label class="w2">Zone<select id="mzone"></select></label>
      <label>X<input id="mx" inputmode="decimal"></label>
      <label>Y<input id="my" inputmode="decimal"></label>
      <label>Z<input id="mz" inputmode="decimal"></label>
      <label>Facing (0-255)<input id="mrot" inputmode="numeric" value="0"></label>
    </div>
    <div class="row"><button id="mgo" class="go">Move there</button></div>
  </div>

  <div class="card" id="ecard" hidden>
    <div class="hd"><h2>Equipment</h2><button id="eall" class="rm">Take everything off</button></div>
    <div class="tw"><table><thead><tr><th>Slot</th><th>Item</th><th></th></tr></thead><tbody id="equip"></tbody></table></div>
  </div>

  <div class="card" id="xcard" hidden>
    <div class="hd"><h2>Empty for a test</h2><span class="sub">Gil stays. Undo in the log puts everything back.</span></div>
    <div class="row">
      <label class="ck"><input type="checkbox" id="x_items" checked> Items in every bag, and what it wears</label>
      <label class="ck"><input type="checkbox" id="x_keys" checked> Key items</label>
      <button id="xgo" class="rm">Empty</button>
    </div>
    <div class="sub">Anything the character gains while empty is lost when you undo, because Undo puts back the rows as they were.</div>
  </div>

  <div class="card" id="vcard" hidden>
    <div class="hd"><h2>Character variables</h2><span class="sub" id="vcount"></span></div>
    <div class="row"><label>Show names containing<input id="vfilter" autocomplete="off" placeholder="Quest, [RoV], Mission"></label></div>
    <div class="tw vars" style="margin-top:8px"><table><thead><tr><th>Name</th><th>Value</th><th></th></tr></thead><tbody id="vars"></tbody></table></div>
    <div class="row">
      <label>Name<input id="vname" autocomplete="off" maxlength="64"></label>
      <label style="flex:0 1 140px">Value<input id="vvalue" inputmode="numeric"></label>
      <button id="vset" class="go">Set</button>
    </div>
    <div class="row">
      <label>Delete every variable starting with<input id="vprefix" autocomplete="off" maxlength="64" placeholder="Quest[8][160]"></label>
      <button id="vdelp" class="rm">Delete them</button>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Lost contact</h2><span class="sub">Sessions the map timed out because the client went silent first:
      a crash, a hang or a lost connection. Newest first.</span></div>
    <div class="tw vars"><table><thead><tr><th>When (UTC)</th><th>Character</th><th>Arrived in</th><th>From</th><th>Arrival cutscene</th>
      <th>After arriving</th><th>Silent</th><th></th></tr></thead><tbody id="lost"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Heavy cutscenes</h2><span class="sub">Arrival cutscenes whose script loads another zone's model or reloads its own:
      the steps that run the PS2 client out of memory (449a64).</span></div>
    <div class="row"><label>Show zones or cutscenes containing<input id="hfilter" autocomplete="off" placeholder="jeuno, 10094, acp"></label></div>
    <div class="tw heavy" style="margin-top:8px"><table><thead><tr><th>Zone</th><th>Cutscene</th><th>Loads</th><th>Played by</th><th></th></tr></thead><tbody id="heavy"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Cutscenes turned off on consoles</h2><span class="sub">A PS2 or Xbox player arriving to one of these is let
      through as if they had watched it, and told they can watch it from a Melody Minstrel. PCs always see it. Takes effect on the next arrival.</span></div>
    <div class="row">
      <label>Zone<select id="o_zone"></select></label>
      <label>Cutscene<input id="o_csid" inputmode="numeric" placeholder="10094"></label>
      <button id="o_go" class="rm">Turn off on consoles</button>
    </div>
    <div class="tw vars" style="margin-top:8px"><table><thead><tr><th>Zone</th><th>Cutscene</th><th>What it is</th><th>Turned off</th><th></th></tr></thead><tbody id="csoff"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Log</h2><span class="sub">Newest first. Undo puts back the rows a change replaced, including anything changed in them since.</span></div>
    <div class="tw"><table><thead><tr><th>When (UTC)</th><th>Who</th><th>Character</th><th>Change</th><th></th></tr></thead><tbody id="log"></tbody></table></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = t => t == null ? '' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');
const RACES = {1:'Hume male',2:'Hume female',3:'Elvaan male',4:'Elvaan female',5:'Tarutaru male',6:'Tarutaru female',7:'Mithra',8:'Galka'};
const NATIONS = ["San d'Oria", 'Bastok', 'Windurst'];
let S = null, C = null, BUSY = false;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }
async function getj(path){ const r = await fetch(qs(path)); const j = await r.json(); if(!r.ok) throw new Error(j.error || r.statusText); return j; }

async function loadState(){
  try{ S = await getj('chars.json'); drawState(); }catch(e){ say('Could not load: ' + e.message, false); }
}
function job(j, l){ return S && S.jobs[j] ? S.jobs[j] + (l != null ? ' ' + l : '') : (j ? '#' + j : ''); }
function drawState(){
  const p = q('#testpill');
  if(S.test){ p.textContent = 'test character: ' + S.test.charname + (S.test.online ? ' (logged in)' : ''); p.className = 'pill acc'; }
  else { p.textContent = 'no test character chosen'; p.className = 'pill'; }
  q('#log').innerHTML = S.log.length ? S.log.map(e => `<tr>
    <td class="n">${when(e.at)}</td><td>${esc(e.who)}</td><td>${esc(e.charname)} <span class="sub">${e.charid}</span></td>
    <td>${esc(e.detail)}${e.undone_at ? `<div class="sub">undone ${when(e.undone_at)} by ${esc(e.undone_by)}</div>` : ''}</td>
    <td>${e.has_rows && !e.undone_at ? `<button class="rm" data-undo="${e.id}">Undo</button>` : ''}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">Nothing changed yet.</td></tr>';
  drawHeavy();
  drawLost();
  drawCsOff();
}
function drawCsOff(){
  const sel = q('#o_zone');
  if(!sel.options.length){
    sel.innerHTML = Object.keys(S.zone_names || {}).filter(id => +id > 0).sort((a, b) => zname(+a).localeCompare(zname(+b)))
      .map(id => `<option value="${id}">${esc(zname(+id))} (${id})</option>`).join('');
  }
  const off = S.cs_off || [];
  q('#csoff').innerHTML = off.length ? off.map(o => {
    const h = (S.heavy || []).find(x => x.zone === o.zone && x.csid === o.csid);
    return `<tr><td>${esc(zname(o.zone))} <span class="sub">${o.zone}</span></td><td class="n">${o.csid}</td>
      <td>${h ? esc(heavyLine(h)) : '<span class="sub">not in the heavy list</span>'}</td>
      <td class="n">${o.at ? when(o.at) + ' <span class="sub">' + esc(o.who || '') + '</span>' : ''}</td>
      <td><button data-cson="${o.csid}" data-cszone="${o.zone}">Turn back on</button></td></tr>`;
  }).join('') : '<tr><td colspan="5" class="empty">None: every arrival cutscene plays on consoles.</td></tr>';
}

function ago(s){ s = Math.max(0, s | 0); return s < 120 ? s + ' s' : s < 7200 ? Math.round(s / 60) + ' min' : Math.round(s / 3600) + ' h'; }
function drawLost(){
  const canUse = C && C.is_test;
  const heavy = new Set((S.heavy || []).map(h => h.zone + ':' + h.csid));
  q('#lost').innerHTML = (S.lost || []).length ? S.lost.map(r => {
    const zone = r.trail_zone || r.zone, from = r.trail_zone ? r.trail_prev : r.prevzone;
    const cs = r.trail_csid >= 0 ? r.trail_csid + (heavy.has(zone + ':' + r.trail_csid) ? ' <span class="pill warn">heavy</span>' : '') : '<span class="sub">none</span>';
    const since = r.trail_at ? ago(r.at - r.silent_s - r.trail_at) : '';
    return `<tr><td class="n">${when(r.at)}</td><td>${esc(r.charname)} <span class="sub">${r.charid}${r.console ? ' · console' : ''}</span></td>
      <td>${esc(zname(zone))}</td><td>${esc(zname(from))}</td><td class="n">${cs}</td><td class="n">${since}</td><td class="n">${ago(r.silent_s)}</td>
      <td>${r.trail_csid >= 0 ? `<button data-lostzone="${zone}" data-lostfrom="${from}" data-lostcs="${r.trail_csid}"${canUse ? '' : ' disabled title="Open the test character first"'}>Set up</button>` : ''}</td></tr>`;
  }).join('') : '<tr><td colspan="8" class="empty">None recorded yet.</td></tr>';
}

async function find(){
  const text = q('#q').value.trim();
  if(!text) return;
  try{
    const j = await getj('chars/search.json?q=' + encodeURIComponent(text));
    q('#rtable').hidden = false;
    q('#results').innerHTML = j.rows.length ? j.rows.map(r => `<tr class="pick" data-open="${r.charid}">
      <td>${esc(r.charname)}</td><td class="n">${r.charid}</td><td>${esc(job(r.mjob, r.mlvl))}${r.sjob ? ' / ' + esc(job(r.sjob, r.slvl)) : ''}</td>
      <td>${esc(r.zone_name || r.pos_zone)}</td><td>${r.online ? '<span class="pill on">logged in</span>' : ''}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty">No character by that name or id.</td></tr>';
  }catch(e){ say(e.message, false); }
}

async function openChar(id){
  try{ C = await getj('chars/char.json?id=' + id); drawChar(); }catch(e){ say(e.message, false); }
}
function drawChar(){
  const c = C.char;
  ['#ccard', '#mcard', '#ecard', '#xcard', '#vcard'].forEach(s => q(s).hidden = false);
  q('#cname').textContent = c.charname + (C.is_test ? ' (test character)' : '');
  const acts = [];
  if(c.online) acts.push('<span class="pill on">logged in: changes refused</span>');
  if(!C.is_test) acts.push(`<button data-settest="${c.charid}">Use as test character</button>`);
  q('#copyrow').hidden = C.is_test || !S.test;
  q('#cacts').innerHTML = acts.join('');
  const pos = (x, y, z) => [x, y, z].map(v => Number(v).toFixed(2)).join(', ');
  const facts = [
    ['Id / account', c.charid + ' / ' + c.accid],
    ['Job', job(c.mjob, c.mlvl) + (c.sjob ? ' / ' + job(c.sjob, c.slvl) : '')],
    ['Race', (RACES[c.race] || c.race) + ', face ' + c.face],
    ['Nation', NATIONS[c.nation] || c.nation],
    ['Zone', (c.zone_name || '') + ' (' + c.pos_zone + ')'],
    ['Position', pos(c.pos_x, c.pos_y, c.pos_z) + ', facing ' + c.pos_rot],
    ['Previous zone', (c.prevzone_name || '') + ' (' + c.pos_prevzone + ')'],
    ['Home point', c.home_zone ? (c.home_zone_name || '') + ' (' + c.home_zone + ') ' + pos(c.home_x, c.home_y, c.home_z) : 'none'],
    ['Last logout', c.last_logout || ''],
  ];
  q('#cfacts').innerHTML = facts.map(([k, v]) => `<div><b>${esc(k)}</b>${esc(v)}</div>`).join('');
  q('#mzone').innerHTML = C.zones.map(z => `<option value="${z.zoneid}"${z.zoneid === c.pos_zone ? ' selected' : ''}>${esc(z.name)} (${z.zoneid})</option>`).join('');
  q('#mx').value = Number(c.pos_x).toFixed(3); q('#my').value = Number(c.pos_y).toFixed(3); q('#mz').value = Number(c.pos_z).toFixed(3); q('#mrot').value = c.pos_rot;
  q('#mhome').disabled = !c.home_zone;
  q('#equip').innerHTML = C.equipment.length ? C.equipment.map(e => `<tr><td>${esc(S.slots[e.equipslotid] || e.equipslotid)}</td>
    <td>${esc(e.name)} <span class="sub">${e.itemId ?? ''}</span></td><td><button class="rm" data-unequip="${e.equipslotid}">Take off</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">Nothing equipped.</td></tr>';
  q('#eall').disabled = !C.equipment.length;
  q('#fcard').hidden = false;
  drawVars();
  drawFlags();
  drawScenario();
  drawHeavy();
  drawLost();
  loadData(c.charid);
}

let D = null;
const CONT = ['inventory','safe','storage','temp','locker','satchel','sack','case','wardrobe','safe 2','wardrobe 2','wardrobe 3','wardrobe 4','wardrobe 5','wardrobe 6','wardrobe 7','wardrobe 8','recycle'];
async function loadData(id){
  try{ D = await getj('chars/data.json?id=' + id); drawData(); }catch(e){ say(e.message, false); }
}
function drawData(){
  ['#dcard', '#icard'].forEach(s => q(s).hidden = false);
  const groups = D.groups, keep = new Set([...document.querySelectorAll('#cgroups input:checked')].map(i => i.value));
  q('#cgroups').innerHTML = Object.entries(groups).map(([k, label]) =>
    `<label class="ck"><input type="checkbox" value="${k}"${keep.size && !keep.has(k) ? '' : ' checked'}> ${esc(label)}</label>`).join('');
  q('#dthis').textContent = D.this.charname;
  q('#dtest').textContent = D.test ? D.test.charname + ' (test)' : 'Test character';
  const t = new Map((D.test ? D.test.rows : []).map(([k, v]) => [k, v]));
  const parts = [...new Set([...D.this.rows.map(r => r[0]), ...t.keys()])];
  const mine = new Map(D.this.rows);
  const only = q('#donly').checked && D.test;
  const rows = parts.filter(p => !only || mine.get(p) !== t.get(p));
  q('#drows').innerHTML = rows.length ? rows.map(p => {
    const a = mine.get(p) ?? '', b = D.test ? (t.get(p) ?? '') : '';
    return `<tr${D.test && a !== b ? ' class="diff"' : ''}><td>${esc(p)}</td><td class="n">${esc(a)}</td><td class="n">${esc(b)}</td></tr>`;
  }).join('') : '<tr><td colspan="3" class="empty">No differences.</td></tr>';
  const used = {};
  D.this.items.forEach(i => { used[i.location] = (used[i.location] || 0) + 1; });
  const loc = q('#g_loc').value || '0';
  q('#g_loc').innerHTML = Object.entries(D.this.sizes).filter(([l, n]) => n > 0)
    .map(([l, n]) => `<option value="${l}">${esc(CONT[l])} (${Math.max(n - (used[l] || 0), 0)} free)</option>`).join('');
  q('#g_loc').value = loc;
  if(!q('#g_loc').value && q('#g_loc').options.length) q('#g_loc').selectedIndex = 0;
  if(document.activeElement !== q('#g_gil')) q('#g_gil').value = D.this.gil;
  const flagged = new Map(D.this.flags.map(f => [f.location + ':' + f.slot, f.why]));
  const items = D.this.items.filter(i => !q('#ionly').checked || flagged.has(i.location + ':' + i.slot));
  q('#items').innerHTML = items.length ? items.map(i => `<tr><td>${esc(CONT[i.location] || i.location)} ${i.slot}${i.equipped ? ' <span class="pill">worn</span>' : ''}</td>
    <td>${esc(i.name)} <span class="sub">${i.itemId}${i.quantity > 1 ? ' x' + i.quantity : ''}</span></td>
    <td class="sub">${esc(flagged.get(i.location + ':' + i.slot) || '')}${i.extra_len ? `<div class="n" style="font-size:11px">${esc(i.extra_hex.replace(/(..)/g, '$1 ').trim())}</div>` : ''}</td>
    <td><button class="rm" data-rmloc="${i.location}" data-rmslot="${i.slot}" data-rmname="${esc(i.name)}">Remove</button></td></tr>`).join('')
    : '<tr><td colspan="4" class="empty">' + (q('#ionly').checked ? 'Nothing flagged.' : 'No items.') + '</td></tr>';
}
const FLAGS = [
  {name: '[Console]DelayZoneInCS', title: 'Start arrival cutscenes 8 s after arriving',
   text: 'PS2 and Xbox only. The cutscene is left out of the zone-in and started once the area has had time to load.'},
  {name: '[Console]HideForZoneInCS', title: 'Hide bystanders in arrival cutscenes',
   text: 'PS2 and Xbox only. Players and NPCs that are not in the cutscene are not drawn during it.'},
  {name: '[RoV]ConsoleTest', title: 'Let the Rhapsodies city cutscenes through',
   text: 'PS2 and Xbox only. Other consoles never get them; this character gets them, with one zone-in in place first.'},
];
const zname = id => S.zone_names[id] ? S.zone_names[id].replace(/_/g, ' ').replace(/\b[a-z]/g, c => c.toUpperCase()) : 'Zone ' + id;
function heavyLine(h){ return (h.loads.length ? 'loads ' + h.loads.map(zname).join(', ') : '') + (h.reloads ? (h.loads.length ? '; ' : '') + 'reloads this zone' : ''); }
function drawHeavy(){
  const f = q('#hfilter').value.trim().toLowerCase();
  const rows = (S.heavy || []).filter(h => !f || (zname(h.zone) + ' ' + h.csid + ' ' + h.file).toLowerCase().includes(f));
  const canUse = C && C.is_test;
  q('#heavy').innerHTML = rows.length ? rows.map(h => `<tr><td>${esc(zname(h.zone))} <span class="sub">${h.zone}</span></td>
    <td class="n">${h.csid}</td><td>${esc(heavyLine(h))}</td>
    <td class="sub">${esc(h.file.replace(/^scripts\//, ''))}${h.conds ? '<br>' + esc(h.conds) : ''}</td>
    <td><button data-usecs="${h.csid}" data-usezone="${h.zone}"${canUse ? '' : ' disabled title="Open the test character first"'}>Set up</button>
      ${(S.cs_off || []).some(o => o.zone === h.zone && o.csid === h.csid) ? '<span class="pill">off on consoles</span>'
        : `<button class="rm" data-csoff="${h.csid}" data-cszone="${h.zone}">Turn off on consoles</button>`}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">None.</td></tr>';
}
function drawFlags(){
  const have = new Set(C.variables.filter(v => v.value).map(v => v.varname));
  q('#flags').innerHTML = FLAGS.map(f => `<label class="flag"><input type="checkbox" data-flag="${esc(f.name)}"${have.has(f.name) ? ' checked' : ''}>
    <span><b>${esc(f.title)}</b><div class="sub">${esc(f.text)} <span class="n">${esc(f.name)}</span></div></span></label>`).join('');
}
async function loadArrivals(to, pick){
  const j = await getj('chars/arrivals.json?to=' + to);
  q('#s_from').innerHTML = '<option value="0">Nowhere: log in straight into it</option>' +
    j.from.map(a => `<option value="${a.from}">${esc(zname(a.from))} (${a.from})</option>`).join('');
  if(pick != null) q('#s_from').value = String(pick);
  q('#s_heavy').innerHTML = '<option value="">' + (j.heavy.length ? 'Choose one' : 'None known in this zone') + '</option>' +
    j.heavy.map(h => `<option value="${h.csid}">${h.csid}: ${esc(heavyLine(h))}</option>`).join('');
}
function drawScenario(){
  q('#scard').hidden = !C.is_test;
  if(!C.is_test) return;
  const sc = C.scenario, st = q('#sstate');
  st.textContent = sc ? sc.words : 'no scenario armed';
  st.className = 'pill' + (sc && sc.Result === 2 ? ' on' : sc && sc.Result === 1 ? ' warn' : sc && sc.Armed ? ' acc' : '');
  q('#s_to').innerHTML = C.zones.map(z => `<option value="${z.zoneid}">${esc(z.name.replace(/_/g, ' '))} (${z.zoneid})</option>`).join('');
  const to = sc && sc.To ? sc.To : C.char.pos_zone;
  q('#s_to').value = String(to);
  if(sc){
    q('#s_csid').value = sc.Csid || ''; q('#s_wait').value = sc.Wait || 20; q('#s_delay').value = sc.Delay || 0;
    q('#s_pcs').checked = !!(sc.Flags & 2); q('#s_npcs').checked = !!(sc.Flags & 16);
    q('#s_rezone').checked = !!sc.PreRezone; q('#s_quiet').checked = sc.Quiet !== 0;
  }
  loadArrivals(to, sc ? sc.From : 0).then(() => { if(sc && sc.Csid) q('#s_heavy').value = String(sc.Csid); })
    .catch(e => say(e.message, false));
}

function drawVars(){
  const f = q('#vfilter').value.trim().toLowerCase();
  const rows = C.variables.filter(v => !f || v.varname.toLowerCase().includes(f));
  q('#vcount').textContent = rows.length + ' of ' + C.variables.length;
  q('#vars').innerHTML = rows.length ? rows.map(v => `<tr><td class="n">${esc(v.varname)}</td><td class="n">${v.value}</td>
    <td><button class="rm" data-delvar="${esc(v.varname)}">Delete</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">None.</td></tr>';
}

async function post(path, body, confirmText){
  if(BUSY) return;
  if(confirmText && !confirm(confirmText)) return;
  BUSY = true;
  let j = null;
  try{
    const r = await fetch(qs(path), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    j = await r.json();
    await loadState();
    if(C) await openChar(C.char.charid);
    say(j.msg || (j.ok ? 'Done.' : 'Refused.'), !!j.ok);
  }catch(e){ say('Failed: ' + e.message, false); }
  BUSY = false;
}

const iname = n => String(n).replace(/_/g, ' ');
async function findItem(){
  const text = q('#g_q').value.trim();
  if(!text) return;
  try{
    const j = await getj('chars/items.json?q=' + encodeURIComponent(text));
    q('#g_pick').hidden = !j.rows.length;
    if(!j.rows.length) return say('No item by that name or id.', false);
    q('#g_item').innerHTML = j.rows.map(r => `<option value="${r.itemid}">${esc(iname(r.name))} (${r.itemid})${r.stackSize > 1 ?
      ', stacks to ' + r.stackSize : ''}${r.flags & 0x8000 ? ', rare' : ''}${r.flags & 0x4000 ? ', ex' : ''}</option>`).join('');
    say('', true);
  }catch(e){ say(e.message, false); }
}
q('#g_find').onclick = findItem;
q('#g_q').onkeydown = e => { if(e.key === 'Enter') findItem(); };
q('#g_go').onclick = () => post('chars/additem', {id: C.char.charid, item: +q('#g_item').value, quantity: q('#g_qty').value.trim(),
  location: +q('#g_loc').value});
q('#g_gilset').onclick = () => post('chars/gil', {id: C.char.charid, gil: q('#g_gil').value.trim()});
q('#find').onclick = find;
q('#q').onkeydown = e => { if(e.key === 'Enter') find(); };
q('#vfilter').oninput = () => C && drawVars();
q('#hfilter').oninput = () => S && drawHeavy();
q('#donly').onchange = () => D && drawData();
q('#ionly').onchange = () => D && drawData();
q('#ccopy').onclick = () => {
  const groups = [...document.querySelectorAll('#cgroups input:checked')].map(i => i.value);
  const labels = groups.map(g => D.groups[g].toLowerCase()).join(', ');
  post('chars/copy', {id: C.char.charid, groups}, 'Copy ' + C.char.charname + "'s " + labels + ' onto the test character ' +
    (S.test ? S.test.charname : '') + '? Its current state is saved in the log and can be undone.');
};
q('#s_to').onchange = () => loadArrivals(+q('#s_to').value, 0).catch(e => say(e.message, false));
q('#s_heavy').onchange = () => { if(q('#s_heavy').value) q('#s_csid').value = q('#s_heavy').value; };
q('#s_arm').onclick = () => post('chars/scenario', {id: C.char.charid, to: +q('#s_to').value, from: +q('#s_from').value,
  csid: q('#s_csid').value.trim(), wait: q('#s_wait').value.trim(), delay: q('#s_delay').value.trim(),
  hide_pcs: q('#s_pcs').checked, hide_npcs: q('#s_npcs').checked, prerezone: q('#s_rezone').checked, quiet: q('#s_quiet').checked});
q('#s_clear').onclick = () => post('chars/unscenario', {id: C.char.charid});
document.addEventListener('change', e => {
  const f = e.target.dataset && e.target.dataset.flag;
  if(!f || !C) return;
  if(e.target.checked) post('chars/setvar', {id: C.char.charid, name: f, value: 1});
  else post('chars/delvars', {id: C.char.charid, name: f});
});
q('#mhome').onclick = () => post('chars/move', {id: C.char.charid, home: true});
q('#mgo').onclick = () => post('chars/move', {id: C.char.charid, zone: +q('#mzone').value, x: q('#mx').value, y: q('#my').value, z: q('#mz').value, rot: q('#mrot').value});
q('#o_go').onclick = () => post('chars/csoff', {zone: +q('#o_zone').value, csid: q('#o_csid').value.trim(), off: true},
  'Turn cutscene ' + q('#o_csid').value.trim() + ' in ' + zname(+q('#o_zone').value) + ' off for consoles? PS2 and Xbox players arriving to it will be let through and pointed to a Melody Minstrel.');
q('#eall').onclick = () => post('chars/unequip', {id: C.char.charid, all: true}, 'Take off everything ' + C.char.charname + ' has equipped?');
q('#xgo').onclick = () => {
  const parts = [q('#x_items').checked && 'items', q('#x_keys').checked && 'keyitems'].filter(Boolean);
  const what = parts.map(p => p === 'items' ? 'every item (gil stays) and everything worn' : 'every key item').join(' and ');
  post('chars/empty', {id: C.char.charid, parts}, 'Take ' + what + ' off ' + C.char.charname + '? Undo in the log puts them back.');
};
q('#vset').onclick = () => post('chars/setvar', {id: C.char.charid, name: q('#vname').value.trim(), value: q('#vvalue').value.trim()});
q('#vdelp').onclick = () => post('chars/delvars', {id: C.char.charid, prefix: q('#vprefix').value.trim()},
  'Delete every variable of ' + C.char.charname + ' starting with ' + q('#vprefix').value.trim() + '?');
document.addEventListener('click', e => {
  const t = e.target.closest('[data-open],[data-unequip],[data-delvar],[data-undo],[data-settest],[data-usecs],[data-rmloc],[data-lostcs],[data-csoff],[data-cson]');
  if(!t) return;
  const d = t.dataset;
  if(d.lostcs){
    q('#s_to').value = d.lostzone; q('#s_csid').value = d.lostcs;
    loadArrivals(+d.lostzone, +d.lostfrom).then(() => { q('#s_heavy').value = d.lostcs; q('#scard').scrollIntoView({block: 'start'}); });
    return;
  }
  if(d.rmloc != null) return post('chars/rmitem', {id: C.char.charid, location: +d.rmloc, slot: +d.rmslot},
    'Remove ' + d.rmname + ' from ' + C.char.charname + '? Undo puts it back.');
  if(d.csoff) return post('chars/csoff', {zone: +d.cszone, csid: +d.csoff, off: true},
    'Turn cutscene ' + d.csoff + ' in ' + zname(+d.cszone) + ' off for consoles? PS2 and Xbox players arriving to it will be let through and pointed to a Melody Minstrel.');
  if(d.cson) return post('chars/csoff', {zone: +d.cszone, csid: +d.cson, off: false});
  if(d.usecs){
    q('#s_to').value = d.usezone; q('#s_csid').value = d.usecs;
    loadArrivals(+d.usezone, 0).then(() => { q('#s_heavy').value = d.usecs; q('#scard').scrollIntoView({block: 'start'}); });
    return;
  }
  if(d.open) return openChar(d.open);
  if(d.unequip != null) return post('chars/unequip', {id: C.char.charid, slot: +d.unequip});
  if(d.delvar != null) return post('chars/delvars', {id: C.char.charid, name: d.delvar}, 'Delete ' + d.delvar + '?');
  if(d.undo) return post('chars/undo', {entry: +d.undo}, 'Undo change #' + d.undo + '? This puts back the rows it replaced, including anything changed in them since.');
  if(d.settest) return post('chars/settest', {id: +d.settest}, 'Use ' + C.char.charname + ' as the test character? Copies will overwrite it.');
  if(d.copy) return post('chars/copy', {id: +d.copy}, 'Copy ' + C.char.charname + ' onto the test character ' + (S.test ? S.test.charname : '') + '? Its current state is saved in the log and can be undone.');
});
loadState();
</script>
</body></html>
"""
