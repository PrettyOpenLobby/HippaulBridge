"""The FINAL FANTASY XI title plugin for the OpenLobby core.

The bridge (ffxi_bridge.py) runs as its own service; this small module is the
part of FFXI that has to live INSIDE the core's login process, because the
core builds two things per Content ID that only the bridge knows:

- the WORLD IDENTITY dword served at +0x0C of a character record (1:3) and as
  `z_ctsid` in the profile's content block. The client's world connect
  insists on it; a zero here is POL-0001;
- the content profile's tail (world name, nation, zone, job, job level, race),
  which the bridge copies straight off LSB's own char-list record.

Both come from the id map the bridge writes on the shared data volume
(FFXI_IDMAP_FILE there, POL_FFXI_IDMAP here): `{charid: {"content_id": N,
"world_field": dword, "profile": {...}}}`. The map is re-read whenever its
mtime moves, because the bridge rewrites it the moment a character is created
and the client re-fetches 1:3 about three seconds later.

Loaded with POL_TITLES=ffxititle in the core's login and authsess services;
see docker-compose.title.yml.
"""
import json
import os

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


class FinalFantasyXI(titles.Title):
    tag = b"XI0"
    content_code = CONTENT_CODE

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
