# CrystalBridge

The bridge between the OpenLobby PlayOnline core and a LandSandBoat FINAL
FANTASY XI world server. With both running, a player signs into an unmodified
PlayOnline Viewer, presses Play on FINAL FANTASY XI, and lands in a
LandSandBoat world under their own PlayOnline account, with no connection to
Square Enix and no client patching.

This is a bridge, not a world server. The world is upstream LandSandBoat
(LSB), run here unmodified from the LSB team's own prebuilt images, pinned by
digest. OpenLobby supplies accounts, login and Content IDs. This repository
supplies the piece in between, which plays the role xiloader plays for
LSB-only setups, but server-side, so the real Viewer launch works as shipped.

This project contains no Square Enix code, art or data. You need your own
FFXI client and PlayOnline Viewer.

## How it fits together

1. The Viewer logs into OpenLobby, which serves the account's FFXI Content IDs
   in its character table (lobby message `1:3`).
2. Play launches the FFXI client, which resolves `ffxi00.pol.com` to the
   OpenLobby host (OpenLobby's DNS does that) and dials 54001 (view) and 54230
   (data). Both land on the bridge.
3. The bridge asks OpenLobby's session table which member is launching,
   authenticates to LSB's auth server (54231, TLS+JSON) as that member's own
   LSB account (`pol<member id>`, created on first use), and relays the lobby
   stream, injecting LSB's session hash into every packet exactly as xiloader
   would client-side.
4. On the way back it translates LSB's character ids into the PlayOnline
   Content IDs the Viewer expects, and records the pairing plus the world
   identity dword in the `ffxi_idmap` table of OpenLobby's PostgreSQL
   database, which OpenLobby's login service reads so its `1:3` record matches
   what the client was told. Without that match the client refuses to open the
   world socket (POL-0001).
5. After character select the client sends its world login by UDP to
   `LSB_ADVERTISE_IP:54230`; the bridge relays that to LSB's map server
   unchanged, purely so the exchange is observable.

## The title plugin (the Viewer's profile)

The core builds the profile the Viewer shows for a FINAL FANTASY XI Content ID from
data only this title holds, so a small plugin runs inside the core's `login`
and `authsess` processes (OpenLobby's `services/titles.py`, `POL_TITLES`).
`lsb/Dockerfile.title` layers it on the core image and `docker-compose.title.yml`
swaps that image into those two services. From this directory, with the core
checked out beside it:

```
docker compose --project-directory ../openlobby     -f ../openlobby/docker-compose.yml -f docker-compose.title.yml     up -d --build login authsess
```

Without it the core still logs FFXI accounts in and lists their characters, but serves them with no world identity, and the world connect fails with POL-0001; the plugin also fills the profile's world, nation, zone, job and race from the bridge's id map. To run several titles, build each title image on the previous
one (`OPENLOBBY_IMAGE`) and list them all in `POL_TITLES` in OpenLobby's
`.env`, for example `POL_TITLES=tmtitle,ffxititle`.

## Prerequisites

- Docker with Compose v2
- OpenLobby checked out beside this repository as `../openlobby`, with its
  image built (`docker compose build` there, which tags `openlobby:latest`).
  This stack runs in OpenLobby's compose project: the bridge image is built on
  the core image, and the bridge keeps its state in the core's PostgreSQL
  (the `ffxi_*` tables, created by the bridge at start) and reads the accounts
  through the core's own accounts module. OpenLobby's login service reads the
  bridge's id map from the same database, so nothing needs to be configured
  on the OpenLobby side.
- An FFXI client of the version the pinned LSB image expects, launched from
  the PlayOnline Viewer that OpenLobby already serves. The bridge itself does
  not care about the client version; LSB does (see Troubleshooting).
- The two LSB mesh volumes, populated once (below). About 570 MB.
- Python 3.10+ on the host for the selftests and the admin tools

## Bring-up

**1. Mesh volumes (one time).** LSB's map server needs both `/navmeshes`
(pathfinding) and `/ximeshes` (zone geometry), and aborts at start without the
second one, which LSB's own Docker notes do not mention. Both come from the
LSB team's ximeshes image, which populates a volume on first mount:

```
docker volume create lsb_navmeshes
docker run --rm -v lsb_navmeshes:/navmeshes ghcr.io/landsandboat/ximeshes:latest
docker volume create lsb_ximeshes
docker run --rm -v lsb_ximeshes:/ximeshes ghcr.io/landsandboat/ximeshes:latest
```

They are external volumes on purpose: `docker compose down -v` never touches
them. The ximeshes image is used by tag here because it is consumed once for
its data; pin it by digest yourself if you want reproducibility there too.

**2. Configure and start.**

```
cp .env.example .env      # set LSB_ADVERTISE_IP and change every password
docker compose --project-directory ../openlobby \
    --env-file ../openlobby/.env --env-file .env \
    -f ../openlobby/docker-compose.yml -f docker-compose.yml \
    up -d --build db db-update db-zoneip connect search world map bridge
```

OpenLobby's `.env` comes first so its `POL_DB_PASSWORD` reaches the bridge's
database URL. The rest of this README writes `docker compose` for that whole
invocation (the project directory, both env files and both compose files).

Without building the bridge image (published to
`ghcr.io/prettyopenlobby/crystalbridge` on every push), add
`-f docker-compose.ghcr.yml` after the other two files.

The first start imports LSB's schema (a minute or two; `db-update` runs
once and exits) and points every zone at `LSB_ADVERTISE_IP`. `LSB_ADVERTISE_IP`
must be an address the FFXI client can route to; the default 127.0.0.1 works
only for a client on the same machine.

**3. Check.** `docker compose ps` shows db, connect, search, world, map and
bridge up, and `docker compose logs bridge` ends with `startup auth OK` (or
`created the shared LSB account ... and authenticated` on a fresh database)
and `listening on 0.0.0.0:54001`. Then run `python tools/ffxi_idmap_check.py`
with `OPENLOBBY_DIR` pointing at your OpenLobby checkout: it proves the bridge
and the core's login service use the same database, and that nothing in the
core stack publishes 54002, which LSB's search server needs.

The bridge's state is two tables in OpenLobby's PostgreSQL, backed up with
it: `ffxi_idmap` (which LSB character is which Content ID) and
`ffxi_lsb_account` (which LSB account each member has). The rest lives in
named volumes that survive restarts: `crystalbridge_lsb-db` (the world
database), `crystalbridge_bridge-logs` and `crystalbridge_lsb-logs`. The bridge
reads OpenLobby's session table from its Valkey.

A deployment that ran an earlier release kept the two maps as files,
`ffxi_idmap.json` on OpenLobby's data volume and `ffxi_accounts.json` on the
bridge's own `bridge-state` volume (`crystalbridge_bridge-state`, from when
the bridge was a compose project of its own). Import them before the first
start of this one, after OpenLobby's own import (its docs/database.md,
"Moving an existing /data"), from this directory:

```
DC="docker compose --project-directory ../openlobby --env-file ../openlobby/.env --env-file .env -f ../openlobby/docker-compose.yml -f docker-compose.yml"
$DC run --rm --no-deps --entrypoint python bridge ffxidb.py import idmap /data/ffxi_idmap.json
$DC run --rm --no-deps -v crystalbridge_bridge-state:/state:ro --entrypoint python bridge ffxidb.py import accounts /state/ffxi_accounts.json
```

Each reads its file without changing it, runs in one transaction, prints what
it imported and each entry it could not map, and refuses a table that already
holds rows unless given `--merge`, which adds only the keys the table lacks.
`--dry-run` prints the same report and writes nothing. Running one again
changes nothing.

A bridge that starts on an empty `ffxi_idmap` pairs every character afresh,
and a character the Viewer knows under another Content ID gets POL-0001. So
while the table is empty and `/data/ffxi_idmap.json` (the core's data volume,
mounted read-only) still holds pairings, the bridge does not open its ports:
it logs `NOT STARTING` with the command to run, and opens by itself once the
table holds rows. `FFXI_IDMAP_START_EMPTY=1` starts it on the empty table
anyway. On a new stack there is no old file, and the bridge starts at once.
A missing `ffxi_lsb_account` costs nothing lasting: the bridge records each
member's account again at their next launch.

## Giving an account FINAL FANTASY XI

A PlayOnline account plays FFXI when its handle holds FFXI Content IDs
(content code 1). OpenLobby mints them:

- through its admin panel (`http://127.0.0.1:8090` on the OpenLobby host):
  create the account with FINAL FANTASY XI among its per-title grants, or add
  the grant to an existing account; or
- through in-client sign-up, if the registration code was issued with FFXI
  granted.

A handle holding FFXI is given `POL_FFXI_CHARACTER_SLOTS` Content IDs
(default 1), one per character slot, because FFXI issues one Content ID per
character. The setting belongs to the title plugin (its `content_slots`), so it
takes effect where the plugin is loaded: set it in OpenLobby's `.env` and
`docker-compose.title.yml` passes it to `login` and `authsess`. The core mints
the extra ids when a process that loads the plugin grants FFXI, and tops a
member's handles up at each login, which covers accounts made by the sign-up
page or the admin panel. A handle can hold at most eight Content IDs across
all titles, and OpenLobby never mints or serves a ninth: the Viewer refuses to
open FFXI on a handle it thinks is over that limit. On a handle granted every
title, one FFXI character is all there is room for; raise the setting only for
handles holding fewer titles. FFXI must also be listed in OpenLobby's
`POL_LOBBY_CONTENT_IDS` for the Play button to appear; the default list
includes it.

A database that already went over the limit is cleaned up with the plugin's
own command, which reports unless given `--apply` and never touches an id a
character is on:

```
docker compose --project-directory ../openlobby -f ../openlobby/docker-compose.yml \
    -f docker-compose.title.yml exec login python ffxititle.py - trim-slots
```

The first launch does the rest: the bridge creates the member's LSB account
(`pol<member id>`, password derived from `FFXI_ACCT_SECRET`, never stored),
the client shows empty character slots carrying the member's free Content
IDs, and creating a character pairs the id it spent with the charid LSB
minted. Deleting a character releases its Content ID back to that member's
pool. A Content ID, once served to a client, is never re-minted or moved: the
client keeps that character's macros and settings under `USER/<hex id>/`.

LSB's delete is a soft delete: it parks the row (`accid = 0`) and keeps the
name, and its create check still finds that name, so a deleted character's
name could never be used again. The bridge renames every parked row to
`del<charid>` (a name the client cannot type) at startup, a few seconds after
each delete and every ten minutes, which frees the name and keeps the row for
recovery. The same sweep releases the Content ID of any parked charid a stale
character list re-paired. `FFXI_TOMBSTONE_DELETED=0` turns it off.

Attribution needs a signed-in Viewer session. The FFXI lobby stream carries no
PlayOnline identity, so the bridge reads OpenLobby's session table: one
signed-in session at the client's address is the answer; two behind one NAT
are told apart by which one has already launched; a session from any other
address is never a candidate (`FFXI_REQUIRE_SAME_ADDRESS=1`), and a launch
that cannot be attributed is refused (`FFXI_REQUIRE_SIGNED_IN=1`) rather than
guessed, since a wrong guess shows one player another's characters and spends
their Content ID. A client-side helper that
stamps the session id into the first lobby packet removes the inference
entirely; the bridge honours that stamp when present.

## Importing an existing character

A player who has a character elsewhere (retail or another server) can bring it
over from a `polexport-1` JSON dump: identity, jobs and levels, gil,
inventory with augments and container sizes, equipment, skills, spells, key
items and mounts, quest and mission logs, currencies, job points, teleport
unlocks, home point. `lsb/ffxi_import_core.py` documents the format and what
is and is not transferred; the addon that produces the dump is not part of
this repository. The dump is produced by the player's own client and is an
honour-system import, not a verified transfer.

Admin path, two reviewable steps:

```
python tools/ffxi_import.py sql dump.json --member 5 > import.sql
docker compose exec -T db mariadb -u"$LSB_DB_USER" -p"$LSB_DB_PASSWORD" "$LSB_DB_NAME" < import.sql
# the final SELECT prints the new charid; then pair it with the member's Content ID:
docker compose run --rm --entrypoint python -v "$PWD/tools:/app/tools:ro" bridge \
    tools/ffxi_import.py bind <charid> --member 5 --name <Charname>
docker compose restart bridge
```

(`bind` writes the pairing to the `ffxi_idmap` table; the restart makes the
running bridge read it.) The bridge also exposes the same import as `POST /import` on port 54004 for a
client-side helper that knows the Viewer's session token; it binds the pairing
in-process, so no restart is needed on that path. `BRIDGE_IMPORT_PORT=0`
turns it off.

Other admin tools, run the same way (the tools are not baked into the image,
so they are mounted for the one command): `ffxi_provision.py list|create|rehome`
(LSB accounts per member, and moving a character onto a member's account) and
`ffxi_names.py` (copies the character names the bridge has seen into
OpenLobby's `content_character` table, and looks them up).

## Ports

| host port | proto | owner | role |
|---|---|---|---|
| 54001 | TCP | bridge | lobby VIEW channel (the client dials `ffxi00.pol.com:54001`) |
| 54230 | TCP | bridge | lobby DATA channel |
| 54230 | UDP | bridge | world channel, relayed to LSB's map server |
| 54004 | TCP | bridge | character-import endpoint |
| 54002 | TCP | LSB search | search / auction house (the client dials `LSB_ADVERTISE_IP:54002`) |
| 127.0.0.1:8088 | TCP | LSB world | LSB's HTTP admin API, localhost only |

LSB's auth (54231), conf (51220) and internal view/data ports are reached by
the bridge over the compose network and are not published. The client's own
CONF channel is the PlayOnline lobby, not LSB's.

## Troubleshooting

- **FFXI-3331 at login, LSB logs "incorrect client version".** The classic
  one: LSB's `VER_LOCK` refused the build the client reported. `LSB_VER_LOCK`
  defaults to 0 here (accept any build); if you set 1 or 2, `LSB_CLIENT_VER`
  must match the client's `patch.ver` (2 compares the first six characters
  and requires the client to be at least that). The PS2 client always reads as
  older than any PC build. Client version skew also shows up as an in-world
  mismatch even when login is accepted: the pinned LSB image expects a client
  of its own era.
- **POL-0001 at character select ("writing character data to PlayOnline").**
  The client's world lookup found no entry in PlayOnline's 64-slot character
  table matching the character it picked. Either OpenLobby is not reading the
  bridge's id map (the two point at different databases;
  `tools/ffxi_idmap_check.py`; OpenLobby's lobby log says
  `the FFXI id map (table ffxi_idmap) does not exist`), or the member has no free
  Content ID for the character (the bridge log says `NO FREE FFXI Content
  ID`). Delete a character, or raise `POL_FFXI_CHARACTER_SLOTS` in
  OpenLobby's `.env` if the handle has room under the eight-Content-ID limit
  (the member picks the new ids up at the next POL login).
- **FFXI-3100.** Nothing answered on 54001: the bridge is down, or DNS sent
  the client elsewhere. `docker compose logs bridge`.
- **FFXI-3332 after "Acquiring Player Data".** LSB had no data session for
  the view session. The bridge opens one itself; check its log for
  `DATA-COMP` errors reaching `connect`.
- **Character selects, then hangs connecting to the world.** The 0x0B
  handoff advertised an address the client cannot reach: `LSB_ADVERTISE_IP`
  is wrong or 127.0.0.1. `db-zoneip` writes it into `zone_settings.zoneip`;
  change `.env` and `docker compose up -d` again to rerun it. Also confirm
  UDP 54230 is open to the client.
- **The bridge log says "no signed-in POL session ... refusing the launch".**
  The Viewer's session was not marked signed in (OpenLobby restarted since
  the login, or the launch came from an address no session matches). Sign out
  of the Viewer, sign back in, launch again. The log line `no POL session from
  this address; REFUSING` means sessions exist but only at other addresses:
  the Viewer and FFXI reached the host from different addresses (a proxy, or
  a dev stack behind Docker NAT, where `FFXI_REQUIRE_SAME_ADDRESS=0` applies).
- **`search` fails to start with "port is already allocated".** Something
  else on the host holds 54002 (an older OpenLobby release published it from
  an observation logger; current releases do not).
- **FFXI-3001 mid-session.** The world UDP relay's return path died. Fixed
  for the known cause (LSB's map legally sends empty datagrams); the relay now
  rebuilds a dead flow on the client's next datagram. If it recurs, the world
  capture under `bridge-logs` (`pkt/world/*.jsonl`) holds both directions of
  the flow from its first datagram.
- **Two players on one Viewer machine see each other's characters.** They
  share an address and both are signed in; the bridge tells them apart by
  launch order and blocks character creation while it is ambiguous. Sign one
  out to create.

## Optional: a second world for the Test Server client

The "FINAL FANTASY XI Test Server" client build (PlayOnline content 0015)
dials the same fixed ports as retail and is told apart only by the build it
reports. `docker-compose.lsb-test.yml` runs a second LSB world for it on
ports +1000, and `LSB_ALT_VER=201108` in `.env` makes the bridge route such
clients there. Comments in that file explain the wiring. Retail-only
deployments can ignore it.

## Selftests

```
python tools/bridge_run_all.py
```

runs the offline suite (no LSB, no client). Most suites exercise OpenLobby's
own modules or compose files and need a checkout of it, found via
`OPENLOBBY_DIR` or as `../openlobby` beside this repository; without one they
report `skip`, not failure. The suites that touch the accounts or the bridge's
tables each get a fresh PostgreSQL database from the core's `tools/pgtest.py`:
the runner starts one throwaway `postgres` container for the run and removes
it at the end, or uses the server `POL_TEST_DATABASE_URL` names. Without
either they report `skip`; `POL_TEST_REQUIRE_DB=1` makes that a failure. `tools/ffxi_bfdiff` is a C++ differential harness
for LSB's world-packet cipher; it needs LSB's sources (its header says which)
and is not part of the runner.

## Regenerating the LSB tables

`lsb/ffxi_lsb_tables.py` is generated from LSB's source at the revision the
pinned image was built from. When you re-pin the image, clone that revision
of https://github.com/LandSandBoat/server and run

```
python tools/gen_ffxi_lsb_tables.py --lsb path/to/lsb-server          # rewrite
python tools/gen_ffxi_lsb_tables.py --lsb path/to/lsb-server --check  # is it stale?
```

## What is not included, and why

- No Square Enix files: no client, no Viewer, no game data. LSB's images and
  mesh data are the LSB team's own and are pulled from their registry.
- No world server code: LSB is upstream and unmodified.
- No client-side helper: the session stamp and the self-serve import endpoint
  are supported by the bridge, but the client half is not part of this
  repository. Everything works without it.
- No account data: the LSB database starts empty; accounts come from OpenLobby.

## License

GPL-3.0 (see LICENSE and NOTICE). Two files are derived from LandSandBoat,
which is GPL-3.0: `lsb/ffxi_lsb_tables.py` is generated from its source, and
`tools/ffxi_bfdiff/common/cbasetypes.h` is a verbatim excerpt of one of its
headers. The whole repository therefore carries the same license.

## Credits

- LandSandBoat (https://github.com/LandSandBoat/server): the world server,
  the prebuilt images and the mesh data. This project would be nothing
  without it.
- xiloader, whose client-side session injection this bridge reproduces
  server-side.
- The PlayOnline preservation community.
