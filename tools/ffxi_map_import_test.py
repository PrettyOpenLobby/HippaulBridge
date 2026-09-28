"""`python ffxidb.py import idmap|accounts FILE`: the old map files into
PostgreSQL, and the bridge's hold on an empty map while an old file waits.

    python tools/ffxi_map_import_test.py

The old files are written by the bridge that kept its maps as files, taken
from git (OLD_COMMIT, the commit before the move to PostgreSQL) and run from a
temporary directory: its save_idmap() and save_acctmap() write
ffxi_idmap.json and ffxi_accounts.json exactly as a running bridge did. The
first, flat shape of the id map ({charid: content_id}) is no longer written
by any version, so a few flat entries are added by hand, the way an old file
still holds them, with entries the importer cannot map.

Checked against a fresh PostgreSQL database: the counts, every value, the
flat entries' defaults, the world tags, a second run that changes nothing,
--dry-run, the refusal on a table that already holds rows, --merge, a source
file that is byte for byte what it was, and hold_for_old_idmap().

Needs the OpenLobby core (OPENLOBBY_DIR, or ../openlobby) and PostgreSQL
(Docker or POL_TEST_DATABASE_URL); SKIPS without them, and fails under
POL_TEST_REQUIRE_DB=1.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
LSB = os.path.join(ROOT, "lsb")
sys.path.insert(0, HERE)
from openlobby_paths import require_database                      # noqa: E402
URL = require_database("ffxi_map_import_test")

sys.path.insert(0, LSB)
import ffxidb                                                      # noqa: E402
from polcore import db                                             # noqa: E402

#: The last commit whose bridge kept its maps as JSON files.
OLD_COMMIT = "3819b8c8cb7d58807ccbbc17071a36af7918423b"

fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  --  {detail}" if not cond else ""))
    if not cond:
        fails.append(name)


FIXTURE = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import ffxi_bridge as B
B._idmap.update({"17825793": 30000045, "17825794": 30000046, "17825795": 30000047})
B._charnames.update({"17825793": "Kharn", "17825794": "Lyra"})
B._worldfields.update({"17825793": 0x11223344})
B._charfields.update({"17825793": {"world": 1, "nation": 2, "zone": 230,
                                   "job": 3, "joblevel": 0, "race": 5}})
B.save_idmap()
B._acctmap.update({"5": {"login": "pol5", "created": "2026-09-01T10:00:00Z"},
                   "alt:5": {"login": "pol5", "created": "2026-09-02T11:00:00Z"},
                   "12": {"login": "pol12", "created": "2026-09-03T12:00:00Z"}})
B.save_acctmap()
'''


def old_files(base):
    """ffxi_idmap.json and ffxi_accounts.json as the old bridge wrote them."""
    probe = subprocess.run(["git", "cat-file", "-e", OLD_COMMIT + "^{commit}"],
                           cwd=ROOT, capture_output=True)
    if probe.returncode != 0:
        print(f"FAIL: this suite needs the file-based bridge at {OLD_COMMIT} "
              "(a shallow clone lacks it: git fetch --unshallow)")
        sys.exit(1)
    code = os.path.join(base, "old")
    os.makedirs(code)
    src = subprocess.run(["git", "show", f"{OLD_COMMIT}:lsb/ffxi_bridge.py"],
                         cwd=ROOT, capture_output=True, check=True).stdout
    with open(os.path.join(code, "ffxi_bridge.py"), "wb") as fh:
        fh.write(src)
    with open(os.path.join(base, "fixture.py"), "w", encoding="utf-8") as fh:
        fh.write(FIXTURE)
    idmap = os.path.join(base, "data", "ffxi_idmap.json")
    accts = os.path.join(base, "state", "ffxi_accounts.json")
    os.makedirs(os.path.dirname(idmap))
    os.makedirs(os.path.dirname(accts))
    env = dict(os.environ, FFXI_IDMAP_FILE=idmap, FFXI_ACCTMAP_FILE=accts)
    env.pop("POL_DATABASE_URL", None)
    p = subprocess.run([sys.executable, os.path.join(base, "fixture.py"), code],
                       env=env, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout + p.stderr)
        print("FAIL: the old bridge could not write its files")
        sys.exit(1)
    # the first shape, and entries no version could have mapped
    with open(idmap, encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.update({"17825800": 30000050, "17825801": "30000051",
                "not-a-charid": 30000052, "17825802": {"name": "NoId"},
                "17825803": {"content_id": 30000053, "profile": [1, 2]}})
    with open(idmap, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=1, sort_keys=True)
    with open(accts, encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.update({"alt:x": {"login": "polx", "created": "2026-09-04T00:00:00Z"},
                "13": {"login": "pol13"}})
    with open(accts, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=1, sort_keys=True)
    old = 1_700_000_000
    os.utime(idmap, (old, old))
    return idmap, accts


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest(), os.stat(path).st_mtime_ns


def run(store, path, *extra):
    """(exit status, printed report) of `python ffxidb.py import ...` in a
    process of its own, as an operator runs it."""
    p = subprocess.run([sys.executable, os.path.join(LSB, "ffxidb.py"), "import",
                        store, path] + list(extra), cwd=LSB,
                       env=dict(os.environ, POL_DATABASE_URL=URL),
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def fingerprint():
    out = []
    for t in (ffxidb.IDMAP_TABLE, ffxidb.ACCOUNT_TABLE):
        if db.query_one("SELECT to_regclass(%s) IS NOT NULL AS ok", (t,))["ok"]:
            out.append(sorted(repr(sorted(r.items())) for r in db.query(f"SELECT * FROM {t}")))
    return out


def main():
    base = tempfile.mkdtemp(prefix="ffxi-map-import-")
    try:
        idmap, accts = old_files(base)
        before = {p: digest(p) for p in (idmap, accts)}

        print("--dry-run: a report, and nothing written (not even the tables)")
        code, out = run("idmap", idmap, "--dry-run")
        check("exit 0", code == 0, out)
        check("says so", "Dry run: nothing was written." in out, out)
        check("plans every mappable entry", "to insert             5" in out, out)
        check("no table was created",
              db.query_one("SELECT to_regclass('ffxi_idmap') IS NULL AS gone")["gone"])

        print("import idmap")
        code, out = run("idmap", idmap)
        check("exit 0", code == 0, out)
        check("8 entries read", "entries in the file   8" in out, out)
        for bad in ("'not-a-charid'", "'17825802'", "'17825803'"):
            check(f"entry {bad} reported and skipped", f"skipped {bad}" in out, out)
        rows = {r["charid"]: r for r in ffxidb.load_idmap()}
        check("five rows", sorted(rows) == [17825793, 17825794, 17825795,
                                           17825800, 17825801], str(sorted(rows)))
        k = rows.get(17825793) or {}
        check("content_id, name and world_field copied",
              (k.get("content_id"), k.get("name"), k.get("world_field"))
              == (30000045, "Kharn", 0x11223344), str(k))
        check("the profile as JSONB",
              k.get("profile") == {"world": 1, "nation": 2, "zone": 230, "job": 3,
                                   "joblevel": 0, "race": 5}, str(k.get("profile")))
        check("seen carried over", str(k.get("seen", "")).endswith("Z"), str(k))
        l = rows.get(17825795) or {}
        check("an entry the old bridge saw no 0x20 for: NULL profile, name ''",
              (l.get("profile"), l.get("name"), l.get("world_field"))
              == (None, "", 0), str(l))
        f = rows.get(17825800) or {}
        check("a flat entry: name '' and world_field 0, NULL profile",
              (f.get("content_id"), f.get("name"), f.get("world_field"), f.get("profile"))
              == (30000050, "", 0, None), str(f))
        check("a flat entry's seen is the file's time",
              f.get("seen") == "2023-11-14T22:13:20Z", str(f.get("seen")))
        check("a Content ID written as text", (rows.get(17825801) or {}).get("content_id")
              == 30000051)
        check("the change counter moved (the title plugin re-reads)",
              (ffxidb.idmap_rev() or 0) > 0)

        print("import accounts")
        code, out = run("accounts", accts)
        check("exit 0", code == 0, out)
        acc = {(r["world_tag"], r["member_id"]): r for r in ffxidb.load_accounts()}
        check("'5' -> ('', 5) and 'alt:5' -> ('alt', 5)",
              sorted(acc) == [("", 5), ("", 12), ("alt", 5)], str(sorted(acc)))
        check("login and created carry over",
              (acc.get(("alt", 5)) or {}).get("created") == "2026-09-02T11:00:00Z"
              and (acc.get(("", 12)) or {}).get("login") == "pol12", str(acc))
        check("an entry with no member id, and one with no created, are skipped",
              "skipped 'alt:x'" in out and "skipped '13'" in out, out)

        print("a second run changes nothing")
        fp = fingerprint()
        rev = ffxidb.idmap_rev()
        for store, path in (("idmap", idmap), ("accounts", accts)):
            code, out = run(store, path)
            check(f"{store}: exit 0, nothing to import",
                  code == 0 and "Nothing to import" in out, out)
            code, out = run(store, path, "--merge")
            check(f"{store} --merge: nothing either",
                  code == 0 and "Nothing to import" in out, out)
        check("every row as it was", fingerprint() == fp)
        check("the change counter did not move", ffxidb.idmap_rev() == rev)

        print("a table that already holds rows: refused, then --merge")
        with open(idmap, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["17825900"] = 30000060
        raw["17825794"] = {"content_id": 30000099, "name": "Lyra"}
        idmap2 = os.path.join(base, "data", "ffxi_idmap2.json")
        with open(idmap2, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        code, out = run("idmap", idmap2)
        check("refused: exit 2", code == 2, out)
        check("says why", "REFUSED" in out and "--merge" in out, out)
        check("nothing written", fingerprint() == fp)
        code, out = run("idmap", idmap2, "--dry-run", "--merge")
        check("--merge --dry-run writes nothing", code == 0 and fingerprint() == fp, out)
        code, out = run("idmap", idmap2, "--merge")
        check("--merge: exit 0, one row", code == 0 and "Done: 1 row(s) written." in out, out)
        rows = {r["charid"]: r for r in ffxidb.load_idmap()}
        check("the new charid is in", (rows.get(17825900) or {}).get("content_id") == 30000060)
        check("a charid in both keeps the table's Content ID",
              (rows.get(17825794) or {}).get("content_id") == 30000046)
        check("and the report names it", "kept the table's row, the file's differs: (17825794,)"
              in out, out)

        print("bad sources")
        code, out = run("idmap", os.path.join(base, "missing.json"))
        check("a missing file: exit 1", code == 1 and "cannot read" in out, out)
        lst = os.path.join(base, "list.json")
        with open(lst, "w") as fh:
            fh.write("[1, 2]")
        code, out = run("accounts", lst)
        check("a file that is not an object: exit 1", code == 1, out)

        check("the source files are byte for byte what they were",
              {p: digest(p) for p in (idmap, accts)} == before)

        print("the bridge holds while the table is empty and an old file waits")
        import ffxi_bridge as B
        logged = []
        B.log = lambda tag, msg: logged.append(msg)
        B.OLD_IDMAP_FILES = [os.path.join(base, "nowhere.json"), idmap]
        B._idmap.clear()
        B.IDMAP_START_EMPTY = False
        orig_load = B.load_idmap
        B.load_idmap = lambda: None
        check("held, not opened", B.hold_for_old_idmap(every=0, rounds=2) is False)
        check("and says what to run", any("NOT STARTING" in m and
                                          "ffxidb.py import idmap" in m for m in logged),
              str(logged))
        B.IDMAP_START_EMPTY = True
        check("FFXI_IDMAP_START_EMPTY=1 opens on the empty table",
              B.hold_for_old_idmap(every=0, rounds=2) is True)
        B.IDMAP_START_EMPTY = False
        B.OLD_IDMAP_FILES = [os.path.join(base, "nowhere.json")]
        check("no old file: opens (a new stack)", B.hold_for_old_idmap(every=0, rounds=2))
        empty = os.path.join(base, "empty.json")
        with open(empty, "w") as fh:
            fh.write("{}")
        B.OLD_IDMAP_FILES = [empty]
        check("an old file with no pairings: opens", B.hold_for_old_idmap(every=0, rounds=2))
        B.OLD_IDMAP_FILES = [idmap]
        B.load_idmap = orig_load
        check("once the table holds rows (read on the next round): opens",
              B.hold_for_old_idmap(every=0, rounds=3) is True and bool(B._idmap))
    finally:
        db.close()
        shutil.rmtree(base, ignore_errors=True)

    print()
    print("FAIL: %d check(s)" % len(fails) if fails else "ALL PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
