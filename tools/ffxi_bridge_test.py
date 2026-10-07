#!/usr/bin/env python3
"""The FFXI bridge's pairing and attribution logic, driven offline.

WHAT THIS PINS
--------------
`lsb/ffxi_bridge.py` is a relay whose one job beyond copying bytes is to keep
POL Content IDs and LSB charids paired, and to know WHICH POL member launched.
Four defects were found by driving its own functions with synthetic packets --
none had been seen live, because the paths had never been exercised:

  1. **A second character SWAPPED Content IDs with the first.** The client
     names the Content ID on both `0x22` and `0x21` (packet captures
     show it), so the pending list grew two entries; the next
     `0x20` handed the first entry to slot 0 -- the EXISTING character -- and
     `content_id_for` rebound it. The new character was then refused as a
     duplicate, served untranslated (POL-0001 for that session), and on the
     client's own refetch inherited the freed old id. The client keeps macros
     under `USER/<hexid>/`, so the swap silently loses both characters' files.
  2. **A relaunch went to the other signed-in player.** A POL session was
     claimed one-shot on the premise that it launches FFXI once; after any
     drop the same Viewer relaunches, finds its own session claimed and sorted
     last, and gets whichever OTHER member is signed in.
  3. **The account map was written before the create was attempted**, so one
     unreachable LSB at a member's first launch wedged that member for good.
  4. **A refused delete re-paired the character to the lowest free id**, not
     the one it had -- another silent `USER/<hexid>/` move.

Plus the per-connection keying: companion, pending-create and swallow state
were keyed by client IP, so two players behind one router replaced each
other's LSB data session.

Run from tools/: `python ffxi_bridge_test.py`. Exits non-zero on failure. No
network and no LSB: every LSB call is stubbed. The bridge's two maps are
tables in PostgreSQL now, so the suite gets a fresh database from the core's
tools/pgtest.py (a throwaway container, or POL_TEST_DATABASE_URL's server) and
checks that what the bridge decides is what it wrote there.
"""

import json
import os
import shutil
import struct
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from openlobby_paths import require_database  # noqa: E402
require_database("ffxi_bridge_test")
TMP = tempfile.mkdtemp(prefix="ffxi-bridge-test-")
os.environ.pop("POL_VALKEY_URL", None)       # never a real stack's live state
os.environ["FFXI_PKT_DUMP"] = "0"
os.environ["FFXI_WORLD_CAPTURE"] = "0"
# The 0x14 hook would otherwise start a timer that dials a database this test
# does not have; the sweep logic is driven with a fake cursor below.
os.environ["FFXI_TOMBSTONE_DELETED"] = "0"
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_bridge as B  # noqa: E402
import ffxidb  # noqa: E402
from polcore import kv  # noqa: E402
KV = kv.MemoryKV()           # the core's live-state store, in process
kv.reset(KV)

FAILS = []


def check(cond, what):
    print(f"  {'OK  ' if cond else 'FAIL'} {what}")
    if not cond:
        FAILS.append(what)


def reset(pool):
    # getattr: an older bridge lacks some of these, and this test
    # must reach its logic checks against that code to prove it discriminates.
    for name in ("_idmap", "_charnames", "_worldfields", "_released_ids",
                 "_pending_create", "_create_pending", "_swallow_charlist",
                 "_claimed_sids"):
        getattr(B, name, {}).clear()
    B.pol_content_ids = lambda member_id=None: list(pool)


def c2s(cmd, cid, name=b""):
    p = bytearray(96)
    struct.pack_into("<I", p, 0, 96)
    p[4:8] = b"IXFF"
    p[8] = cmd
    struct.pack_into("<I", p, 28, cid)
    p[32:32 + len(name)] = name
    return bytes(p)


def s2c_20(chars):
    """A 0x20 char list: [(charid, name)] in LSB's slot order."""
    n = len(chars)
    size = 28 + 4 + 140 * n
    p = bytearray(size)
    struct.pack_into("<I", p, 0, size)
    p[4:8] = b"IXFF"
    p[8] = 0x20
    struct.pack_into("<I", p, 28, n)
    for i, (cid, nm) in enumerate(chars):
        off = 32 + i * 140
        struct.pack_into("<I", p, off, cid)
        struct.pack_into("<H", p, off + 4, cid & 0xFFFF)
        p[off + 12:off + 12 + len(nm)] = nm
    return bytes(p)


def served(pkt):
    n = struct.unpack_from("<I", pkt, 28)[0]
    return [struct.unpack_from("<I", pkt, 32 + i * 140)[0] for i in range(n)]


def s2c(pkt, member, ckey):
    """rewrite_s2c with the connection key; the pre-fix bridge has no such
    parameter, and the negative control must still reach the logic checks."""
    try:
        return B.rewrite_s2c(pkt, "s->c", member, ckey)
    except TypeError:
        return B.rewrite_s2c(pkt, "s->c", member)


def create(ckey, cid, name, member):
    """The client's create sequence as the captures show it: 0x22 then 0x21,
    both naming the Content ID of the empty slot it was shown."""
    B.rewrite_c2s(c2s(0x22, cid, name), "c->s", member, ckey)
    B.rewrite_c2s(c2s(0x21, cid, name), "c->s", member, ckey)


# ---------------------------------------------------------------------------
print("1. A SECOND CHARACTER KEEPS THE FIRST ONE'S CONTENT ID WHERE IT IS")
reset([30000100, 30000101, 30000102, 30000103])
K = "192.0.2.5:50001"
out = s2c(s2c_20([(1, b"First")]), 5, K)
check(served(out) == [30000100], f"first login pairs charid 1 -> 30000100 ({served(out)})")
create(K, 30000101, b"Second", 5)
# The early char list the bridge asks for after the create ACK, then the
# client's own refetch -- both carry the existing character in slot 0.
out = s2c(s2c_20([(1, b"First"), (2, b"Second")]), 5, K)
check(served(out) == [30000100, 30000101],
      f"post-create list keeps First=30000100 and pairs Second=30000101 ({served(out)})")
check(B._idmap == {"1": 30000100, "2": 30000101}, f"idmap is {B._idmap}")
out = s2c(s2c_20([(1, b"First"), (2, b"Second")]), 5, K)
check(served(out) == [30000100, 30000101], "the refetch serves the same pairing")
check(B._pending_create == {}, "no pending id is left behind to hit a later list")

print("   ...and a THIRD on a two-character account (the middle one used to be POL-0001)")
create(K, 30000102, b"Third", 5)
out = s2c(s2c_20([(1, b"First"), (2, b"Second"), (3, b"Third")]), 5, K)
check(served(out) == [30000100, 30000101, 30000102],
      f"three characters, three stable ids ({served(out)})")

print("   ...and a restarted bridge reads back exactly that pairing")
before = (dict(B._idmap), dict(B._charnames))
check(B.load_idmap() is True, "the map is read back from the database")
check((B._idmap, B._charnames) == before,
      f"ids and names survive the reload ({B._idmap}, {B._charnames})")

# ---------------------------------------------------------------------------
print("2. TWO CONNECTIONS CREATING AT ONCE DO NOT TRADE IDS")
reset([30000200, 30000201, 30000300, 30000301])
B.pol_content_ids = lambda member_id=None: ({6: [30000200, 30000201],
                                              7: [30000300, 30000301]}[member_id])
KA, KB = "192.0.2.6:50002", "192.0.2.7:50003"
create(KA, 30000200, b"Alpha", 6)
create(KB, 30000300, b"Beta", 7)
outb = s2c(s2c_20([(11, b"Beta")]), 7, KB)     # B's list lands first
outa = s2c(s2c_20([(10, b"Alpha")]), 6, KA)
check(served(outb) == [30000300] and served(outa) == [30000200],
      f"each connection's named id went to its own charid ({served(outa)}, {served(outb)})")

# ---------------------------------------------------------------------------
print("3. A RELAUNCH FROM THE SAME VIEWER STAYS ON ITS OWN MEMBER")
reset([])
now = time.time()


def sessions(**ents):
    """The core's session table as its auth service writes it: one key per
    session, the slot as JSON."""
    KV.flush()
    for sid, ent in ents.items():
        KV.set(B.AUTH_SESSION_KEY + sid, json.dumps(ent))


sessions(uAAAA={"member_id": 1, "peer_ip": "192.0.2.1", "at": now - 30, "chars_at": now - 20, "viewer_open": True},
         uBBBB={"member_id": 2, "peer_ip": "192.0.2.2", "at": now - 25, "chars_at": now - 15, "viewer_open": True})
check(B.resolve_pol_member("192.0.2.1")[0] == 1, "A's first launch -> member 1")
check(B.resolve_pol_member("192.0.2.2")[0] == 2, "B's first launch -> member 2")
m, how = B.resolve_pol_member("192.0.2.1")
check(m == 1, f"A RELAUNCHES (after a drop) -> member 1, not B ({m}: {how})")
check("deterministic" in how, "...and it is the address match, not a heuristic")

print("   ...and two Viewers behind ONE address are told apart by the claim")
B._claimed_sids.clear()
sessions(uAAAA={"member_id": 1, "peer_ip": "192.0.2.9", "at": now - 30, "chars_at": now - 20, "viewer_open": True},
         uBBBB={"member_id": 2, "peer_ip": "192.0.2.9", "at": now - 25, "chars_at": now - 15, "viewer_open": True})
first = B.resolve_pol_member("192.0.2.9")[0]
second = B.resolve_pol_member("192.0.2.9")[0]
check({first, second} == {1, 2}, f"two launches from one NAT reach two members ({first}, {second})")

print("   ...and a launch is NEVER handed a session from another address")
# A player on their own IP was once shown another player's characters. The
# address was only the last tie-breaker, so any miss of the exact match fell
# through to the most recently active OTHER member.
B._claimed_sids.clear()
# (a) the player's own session is not flagged signed in; someone else's is.
sessions(uAAAA={"member_id": 1, "peer_ip": "203.0.113.5", "at": now - 60, "chars_at": now - 50, "viewer_open": False},
         uBBBB={"member_id": 2, "peer_ip": "198.51.100.7", "at": now - 5, "chars_at": now - 4, "viewer_open": True})
m, how = B.resolve_pol_member("203.0.113.5")
check(m != 2, f"own session not signed in -> NOT the other player's member 2 ({m}: {how})")
# (b) the player's address matches no session at all.
B._claimed_sids.clear()
sessions(uBBBB={"member_id": 2, "peer_ip": "198.51.100.7", "at": now - 5, "chars_at": now - 4, "viewer_open": True})
m, how = B.resolve_pol_member("203.0.113.9")
check(m is None, f"no session from this address -> refused, not member 2 ({m}: {how})")
# (c) an IPv4-mapped IPv6 peer is the same client, not a stranger.
B._claimed_sids.clear()
sessions(uAAAA={"member_id": 1, "peer_ip": "203.0.113.5", "at": now - 60, "chars_at": now - 50, "viewer_open": True},
         uBBBB={"member_id": 2, "peer_ip": "198.51.100.7", "at": now - 5, "chars_at": now - 4, "viewer_open": True})
m, how = B.resolve_pol_member("::ffff:203.0.113.5")
check(m == 1, f"::ffff:203.0.113.5 is the player at 203.0.113.5 ({m}: {how})")
# (d) The field case: member 26 held several stale signed-in sessions at its
# own address, all already claimed by earlier launches; the same-address rule
# saw >1 with none unclaimed and gave up, and the global ranking sorts claimed
# LAST, so member 30's unclaimed session at another address won.
B._claimed_sids.clear()
sessions(u26a={"member_id": 26, "peer_ip": "203.0.113.26", "at": now - 900, "chars_at": now - 950, "viewer_open": True},
         u26b={"member_id": 26, "peer_ip": "203.0.113.26", "at": now - 600, "chars_at": now - 650, "viewer_open": True},
         u30={"member_id": 30, "peer_ip": "198.51.100.30", "at": now - 20000, "chars_at": now - 21000, "viewer_open": True})
B._claimed_sids.update({"u26a": now - 800, "u26b": now - 500})
m, how = B.resolve_pol_member("203.0.113.26")
check(m == 26, f"own sessions all claimed -> still member 26, not 30 ({m}: {how})")
# (e) a stranger (scanner) at an address with no POL session gets nobody.
m, how = B.resolve_pol_member("192.0.2.21")
check(m is None, f"an address with no POL session is refused ({m}: {how})")

# ---------------------------------------------------------------------------
print("4. A REFUSED DELETE RE-PAIRS THE CHARACTER TO THE ID IT HAD")
reset([30000400, 30000401, 30000402])
K = "192.0.2.8:50004"
B._idmap.update({"1": 30000401})          # 30000400 is free and LOWER
pkt = B.rewrite_c2s(c2s(0x14, 30000401), "c->s", 8, K)
check(struct.unpack_from("<I", pkt, 28)[0] == 1, "0x14 delete translated to charid 1")
check("1" not in B._idmap, "the pairing is released on the delete request")
check(1 not in [r["charid"] for r in ffxidb.load_idmap()],
      "...and the release is written to the database")
out = s2c(s2c_20([(1, b"Back")]), 8, K)   # LSB refused; the char is back
check(served(out) == [30000401], f"it comes back on 30000401, not the lower free 30000400 ({served(out)})")
check([r["content_id"] for r in ffxidb.load_idmap() if r["charid"] == 1] == [30000401],
      "...and the database holds charid 1 on 30000401 again")

# ---------------------------------------------------------------------------
print("5. THE ACCOUNT MAP RECORDS ONLY ACCOUNTS LSB CONFIRMED")
reset([])
B._acctmap.clear()
calls = []


def auth_request(command, username, password, new_password=None):
    calls.append(command)
    raise OSError("connection refused")


B.lsb_auth_request = auth_request
try:
    B.lsb_account_for(42)
    check(False, "an unreachable LSB raises")
except Exception:
    check("42" not in B._acctmap, "an unreachable LSB leaves NO map entry (retried next launch)")

B.lsb_auth_request = lambda *a, **k: {"result": B.LOGIN_ERROR_CREATE_TAKEN}
login, pw = B.lsb_account_for(42)
check("42" in B._acctmap and login == "pol42", "an account LSB already has is recorded")
check([(r["world_tag"], r["member_id"], r["login"]) for r in ffxidb.load_accounts()]
      == [("", 42, "pol42")],
      "...in the database, and the unreachable attempt left nothing there")
B._acctmap.clear()
check(B.load_acctmap() is True and B._acctmap.get("42", {}).get("login") == "pol42",
      "a restarted bridge reads the account back")

B._acctmap.clear()
B.lsb_auth_request = lambda *a, **k: {"result": 0x09}
try:
    B.lsb_account_for(43)
    check(False, "a refused create raises")
except RuntimeError:
    check("43" not in B._acctmap, "a refused create leaves no map entry")

print("   ...and a map entry whose account LSB lost heals itself")
B._acctmap["44"] = {"login": "pol44", "created": "x"}
state = {"created": False}


def auth_request2(command, username, password, new_password=None):
    state["created"] = True
    return {"result": B.LOGIN_SUCCESS_CREATE}


def authenticate(username=None, password=None):
    if not state["created"]:
        raise RuntimeError("LSB auth failed: {'result': 2}")
    return 1044, b"\x11" * 16


B.lsb_auth_request = auth_request2
B.lsb_authenticate = authenticate
aid, sh, login = B.lsb_member_session(44)
check(aid == 1044 and login == "pol44", "auth failure -> create -> auth succeeds, no admin step")

# ---------------------------------------------------------------------------
# Per-client advertise map. The packet is SYNTHETIC: same 72-byte layout as a
# real 0x0B handoff (header, LSB's md5 at 12..28, ids, a 16-byte name, the zone
# ip/port at 56..64 and the search ip/port at 64..72), built here with made-up
# values. Never paste a captured packet into a test: it carries a real account
# name and real addresses as bytes, where no text scan can see them.
import hashlib as _hl, socket as _so


def _handoff(zone_ip, search_ip, name=b"Examplemember"):
    pkt = bytearray(bytes.fromhex("48000000495846460b000000") + b"\0" * 16
                    + bytes.fromhex("0500000005000000")
                    + name.ljust(16, b"\0") + bytes.fromhex("20000000")
                    + _so.inet_aton(zone_ip) + bytes.fromhex("d6d30000")
                    + _so.inet_aton(search_ip) + bytes.fromhex("f2d20000"))
    pkt[12:28] = _hl.md5(bytes(pkt)).digest()      # signed over a zeroed field
    return bytes(pkt)


OVERLAY = "198.51.100.60"          # TEST-NET-2: stands in for an overlay address
LAN_IP = "192.168.0.10"  # generic example address; polcheck: allow
HANDOFF = _handoff(OVERLAY, OVERLAY)
check(len(HANDOFF) == 72 and _so.inet_ntoa(HANDOFF[56:60]) == OVERLAY,
      "fixture has the handoff layout: zone ip at offset 56")
_saved_map, _saved_fix = B.ADVERTISE_MAP, B.WORLD_ID_FIX
B.WORLD_ID_FIX = False
B.ADVERTISE_MAP = B._parse_advertise_map("192.168.0.0/16=" + LAN_IP)  # generic example address; polcheck: allow
lan = B.rewrite_s2c(HANDOFF, "T", None, "192.168.0.50:61709")  # generic example address; polcheck: allow
check(_so.inet_ntoa(lan[56:60]) == LAN_IP and _so.inet_ntoa(lan[64:68]) == LAN_IP,
      "a LAN client is handed the LAN address for zone AND search")
check(lan[60:64] == HANDOFF[60:64] and lan[68:72] == HANDOFF[68:72] and lan[28:56] == HANDOFF[28:56],
      "...and nothing else in the handoff moves (ports, charid, name, server id)")
_z = bytearray(lan); _z[12:28] = b"\0" * 16
check(_hl.md5(bytes(_z)).digest() == lan[12:28], "...and the packet is re-signed (LSB's md5)")
ts = B.rewrite_s2c(HANDOFF, "T", None, "198.51.100.37:50944")
check(ts == HANDOFF, "a client outside every CIDR gets LSB's packet byte for byte")
B.ADVERTISE_MAP = []
check(B.rewrite_s2c(HANDOFF, "T", None, "192.168.0.50:61709") == HANDOFF,  # generic example address; polcheck: allow
      "with no map configured the handoff is untouched (old behaviour)")
check(B._parse_advertise_map("garbage,10.0.0.0/8=10.0.0.5")[0][2] == "10.0.0.5",  # generic example address; polcheck: allow
      "a malformed map entry is skipped, the good one kept")
B.ADVERTISE_MAP, B.WORLD_ID_FIX = _saved_map, _saved_fix

# ---------------------------------------------------------------------------
# A deleted character's NAME is freed (LSB soft-deletes and keeps the name)
print("\n[deleted names] LSB parks a deleted row with accid = 0; the bridge tombstones it")
if hasattr(B, "tombstone_deleted_rows"):
    class FakeCur:
        def __init__(self, rows):
            self.rows, self.sql, self.rowcount = rows, [], 0

        def execute(self, q, args=()):
            self.sql.append((q, tuple(args)))
            self.rowcount = 1 if q.startswith("UPDATE") else 0

        def fetchall(self):
            return list(self.rows)

    cur = FakeCur([(14, "Examplechar", 1013), (2, "del2", 1001), (20, "Delmar", 1005)])
    done = B.tombstone_deleted_rows(cur)
    check(done == [(14, "Examplechar", 1013), (20, "Delmar", 1005)],
          "every parked row not yet tombstoned is renamed (a real name starting 'del' too)")
    ups = [a for q, a in cur.sql if q.startswith("UPDATE")]
    check(ups == [("del14", 14, "Examplechar"), ("del20", 20, "Delmar")],
          "renamed to del<charid>, guarded by charid AND accid = 0 AND the name just read")
    check(all("accid = 0" in q for q, _ in cur.sql),
          "no statement can touch a row that still belongs to an account")
    check(any(ch.isdigit() for ch in B.tombstone_name(14)),
          "the tombstone holds a digit, which LSB's create validator never accepts")
    _calls = []
    _saved_sched = B.schedule_tombstone
    B.schedule_tombstone = lambda why, delay=None: _calls.append(why)
    reset([30000401, 30000402])
    B._idmap["1"] = 30000401
    B.rewrite_c2s(c2s(0x14, 30000401), "c->s", 8, K)
    B.schedule_tombstone = _saved_sched
    check(bool(_calls) and "charid 1" in _calls[0],
          "a client's 0x14 schedules a sweep for that charid")
    check(B.tombstone_deleted_names(why="test") is None,
          "with FFXI_TOMBSTONE_DELETED=0 a sweep is a no-op (no DB is touched here)")
else:
    check(False, "bridge has no tombstone_deleted_rows")

# ---------------------------------------------------------------------------
# A parked charid gives its Content ID back even if a stale list re-paired it
print("\n[parked ids] a deleted charid re-paired by a stale char list is released by the sweep")
if hasattr(B, "release_parked"):
    # Member 26 holds ONE Content ID; charid 14 is deleted, released on the
    # 0x14 and re-paired by a stale list, so charid 17 (the recreated
    # character) finds no free id -> FFXI-3120 at select.
    reset([30000181])
    B._idmap["14"] = 30000181
    B.rewrite_c2s(c2s(0x14, 30000181), "c->s", 26, K)
    check("14" not in B._idmap, "the 0x14 releases charid 14")
    B.content_id_for(14, member_id=26)
    check(B._idmap.get("14") == 30000181, "a list that still carries charid 14 re-pairs it")
    check(B.content_id_for(17, member_id=26) is None,
          "...so the recreated charid 17 has no free id (the field failure)")

    class ParkedCur:
        def execute(self, q, args=()):
            self.q = q

        def fetchall(self):
            return [(14,), (2,)]
    parked = B.parked_charids(ParkedCur())
    check(parked == [2, 14], "parked_charids reads every accid = 0 row")
    check(B.release_parked(parked, why="test") == [14],
          "the sweep releases only parked charids still in the map")
    check(B.content_id_for(17, member_id=26) == 30000181,
          "and charid 17 then gets the member's Content ID")
    check(B.release_parked([17], why="test") == [17] and "17" not in B._idmap,
          "(sanity) release_parked acts on what it is given; only the SELECT decides 'parked'")
else:
    check(False, "bridge has no release_parked")

# ---------------------------------------------------------------------------
# LSB caches the login's char list: a delete blanks only the NAME, a create
# adds a record with no profile tail, and the list is padded to content_ids.
print("\n[cached list] a create in the same login after a delete, and slots no id backs")
reset([30000221])
K = "192.0.2.9:50009"
B._idmap["9"] = 30000221
B.rewrite_c2s(c2s(0x14, 30000221), "c->s", 10, K)
# LSB's next list: charid 9 still there, its name blanked to " ".
out = s2c(s2c_20([(9, b" ")]), 10, K)
check("9" not in B._idmap, f"the blanked record does not re-pair charid 9 ({B._idmap})")
check(served(out) == [30000221], f"...and its slot is offered the freed id ({served(out)})")
create(K, 30000221, b"Weasel", 10)
out = s2c(s2c_20([(38, b"Weasel")]), 10, K)
check(B._idmap.get("38") == 30000221,
      f"the recreated charid 38 gets the member's Content ID ({B._idmap})")
check(served(out) == [30000221], f"...and is served on it ({served(out)})")

print("   ...and slots past the member's Content IDs are not offered at all")
reset([30000312])
B._idmap["16"] = 30000312
out = s2c(s2c_20([(16, b"Koshin")] + [(0, b" ")] * 15), 26, K)
check(served(out) == [30000312], f"a one-id member is shown one slot, not sixteen ({served(out)})")
check(struct.unpack_from("<I", out, 0)[0] == len(out) == 28 + 4 + 140,
      f"packet_size matches the shortened list ({len(out)})")
reset([30000312, 30000313])
B._idmap["16"] = 30000312
out = s2c(s2c_20([(16, b"Koshin")] + [(0, b" ")] * 15), 26, K)
check(served(out) == [30000312, 30000313], f"a two-id member keeps one Create slot ({served(out)})")
reset([])
out = s2c(s2c_20([(16, b"Koshin")] + [(0, b" ")] * 3), 26, K)
check(len(served(out)) == 4, "an empty pool (a failed account read) drops nothing")

print("   ...and a just-created record with no profile tail does not zero the profile")
reset([30000415])
B._idmap["37"] = 30000415
B._charfields["37"] = {"race": 7, "job": 6, "zone": 230}
s2c(s2c_20([(37, b"Artemis")]), 23, K)
check(B._charfields.get("37", {}).get("race") == 7,
      f"race 0 in LSB's cached record is not recorded ({B._charfields.get('37')})")

# ---------------------------------------------------------------------------
shutil.rmtree(TMP, ignore_errors=True)
if FAILS:
    print(f"\nFAIL: {len(FAILS)} check(s):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("\nRESULT: the bridge pairs by charid, attributes by address, and records only real accounts")
