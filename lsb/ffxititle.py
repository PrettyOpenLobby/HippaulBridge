"""The FINAL FANTASY XI title plugin for the OpenLobby core.

The bridge (ffxi_bridge.py) runs as its own service; this small module is the
part of FFXI that has to live INSIDE the core's login process, because the
core builds two things per Content ID that only the bridge knows:

- the WORLD IDENTITY dword served at +0x0C of a character record (1:3) and as
  `z_ctsid` in the profile's content block. The client's world connect
  insists on it; a zero here is POL-0001;
- the content profile's tail (world name, nation, zone, job, job level, race),
  which the bridge copies straight off LSB's own char-list record.

It also carries FFXI's one rule about the account database: FFXI issues one
Content ID per CHARACTER, so a handle holding FFXI is given CHARACTER_SLOTS of
them (`content_slots`, which the core's accounts module reads), and the
operator command that trims the extra ones back (`trim-slots`, below).

Both come from the id map the bridge writes on the shared data volume
(FFXI_IDMAP_FILE there, POL_FFXI_IDMAP here): `{charid: {"content_id": N,
"world_field": dword, "profile": {...}}}`. The map is re-read whenever its
mtime moves, because the bridge rewrites it the moment a character is created
and the client re-fetches 1:3 about three seconds later.

Loaded with POL_TITLES=ffxititle in the core's login and authsess services;
see docker-compose.title.yml.

    python ffxititle.py DB trim-slots [--apply] [--handle NAME] [--idmap PATH]
                                      [--force] [--restore]
"""
import argparse
import json
import os
import sys

import titles

#: FFXI's content code: the N of prof_001.pfb, and the value the client's
#: world lookup insists on finding at character-table +0x02.
CONTENT_CODE = 1

#: the profile's slots for the six values the bridge records (prof_001.pfb)
SLOT_WORLD, SLOT_NATION, SLOT_ZONE, SLOT_JOB, SLOT_JOBLEVEL, SLOT_RACE = 6, 7, 8, 9, 10, 11

IDMAP = os.environ.get("POL_FFXI_IDMAP", "/data/ffxi_idmap.json")
#: Only used to DERIVE a world field for a character the bridge has recorded
#: a charid for but not yet a world_field. Must agree with the bridge's
#: FFXI_WORLD_ID / FFXI_WORLD_ID_FIX.
WORLD_ID = int(os.environ.get("POL_FFXI_WORLD_ID", "0x20"), 0)

#: How many FFXI Content IDs a handle is given, i.e. how many FFXI characters
#: it can actually play. SE sells these; we grant them, because nothing on our
#: side sells anything and a player who makes a second character and cannot log
#: into it has no way to tell that from a bug (they get POL-0001, which says
#: nothing). The bridge offers a member's unspent ids as the client's empty
#: character slots, so slots that exist are slots the player can create into.
#:
#: WARNING: DEFAULT 1, AND EIGHT PER HANDLE IS A HARD CEILING ACROSS EVERY
#: TITLE (accounts.CONTENT_IDS_PER_HANDLE). A handle holds one link per title
#: and there are eight titles, so 1 is the only value that cannot collide with
#: a later title grant. Raising it is safe only on a deployment that grants
#: fewer titles: the mint clamps to the ceiling and the core truncates the
#: wire, but a title granted AFTER the extra ids exist still pushes them out,
#: and an FFXI character sitting on a pushed-out id is POL-0001 with no
#: explanation. `trim-slots` cleans that up.
CHARACTER_SLOTS = max(1, int(os.environ.get("POL_FFXI_CHARACTER_SLOTS") or 1))

_cache = {"mtime": None, "map": {}, "prof": {}}
_missing_warned = set()


def _log(text):
    titles.core.log("lobby", text)


def derive_world_field(charid, world_id=WORLD_ID):
    """The world identity dword for a charid, packed the way the bridge packs
    it: world id, the charid's top byte, then its low 16 bits."""
    cid24 = int(charid) & 0xFFFFFF
    return (((((world_id & 0xFFFF) << 8) | (cid24 & 0xFFFF0000)) << 8)
            | (cid24 & 0xFFFF)) & 0xFFFFFFFF


def world_fields():
    """`{Content ID: world identity dword}` from the bridge's id map.

    A charid with no recorded world_field gets one derived here from the same
    packing. Without that, a map written before this field existed (or by a
    bridge that has not seen a char list yet) would cost a whole extra
    launch-relogin-launch cycle before FFXI could connect: the table is filled
    at POL login and the field is only learned once the game is already
    running. The recorded value always wins; it is what the client was told.
    """
    try:
        mtime = os.path.getmtime(IDMAP)
    except OSError:
        if IDMAP not in _missing_warned:
            _missing_warned.add(IDMAP)
            _log(f"WARNING: FFXI id map {IDMAP} does not exist: every FFXI "
                 f"character will be served world field 0, and FFXI will refuse "
                 f"the world connect (POL-0001). If the bridge is running, "
                 f"POL_FFXI_IDMAP disagrees with its FFXI_IDMAP_FILE.")
        return {}
    _missing_warned.discard(IDMAP)
    if _cache["mtime"] == mtime:
        return _cache["map"]
    out, prof = {}, {}
    try:
        with open(IDMAP, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for charid, ent in raw.items():
            if isinstance(ent, dict):
                if not ent.get("content_id"):
                    continue
                cid = int(ent["content_id"])
                field = int(ent.get("world_field") or 0) & 0xFFFFFFFF
                if isinstance(ent.get("profile"), dict):
                    prof[cid] = dict(ent["profile"])
            else:
                cid = int(ent)
                field = 0
            if not field:
                field = derive_world_field(charid)
            out[cid] = field
    except Exception as exc:
        _log(f"FFXI id map {IDMAP} unreadable ({exc!r}); keeping the previous "
             f"map ({len(_cache['map'])} entries) and retrying on the next fetch")
        return _cache["map"]
    _cache["mtime"] = mtime
    _cache["map"] = out
    _cache["prof"] = prof
    return out


def char_fields(cid):
    """The profile tail the bridge recorded for one Content ID, or {}.

    Goes through `world_fields` so the two share one mtime poll and can never
    be a write apart. Empty for a character whose char list the bridge has not
    seen; the profile then leaves those fields unset, which is correct: a zero
    Job Level would read as a fact.
    """
    try:
        world_fields()
    except Exception:
        return {}
    return _cache.get("prof", {}).get(int(cid)) or {}


def ids_in_use(path=None):
    """`{Content ID (str): character name}` for every FFXI character the bridge
    has paired. Raises OSError or ValueError if the map cannot be read.

    WARNING: **A FAILURE HERE IS NOT "NOTHING IS IN USE".** This is the check
    that stands between an operator and deactivating the Content ID a live
    character is named after; that character then misses POL's table and is
    POL-0001 at select, for ever, with nothing on screen to explain it. So it
    RAISES rather than returning an empty dict, and `trim-slots` refuses to
    write when it does.

    Both on-disk shapes `ffxi_bridge.save_idmap` has used are read: the
    original flat `{"<charid>": <ContentID>}` and the current
    `{"<charid>": {"content_id": N, "name": "Lex", ...}}`. An entry in neither
    shape raises too: a map we cannot parse is a map we cannot vouch for.
    """
    path = path or IDMAP
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError("%s: expected an object, got %s"
                         % (path, type(raw).__name__))
    out = {}
    for charid, val in raw.items():
        if isinstance(val, dict):
            out[str(int(val["content_id"]))] = val.get("name") or ("charid " + str(charid))
        else:
            out[str(int(val))] = "charid " + str(charid)
    return out


class FinalFantasyXI(titles.Title):
    tag = b"XI0"
    content_code = CONTENT_CODE
    content_slots = CHARACTER_SLOTS

    def describe(self):
        return f"FFXI id map {IDMAP}"

    def character_world(self, cid):
        try:
            return world_fields().get(int(cid))
        except (TypeError, ValueError):
            return None

    def profile_fields(self, cid, member_id):
        # STRAIGHT OFF LSB'S OWN CHAR-LIST RECORD, via the bridge. prof_001's
        # six tail fields and LSB's record are the same six values with the
        # same numbering (nation 0..2, zone id, job 1..20, race 1..8): both
        # ends are Square Enix's, so nothing is mapped or rescaled here.
        fx = char_fields(cid)
        out = {}
        if fx.get("world"):
            out[SLOT_WORLD] = fx["world"]
        for key, slot in (("nation", SLOT_NATION), ("zone", SLOT_ZONE),
                          ("job", SLOT_JOB), ("joblevel", SLOT_JOBLEVEL),
                          ("race", SLOT_RACE)):
            if fx.get(key) is not None:
                out[slot] = int(fx[key])
        return out


def register():
    return titles.register(FinalFantasyXI())


# --- operator command ------------------------------------------------------- #
def trim_slots(conn, accounts, apply=False, handle=None, idmap=None,
               force=False, restore=False):
    """Deactivate the EXTRA FFXI Content IDs (slot <> 0) that push a handle
    past the core's ceiling of eight, which makes the Viewer refuse to open
    FFXI from it at all (string 26069). Reports by default; `apply` writes.
    Skips any id a character is already on. Returns the exit status.

    What it deactivates is recorded in `handle_content_trimmed`, so `restore`
    puts exactly that back.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS handle_content_trimmed ("
        " handle_id INTEGER NOT NULL, content_code INTEGER NOT NULL,"
        " slot INTEGER NOT NULL, content_id TEXT, prev_status TEXT,"
        " trimmed_at TEXT)")
    if restore:
        n = 0
        for r in conn.execute("SELECT * FROM handle_content_trimmed").fetchall():
            n += conn.execute(
                "UPDATE handle_content SET status = ? WHERE handle_id = ?"
                " AND content_code = ? AND slot = ?",
                (r["prev_status"], r["handle_id"], r["content_code"],
                 r["slot"])).rowcount
        conn.execute("DELETE FROM handle_content_trimmed")
        conn.commit()
        print("restored %d row(s)" % n)
        return 0

    # WHAT IS ON THESE IDS COMES FIRST. A row here is only safe to deactivate
    # if no character is named after its Content ID; see ids_in_use, which
    # raises rather than shrugging.
    inuse, why = {}, None
    try:
        inuse = ids_in_use(idmap)
    except Exception as exc:
        why = "%s: %s" % (exc.__class__.__name__, exc)
        print("WARNING: could not read the FFXI id map (%s)" % why)
        print("  so this cannot tell which ids have a character on them.")

    ceiling = accounts.CONTENT_IDS_PER_HANDLE
    q = ("SELECT h.id AS hid, h.handle_name, c.slot, c.content_id, c.status"
         "  FROM handle_content c JOIN handle h ON h.id = c.handle_id"
         " WHERE c.content_code = ? AND c.slot <> 0")
    params = [CONTENT_CODE]
    if handle:
        q += " AND h.handle_name = ?"
        params.append(handle)
    rows = conn.execute(q + " ORDER BY h.id, c.slot", params).fetchall()
    if not rows:
        print("no extra FFXI Content IDs: every handle is already at one")
        return 0

    free, skipped = 0, 0
    for r in rows:
        who = inuse.get(str(r["content_id"]))
        if r["status"] == "active":
            skipped += 1 if who else 0
            free += 0 if who else 1
        print("  handle %3d %-16s slot %d  id %-10s %-8s %s"
              % (r["hid"], r["handle_name"], r["slot"], r["content_id"],
                 r["status"], ("<-- %s IS ON THIS ID" % who) if who else ""))
    over = sum(1 for hid in set(r["hid"] for r in rows)
               if accounts.handle_link_count(conn, hid) > ceiling)
    print("%d extra row(s); %d active and free to trim, %d active with a "
          "character on them, %d handle(s) over the ceiling of %d"
          % (len(rows), free, skipped, over, ceiling))

    if not apply:
        print("report only -- pass --apply to write")
        return 0
    if why and not force:
        print("REFUSING to write: the id map could not be read, so a trim could "
              "silently kill a live character. Fix the path (--idmap / "
              "POL_FFXI_IDMAP) or pass --force.", file=sys.stderr)
        return 1
    now = accounts._now()
    n = 0
    for r in rows:
        if r["status"] != "active":
            continue
        if inuse.get(str(r["content_id"])) and not force:
            continue
        conn.execute(
            "INSERT INTO handle_content_trimmed (handle_id, content_code,"
            " slot, content_id, prev_status, trimmed_at) VALUES (?,?,?,?,?,?)",
            (r["hid"], CONTENT_CODE, r["slot"], r["content_id"],
             r["status"], now))
        n += conn.execute(
            "UPDATE handle_content SET status = 'inactive' WHERE handle_id = ?"
            " AND content_code = ? AND slot = ?",
            (r["hid"], CONTENT_CODE, r["slot"])).rowcount
    conn.commit()
    print("deactivated %d row(s); `trim-slots --restore` puts them back" % n)
    print("read live per request -- do NOT restart anything to apply it")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="ffxititle.py",
        description="FFXI operator commands on the core's account database.")
    ap.add_argument("db", help="path to accounts.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("trim-slots", help="deactivate the extra FFXI Content IDs that push a "
                       "handle past the ceiling of eight; reports unless --apply")
    p.add_argument("--apply", action="store_true",
                   help="actually write; without it this only reports")
    p.add_argument("--handle", help="just this handle (name), not every one")
    p.add_argument("--idmap", help="path to the bridge's id map (default "
                                   "POL_FFXI_IDMAP, else /data/ffxi_idmap.json)")
    p.add_argument("--force", action="store_true",
                   help="write even when the idmap is unreadable, or when a "
                        "character IS on an id. That character becomes "
                        "unplayable (POL-0001 at select). Say so out loud first")
    p.add_argument("--restore", action="store_true",
                   help="put back what a previous --apply deactivated "
                        "(reads handle_content_trimmed)")
    args = ap.parse_args(argv)
    import accounts                      # the core's, beside this module
    conn = accounts.connect(args.db)
    try:
        return trim_slots(conn, accounts, apply=args.apply, handle=args.handle,
                          idmap=args.idmap, force=args.force,
                          restore=args.restore)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
