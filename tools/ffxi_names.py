#!/usr/bin/env python3
"""FFXI character name <-> PlayOnline handle mapping.

WHY THIS EXISTS
---------------
PlayOnline identifies people by HANDLE. FINAL FANTASY XI identifies them by
CHARACTER NAME. Retail could join the two because SE ran both sides; we cannot,
because the two halves of the chain live in different databases and neither knows
the other exists:

    FFXI character name   ->  Content ID   ->  handle
    \\__ LandSandBoat (xidb) __/                \\__ POL accounts __/
                          \\____ nobody ____/

The POL account database knows `handle_content(handle_id, content_code,
content_id)` -- which handle owns Content ID `1000000001` -- but has never been
told that the FFXI character on it is called "Alice". LandSandBoat knows the
character but nothing about PlayOnline. **The bridge is the only place both are
ever seen together**, so it records the pairing in its id map (the `ffxi_idmap`
table) as it watches the lobby: charid 1, Content ID 1000000001, name "Alice".

This tool copies the named rows of that map into the core's `content_character`
table through `accounts.record_character_name`, which is the POL-side answer to
"who is Alice?" -- and, read the other way, "what is this handle's FFXI
character called?". The table is part of the core's schema; the core's
`accounts.character_names` is its read side.

USAGE
    python tools/ffxi_names.py import
    python tools/ffxi_names.py list
    python tools/ffxi_names.py lookup <character-name>
    python tools/ffxi_names.py whois <handle-name>

`import` is idempotent: re-running it updates names and timestamps in place.

Every command uses the core's database, POL_DATABASE_URL (or `--db URL`), so
the usual invocation is inside the bridge container:

    docker compose run --rm --entrypoint python \
        -v "$PWD/tools:/app/tools:ro" bridge tools/ffxi_names.py list
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# `lsb/` beside this tool in the repo, `/app` inside the bridge image.
sys.path.insert(0, os.path.join(_HERE, os.pardir, "lsb"))
sys.path.append("/app")
import ffxidb  # noqa: E402

#: FFXI's content code.
CONTENT_CODE = 1


def connect(url=None):
    """A connection to the core's account database (POL_DATABASE_URL, or
    `url` when it is a postgresql:// URL)."""
    try:
        return ffxidb.accounts().connect(url)
    except Exception as exc:
        sys.exit(f"cannot open the account database: {exc}")


def load_map():
    """The bridge's named characters: [(content_id, charid, name, world)].

    Rows the bridge has not learned a name for yet are skipped rather than
    imported as blanks, so a fresh map produces "nothing to import" instead of
    a table full of empty names.
    """
    try:
        rows = ffxidb.load_idmap()
    except Exception as exc:
        sys.exit(f"cannot read the bridge's id map ({exc}) -- has the bridge "
                 f"run against this database yet?")
    out = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        out.append((str(r["content_id"]), int(r["charid"]), name, None))
    return out


def cmd_import(args):
    conn = connect(args.db)
    entries = load_map()
    if not entries:
        print(f"table {ffxidb.IDMAP_TABLE}: no named characters yet -- nothing "
              f"to import.")
        print("The bridge fills the name in when it relays a character list or a")
        print("world handoff, so launch FFXI once and re-run this.")
        conn.close()
        return 0
    A = ffxidb.accounts()
    added = updated = 0
    try:
        for content_id, charid, name, world in entries:
            outcome, old = A.record_character_name(
                conn, content_id, name, content_code=CONTENT_CODE,
                world_charid=charid, world_name=world)
            if outcome == "added":
                added += 1
            else:
                if outcome == "renamed":
                    print(f"  renamed: {old!r} -> {name!r} on Content ID {content_id}")
                updated += 1
    finally:
        conn.close()
    print(f"imported {added} new, refreshed {updated} "
          f"(from table {ffxidb.IDMAP_TABLE})")
    return 0


def _joined(conn, where="", params=()):
    """content_character joined through handle_content to the owning handle.

    LEFT JOIN on purpose: a character whose Content ID is not linked to any handle
    is a real and interesting state (the link was deleted, or the content belongs
    to another member), and an INNER JOIN would hide it.
    """
    return conn.execute(
        "SELECT cc.character_name, cc.content_id, cc.world_charid, "
        "       h.handle_name, h.id AS handle_id "
        "FROM content_character cc "
        "LEFT JOIN handle_content hc "
        "       ON hc.content_id = cc.content_id AND hc.content_code = cc.content_code "
        "      AND hc.status = 'active' "
        "LEFT JOIN handle h ON h.id = hc.handle_id "
        + where + " ORDER BY cc.character_name", params).fetchall()


def _print(rows):
    if not rows:
        print("(none)")
        return
    print(f"{'character':16} {'content id':12} {'charid':>6}  handle")
    for r in rows:
        handle = r["handle_name"] or "-- not linked to any handle --"
        print(f"{r['character_name']:16} {r['content_id']:12} "
              f"{r['world_charid'] if r['world_charid'] is not None else '?':>6}  {handle}")


def cmd_list(args):
    conn = connect(args.db)
    try:
        _print(_joined(conn))
    finally:
        conn.close()
    return 0


def cmd_lookup(args):
    conn = connect(args.db)
    try:
        # Case-insensitive: FFXI capitalises names, POL does not care, and a
        # lookup that only matched exact case would fail on user input.
        _print(_joined(conn, "WHERE UPPER(cc.character_name) = UPPER(%s)", (args.name,)))
    finally:
        conn.close()
    return 0


def cmd_whois(args):
    conn = connect(args.db)
    try:
        _print(_joined(conn, "WHERE UPPER(h.handle_name) = UPPER(%s)", (args.handle,)))
    finally:
        conn.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=None,
                    help="a postgresql:// URL (default POL_DATABASE_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import", help="copy the bridge's character names into "
                                      "the core's content_character")
    p.set_defaults(fn=cmd_import)
    sub.add_parser("list", help="every known character and its handle").set_defaults(fn=cmd_list)
    p = sub.add_parser("lookup", help="character name -> handle")
    p.add_argument("name")
    p.set_defaults(fn=cmd_lookup)
    p = sub.add_parser("whois", help="handle -> character name")
    p.add_argument("handle")
    p.set_defaults(fn=cmd_whois)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
