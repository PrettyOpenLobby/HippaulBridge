#!/usr/bin/env python3
"""The FFXI title plugin: world identity and profile tail out of the bridge's id map.

    python tools/ffxi_title_test.py

Needs the OpenLobby core checked out beside this repository (or OPENLOBBY_DIR
pointing at it) for `titles.py`. Offline; writes an id map in a temp directory.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENLOBBY = os.environ.get("OPENLOBBY_DIR", os.path.join(ROOT, os.pardir, "openlobby"))
sys.path.insert(0, os.path.join(OPENLOBBY, "services"))
sys.path.insert(0, os.path.join(ROOT, "lsb"))

TMP = tempfile.mkdtemp(prefix="ffxi-title-")
IDMAP = os.path.join(TMP, "ffxi_idmap.json")
os.environ["POL_FFXI_IDMAP"] = IDMAP

import titles          # noqa: E402
import ffxititle       # noqa: E402

CTID = 30000101
CHARID = 0x040506
FAILS = []


def check(ok, label, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  --  {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def write_map(entries):
    with open(IDMAP, "w", encoding="utf-8") as fh:
        json.dump(entries, fh)
    # a fresh mtime is what tells the plugin to re-read; keep it moving
    ffxititle._cache.update(mtime=None, map={}, prof={})


def main():
    t = ffxititle.register()
    check(titles.for_code(1) is t, "registers as content code 1")

    print("\nno id map yet ->")
    if os.path.exists(IDMAP):
        os.remove(IDMAP)
    ffxititle._cache.update(mtime=None, map={}, prof={})
    check(titles.character_world(1, CTID) is None,
          "no map: no world identity (the core then serves 0)")
    check(titles.profile_fields(1, CTID, 7) == {},
          "no map: the profile tail stays UNSET, not zeroed")

    print("\nthe bridge's record of LSB's own char list ->")
    write_map({str(CHARID): {"content_id": CTID, "name": "Foxffxi",
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
    with open(IDMAP, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    os.utime(IDMAP, None)
    ffxititle._cache["mtime"] = None
    check(titles.character_world(1, CTID) == want,
          "keeps the previous map rather than serving nothing")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: " + ", ".join(FAILS))
        return 1
    print("all FFXI title checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
