#!/usr/bin/env python3
"""A POL handle must be able to hold SEVERAL FFXI Content IDs -- one per character.

WHAT THIS PINS, AND THE FIELD FAILURE THAT ASKED FOR IT
------------------------------------------------------
FFXI issues one Content ID per CHARACTER. `handle_content` used to be keyed
`(handle_id, content_code)`, which can express exactly one, so a player's second
character got no pairing: `ffxi_bridge.content_id_for` refuses to bind one
Content ID to two charids (a duplicate silently destroys the OTHER character's
world identity, and a Content ID cannot be re-minted without orphaning that
player's local `FINAL FANTASY XI/USER/<hexid>/` files), the character was served
untranslated, it missed POL's 64-slot character table, and FFXI drew **POL-0001**
at char select. For ever, with nothing about creating it looking wrong.

Measured live 2026-08-28 on one account: a single Content ID, held by its
first character; the second character held nothing. Five select attempts, five
world-server pending sessions, zero zone-ins. The same account had already hit
the same ceiling once on 2026-08-26.

The five things below are the ones that can silently come back:

  1. a fresh FFXI account gets its whole set of character slots, all distinct
     and globally unique -- the mint's invariant is not weakened by there being
     more of them;
  2. an account that already exists is TOPPED UP without any existing id moving
     -- the never-re-mint rule (see accounts.allocate_content_id);
  3. moving a title between handles carries EVERY slot. This one is a real trap:
     the old `link_content_to_handle` did DELETE-then-INSERT, which was lossless
     when a game had one id and destroys all but one now;
  4. the wire never carries more than eight Content IDs for one handle, all
     bound, and what falls off the end is an extra FFXI slot, never another
     title; the mint itself stops at that ceiling;
  5. the bridge sees the whole pool, because that is what it offers the client
     as empty character slots;
  6. `ffxititle.py trim-slots` cleans up a database written before the
     ceiling, and never deactivates an id a character is named after.

The slot count is the FFXI title plugin's (`ffxititle.CHARACTER_SLOTS`, its
`content_slots`); the core's accounts module only mints what a loaded title
asks for. Section 1b proves that path with a count above one.

Run from tools/: `python ffxi_char_slots_test.py`. Exits non-zero on failure.
Needs the OpenLobby core's `services/` (accounts.py, responders.py, titles.py)
and its tools/pgtest.py, which gives every section its own fresh PostgreSQL
database: set OPENLOBBY_DIR to a checkout of it, or keep one beside this
repository as ../openlobby. Without it the suite SKIPS (exit 77) rather than
failing, and so it does without Docker or POL_TEST_DATABASE_URL unless
POL_TEST_REQUIRE_DB=1.
"""

import os
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from openlobby_paths import require_database, require_services    # noqa: E402
SERVICES = require_services("ffxi_char_slots_test")

TMP = tempfile.mkdtemp(prefix="ffxi-char-slots-")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")
_DBS = {"accounts": require_database("ffxi_char_slots_test")}

import accounts as A                                               # noqa: E402
import responders as R                                             # noqa: E402
import pgtest                                                      # noqa: E402

LSB = os.path.join(HERE, os.pardir, "lsb")
sys.path.insert(0, LSB)
import ffxititle as X                                              # noqa: E402
import ffxidb                                                      # noqa: E402
TITLE = X.register()          # what POL_TITLES=ffxititle does in the core

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def fresh_db(name):
    """A new, empty account database for one section, and this process
    pointed at it. Dropped when the suite exits."""
    _DBS[name] = pgtest.use_fresh_database()
    return A.connect(_DBS[name])


def use_db(name):
    """Point this process back at a section's database. A connection object
    borrows from the process's pool per statement, so this is what decides
    which database every open connection (and responders) reads."""
    return A.connect(_DBS[name])


def ffxi_ids(db, handle_id):
    return [r["content_id"] for r in A.handle_content_list(db, handle_id)
            if int(r["content_code"]) == X.CONTENT_CODE]


N = X.CHARACTER_SLOTS

# --------------------------------------------------------------------------- #
print("1. A NEW ACCOUNT GETS ITS FFXI CHARACTER SLOTS (want %d)" % N)

db = use_db("accounts")
A.create_polid(db, "SLOTS1", "pw-account")
mid = A.add_member(db, "SLOTS1", "slots-one", "pw-member")
A.set_handle(db, mid, "Tester")
hid = A.primary_handle_row(db, mid)["id"]
A.grant_content(db, mid, X.CONTENT_CODE)
A.link_member_content_to_primary(db, mid)

ids = ffxi_ids(db, hid)
check(len(ids) == N, "the handle holds %d FFXI Content ID(s) (got %d: %s)"
      % (N, len(ids), ids))
check(len(set(ids)) == len(ids), "...all distinct (%s)" % (ids,))
dupes = db.execute(
    "SELECT content_id, COUNT(*) n FROM handle_content WHERE content_id IS NOT NULL"
    " GROUP BY content_id HAVING COUNT(*) > 1").fetchall()
check(not dupes, "no Content ID appears twice anywhere in the DB (%s)"
      % ([dict(r) for r in dupes],))
slots = sorted(int(r["slot"]) for r in A.handle_content_list(db, hid)
               if int(r["content_code"]) == X.CONTENT_CODE)
check(slots == list(range(N)), "slots are 0..%d with no gaps (%s)" % (N - 1, slots))

# A title that is NOT FFXI keeps exactly one -- the slots are FFXI's rule, not a
# blanket one. Granting every title four ids would burn the handle's eight
# binding positions on games that only ever need one.
A.grant_content(db, mid, 2)
A.link_member_content_to_primary(db, mid)
tm = [r for r in A.handle_content_list(db, hid) if int(r["content_code"]) == 2]
check(len(tm) == 1, "Tetra Master still gets exactly one (%d)" % len(tm))

# ...and `member_content_id` must still mean slot 0, not "whichever sorted last".
primary_id = A.member_content_id(db, mid, X.CONTENT_CODE)
slot0 = [r["content_id"] for r in A.handle_content_list(db, hid)
         if int(r["content_code"]) == X.CONTENT_CODE and int(r["slot"]) == 0]
check(primary_id == slot0[0],
      "member_content_id returns slot 0 (%s vs %s)" % (primary_id, slot0[0]))

# --------------------------------------------------------------------------- #
print()
print("1b. THE TITLE'S SLOT COUNT IS WHAT THE ACCOUNT DATABASE MINTS")

# With the default of one, every check above would pass with the slot code
# deleted outright. Raise the plugin's count for this section so the mint,
# the per-title rule and the login top-up each have something to prove.
check(A.content_slots_for(X.CONTENT_CODE) == N,
      "accounts reads the plugin's count (%d)" % A.content_slots_for(X.CONTENT_CODE))
db1 = fresh_db("hook")
A.create_polid(db1, "HOOK", "pw-account")
hm = A.add_member(db1, "HOOK", "hook", "pw-member")
A.set_handle(db1, hm, "Hooked")
hh = A.primary_handle_row(db1, hm)["id"]
A.grant_content(db1, hm, X.CONTENT_CODE)
A.link_member_content_to_primary(db1, hm)
check(len(ffxi_ids(db1, hh)) == N,
      "before the raise the handle holds %d (%d)" % (N, len(ffxi_ids(db1, hh))))
TITLE.content_slots = 3
try:
    got = A.ensure_title_slots(db1, member_id=hm)     # what a login runs
    check(got == 3 - N and len(ffxi_ids(db1, hh)) == 3,
          "the login top-up raises it to the plugin's 3 (%d minted)" % got)
    check(A.ensure_title_slots(db1, member_id=hm) == 0,
          "...and a second login mints nothing")
    A.grant_content(db1, hm, 2)
    A.link_member_content_to_primary(db1, hm)
    check(len([r for r in A.handle_content_list(db1, hh)
               if int(r["content_code"]) == 2]) == 1,
          "a title whose plugin asks for nothing still gets one")
    A.create_polid(db1, "HOOK2", "pw-account")
    pm = A.add_member(db1, "HOOK2", "hook-two", "pw-member")
    A.set_handle(db1, pm, "Placed")
    ph = A.primary_handle_row(db1, pm)["id"]
    A.grant_content(db1, pm, X.CONTENT_CODE)
    A.link_member_content_to_primary(db1, pm)       # a redeemed code
    check(len(ffxi_ids(db1, ph)) == 3,
          "placing the title mints the plugin's 3 (%d)" % len(ffxi_ids(db1, ph)))
    reg = A.register_account(db1, "Hookreg", "password123", contents=(1, 2))
    rh = db1.execute("SELECT id FROM handle WHERE member_id = %s",
                     (reg["member_id"],)).fetchone()["id"]
    check(len(ffxi_ids(db1, rh)) == 3,
          "registration mints the plugin's 3 in its one transaction (%d)"
          % len(ffxi_ids(db1, rh)))
finally:
    TITLE.content_slots = X.CHARACTER_SLOTS
db1.close()

# --------------------------------------------------------------------------- #
print()
print("2. AN ACCOUNT THAT ALREADY EXISTS IS TOPPED UP, AND NOTHING MOVES")

# The PRE-SLOT shape, written by hand: one row per title at slot 0, which is
# what every account created before the slot column existed holds (an import
# of such an accounts.db lands exactly like this). The point of the check is
# that the id it is already serving does not change. A re-mint here costs that
# player every macro they ever wrote.
db2 = fresh_db("legacy")
A.create_polid(db2, "LEGACY", "pw-account")
lmid = A.add_member(db2, "LEGACY", "legacy", "pw-member")
LH = A.set_handle(db2, lmid, "Legacy")
for code, cid in ((1, "30000037"), (2, "30000038")):
    db2.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                " content_id, status, linked_at) VALUES (%s,%s,0,%s,'active',%s)",
                (LH, code, cid, "2026-08-22T02:13:28Z"))
db2.commit()

minted = A.ensure_title_slots(db2)            # what the schema step runs
after = ffxi_ids(db2, LH)
check("30000037" in after, "the served FFXI id survived the top-up (%s)" % (after,))
row = db2.execute("SELECT slot FROM handle_content WHERE handle_id = %s"
                  " AND content_code = 1 AND content_id = '30000037'",
                  (LH,)).fetchone()
check(row is not None and int(row["slot"]) == 0,
      "...and it is slot 0, i.e. still the handle's FFXI identity")
check(len(after) == N, "...and the handle was topped up to %d (%d)" % (N, len(after)))
tm2 = [r["content_id"] for r in A.handle_content_list(db2, LH)
       if int(r["content_code"]) == 2]
check(tm2 == ["30000038"], "the Tetra Master id was left alone (%s)" % (tm2,))

# Idempotent: running it again must not mint a second set.
again = A.ensure_title_slots(db2)
check(again == 0, "a second top-up mints nothing (%s)" % (again,))
check(len(ffxi_ids(db2, LH)) == N, "...and the count is unchanged")

# A handle that does NOT hold FFXI gets nothing -- entitlement is `content`, and
# this must not hand the title to everybody.
NH = A.set_handle(db2, lmid, "NoFFXI", primary=False)
A.ensure_title_slots(db2)
check(not ffxi_ids(db2, NH), "a handle without FFXI is not given any")

# --------------------------------------------------------------------------- #
print()
print("3. MOVING A TITLE BETWEEN HANDLES CARRIES EVERY SLOT")

# THE REGRESSION THIS EXISTS FOR. `link_content_to_handle` used to DELETE the
# code off the sibling handles and re-INSERT one row. With one id per game that
# was lossless. With four it would silently destroy three Content IDs -- and an
# id that has been served cannot be re-minted.
db3 = fresh_db("move")
A.create_polid(db3, "MOVE", "pw-account")
mm = A.add_member(db3, "MOVE", "mover", "pw-member")
A.set_handle(db3, mm, "First")
h1 = A.primary_handle_row(db3, mm)["id"]
A.grant_content(db3, mm, X.CONTENT_CODE)
A.link_member_content_to_primary(db3, mm)
before_ids = set(ffxi_ids(db3, h1))
check(len(before_ids) == N, "the source handle holds %d (%s)"
      % (N, sorted(before_ids)))

db3.execute("INSERT INTO handle (member_id, handle_name, is_primary, created_at,"
            " client_guid) VALUES (%s,%s,0,%s,0)", (mm, "Second", "2026-09-03T00:00:00Z"))
db3.commit()
h2 = db3.execute("SELECT id FROM handle WHERE handle_name = 'Second'").fetchone()["id"]
A.link_content_to_handle(db3, h2, X.CONTENT_CODE)

moved = set(ffxi_ids(db3, h2))
check(moved == before_ids,
      "every Content ID moved, none lost or re-minted (%s)" % (sorted(moved),))
check(not ffxi_ids(db3, h1), "...and none was left behind on the source handle")
moved_slots = sorted(int(r["slot"]) for r in A.handle_content_list(db3, h2)
                     if int(r["content_code"]) == X.CONTENT_CODE)
check(moved_slots == list(range(N)),
      "slots renumbered without a gap (%s)" % (moved_slots,))
dupes3 = db3.execute(
    "SELECT content_id, COUNT(*) n FROM handle_content WHERE content_id IS NOT NULL"
    " GROUP BY content_id HAVING COUNT(*) > 1").fetchall()
check(not dupes3, "the move did not duplicate an id (%s)"
      % ([dict(r) for r in dupes3],))

# --------------------------------------------------------------------------- #
print()
print("4. THE WIRE NEVER CARRIES A NINTH CONTENT ID FOR ONE HANDLE")

# THE REGRESSION THIS SECTION EXISTS FOR.
# `_db_chars` used to serve a handle's ninth and later Content IDs UNBOUND
# (`+0x04 = 0`), on the reasoning that the launch gate (app.dll+0x199093) and
# FFXI's world lookup (FUN_100FFE00) read the 64-slot table and ignore the
# binding. They do. They are also not the consumer that decides: the VIEWER
# reads a present-but-unbound Content ID as one that still needs a handle and
# opens the assign-a-handle flow, which dead-ends on the very ceiling that left
# it unbound -- string 26069, "The handle "%s" is already linked to 8 Content
# IDs." FFXI is then unreachable from that handle. It came back once after a
# data-only revert, which is why the check below is on the code path.
#
# So the check is not "are the extras encoded correctly" any more. It is: given
# a handle holding MORE than the ceiling, does the wire carry eight?
REC = 104
over = fresh_db("overflow")
A.create_polid(over, "OVERFLW", "pw-account")
omid = A.add_member(over, "OVERFLW", "overflow", "pw-member")
A.set_handle(over, omid, "Tester")
ohid = A.primary_handle_row(over, omid)["id"]
for code in (1, 2, 3, 4, 10, 11, 14, 15):        # every title we serve
    A.grant_content(over, omid, code)
A.link_member_content_to_primary(over, omid)
# ...plus the three extra FFXI ids a pre-fix account was minted at sign-up. Put
# them in by hand: `ensure_content_slots` now clamps, which is check 4c below,
# and the input to 4a has to be an over-ceiling handle or it proves nothing.
for slot in (1, 2, 3):
    over.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                 " content_id, status, linked_at) VALUES (%s,1,%s,%s,'active',%s)",
                 (ohid, slot, "3000099%d" % slot, "2026-09-22T00:00:00Z"))
over.commit()
held = len(A.handle_content_list(over, ohid))
check(held > R._CHAR_PER_HANDLE,
      "the handle under test really is over the ceiling (%d links)" % held)

served = R._db_chars()
check(len(served) == R._CHAR_PER_HANDLE,
      "4a. the wire carries exactly %d records, not %d (%d)"
      % (R._CHAR_PER_HANDLE, held, len(served)))
check(all(rec[4] for rec in served),
      "4b. EVERY served record is BOUND (unbound: %s)"
      % ([r[2:4] for r in served if not r[4]],))
codes = [rec[2] for rec in served]
check(set(codes) >= set([1, 2, 3, 4, 10, 11, 14, 15]),
      "...and no TITLE was dropped to make room (%s)" % (sorted(set(codes)),))
check(sorted(rec[1] for rec in served) == list(range(R._CHAR_PER_HANDLE)),
      "...positions are 0..%d with no gap (%s)"
      % (R._CHAR_PER_HANDLE - 1, [r[1] for r in served]))

# 4c. THE MINT ITSELF STOPS AT THE CEILING, so 4a should never have to bite.
# A fully granted handle asking for four FFXI ids gets none: eight links is
# every title, and a ninth is the dialog above.
full = fresh_db("ceiling")
A.create_polid(full, "CEILING", "pw-account")
fmid = A.add_member(full, "CEILING", "ceiling", "pw-member")
A.set_handle(full, fmid, "Full")
fhid = A.primary_handle_row(full, fmid)["id"]
for code in (1, 2, 3, 4, 10, 11, 14, 15):
    A.grant_content(full, fmid, code)
A.link_member_content_to_primary(full, fmid)
minted = A.ensure_content_slots(full, fhid, X.CONTENT_CODE, 4)
check(minted == 0, "4c. a fully granted handle mints no extra slot (%d)" % minted)
check(A.handle_link_count(full, fhid) <= A.CONTENT_IDS_PER_HANDLE,
      "...and holds at most %d Content IDs (%d)"
      % (A.CONTENT_IDS_PER_HANDLE, A.handle_link_count(full, fhid)))
# A handle with ROOM still gets what fits -- the clamp is a ceiling, not a ban.
room = fresh_db("room")
A.create_polid(room, "ROOM", "pw-account")
rmid = A.add_member(room, "ROOM", "room", "pw-member")
A.set_handle(room, rmid, "Roomy")
rhid = A.primary_handle_row(room, rmid)["id"]
A.grant_content(room, rmid, X.CONTENT_CODE)
A.link_member_content_to_primary(room, rmid)
got = A.ensure_content_slots(room, rhid, X.CONTENT_CODE, 4)
check(got == 3 and A.handle_link_count(room, rhid) == 4,
      "4d. a handle holding only FFXI can still be raised to four (%d minted)" % got)

# The record encoder still has to be able to say both things -- `bind` is what
# 4b reads -- even though `_db_chars` now only ever passes True.
bound = R._char_record(REC, 0, 0, 0, X.CONTENT_CODE, "30000037", bind=True)
unbound = R._char_record(REC, 9, 0, 9, X.CONTENT_CODE, "30000099", bind=False)
check(bound[0x04] == 1, "a bound record sets the bind flag (+0x04 = %d)" % bound[0x04])
check(unbound[0x04] == 0, "an unbound record clears it (+0x04 = %d)" % unbound[0x04])
check(struct.unpack_from("<H", bound, 0x08)[0] == X.CONTENT_CODE,
      "a served record carries its content code (the launch gate's field)")
check(struct.unpack_from("<I", bound, 0x10)[0] == 30000037,
      "...and the Content ID FFXI's world lookup compares")

# The ORDERING rule, unchanged and now load-bearing: positions go to every
# game's slot 0 first, so what falls off the end is an extra FFXI character slot
# and never a whole TITLE.
links = ([{"content_code": 1, "slot": i, "content_id": "3000010%d" % i}
          for i in range(4)]
         + [{"content_code": c, "slot": 0, "content_id": "300002%02d" % c}
            for c in (2, 3, 4, 10, 11, 14, 15)])
primary = [l for l in links if int(l.get("slot", 0)) == 0]
extra = [l for l in links if int(l.get("slot", 0)) != 0]
order = [(l["content_code"], l["slot"]) for l in primary + extra]
bound_codes = set(c for c, _s in order[:R._CHAR_PER_HANDLE])
check(bound_codes == set([1, 2, 3, 4, 10, 11, 14, 15]),
      "every TITLE keeps a binding position (%s)" % (sorted(bound_codes),))
check(all(c == 1 and s > 0 for c, s in order[R._CHAR_PER_HANDLE:]),
      "only extra FFXI slots fall off the end (%s)" % (order[R._CHAR_PER_HANDLE:],))

print()
print("5. THE BRIDGE SEES THE WHOLE POOL")

# This is the payoff: the bridge offers a member's UNSPENT Content IDs as the
# client's empty character slots (`rewrite_s2c`), and picks one when the client
# creates. Pool of one = the failure this suite exists for.
import ffxi_bridge as B                                            # noqa: E402
use_db("accounts")                 # section 1's member, read by the bridge
pool = B.pol_content_ids(mid)
check(len(pool) == N,
      "the bridge sees %d FFXI Content ID(s) for the member (%s)" % (N, pool))
check(sorted(pool) == sorted(int(x) for x in ffxi_ids(db, hid)),
      "...and they are exactly the handle's ids")

# The failure it prevents, stated as the bridge states it: with N ids, N-1
# characters already paired still leaves one free for the next create.
B._idmap = dict((str(i + 1), pool[i]) for i in range(N - 1))
free = [c for c in B.pol_content_ids(mid) if c not in set(B._idmap.values())]
check(len(free) == 1,
      "%d characters in, one Content ID still free (%s)" % (N - 1, free))

# --------------------------------------------------------------------------- #
print()
print("6. THE OPERATOR COMMAND THAT CLEANS UP EXISTING ACCOUNTS")

# `ffxititle.py trim-slots` is what an operator runs on a database that was written
# BEFORE the ceiling was enforced -- the rows are still there, and the bridge
# still offers them as empty character slots even though the wire drops them.
# The whole risk of the command is in one place: deactivating an id a character
# is already named after makes that character POL-0001 at select for ever. So
# the checks below are mostly about what it REFUSES to do.
import subprocess                                                  # noqa: E402

cli = fresh_db("trim")
CLI_DB = _DBS["trim"]
acct = A.register_account(cli, "Examplemember05", "password123",
                          contents=(1, 2, 3, 4, 10, 11, 14))
chid = cli.execute("SELECT id FROM handle WHERE member_id = %s",
                   (acct["member_id"],)).fetchone()["id"]
for slot in (1, 2, 3):                       # what a pre-fix sign-up was minted
    cli.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                " content_id, status, linked_at) VALUES (%s,1,%s,%s,'active',%s)",
                (chid, slot, "3000099%d" % slot, "2026-09-22T00:00:00Z"))
cli.commit()
extras = [r["content_id"] for r in A.handle_content_list(cli, chid)
          if int(r["content_code"]) == 1 and int(r["slot"]) != 0]


def bridge_map(name):
    """The bridge's id map: charid 7 on the first extra id, called `name`."""
    use_db("trim")
    ffxidb.write_idmap([{"charid": 7, "content_id": int(extras[0]), "name": name,
                         "world_field": 1, "seen": "2026-09-22T00:00:00Z"}],
                       replace=True)


def trim(*argv):
    # The plugin sits beside the core's modules in the title image; here the
    # core's services/ goes on the path the same way.
    env = dict(os.environ, PYTHONPATH=SERVICES, POL_DATABASE_URL=CLI_DB)
    r = subprocess.run([sys.executable, os.path.join(LSB, "ffxititle.py"), CLI_DB,
                        "trim-slots"] + list(argv),
                       cwd=SERVICES, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def active_extras():
    db = A.connect(CLI_DB)
    try:
        return int(db.execute(
            "SELECT COUNT(*) n FROM handle_content WHERE content_code = 1"
            " AND slot <> 0 AND status = 'active'").fetchone()["n"])
    finally:
        db.close()


# 6b. THE ONE THAT MATTERS. No id map = no way to know what is in use, so the
# command must not guess. A map that cannot be read must never read as
# "nothing is in use". The bridge has never run against this database, so its
# table does not exist yet.
rc, out = trim("--apply")
check(rc != 0 and "REFUSING" in out and active_extras() == 3,
      "6b. --apply REFUSES when the id map cannot be read (rc %d, %d active)"
      % (rc, active_extras()))
check(all(ord(ch) < 127 for ch in out),
      "...and says so in ASCII, which a cp1252 console can actually print")

bridge_map("Ayla")
rc, out = trim()
check(rc == 0 and active_extras() == 3,
      "6a. the default is a REPORT and writes nothing (rc %d, %d active)"
      % (rc, active_extras()))
check("Ayla IS ON THIS ID" in out,
      "...and it names the character sitting on an id")

rc, out = trim("--apply")
check(rc == 0 and active_extras() == 1,
      "6c. --apply trims the free ids and KEEPS the one with a character on it"
      " (%d left)" % active_extras())
kept = A.connect(CLI_DB)
check([r["content_id"] for r in A.handle_content_list(kept, chid)
       if int(r["content_code"]) == 1 and int(r["slot"]) != 0] == [extras[0]],
      "...and the one it kept is Ayla's")
kept.close()

use_db("trim")
after = R._db_chars()
check(len(after) <= R._CHAR_PER_HANDLE and all(rec[4] for rec in after),
      "...so the wire is back under the ceiling, all bound (%d records)"
      % len(after))

rc, out = trim("--restore")
check(rc == 0 and active_extras() == 3,
      "6d. --restore puts back exactly what it deactivated (%d active)"
      % active_extras())

# A character the bridge has paired but not learned a name for yet (a map
# imported from the old flat file shape carries no names at all) is still ON
# its id: the in-use check must not skip it, or it reports a live character's
# id as free.
bridge_map("")
rc, out = trim()
check(rc == 0 and "charid 7 IS ON THIS ID" in out,
      "6e. a paired character with no name yet still counts as in use")

# --------------------------------------------------------------------------- #
print()
print("7. THE PROFILE CARD NAMES A GAME ONCE, NOT ONCE PER CHARACTER")

# A handle with several FFXI characters (several Content IDs under one code)
# still shows the game exactly once on the identity card -- the separate
# characters live on the title's own select screen (the 1:3 list `_db_chars`
# builds), not as repeated rows here. Before the collapse the card listed a game
# once per Content ID.
import struct                                                      # noqa: E402
card = fresh_db("card")
A.create_polid(card, "CARD", "pw-account")
cmid = A.add_member(card, "CARD", "card", "pw-member")
A.set_handle(card, cmid, "Carder")
chid7 = A.primary_handle_row(card, cmid)["id"]
for code in (1, 2, 3):                        # FFXI + Tetra Master + content 3
    A.grant_content(card, cmid, code)
A.link_member_content_to_primary(card, cmid)
A.ensure_content_slots(card, chid7, X.CONTENT_CODE, 3)    # FFXI x3
card.commit()
check(len(ffxi_ids(card, chid7)) == 3,
      "the handle holds 3 FFXI Content IDs (%d)" % len(ffxi_ids(card, chid7)))
use_db("card")


def card_codes(buf):
    out = []
    for k in range(R._IDREC_CONTENT_MAX):
        present, f02, ctsid, cid = struct.unpack_from(
            "<HHII", buf, k * R._IDREC_CONTENT_STRIDE)
        if present or f02 or ctsid or cid:
            out.append(f02)
    return out


codes7 = card_codes(R._identity_content_entries(A.connect(_DBS["card"]), chid7))
check(codes7.count(X.CONTENT_CODE) == 1,
      "7a. FFXI appears exactly once on the card (%s)" % (codes7,))
check(sorted(codes7) == [1, 2, 3],
      "7b. each owned game appears once -- none lost, none doubled (%s)" % (codes7,))

# --------------------------------------------------------------------------- #
print()
print("8. EXTRA CHARACTER SLOTS NEVER CROWD OUT A TITLE THE MEMBER OWNS")

# `safe_slot_target` reserves one binding position per title the member owns but
# has not placed yet, so raising a title's slot count cannot evict a character
# once the other titles are placed. It only
# bites above a count of one, so at the default this proves the guard.
res = fresh_db("reserve")
A.create_polid(res, "RESV", "pw-account")
rmid8 = A.add_member(res, "RESV", "resv", "pw-member")
A.set_handle(res, rmid8, "Reserver")
rhid8 = A.primary_handle_row(res, rmid8)["id"]
for code in (1, 2, 3, 4, 10, 11):             # owns six titles
    A.grant_content(res, rmid8, code)
res.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
            " content_id, status, linked_at) VALUES (%s,%s,0,%s,'active',%s)",
            (rhid8, X.CONTENT_CODE, "30008000", "2026-10-03T00:00:00Z"))
res.commit()                                  # place ONLY FFXI; five titles unplaced
tgt8 = A.safe_slot_target(res, rhid8, X.CONTENT_CODE, 4, rmid8)
check(tgt8 == 3, "8a. five unplaced titles reserve five positions, want 4 -> 3 (%d)"
      % tgt8)

solo = fresh_db("solo")
A.create_polid(solo, "SOLO", "pw-account")
smid = A.add_member(solo, "SOLO", "solo", "pw-member")
A.set_handle(solo, smid, "Solo")
shid = A.primary_handle_row(solo, smid)["id"]
A.grant_content(solo, smid, X.CONTENT_CODE)   # owns only FFXI
solo.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
             " content_id, status, linked_at) VALUES (%s,%s,0,%s,'active',%s)",
             (shid, X.CONTENT_CODE, "30008100", "2026-10-03T00:00:00Z"))
solo.commit()
tgt8b = A.safe_slot_target(solo, shid, X.CONTENT_CODE, 4, smid)
check(tgt8b == 4, "8b. a handle owning only FFXI keeps the full four (%d)" % tgt8b)

# End to end: a bundled six-title sign-up with the plugin raised to four never
# exceeds the ceiling and loses no title.
TITLE.content_slots = 4
try:
    e2e = fresh_db("e2e")
    acct8 = A.register_account(e2e, "SixTitles", "password123",
                               contents=(1, 2, 3, 4, 10, 11))
    ehid = e2e.execute("SELECT id FROM handle WHERE member_id = %s",
                       (acct8["member_id"],)).fetchone()["id"]
    total8 = A.handle_link_count(e2e, ehid)
    codes8 = {int(r["content_code"]) for r in A.handle_content_list(e2e, ehid)}
    check(total8 <= A.CONTENT_IDS_PER_HANDLE,
          "8c. a bundled sign-up never exceeds %d Content IDs (%d)"
          % (A.CONTENT_IDS_PER_HANDLE, total8))
    check(codes8 >= {1, 2, 3, 4, 10, 11},
          "...and every owned title kept a binding position (%s)" % (sorted(codes8),))
finally:
    TITLE.content_slots = X.CHARACTER_SLOTS

# --------------------------------------------------------------------------- #
print()
print("9. OVER THE CEILING, AN EMPTY CREATE SLOT IS DROPPED, NEVER A CHARACTER")

# A title granted after the extra slots were minted pushes a handle past eight.
# The wire carries only eight, so one slot falls off -- and it must be an unused
# create slot, never one a character sits on. The old order dropped the highest
# slot; here the character is ON the highest slot, so the old order would have
# stranded it.
nine = fresh_db("ninth")
nacct = A.register_account(nine, "NinthChar", "password123",
                           contents=(1, 2, 3, 4, 10, 11))
nhid = nine.execute("SELECT id FROM handle WHERE member_id = %s",
                    (nacct["member_id"],)).fetchone()["id"]
for slot, cid in ((1, "30009001"), (2, "30009002"), (3, "30009003")):
    nine.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                 " content_id, status, linked_at) VALUES (%s,1,%s,%s,'active',%s)",
                 (nhid, slot, cid, "2026-10-03T00:00:00Z"))
nine.commit()
check(A.handle_link_count(nine, nhid) == 9,
      "the handle is one over the ceiling (%d links)" % A.handle_link_count(nine, nhid))
use_db("ninth")
# the bridge id map says a CHARACTER sits on the HIGHEST extra slot (30009003)
ffxidb.write_idmap([{"charid": 42, "content_id": 30009003, "name": "Casdeck",
                     "world_field": 1, "seen": "2026-10-03T00:00:00Z"}], replace=True)
# the plugin caches the id map by a per-database change counter; this suite
# hops between fresh databases, so drop the cache to force a read of THIS one
# (a live server has one database and the counter only ever moves forward).
X._cache["rev"] = None
served9 = R._db_chars()
served_ids = {rec[3] for rec in served9}
served_codes = {rec[2] for rec in served9}
check(len(served9) == R._CHAR_PER_HANDLE,
      "9a. the wire carries exactly %d records (%d)" % (R._CHAR_PER_HANDLE, len(served9)))
check("30009003" in served_ids,
      "9b. the character's Content ID survived, not the old highest-slot drop (%s)"
      % (sorted(served_ids),))
check(served_codes >= {1, 2, 3, 4, 10, 11},
      "9c. every title kept its binding position (%s)" % (sorted(served_codes),))
check(all(rec[4] for rec in served9), "9d. every served record is bound")

# --------------------------------------------------------------------------- #
print()
if FAILED:
    print("RESULT: %d FAILURE(S)" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
else:
    print("RESULT: a handle can hold one FFXI Content ID per character")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAILED else 0)
