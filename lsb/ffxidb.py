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
    python ffxidb.py import idmap FILE [--merge] [--dry-run]
    python ffxidb.py import accounts FILE [--merge] [--dry-run]

`import` moves a map an earlier release kept as a file into its table:
`idmap` reads ffxi_idmap.json (on the core's data volume) into ffxi_idmap,
`accounts` reads ffxi_accounts.json (on the bridge's old state volume) into
ffxi_lsb_account. The file is only read. The import runs in one transaction
and refuses a table that already holds rows (exit 2) unless --merge is given,
which adds only the keys the table lacks. A second run finds nothing to add
and changes nothing. --dry-run prints the same report and writes nothing.
Entries it cannot map are listed and skipped. Import the id map before the
bridge first starts on this database: a bridge on an empty map deals every
character a Content ID afresh.
"""
import datetime
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


# --------------------------------------------------------------------------- #
# importing the files an earlier release kept
# --------------------------------------------------------------------------- #
class ImportRefused(RuntimeError):
    """The source cannot be read as the file it should be."""


class _Rollback(Exception):
    pass


def _iso_mtime(path):
    return datetime.datetime.fromtimestamp(
        os.path.getmtime(path), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json_object(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ImportRefused(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise ImportRefused(f"{path} is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ImportRefused(f"{path} holds a {type(data).__name__}, "
                            "not the JSON object the bridge wrote")
    return data


def _int(value, what):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{what} is {value!r}")
    if isinstance(value, int):
        out = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        out = int(value.strip())
    else:
        raise ValueError(f"{what} is {value!r}, not a whole number")
    if not -(1 << 63) <= out < (1 << 63):
        raise ValueError(f"{what} {out} does not fit a BIGINT")
    return out


def _text(value, what):
    if not isinstance(value, str):
        raise ValueError(f"{what} is {value!r}, not text")
    if "\x00" in value:
        raise ValueError(f"{what} holds a NUL character")
    return value


def read_old_idmap(path):
    """(rows, skipped) from an old ffxi_idmap.json. Both shapes the bridge
    wrote are read: `{charid: content_id}` (the first) and `{charid:
    {content_id, name, world_field, profile, seen}}`. A flat entry gets name
    '' and world_field 0, which mean "not seen yet"; an entry with no profile
    gets NULL, which the profile keeps apart from real zeros. An entry with no
    `seen` gets the file's modification time."""
    raw = _read_json_object(path)
    stamp = _iso_mtime(path)
    rows, skipped = [], []
    for key, ent in raw.items():
        try:
            charid = _int(key, "the charid")
            if isinstance(ent, dict):
                if "content_id" not in ent:
                    raise ValueError("no content_id")
                profile = ent.get("profile")
                if profile is not None and not isinstance(profile, dict):
                    raise ValueError(f"the profile is {profile!r}, not an object")
                row = {"charid": charid,
                       "content_id": _int(ent["content_id"], "the content_id"),
                       "name": _text(ent.get("name") or "", "the name"),
                       "world_field": _int(ent.get("world_field") or 0,
                                           "the world_field"),
                       "profile": profile,
                       "seen": _text(ent.get("seen") or stamp, "seen")}
            else:
                row = {"charid": charid,
                       "content_id": _int(ent, "the content_id"),
                       "name": "", "world_field": 0, "profile": None,
                       "seen": stamp}
        except ValueError as exc:
            skipped.append((key, str(exc)))
            continue
        rows.append(row)
    return rows, skipped


def read_old_accounts(path):
    """(rows, skipped) from an old ffxi_accounts.json, `{key: {login,
    created}}`. A bare member id is the primary world ("5" -> ('', 5)), and
    `tag:id` another ("alt:5" -> ('alt', 5))."""
    raw = _read_json_object(path)
    rows, skipped = [], []
    for key, ent in raw.items():
        try:
            tag, _, mid = str(key).rpartition(":")
            if not isinstance(ent, dict):
                raise ValueError(f"the entry is {ent!r}, not an object")
            rows.append({"world_tag": _text(tag, "the world tag"),
                         "member_id": _int(mid, "the member id"),
                         "login": _text(ent.get("login"), "the login"),
                         "created": _text(ent.get("created"), "created")})
        except ValueError as exc:
            skipped.append((key, str(exc)))
    return rows, skipped


#: store -> (table, key columns, columns, JSONB columns, reader)
IMPORTS = {
    "idmap": (IDMAP_TABLE, ("charid",),
              ("charid", "content_id", "name", "world_field", "profile", "seen"),
              ("profile",), read_old_idmap),
    "accounts": (ACCOUNT_TABLE, ("world_tag", "member_id"),
                 ("world_tag", "member_id", "login", "created"), (),
                 read_old_accounts),
}

#: What an existing row is compared on: `seen` and `created` are stamps, and a
#: row that differs only there is the same pairing.
_COMPARE_SKIP = {"seen", "created"}


def import_file(store, path, merge=False, dry_run=False, out=print):
    """Import one old file. Returns the exit status: 0 done, nothing to do or
    dry run; 1 the source or the database failed; 2 refused, the table
    already holds rows and the source has rows it lacks (without --merge)."""
    table, key, cols, jsonb, reader = IMPORTS[store]
    try:
        rows, skipped = reader(path)
    except ImportRefused as exc:
        out(f"error: {exc}")
        return 1
    pg = db()
    out(f"import {store}: {path} -> {table}")
    out(f"  entries in the file   {len(rows) + len(skipped)}")
    for k, why in skipped:
        out(f"  skipped {k!r}: {why}")
    if not dry_run:
        ready()
    kidx = lambda r: tuple(r[c] for c in key)          # noqa: E731
    result = {}
    try:
        with pg.transaction(lock="crystalbridge.import:" + table) as conn:
            exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok",
                                  (table,)).fetchone()["ok"]
            have = {}
            if exists:
                for r in conn.execute("SELECT %s FROM %s" % (", ".join(cols), table)):
                    have[kidx(r)] = r
            elif not dry_run:
                raise RuntimeError(f"{table} does not exist after the migrations")
            new, same, differs, seen, dups = [], 0, [], set(), 0
            for r in rows:
                k = kidx(r)
                if k in seen:
                    dups += 1
                    continue
                seen.add(k)
                if k not in have:
                    new.append(r)
                elif all(have[k][c] == r[c] for c in cols if c not in _COMPARE_SKIP):
                    same += 1
                else:
                    differs.append(k)
            out(f"  in the table before   {len(have)}"
                + ("" if exists else " (the table does not exist yet)"))
            if dups:
                out(f"  skipped, key repeated  {dups}")
            out(f"  already there         {same + len(differs)}")
            for k in differs:
                out(f"  kept the table's row, the file's differs: {k}")
            out(f"  to insert             {len(new)}")
            result.update(new=new)
            if new and have and not merge:
                result["status"] = "refused"
                raise _Rollback()
            if dry_run:
                result["status"] = "dry-run"
                raise _Rollback()
            n = 0
            for r in new:
                n += conn.execute(
                    "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT DO NOTHING"
                    % (table, ", ".join(cols),
                       ", ".join("%s::jsonb" if c in jsonb else "%s" for c in cols)),
                    [json.dumps(r[c], sort_keys=True)
                     if c in jsonb and r[c] is not None else r[c]
                     for c in cols]).rowcount
            result.update(status="done" if n else "nothing", inserted=n)
    except _Rollback:
        pass
    except pg.Error as exc:
        out(f"FAILED, rolled back: {exc}")
        return 1
    status = result["status"]
    if status == "refused":
        out(f"REFUSED: {table} already holds rows. Nothing was written. Run "
            "again with --merge to add only the keys it lacks.")
        return 2
    if status == "dry-run":
        out("Dry run: nothing was written.")
    elif status == "nothing":
        out(f"Nothing to import: {table} already holds every entry. "
            "Nothing was changed.")
    else:
        out(f"Done: {result['inserted']} row(s) written.")
    return 0


def _import_main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python ffxidb.py import",
                                 description="Import a map an earlier release "
                                 "kept as a file (uses POL_DATABASE_URL).")
    ap.add_argument("store", choices=sorted(IMPORTS))
    ap.add_argument("file")
    ap.add_argument("--merge", action="store_true",
                    help="add only the keys the table lacks")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be imported; write nothing")
    args = ap.parse_args(argv)
    pg = db()
    try:
        return import_file(args.store, args.file, merge=args.merge,
                           dry_run=args.dry_run)
    except (pg.DatabaseNotConfigured, pg.MigrationError, pg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        pg.close()


def _main(argv):
    if argv and argv[0] == "import":
        return _import_main(argv[1:])
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
