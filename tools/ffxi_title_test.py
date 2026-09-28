#!/usr/bin/env python3
"""The FFXI title plugin: world identity and profile tail out of the bridge's id map.

    python tools/ffxi_title_test.py

Needs the OpenLobby core checked out beside this repository (or OPENLOBBY_DIR
pointing at it) for `titles.py` and its tools/pgtest.py: the id map is a table
in a fresh PostgreSQL database the suite gets from there.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from openlobby_paths import require_database  # noqa: E402
require_database("ffxi_title_test")
sys.path.insert(0, os.path.join(ROOT, "lsb"))

import titles          # noqa: E402
import ffxititle       # noqa: E402
import ffxidb          # noqa: E402
from polcore import db  # noqa: E402

CTID = 30000101
CHARID = 0x040506
FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def write_map(entries):
    """Replace the bridge's map with `{charid: row}`. The plugin's cache is
    left alone on purpose: the change counter the write moves is what must
    tell it to re-read."""
    ffxidb.write_idmap([dict(charid=int(k), content_id=v["content_id"],
                             name=v.get("name", ""),
                             world_field=v.get("world_field", 0),
                             profile=v.get("profile"), seen="2026-09-28T00:00:00Z")
                        for k, v in entries.items()], replace=True)


def main():
    t = ffxititle.register()
    check(titles.for_code(1) is t, "registers as content code 1")

    print("\nno id map yet ->")
    ffxititle._cache.update(rev=None, map={}, prof={})
    check(ffxidb.idmap_rev() is None,
          "a database the bridge never ran on has no map table")
    check(titles.character_world(1, CTID) is None,
          "no map: no world identity (the core then serves 0)")
    check(titles.profile_fields(1, CTID, 7) == {},
          "no map: the profile tail stays UNSET, not zeroed")

    print("\nthe bridge's record of LSB's own char list ->")
    write_map({str(CHARID): {"content_id": CTID, "name": "Lexffxi",
                             "world_field": 1,
                             # zone 291 is the case that matters: LSB splits the
                             # zone across zone_no and zone_no2, so a reader that
                             # takes the low byte alone shows zone 35 instead
                             "profile": {"world": "Bahamut", "nation": 0,
                                         "zone": 291, "job": 5, "joblevel": 62,
                                         "race": 7}}})
    f = titles.profile_fields(1, CTID, 7)
    check(f.get(ffxititle.SLOT_WORLD) == "Bahamut" and f.get(ffxititle.SLOT_JOB) == 5
          and f.get(ffxititle.SLOT_JOBLEVEL) == 62 and f.get(ffxititle.SLOT_RACE) == 7,
          "world/job/level/race come off the bridge's record", repr(f))
    check(f.get(ffxititle.SLOT_ZONE) == 291, "zone keeps its 9th bit (zone_no2)")
    check(ffxititle.SLOT_NATION in f and f[ffxititle.SLOT_NATION] == 0,
          "nation 0 is San d'Oria, not 'missing': it must still be SET")
    check(titles.character_world(1, CTID) == 1,
          "a recorded world_field is served as is")

    print("\na charid with no recorded world_field ->")
    write_map({str(CHARID): {"content_id": CTID}})
    want = ffxititle.derive_world_field(CHARID)
    check(titles.character_world(1, CTID) == want,
          "the world identity is derived from the charid and world id",
          f"0x{want:08X}")
    check(titles.character_world(2, CTID) is None,
          "another content code is not this title's")

    print("\nan unreadable map ->")
    # The counter says the map moved, and then the map cannot be read.
    db.execute("UPDATE ffxi_idmap_rev SET rev = rev + 1")
    db.execute("ALTER TABLE ffxi_idmap RENAME TO ffxi_idmap_away")
    check(titles.character_world(1, CTID) == want,
          "keeps the previous map rather than serving nothing")
    db.execute("ALTER TABLE ffxi_idmap_away RENAME TO ffxi_idmap")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: " + ", ".join(FAILS))
        return 1
    print("all FFXI title checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
