"""FFXI federation, provider side: our members' characters on OTHER operators' worlds.

The bridge relays a POL member's FFXI lobby to our own LandSandBoat world. With federation
the same lobby also lists the member's characters on remote worlds -- worlds that trust our
provider id (PhoenixPS2 ext/xitoken/SPEC.md, lsb/xitoken.py) -- and sends the client to a
remote world's map server when one of those is picked. Nothing changes for our own world:
its packets still go through LSB's xi_connect exactly as before.

For a remote world the bridge is the lobby. It cannot relay to the remote world's
xi_connect (the spec's world exposes no lobby, only its gateway), so it answers the client
itself, from the gateway's JSON:

    client 0x24 world list   LSB's 0x23, plus one entry per remote world
    client 0x1F char list    LSB's 0x20, remote characters placed in its empty slots
    client 0x22/0x21 create  on a remote world: POST /xi/v1/characters, answered here
    client 0x14 delete       of a remote character: DELETE /xi/v1/characters/<id>
    client 0x07 select       of a remote character: POST /xi/v1/world-entry, then a 0x0B
                             built here, pointing the client at the remote map server

The client then speaks to the remote map directly (UDP), with the session key it always
uses (ffxi_bridge.A2_SESSION_KEY), which the world-entry token carries.

POL's own character table (the 1:3 the client checks at select, POL-0001 when it misses)
is served from the bridge's id map, so a remote character is paired with one of the
member's Content IDs and recorded there like a local one -- under a key LSB charids can
never reach (`remote_key`), so nothing that looks charids up in our LSB ever meets one.

OFF unless FFXI_FED_WORLDS names at least one world. Knobs:

  FFXI_FED_WORLDS     JSON file: [{"id": "xi1...", "keyset": "https://<gateway>/xi/v1/keyset"
                      or a file path, "pin": "sha256:..." (optional), "name": "Shown"
                      (optional, <= 15 chars; default the key set's name)}]. Order sets
                      the world numbers the client sees: 0x21, 0x22, ...
  FFXI_FED_KEYS       our provider keys: identity.key + signing-<kid>.key
                      (tools/ffxi_provider.py keygen). The newest kid signs.
  FFXI_FED_MEMBERS    comma list of POL member ids who see the remote worlds; empty =
                      every member. For trying a world out before everyone gets it.
  FFXI_FED_CLIENT_IP  "CIDR=ip,..." the address a remote world will see for clients in
                      CIDR (LAN and overlay clients reach it through our NAT, so their
                      own address is not what it sees). First match wins; no match = the
                      client's own address.
"""
import ipaddress
import json
import os
import threading
import time

import xitoken as X

#: The first world number a remote world gets. LSB's own world is 0x20 (its world list
#: hardcodes it; the bridge follows whatever the 0x23 says).
FIRST_WORLD_NO = 0x21
#: World names are 16 bytes in the lobby's world list and char records, NUL-terminated.
WORLD_NAME_MAX = 15
#: How long a fetched character list is reused within one launch (the client asks for the
#: list several times around a create).
CHARS_TTL = 20.0
#: Remote character keys in the id map: world number in the high 32 bits. LSB charids are
#: 24-bit, so a remote key is never a local charid.
REMOTE_KEY_SHIFT = 32
KEYSET_TTL = 600.0
#: Seconds a gateway call may take. The client's char list waits on them, and a list that
#: takes too long ends in lobby error 3113 for the whole lobby, not just the remote world.
GATEWAY_TIMEOUT = float(os.environ.get("FFXI_FED_TIMEOUT", "4"))
#: A world that failed at the transport level (down, refusing, timing out) is not asked
#: again for this long, so every member's char list does not wait on it in turn.
DOWN_TTL = 60.0


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [fed] {msg}", flush=True)


class RemoteWorld:
    def __init__(self, no, server_id, keyset_source, pin=None, name=None):
        self.no = no
        self.server_id = server_id
        self.keyset_source = keyset_source
        self.pin = pin
        self.name_override = name
        self.keyset = None
        self.loaded_at = 0.0
        self.gateway = None
        self.error = None                         # the last refresh's failure, for the admin tool
        self.down_at = 0.0                        # last transport failure (refresh or char list)

    def is_down(self):
        return time.time() - self.down_at < DOWN_TTL

    @property
    def name(self):
        n = self.name_override or (self.keyset.name if self.keyset else None) or self.server_id[:15]
        return n[:WORLD_NAME_MAX]

    @property
    def search(self):
        """(ip, port) of the world's search server from its key set, or None."""
        text = (self.keyset.world or {}).get("search") if self.keyset else None
        if not text or ":" not in text:
            return None
        ip, _, port = text.rpartition(":")
        try:
            ipaddress.IPv4Address(ip)
            return ip, int(port)
        except ValueError:
            return None

    def refresh(self, force=False):
        """Load and check the world's key set (from a URL or a file). Keeps the last good
        one when a refresh fails; a world never loaded is unusable."""
        if not force and self.keyset and time.time() - self.loaded_at < KEYSET_TTL:
            return True
        if not force and self.is_down():
            return self.keyset is not None
        try:
            src = self.keyset_source
            if src.startswith(("https://", "http://")):
                base = src[:-len("/xi/v1/keyset")] if src.endswith("/xi/v1/keyset") else src
                text = X.WorldGateway(base, pin=self.pin, timeout=GATEWAY_TIMEOUT).keyset()
            else:
                with open(src) as f:
                    text = f.read().strip()
            ks = X.load_keyset(text, self.server_id)
            if not ks.world or not ks.world.get("gateway"):
                raise X.XiTokenError("bad_claims", "the key set does not describe a world")
        except Exception as exc:  # any failure: this world is skipped, the lobby goes on
            if not isinstance(exc, (X.XiTokenError, X.GatewayError)):
                self.down_at = time.time()
            self.error = str(exc)
            if self.keyset is None:
                _log(f"world {self.server_id}: key set unusable ({exc}); not listed")
            else:
                _log(f"world {self.server_id}: key set refresh failed ({exc}); keeping the last one")
            return self.keyset is not None
        self.error = None
        if self.keyset is None or ks.issued >= self.keyset.issued:
            moved = self.keyset is None or ks.world != self.keyset.world
            self.keyset = ks
            self.gateway = X.WorldGateway(ks.world["gateway"], pin=self.pin, timeout=GATEWAY_TIMEOUT)
            if moved:
                _log(f"world 0x{self.no:02X} {self.name!r} = {ks.server_id}, gateway "
                     f"{ks.world['gateway']}, search {ks.world.get('search') or '-'}")
        self.loaded_at = time.time()
        return True


class Federation:
    """The configured remote worlds and our provider identity. One per bridge process."""

    def __init__(self, worlds, issuer, client_ip_map=(), members=None):
        self.worlds = worlds                      # [RemoteWorld], in world-number order
        self.issuer = issuer
        self.members = members                    # None = everyone, else {str(member id)}
        self.client_ip_map = list(client_ip_map)  # [(network, ip)]
        self._chars = {}                          # (member, world no) -> (at, [char dict])
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, environ=os.environ):
        path = (environ.get("FFXI_FED_WORLDS") or "").strip()
        if not path:
            return None
        keys = (environ.get("FFXI_FED_KEYS") or "").strip()
        try:
            entries = read_worlds(path)
            if not entries:
                return None             # no world listed yet (the admin tool adds them)
            issuer = load_issuer(keys)
        except (OSError, ValueError) as exc:
            _log(f"federation OFF: {exc}")
            return None
        worlds = []
        for i, ent in enumerate(entries if isinstance(entries, list) else []):
            sid, src = ent.get("id"), ent.get("keyset")
            if not X.is_server_id(sid) or not src:
                _log(f"FFXI_FED_WORLDS entry {i} ignored: needs a server id and a keyset")
                continue
            worlds.append(RemoteWorld(FIRST_WORLD_NO + len(worlds), sid, src, ent.get("pin"), ent.get("name")))
        if not worlds:
            _log("federation OFF: FFXI_FED_WORLDS lists no usable world")
            return None
        only = {m.strip() for m in (environ.get("FFXI_FED_MEMBERS") or "").split(",") if m.strip()}
        fed = cls(worlds, issuer, parse_ip_map(environ.get("FFXI_FED_CLIENT_IP", "")), only or None)
        for w in worlds:
            w.refresh(force=True)
        _log(f"federation ON as provider {issuer.issuer_id}: "
             + ", ".join(f"0x{w.no:02X} {w.name!r}" for w in worlds)
             + (f"; members {sorted(only)} only" if only else "; every member"))
        return fed

    def allows(self, member_id):
        return member_id is not None and (self.members is None or str(member_id) in self.members)

    def usable(self):
        return [w for w in self.worlds if w.refresh()]

    def world(self, no):
        for w in self.worlds:
            if w.no == no:
                return w
        return None

    def client_ip(self, seen_ip):
        """The address a remote world will see for this client."""
        try:
            addr = ipaddress.ip_address((seen_ip or "").strip("[]"))
        except ValueError:
            return seen_ip
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        for net, ip in self.client_ip_map:
            if addr.version == net.version and addr in net:
                return ip
        return str(addr)

    def characters(self, member_id, world, fresh=False):
        """The member's characters on `world` ([] on any failure, logged): a remote world
        that is down must never cost the member their own world's list."""
        key = (str(member_id), world.no)
        with self._lock:
            hit = self._chars.get(key)
        if hit and not fresh and time.time() - hit[0] < CHARS_TTL:
            return hit[1]
        if world.is_down():
            return hit[1] if hit else []
        try:
            chars = world.gateway.characters(self.issuer.account(world.server_id, str(member_id)))
            if not isinstance(chars, list):
                raise X.GatewayError(200, "bad_reply", "characters is not a list")
        except Exception as exc:  # any failure leaves this world's characters out, never the list
            if not isinstance(exc, (X.GatewayError, X.XiTokenError)):
                world.down_at = time.time()           # transport trouble: skip the world for DOWN_TTL
            _log(f"member {member_id}: characters on {world.name!r} unavailable ({exc!r})")
            return hit[1] if hit else []
        chars = [c for c in chars if isinstance(c, dict) and isinstance(c.get("id"), int)]
        with self._lock:
            self._chars[key] = (time.time(), chars)
        return chars

    def forget(self, member_id, world=None):
        with self._lock:
            for k in [k for k in self._chars if k[0] == str(member_id) and (world is None or k[1] == world.no)]:
                del self._chars[k]


def read_worlds(path):
    """The FFXI_FED_WORLDS entries ([] for a missing file)."""
    try:
        with open(path) as f:
            entries = json.load(f)
    except FileNotFoundError:
        return []
    if not isinstance(entries, list):
        raise ValueError("%s is not a JSON list" % path)
    return entries


def write_worlds(path, entries):
    """Replace the world list in one step (a reader never sees half a file)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_issuer(keys_dir):
    if not keys_dir:
        raise ValueError("FFXI_FED_KEYS is not set")
    identity = X.SigningKey.from_file("identity", os.path.join(keys_dir, "identity.key"))
    signers = sorted(n for n in os.listdir(keys_dir) if n.startswith("signing-") and n.endswith(".key"))
    if not signers:
        raise ValueError(f"{keys_dir} has no signing-*.key")
    kid = signers[-1][len("signing-"):-len(".key")]
    return X.Issuer(identity.server_id, X.SigningKey.from_file(kid, os.path.join(keys_dir, signers[-1])))


def parse_ip_map(text):
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cidr, ip = part.split("=", 1)
            out.append((ipaddress.ip_network(cidr.strip(), strict=False), str(ipaddress.IPv4Address(ip.strip()))))
        except ValueError as exc:
            _log(f"FFXI_FED_CLIENT_IP entry {part!r} ignored: {exc}")
    return out


# --------------------------------------------------------------------------------------------
# Lobby packets the bridge builds for a remote world. Layouts from LSB's own xi_connect
# (login_packets.h, data_session.cpp, view_session.cpp, login_helpers.cpp at PhoenixPS2
# 5c667078a37), and checked against the client's create packets the bridge captured.

HDR = 0x1C                      # size u32, "IXFF", command u32, identifier[16]
REC_LEN = 0x8C                  # one character record (lpkt_chr_info_sub2)
WORLD_REC_LEN = 20              # one world list entry: no u32, name[16]
#: 0x22 (name check) carries the WORLD the player chose, by name, at 0x40 -- seen on the
#: wire (prod capture 2026-10-04: "OpenLobby" there). LSB never reads it: it has one world.
#: 0x21 (create) does not name the world (its TC world_no is 0), so the 0x22's choice is
#: remembered per connection.
NAME_CHECK_NAME = 0x20
NAME_CHECK_WORLD = 0x40
#: 0x21 create, the fields LSB's createCharacter reads (login_helpers.cpp:636-) and the
#: job/nation beside them in TC_OPERATION_MAKE at 0x30.
CREATE_RACE, CREATE_JOB, CREATE_NATION, CREATE_SIZE, CREATE_FACE = 0x30, 0x32, 0x36, 0x39, 0x3C
#: login_errors.h
ERR_NAME_UNAVAILABLE = 313
ERR_CANNOT_CONNECT_WORLD = 305
ERR_LOBBY = 332


def sign(pkt):
    """A server->client lobby packet: size and IXFF filled in, then the MD5 over the whole
    packet with the identifier zeroed (LSB's md5 + copyHashIntoPacket)."""
    import hashlib
    import struct
    p = bytearray(pkt)
    struct.pack_into("<I", p, 0, len(p))
    p[4:8] = b"IXFF"
    p[0x0C:0x1C] = bytes(16)
    p[0x0C:0x1C] = hashlib.md5(bytes(p)).digest()
    return bytes(p)


def ok_packet():
    """0x03: the plain OK that answers 0x14, 0x21, 0x22 and 0x28."""
    p = bytearray(0x20)
    p[8] = 0x03
    return sign(p)


def error_packet(code):
    """0x04 with an error code (login_helpers generateErrorMessage)."""
    import struct
    p = bytearray(0x24)
    p[8] = 0x04
    p[0x1C] = 0x10
    struct.pack_into("<H", p, 0x20, code)
    return sign(p)


def _name16(text):
    raw = (text or "").encode("latin1", "replace")[:15]
    return raw + bytes(16 - len(raw))


def world_list_entries(worlds):
    """The 0x23 records to append for remote worlds."""
    import struct
    return b"".join(struct.pack("<I", w.no) + _name16(w.name) for w in worlds)


def char_record(world, char, content_id):
    """One 0x20 record for a remote character, filled the way LSB's data_session fills one
    from its own `chars` row, from the gateway's JSON for the same columns."""
    import struct
    rec = bytearray(REC_LEN)
    charid = int(char["id"])
    struct.pack_into("<IHHHBB", rec, 0, content_id & 0xFFFFFFFF, charid & 0xFFFF, world.no, 1,
                     1 if char.get("rename") else 0, (charid >> 16) & 0xFF)
    rec[0x0C:0x1C] = _name16(char.get("name"))
    rec[0x1C:0x2C] = _name16(world.name)
    job = char.get("job") or {}
    look = char.get("look") or {}
    zone = int(char.get("zone") or 0)
    face = int(char.get("face") or 0)
    struct.pack_into("<HBBHBBBBH", rec, 0x2C, int(char.get("race") or 0), int(job.get("main") or 0),
                     int(job.get("sub") or 0), face, int(char.get("nation") or 0), 0, face & 0xFF,
                     int(char.get("size") or 0), 0)       # world_no: LSB leaves it 0 too
    struct.pack_into("<8H", rec, 0x38, face, *(int(look.get(k) or 0) & 0xFFFF
                                              for k in ("head", "body", "hands", "legs", "feet", "main", "sub")))
    rec[0x48] = zone & 0xFF
    rec[0x49] = int(job.get("main_level") or 0) & 0xFF
    rec[0x4F] = (zone >> 8) & 1
    return bytes(rec)


def next_login_packet(charid, name, world_no, map_ip, map_port, search):
    """0x0B: the world handoff. ffxi_id is the WORLD's charid, because the client puts it in
    the map login's UniqueNo and the map looks the session up by it (ffxi_bridge MAP_HANDOFF)."""
    import socket
    import struct
    p = bytearray(0x48)
    p[8] = 0x0B
    struct.pack_into("<II", p, 0x1C, charid, charid & 0xFFFF)
    p[0x24:0x34] = _name16(name)
    struct.pack_into("<I", p, 0x34, world_no)
    p[0x38:0x3C] = socket.inet_aton(map_ip)
    struct.pack_into("<I", p, 0x3C, map_port)
    if search:
        p[0x40:0x44] = socket.inet_aton(search[0])
        struct.pack_into("<I", p, 0x44, search[1])
    return sign(p)


def create_fields(pkt):
    """The gateway's create body fields from a client 0x21 (LSB's validation ranges)."""
    return {"race": pkt[CREATE_RACE], "job": pkt[CREATE_JOB], "nation": pkt[CREATE_NATION],
            "size": pkt[CREATE_SIZE], "face": pkt[CREATE_FACE]}


def name_check_fields(pkt):
    """(character name, world name) a client 0x22 carries."""
    def text(off):
        return bytes(pkt[off:off + 16]).split(b"\0")[0].decode("latin1").strip()
    return text(NAME_CHECK_NAME), (text(NAME_CHECK_WORLD) if len(pkt) >= NAME_CHECK_WORLD + 16 else "")


def remote_key(world_no, charid):
    """The id-map key of a remote character: never a 24-bit LSB charid."""
    return (int(world_no) << REMOTE_KEY_SHIFT) | (int(charid) & 0xFFFFFFFF)


def split_remote_key(key):
    """(world no, remote charid) for a remote key, None for a local charid."""
    key = int(key)
    if key >> REMOTE_KEY_SHIFT == 0:
        return None
    return key >> REMOTE_KEY_SHIFT, key & 0xFFFFFFFF
