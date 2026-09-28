"""Do the FFXI id map's WRITER and READER use the same database?

    python tools/ffxi_idmap_check.py

No containers, no network: it reads this repository's docker-compose.yml (the
WRITER: the `bridge` service's POL_DATABASE_URL) and the OpenLobby core's
docker-compose.yml with this repository's docker-compose.title.yml on top (the
READER: the `login` service's POL_DATABASE_URL, where the title plugin
lsb/ffxititle.py runs), and compares them. It checks that the table the
bridge's migrations create is the one both sides name, that the bridge waits
for the database it writes, that the bridge reads the core's session table
under the key the core writes it, and that no core service publishes host
port 54002, which LSB's search server needs.

WHY THIS EXISTS -- a bug that ran on a live deployment for days with no symptom
anybody could see (found 2026-08-23):

  * The **bridge** persisted `{charid: {content_id, world_field}}` to a file on
    the shared data volume, `/data/ffxi_idmap.json`.
  * The **lobby** read it back through a variable whose built-in default named
    a different file, which did not exist. Nothing set the variable.
  * The reader turned the missing file into `{}` WITHOUT LOGGING, on the
    perfectly good reasoning that a stack with no bridge legitimately has no
    map. So a misconfigured stack looked exactly like an unconfigured one.

The consequence is not cosmetic. The lobby's `1:3` FFXI record carries the world
identity dword at record `+0x0C`, which comes from this map; FFXI's world lookup
(`FFXiMain FUN_100FFE00`, reached from char-select sub-state 14) will not open
the world socket unless it matches, and on no match writes -1 and aborts --
**POL-0001**. An empty map means it can never match.

The map is a table in the core's PostgreSQL now (ffxi_idmap), so the file
paths are gone, but the failure has the same shape: a bridge writing to one
database and a lobby reading another serves world field 0 to everybody. This
check crosses the two compose files a plain env-var diff cannot, so a
re-pointed URL or a stray environment override is caught before a player
finds it.

The OpenLobby checkout is found through OPENLOBBY_DIR, or as ../openlobby
beside this repository; without one the check SKIPS (exit 77).
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, os.pardir))
sys.path.insert(0, HERE)
from openlobby_paths import require_root, skip                      # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "lsb"))
import ffxidb                                                       # noqa: E402

READER_SERVICE = "login"
WRITER_SERVICE = "bridge"
SEARCH_PORT = "54002"

FAILED = []


def check(cond, what):
    print(f"  {'OK  ' if cond else 'FAIL'} {what}")
    if not cond:
        FAILED.append(what)
    return cond


def yaml_module():
    try:
        import yaml
    except ImportError:
        skip("ffxi_idmap_check", "PyYAML (pip install pyyaml)")
    return yaml


def load(yaml, path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def read_source(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def expand_default(value):
    """`${VAR:-default}` inside a value, as compose expands it with VAR unset."""
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", value or "")


def service(doc, name):
    return (doc.get("services") or {}).get(name) or {}


def env_of(svc):
    env = svc.get("environment")
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    if isinstance(env, list):                       # "KEY=value" form
        out = {}
        for item in env:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out
    return {}


def url_host(url):
    m = re.match(r"^[a-z]+://(?:[^@/]*@)?([^:/]+)", url or "")
    return m.group(1) if m else None


def published_host_ports(svc):
    out = set()
    for p in svc.get("ports") or []:
        if isinstance(p, dict):
            out.add(str(p.get("published", "")))
            continue
        s = str(p).split("/")[0]                # strip /udp
        parts = s.split(":")
        # "host:container", "ip:host:container", or just "container"
        out.add(parts[-2] if len(parts) >= 2 else parts[-1])
    return out


def main():
    print(__doc__.splitlines()[0])
    print()
    yaml = yaml_module()
    core = require_root("ffxi_idmap_check")

    # --- the writer: this repository ------------------------------------
    here_doc = load(yaml, os.path.join(ROOT, "docker-compose.yml"))
    writer = service(here_doc, WRITER_SERVICE)
    if not check(bool(writer), f"a {WRITER_SERVICE!r} service exists in docker-compose.yml"):
        return 1
    w_env = env_of(writer)
    w_url = expand_default(w_env.get("POL_DATABASE_URL"))
    print(f"  writer {WRITER_SERVICE}: POL_DATABASE_URL={w_url or None}")
    check(bool(w_url), "the bridge sets POL_DATABASE_URL explicitly")
    stale = sorted(k for k in ("FFXI_IDMAP_FILE", "FFXI_ACCTMAP_FILE", "POL_ACCOUNTS_DB")
                   if k in w_env)
    check(not stale, f"the bridge sets none of the retired file knobs ({stale or 'none'})")

    # --- the reader: the OpenLobby core, with the title override on top ---
    print(f"  reader: {core}")
    core_doc = load(yaml, os.path.join(core, "docker-compose.yml"))
    reader = service(core_doc, READER_SERVICE)
    if not check(bool(reader), f"a {READER_SERVICE!r} service exists in the core compose"):
        return 1
    title_doc = load(yaml, os.path.join(ROOT, "docker-compose.title.yml"))
    r_url = expand_default(env_of(service(title_doc, READER_SERVICE)).get("POL_DATABASE_URL")
                           or env_of(reader).get("POL_DATABASE_URL"))
    print(f"  reader {READER_SERVICE}: POL_DATABASE_URL={r_url or None}")
    check(bool(r_url), "the core's login service sets POL_DATABASE_URL")
    check(bool(w_url) and w_url == r_url,
          f"reader and writer use the SAME database ({r_url!r} vs {w_url!r})")
    host = url_host(w_url)
    check(host is not None and bool(service(core_doc, host)),
          f"the database host {host!r} is a service of the core's compose project")

    # --- the bridge waits for that database -------------------------------
    deps = writer.get("depends_on") or {}
    cond = (deps.get(host) or {}).get("condition") if isinstance(deps, dict) else None
    check(cond == "service_healthy",
          f"the bridge starts after {host!r} is healthy (depends_on: {cond!r})")

    # --- one table name on both sides --------------------------------------
    migrations = "".join(read_source("lsb", "ffxi_migrations", fn)
                         for fn in sorted(os.listdir(ffxidb.MIGRATIONS_DIR))
                         if fn.endswith(".sql"))
    check(re.search(r"CREATE TABLE\s+%s\s*\(" % re.escape(ffxidb.IDMAP_TABLE), migrations)
          is not None,
          f"the bridge's migrations create table {ffxidb.IDMAP_TABLE}")
    title_src = read_source("lsb", "ffxititle.py")
    check(re.search(r"^IDMAP\s*=\s*ffxidb\.IDMAP_TABLE\s*$", title_src, re.M) is not None,
          "the title plugin reads the table ffxidb names (ffxititle.IDMAP)")
    bridge_src = read_source("lsb", "ffxi_bridge.py")
    check("ffxidb.write_idmap(" in bridge_src and "ffxidb.load_idmap()" in bridge_src,
          "the bridge writes and reads the map through ffxidb")

    # --- the core's session table, which says who is launching ------------
    # Not the id map, but the same failure shape: a key the core writes under
    # one name and the bridge reads under another leaves every launch
    # unattributed, and the bridge then refuses them all.
    sess_src = os.path.join(core, "services", "core", "lobbysession.py")
    core_key = None
    if os.path.isfile(sess_src):
        with open(sess_src, encoding="utf-8") as fh:
            m = re.search(r'^_SESSION_KEY\s*=\s*"([^"]+)"', fh.read(), re.M)
        core_key = m.group(1) if m else None
    m = re.search(r'^AUTH_SESSION_KEY\s*=\s*"([^"]+)"', bridge_src, re.M)
    bridge_key = m.group(1) if m else None
    print(f"  session table: core writes {core_key!r}, bridge reads {bridge_key!r}")
    check(core_key is not None and core_key == bridge_key,
          "the bridge reads the core's session table under the key the core writes")

    # --- the port the search server needs ---------------------------------
    holders = sorted(name for name, svc in (core_doc.get("services") or {}).items()
                     if SEARCH_PORT in published_host_ports(svc or {}))
    check(not holders,
          f"no core service publishes host port {SEARCH_PORT} (LSB search needs it)"
          + (f"; held by {holders}" if holders else ""))
    print()

    if FAILED:
        print(f"RESULT: {len(FAILED)} FAILURE(S)")
        for f in FAILED:
            print(f"  - {f}")
        print()
        print("A mismatch here means the lobby serves world field 0 for every FFXI")
        print("character and FFXI refuses the world connect (POL-0001).")
        return 1
    print("RESULT: the FFXI id map's writer and reader agree, and 54002 is free")
    return 0


if __name__ == "__main__":
    sys.exit(main())
