"""ffxidb.py -- the FFXI bridge's tables in the stack's PostgreSQL database.

The bridge keeps two maps: which LSB charid is which PlayOnline Content ID
(`ffxi_idmap`, read back by the title plugin inside the core), and which LSB
account each POL member has (`ffxi_lsb_account`). Both live in the database
the OpenLobby core runs (POL_DATABASE_URL). Their schema is this repository's
own migration set, `lsb/ffxi_migrations/`, applied with OpenLobby's runner
(polcore.db.migrate). Versions 6001-6999 of the shared schema_migrations table
belong to CrystalBridge.

The account database itself is OpenLobby's: the bridge reads it through the
core's `accounts` module, never with its own SQL on the core's tables.

polcore and accounts come with the OpenLobby image the bridge is built on.
Outside the image (the self-tests, a host run) they are found through
OPENLOBBY_SERVICES, OPENLOBBY_DIR/services, or an `openlobby` checkout beside
this repository. Nothing is imported until a function here is called, so the
bridge module itself still loads without the core.

    python ffxidb.py migrate      apply what is pending (uses POL_DATABASE_URL)
    python ffxidb.py status       list this repository's migrations
"""
import importlib
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
MIGRATIONS_DIR = os.environ.get("FFXI_MIGRATIONS_DIR",
                                os.path.join(HERE, "ffxi_migrations"))
#: The schema_migrations versions this repository owns.
VERSION_RANGE = (6001, 6999)

IDMAP_TABLE = "ffxi_idmap"
IDMAP_REV_TABLE = "ffxi_idmap_rev"
ACCOUNT_TABLE = "ffxi_lsb_account"

#: SQLSTATE for "relation does not exist": the bridge's migrations have not
#: run against this database yet.
UNDEFINED_TABLE = "42P01"


def _openlobby_candidates():
    """Directories that may hold OpenLobby's services/ (accounts.py, polcore/)."""
    out = []
    if os.environ.get("OPENLOBBY_SERVICES"):
        out.append(os.environ["OPENLOBBY_SERVICES"])
    if os.environ.get("OPENLOBBY_DIR"):
        out.append(os.path.join(os.environ["OPENLOBBY_DIR"], "services"))
    out.append(os.path.join(HERE, os.pardir, os.pardir, "openlobby", "services"))
    return [os.path.abspath(p) for p in out]


def _import_core(name):
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    for cand in _openlobby_candidates():
        if os.path.isfile(os.path.join(cand, "polcore", "db.py")):
            if cand not in sys.path:
                sys.path.append(cand)
            return importlib.import_module(name)
    raise ImportError(f"{name} (OpenLobby's services/) was not found; set "
                      "OPENLOBBY_SERVICES or OPENLOBBY_DIR")


def db():
    """polcore.db, the core's PostgreSQL layer."""
    return _import_core("polcore.db")


def accounts():
    """The core's accounts module."""
    return _import_core("accounts")


def kv():
    """polcore.kv, the core's live-state store (Valkey, POL_VALKEY_URL)."""
    return _import_core("polcore.kv")


def missing_table(exc):
    """True when `exc` says a table does not exist (migrations not applied)."""
    return getattr(exc, "sqlstate", None) == UNDEFINED_TABLE


_lock = threading.Lock()
_ready = set()


def migration_files():
    """polcore's listing of this repository's migrations, checked against the
    version range so a misnumbered file cannot land in another set's slot
    (schema_migrations is keyed by number, and a reused one is skipped)."""
    pg = db()
    files = pg.migration_files(MIGRATIONS_DIR)
    lo, hi = VERSION_RANGE
    bad = [name for version, name, _ in files if not lo <= version <= hi]
    if bad:
        raise pg.MigrationError(f"{', '.join(bad)}: CrystalBridge's migrations "
                                f"are numbered {lo}-{hi}")
    return files


def ready():
    """Apply this repository's pending migrations, once per process and
    database. Returns polcore.db. Raises when the database cannot be used."""
    pg = db()
    url = pg.database_url()
    if url in _ready:
        return pg
    with _lock:
        if url not in _ready:
            migration_files()
            pg.migrate(directory=MIGRATIONS_DIR,
                       log=lambda msg: print(f"[ffxidb] {msg}", file=sys.stderr,
                                             flush=True))
            _ready.add(url)             # only once it is genuinely ready
    return pg


def forget():
    """Forget which databases were migrated (a test that switches databases)."""
    with _lock:
        _ready.clear()


# --------------------------------------------------------------------------- #
# the id map: charid <-> Content ID, name, world field, profile tail
# --------------------------------------------------------------------------- #
def load_idmap():
    """Every row of the id map as dicts {charid, content_id, name,
    world_field, profile, seen}. Raises when the database cannot be read."""
    pg = ready()
    return pg.query(f"SELECT charid, content_id, name, world_field, profile, seen"
                    f" FROM {IDMAP_TABLE} ORDER BY charid")


def write_idmap(rows, delete=(), replace=False):
    """Upsert `rows` (dicts as load_idmap returns them; `profile` None or a
    dict) and delete the charids in `delete`, in one transaction. With
    `replace`, every row not in `rows` is deleted too, so the table ends up
    holding exactly `rows`."""
    pg = ready()
    with pg.transaction() as conn:
        if replace:
            keep = [int(r["charid"]) for r in rows]
            conn.execute(f"DELETE FROM {IDMAP_TABLE}"
                         f" WHERE NOT (charid = ANY(%s))", (keep,))
        for r in rows:
            profile = r.get("profile")
            conn.execute(
                f"INSERT INTO {IDMAP_TABLE} (charid, content_id, name,"
                f" world_field, profile, seen) VALUES (%s, %s, %s, %s, %s::jsonb, %s)"
                f" ON CONFLICT (charid) DO UPDATE SET"
                f" content_id = EXCLUDED.content_id, name = EXCLUDED.name,"
                f" world_field = EXCLUDED.world_field,"
                f" profile = EXCLUDED.profile, seen = EXCLUDED.seen",
                (int(r["charid"]), int(r["content_id"]), r.get("name") or "",
                 int(r.get("world_field") or 0),
                 None if profile is None else json.dumps(profile, sort_keys=True),
                 r["seen"]))
        if delete:
            conn.execute(f"DELETE FROM {IDMAP_TABLE} WHERE charid = ANY(%s)",
                         ([int(c) for c in delete],))


def idmap_rev():
    """The id map's change counter (moves on every write), or None when the
    bridge's tables do not exist in this database. Does not migrate: the
    title plugin reads a database the bridge owns the schema of."""
    pg = db()
    try:
        row = pg.query_one(f"SELECT rev FROM {IDMAP_REV_TABLE} WHERE id = 1")
    except pg.Error as exc:
        if missing_table(exc):
            return None
        raise
    return int(row["rev"]) if row else 0


def read_idmap_rows(conn=None):
    """The id map's rows without migrating (the title plugin's read). Raises
    when the table cannot be read, including when it does not exist."""
    sql = (f"SELECT charid, content_id, name, world_field, profile"
           f" FROM {IDMAP_TABLE} ORDER BY charid")
    if conn is not None:
        return [dict(zip(r.keys(), r)) for r in conn.execute(sql).fetchall()]
    return db().query(sql)


# --------------------------------------------------------------------------- #
# the LSB account map: POL member -> LSB account, per world
# --------------------------------------------------------------------------- #
def load_accounts():
    """[{world_tag, member_id, login, created}] for every recorded account."""
    pg = ready()
    return pg.query(f"SELECT world_tag, member_id, login, created"
                    f" FROM {ACCOUNT_TABLE} ORDER BY world_tag, member_id")


def record_account(world_tag, member_id, login, created):
    """Record (or refresh) one member's LSB account in one world."""
    pg = ready()
    pg.upsert(ACCOUNT_TABLE, {"world_tag": world_tag or "",
                              "member_id": int(member_id),
                              "login": login, "created": created},
              key=("world_tag", "member_id"))


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python ffxidb.py",
                                 description="CrystalBridge's migrations "
                                 "(uses POL_DATABASE_URL).")
    ap.add_argument("cmd", choices=("migrate", "status"))
    args = ap.parse_args(argv)
    pg = db()
    try:
        if args.cmd == "migrate":
            before = set(pg.applied_migrations())
            ready()
            done = [name for version, name, _ in migration_files()
                    if version not in before]
            print("applied: " + ", ".join(done) if done else "up to date")
        else:
            have = pg.applied_migrations()
            for version, name, _path in migration_files():
                row = have.get(version)
                state = (f"applied {row['applied_at']:%Y-%m-%d %H:%M}"
                         if row else "pending")
                print(f"{name:<32} {state}")
        return 0
    except (pg.DatabaseNotConfigured, pg.MigrationError, pg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        pg.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
