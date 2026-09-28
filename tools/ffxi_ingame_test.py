#!/usr/bin/env python3
"""The FFXI in-world feed: bridge snapshot -> title plugin -> presence record.

    python tools/ffxi_ingame_test.py

The bridge polls LSB's `accounts_sessions` and publishes which character each
POL member is in the world as (`ffxi_bridge.ingame_snapshot` /
`publish_ingame`, the live-state key `ffxi:ingame`); the title plugin inside
authsess reads it (`ffxititle.presence_character`) and the core puts it in
the friend-status record's 0x08 field. The field layout comes from
LandSandBoat's xi_profile, so this checks our bytes against their struct and
nothing more. Whether the Viewer draws it is a live question.

  1. SNAPSHOT: members resolve by LOGIN through the account map (a rehomed
     account keeps its old login); unknown logins, other worlds' accounts and
     charids with no Content ID are dropped.
  2. HANDOFF: what the bridge publishes is what the plugin reads.
  3. GATE: POL_FFXI_INGAME_PUSH off -> no character and nothing to watch; on,
     the plugin answers with (world field, Content ID).
  4. RECORD: through the core, only in zone 1 (FFXI), with the bytes
     `u16 1 | u16 1 | u32 world field | u64 Content ID`.

Needs the OpenLobby core beside this repository (or OPENLOBBY_DIR /
OPENLOBBY_SERVICES); the live-state store is the core's in-process one.
"""
import os
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from openlobby_paths import require_services  # noqa: E402
require_services("ffxi_ingame_test")
sys.path.insert(0, os.path.join(ROOT, "lsb"))

TMP = tempfile.mkdtemp(prefix="ffxi-ingame-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ.pop("POL_VALKEY_URL", None)          # the in-process store
os.environ.pop("POL_FFXI_INGAME_PUSH", None)

import ffxi_bridge as B  # noqa: E402
import ffxititle  # noqa: E402
import responders as R  # noqa: E402

FAILS = []


def check(ok, label, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", label,
                           "  --  " + detail if detail else ""))
    if not ok:
        FAILS.append(label)


def b64decode(s):
    inv = {c: i for i, c in enumerate(R._B64)}
    out = bytearray()
    for i in range(0, len(s), 4):
        v = 0
        for c in s[i:i + 4]:
            v = (v << 6) | inv[c]
        v <<= 6 * (4 - len(s[i:i + 4]))
        out += bytes([(v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])
    return bytes(out)


print("1. bridge snapshot")
acct = {"1": {"login": "pol1"}, "7": {"login": "pol3"},   # 7 was rehomed
        "t2:9": {"login": "t2pol9"}}                       # another world
idmap = {"1": 1000000101, "4": 1000000701, "5": 1000000901}
names = {"1": "Lex", "4": "Wren", "5": "Moss"}
fields = {"1": 2097153, "4": 2097156, "5": 2097157}
rows = [("pol1", 1), ("pol3", 4), ("poltest", 9), ("pol1", 99), ("t2pol9", 5)]
snap = B.ingame_snapshot(rows, acct, idmap, names, fields)
check(set(snap) == {"1", "7"}, "only our logins with a Content ID", repr(snap))
check(snap.get("7", {}).get("content_id") == 1000000701,
      "rehomed login resolves through the map, not its digits")
check(snap.get("1") == {"charid": 1, "content_id": 1000000101,
                        "world_field": 2097153, "name": "Lex"},
      "entry carries charid, Content ID, world field, name")

print("2. bridge -> plugin handoff")
B.publish_ingame(snap)
check(ffxititle.ingame() == snap, "the plugin reads what the bridge published")
check(ffxititle.INGAME_KEY == B.INGAME_KEY, "both use the same key")

print("3. gate")
t = ffxititle.register()
ffxititle.INGAME_PUSH = False
check(t.presence_character(1) is None, "off by default -> no character")
check(t.playing_characters() == {}, "...and nothing to watch")
ffxititle.INGAME_PUSH = True
check(t.presence_character(1) == (2097153, 1000000101),
      "on -> (world field, Content ID)")
check(t.presence_character(2) is None, "on, member not in the world -> none")
check(t.playing_characters() == {1: 1000000101, 7: 1000000701},
      "the watcher sees who plays what", repr(t.playing_characters()))

print("4. the record, through the core")
check(R._presence_character(1, 1) == (1, 2097153, 1000000101),
      "zone FFXI -> the character")
check(R._presence_character(1, 1000) is None, "in the Viewer -> none")
enc = R.build_field_push_record(0x1122334455667788, 3, state=3, zone=1,
                                character=R._presence_character(1, 1),
                                name="Lex", seq=5, when=1)
main, chunk = b64decode(enc[:96]), b64decode(enc[96:])
check(main[0x12] == 1, "+0x12 playing bit", f"{main[0x12]:#x}")
check(chunk[8:24] == struct.pack("<HHIQ", 1, 1, 2097153, 1000000101),
      "0x08 field = sqPolCharacterPrimitive", chunk[8:24].hex())

print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
