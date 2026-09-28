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
Needs the OpenLobby core's `services/` (accounts.py, responders.py, titles.py): set
OPENLOBBY_DIR to a checkout of it, or keep one beside this repository as
../openlobby. Without it the suite SKIPS (exit 77) rather than failing.
"""

import os
import shutil
import sqlite3
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from openlobby_paths import require_services                      # noqa: E402
SERVICES = require_services("ffxi_char_slots_test")

TMP = tempfile.mkdtemp(prefix="ffxi-char-slots-")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")
os.environ["POL_DATA_DIR"] = TMP
os.environ["POL_LOG_DIR"] = TMP
os.environ["POL_RESOURCE_DIR"] = os.path.join(TMP, "resources")

import accounts as A                                               # noqa: E402
import responders as R                                             # noqa: E402

LSB = os.path.join(HERE, os.pardir, "lsb")
sys.path.insert(0, LSB)
import ffxititle as X                                              # noqa: E402
TITLE = X.register()          # what POL_TITLES=ffxititle does in the core

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def fresh_db(name="accounts.db"):
    path = os.path.join(TMP, name)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return A.connect(path)


def ffxi_ids(db, handle_id):
    return [r["content_id"] for r in A.handle_content_list(db, handle_id)
            if int(r["content_code"]) == X.CONTENT_CODE]


N = X.CHARACTER_SLOTS

# --------------------------------------------------------------------------- #
print("1. A NEW ACCOUNT GETS ITS FFXI CHARACTER SLOTS (want %d)" % N)

db = fresh_db()
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
    " GROUP BY content_id HAVING n > 1").fetchall()
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
db1 = fresh_db("hook.db")
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
    rh = db1.execute("SELECT id FROM handle WHERE member_id = ?",
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

# Build the PRE-SLOT schema by hand: this is what every account created before
# the slot column existed looks like, and the point of the check is that the id it is
# already serving does not change. A re-mint here costs that player every macro
# they ever wrote.
legacy = os.path.join(TMP, "legacy.db")
if os.path.exists(legacy):
    os.remove(legacy)
raw = sqlite3.connect(legacy)
raw.executescript("""
CREATE TABLE polid (id INTEGER PRIMARY KEY AUTOINCREMENT, polid TEXT UNIQUE,
                    pw_hash TEXT, pw_salt TEXT, created_at TEXT);
CREATE TABLE member (id INTEGER PRIMARY KEY AUTOINCREMENT, polid TEXT,
                     member_no INTEGER DEFAULT 0, login_name TEXT, pw_hash TEXT,
                     pw_salt TEXT, access_level INTEGER DEFAULT 0,
                     status TEXT DEFAULT 'active', created_at TEXT);
CREATE TABLE handle (id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER,
                     handle_name TEXT, is_primary INTEGER DEFAULT 1,
                     created_at TEXT, client_guid INTEGER DEFAULT 0);
CREATE TABLE handle_content (handle_id INTEGER NOT NULL, content_code INTEGER NOT NULL,
                             content_id TEXT, status TEXT NOT NULL DEFAULT 'active',
                             linked_at TEXT NOT NULL,
                             PRIMARY KEY (handle_id, content_code));
INSERT INTO polid  VALUES (1, 'LEGACY', 'h', 's', '2026-08-01T00:00:00Z');
INSERT INTO member VALUES (1, 'LEGACY', 0, 'LEGACY', 'h', 's', 0, 'active',
                           '2026-08-01T00:00:00Z');
INSERT INTO handle VALUES (1, 1, 'Legacy', 1, '2026-08-01T00:00:00Z', 0);
INSERT INTO handle_content VALUES (1, 1, '30000037', 'active', '2026-08-22T02:13:28Z');
INSERT INTO handle_content VALUES (1, 2, '30000038', 'active', '2026-08-22T02:13:28Z');
""")
raw.commit()
raw.close()

db2 = A.connect(legacy)                       # runs _migrate, i.e. the rebuild
cols = set(r["name"] for r in db2.execute("PRAGMA table_info(handle_content)"))
check("slot" in cols, "the migration added the slot column")

after = ffxi_ids(db2, 1)
check("30000037" in after, "the served FFXI id survived the migration (%s)" % (after,))
row = db2.execute("SELECT slot FROM handle_content WHERE handle_id = 1"
                  " AND content_code = 1 AND content_id = '30000037'").fetchone()
check(row is not None and int(row["slot"]) == 0,
      "...and it is slot 0, i.e. still the handle's FFXI identity")
check(len(after) == N, "...and the handle was topped up to %d (%d)" % (N, len(after)))
tm2 = [r["content_id"] for r in A.handle_content_list(db2, 1)
       if int(r["content_code"]) == 2]
check(tm2 == ["30000038"], "the Tetra Master id was left alone (%s)" % (tm2,))
kept = db2.execute("SELECT COUNT(*) n FROM handle_content_pre_slots").fetchone()["n"]
check(int(kept) == 2, "the pre-migration rows are kept for recovery (%s)" % (kept,))

# Idempotent: running it again must not mint a second set.
again = A.ensure_title_slots(db2)
check(again == 0, "a second top-up mints nothing (%s)" % (again,))
check(len(ffxi_ids(db2, 1)) == N, "...and the count is unchanged")

# A handle that does NOT hold FFXI gets nothing -- entitlement is `content`, and
# this must not hand the title to everybody.
db2.execute("INSERT INTO handle VALUES (2, 1, 'NoFFXI', 0, '2026-08-01T00:00:00Z', 0)")
db2.commit()
A.ensure_title_slots(db2)
check(not ffxi_ids(db2, 2), "a handle without FFXI is not given any")

# --------------------------------------------------------------------------- #
print()
print("3. MOVING A TITLE BETWEEN HANDLES CARRIES EVERY SLOT")

# THE REGRESSION THIS EXISTS FOR. `link_content_to_handle` used to DELETE the
# code off the sibling handles and re-INSERT one row. With one id per game that
# was lossless. With four it would silently destroy three Content IDs -- and an
# id that has been served cannot be re-minted.
db3 = fresh_db("move.db")
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
            " client_guid) VALUES (?,?,0,?,0)", (mm, "Second", "2026-09-03T00:00:00Z"))
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
    " GROUP BY content_id HAVING n > 1").fetchall()
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
over = fresh_db("overflow.db")
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "overflow.db")
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
                 " content_id, status, linked_at) VALUES (?,1,?,?,'active',?)",
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
full = fresh_db("ceiling.db")
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
room = fresh_db("room.db")
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

os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")


print()
print("5. THE BRIDGE SEES THE WHOLE POOL")

# This is the payoff: the bridge offers a member's UNSPENT Content IDs as the
# client's empty character slots (`rewrite_s2c`), and picks one when the client
# creates. Pool of one = the failure this suite exists for.
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
os.environ["FFXI_IDMAP_FILE"] = os.path.join(TMP, "idmap.json")
import ffxi_bridge as B                                            # noqa: E402
B.ACCOUNTS_DB = os.environ["POL_ACCOUNTS_DB"]
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
import io                                                          # noqa: E402
import json                                                        # noqa: E402
import subprocess                                                  # noqa: E402

CLI_DB = os.path.join(TMP, "trim.db")
IDMAP = os.path.join(TMP, "trim_idmap.json")

cli = fresh_db("trim.db")
acct = A.register_account(cli, "Examplemember05", "password123",
                          contents=(1, 2, 3, 4, 10, 11, 14))
chid = cli.execute("SELECT id FROM handle WHERE member_id = ?",
                   (acct["member_id"],)).fetchone()["id"]
for slot in (1, 2, 3):                       # what a pre-fix sign-up was minted
    cli.execute("INSERT INTO handle_content (handle_id, content_code, slot,"
                " content_id, status, linked_at) VALUES (?,1,?,?,'active',?)",
                (chid, slot, "3000099%d" % slot, "2026-09-22T00:00:00Z"))
cli.commit()
extras = [r["content_id"] for r in A.handle_content_list(cli, chid)
          if int(r["content_code"]) == 1 and int(r["slot"]) != 0]
json.dump({"7": {"content_id": int(extras[0]), "name": "Ayla", "world_field": 1}},
          io.open(IDMAP, "w", encoding="utf-8"))


def trim(*argv):
    # The plugin sits beside the core's modules in the title image; here the
    # core's services/ goes on the path the same way.
    env = dict(os.environ, PYTHONPATH=SERVICES)
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


rc, out = trim("--idmap", IDMAP)
check(rc == 0 and active_extras() == 3,
      "6a. the default is a REPORT and writes nothing (rc %d, %d active)"
      % (rc, active_extras()))
check("Ayla IS ON THIS ID" in out,
      "...and it names the character sitting on an id")

# 6b. THE ONE THAT MATTERS. No id map = no way to know what is in use, so the
# command must not guess. An empty map must never read as "nothing is in use".
rc, out = trim("--apply", "--idmap", os.path.join(TMP, "gone.json"))
check(rc != 0 and "REFUSING" in out and active_extras() == 3,
      "6b. --apply REFUSES when the id map cannot be read (rc %d, %d active)"
      % (rc, active_extras()))
check(all(ord(ch) < 127 for ch in out),
      "...and says so in ASCII, which a cp1252 console can actually print")

rc, out = trim("--apply", "--idmap", IDMAP)
check(rc == 0 and active_extras() == 1,
      "6c. --apply trims the free ids and KEEPS the one with a character on it"
      " (%d left)" % active_extras())
kept = A.connect(CLI_DB)
check([r["content_id"] for r in A.handle_content_list(kept, chid)
       if int(r["content_code"]) == 1 and int(r["slot"]) != 0] == [extras[0]],
      "...and the one it kept is Ayla's")
kept.close()

os.environ["POL_ACCOUNTS_DB"] = CLI_DB
after = R._db_chars()
check(len(after) <= R._CHAR_PER_HANDLE and all(rec[4] for rec in after),
      "...so the wire is back under the ceiling, all bound (%d records)"
      % len(after))
os.environ["POL_ACCOUNTS_DB"] = os.path.join(TMP, "accounts.db")

rc, out = trim("--restore")
check(rc == 0 and active_extras() == 3,
      "6d. --restore puts back exactly what it deactivated (%d active)"
      % active_extras())

# The bridge has written two map shapes over its life and the in-use check has
# to understand both, or it reports a live character's id as free.
json.dump({"7": int(extras[0])}, io.open(IDMAP, "w", encoding="utf-8"))
rc, out = trim("--idmap", IDMAP)
check(rc == 0 and "IS ON THIS ID" in out,
      "6e. the flat legacy id map shape is still understood")

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
