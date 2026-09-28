"""ffxititle.world_fields: a failed read must NOT become a sticky POL-0001.

    python tools/ffxi_idmap_read_test.py

WHY (2026-09-02). The FFXI id map is written by the bridge in one container and
read by the title plugin's `world_fields()` in another, which caches it. Two
faults once made an intermittent write race into a durable POL-0001:

  * The bridge truncated the map file in place, so a read landing mid-write
    parsed `{}` -- world_field 0, which is the POL-0001 condition. (The map is
    a table now, written in one transaction, so a reader cannot see half of
    it; a read can still FAIL, which is what this pins.)
  * On any read failure `world_fields` stored `{}` under the version it had
    observed and returned it. If no later write moved that version, every
    subsequent `1:3` was served the empty map -- the failure went sticky, not
    one-shot.

The fix keeps the last good map and retries on the next fetch instead of
caching the empty result. It also derives a world field for an entry the
bridge has not recorded one for (world_field 0, as a map imported from the
old flat file shape has), which it used to skip -- serving world field 0,
i.e. POL-0001, for a map the bridge considered valid.

This test drives the real function through those cases against a fresh
PostgreSQL database. No network beyond the database. Needs the OpenLobby
core's `services/titles.py` and `tools/pgtest.py`: set OPENLOBBY_DIR to a
checkout of it, or keep one beside this repository as ../openlobby. Without
it the suite SKIPS (exit 77) rather than failing.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from openlobby_paths import require_database                      # noqa: E402
require_database("ffxi_idmap_read_test")

sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxidb                                                      # noqa: E402
import ffxititle                                                   # noqa: E402
from polcore import db                                             # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  --  {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


def _reset_cache():
    ffxititle._cache.update(rev=None, map={}, prof={})
    ffxititle._missing_warned.clear()


def write(entries):
    """Make the table hold exactly `{charid: (content_id, world_field)}`."""
    ffxidb.write_idmap([dict(charid=int(k), content_id=cid, world_field=field,
                             seen="2026-09-28T00:00:00Z")
                        for k, (cid, field) in entries.items()], replace=True)


def main():
    # 1) a row with an explicit world_field is served verbatim.
    _reset_cache()
    write({"17825793": (30000045, 0x11223344)})
    m = ffxititle.world_fields()
    check("a row's world_field is served",
          m.get(30000045) == 0x11223344, str(m))
    good = dict(m)

    # 2) a FAILED read must keep the last good map, not cache {}. The change
    #    counter moves (so the cache does not short-circuit the read) and the
    #    table then cannot be read.
    db.execute("UPDATE ffxi_idmap_rev SET rev = rev + 1")
    db.execute("ALTER TABLE ffxi_idmap RENAME TO ffxi_idmap_away")
    m = ffxititle.world_fields()
    check("a failed read keeps the previous map (no POL-0001)",
          m == good and m.get(30000045) == 0x11223344, str(m))
    db.execute("ALTER TABLE ffxi_idmap_away RENAME TO ffxi_idmap")

    # 3) ...and it is NOT cached: once the table reads again, the new map is
    #    served on the very next fetch (the failure was one-shot, not sticky).
    write({"17825793": (30000045, 0x55), "17825794": (30000046, 0x66)})
    m = ffxititle.world_fields()
    check("the recovered map is served on the next fetch",
          m.get(30000045) == 0x55 and m.get(30000046) == 0x66, str(m))

    # 4) an entry with no recorded world field gets a DERIVED one, not 0.
    _reset_cache()
    write({"17825793": (30000045, 0)})
    m = ffxititle.world_fields()
    check("an entry with no world field yields a non-zero derived one (not POL-0001)",
          m.get(30000045, 0) != 0, str(m))

    print(f"\n{'ffxi_idmap_read: OK' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
