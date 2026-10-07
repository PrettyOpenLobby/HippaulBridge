#!/usr/bin/env python3
"""The bridge as an FFXI federation PROVIDER, driven offline against a fake world gateway.

lsb/ffxi_federation.py plus the hooks in lsb/ffxi_bridge.py list a member's characters on
remote worlds in the FFXI lobby, create and delete them there, and hand the client to a
remote map server at select. This drives the bridge's own functions with lobby packets and a
gateway on loopback that VERIFIES every token with lib xitoken, trusting our provider key as
a real world would, so a token the spec would refuse fails here too.

The create packets are the client's own, captured by the bridge on prod 2026-10-04
(logs/lsb/pkt 00856 cmd22 / 00858 cmd21). No database, no LSB, no network beyond loopback.
"""
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.pop("POL_VALKEY_URL", None)
os.environ["FFXI_PKT_DUMP"] = "0"
os.environ["FFXI_WORLD_CAPTURE"] = "0"
os.environ["FFXI_TOMBSTONE_DELETED"] = "0"
os.environ["FFXI_FED_TIMEOUT"] = "1"           # section 11's hanging world, quickly
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_bridge as B                                            # noqa: E402
import ffxi_federation as F                                        # noqa: E402
import xitoken as X                                                # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


# --- the bridge's persistence and pool, in memory --------------------------------------------
B.save_idmap = lambda *keys: None
POOL = {7: [30000700, 30000701, 30000702]}
B.pol_content_ids = lambda member_id=None: list(POOL.get(member_id, []))
B._idmap.clear()

# --- a world that trusts our provider ----------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="ffxi-fed-test-")
provider_id = X.SigningKey.generate("identity")
signer = X.SigningKey.generate("2026-10")
with open(os.path.join(TMP, "identity.key"), "w") as f:
    f.write(provider_id.paserk_secret() + "\n")
with open(os.path.join(TMP, "signing-2026-10.key"), "w") as f:
    f.write(signer.paserk_secret() + "\n")
world_identity = X.SigningKey.generate("identity")
WORLD_ID = world_identity.server_id
RING = X.KeyRing()
RING.add(X.load_keyset(X.make_keyset(provider_id, "OpenLobby", [signer]), provider_id.server_id))
REPLAY = X.MemoryReplayGuard()
CHARS = {"7": [{"id": 4097, "name": "Ayame", "rename": False, "zone": 245, "nation": 1, "race": 2,
                "face": 4, "size": 1, "gm": False, "job": {"main": 3, "main_level": 37, "sub": 5},
                "look": {"head": 0x1010, "body": 0x2020, "hands": 0x3030, "legs": 0x4040, "feet": 0x5050,
                         "main": 0x6060, "sub": 0x7070}}]}
SEEN = []
DOWN = {"on": False}


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _claims(self, typ, token):
        return X.verify(token, RING, WORLD_ID, typ, replay=REPLAY)

    def _any(self):
        if DOWN["on"]:
            return self._reply(503, {"ok": False, "error": "unavailable"})
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path == "/xi/v1/keyset":
            return self._reply(200, KEYSET, "text/plain")
        try:
            if self.path == "/xi/v1/world-entry":
                claims = self._claims(X.WORLD_ENTRY_TYPE, body.decode())
                SEEN.append(("entry", claims))
                return self._reply(200, {"ok": True, "map": {"ip": "203.0.113.9", "port": 54232}})
            auth = self.headers.get("Authorization") or ""
            claims = self._claims(X.ACCOUNT_TYPE, auth[len("XiToken "):])
        except X.XiTokenError as e:
            SEEN.append(("rejected", e.name))
            return self._reply(400, {"ok": False, "error": e.name})
        sub = claims["sub"]
        mine = CHARS.setdefault(sub, [])
        if self.command == "GET" and self.path == "/xi/v1/characters":
            SEEN.append(("list", sub))
            return self._reply(200, {"ok": True, "characters": mine})
        if self.command == "POST" and self.path == "/xi/v1/characters":
            req = json.loads(body)
            SEEN.append(("create", sub, req))
            if req["name"] == "Taken":
                return self._reply(409, {"ok": False, "error": "name_taken"})
            mine.append({"id": 4100, "name": req["name"], "zone": 230, "nation": req["nation"],
                         "race": req["race"], "face": req["face"], "size": req["size"],
                         "job": {"main": req["job"], "main_level": 1, "sub": 0}, "look": {}})
            return self._reply(200, {"ok": True, "id": 4100})
        if self.command == "DELETE" and self.path.startswith("/xi/v1/characters/"):
            cid = int(self.path.rsplit("/", 1)[1])
            SEEN.append(("delete", sub, cid))
            CHARS[sub] = [c for c in mine if c["id"] != cid]
            return self._reply(200, {"ok": True})
        return self._reply(404, {"ok": False, "error": "unknown_character"})

    do_GET = do_POST = do_DELETE = _any


srv = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
threading.Thread(target=srv.serve_forever, daemon=True).start()
GW = "http://127.0.0.1:%d" % srv.server_port
KEYSET = X.make_keyset(world_identity, "Phoenix", [], world={"gateway": GW, "expansions": 0x0FFF,
                                                             "search": "203.0.113.9:54002"})
worlds_file = os.path.join(TMP, "worlds.json")
with open(worlds_file, "w") as f:
    json.dump([{"id": WORLD_ID, "keyset": GW + "/xi/v1/keyset"}], f)

# --- lobby packets ------------------------------------------------------------------------------


def lsb_world_list():
    p = bytearray(0x34)
    p[8] = 0x23
    struct.pack_into("<I", p, 0x1C, 1)
    struct.pack_into("<I", p, 0x20, 0x20)
    p[0x24:0x24 + 9] = b"OpenLobby"
    return F.sign(p)


def lsb_char_list(local=((12, "Kanon"),), slots=4):
    """LSB's 0x20 the way data_session builds it: characters, then empty slots (name ' ')."""
    p = bytearray(0x20 + slots * 0x8C)
    p[8] = 0x20
    struct.pack_into("<I", p, 0x1C, slots)
    for i in range(slots):
        off = 0x20 + i * 0x8C
        struct.pack_into("<H", p, off + 8, 1)
        if i < len(local):
            charid, name = local[i]
            struct.pack_into("<IH", p, off, charid, charid & 0xFFFF)
            p[off + 0x0C:off + 0x0C + len(name)] = name.encode()
            p[off + 0x1C:off + 0x25] = b"OpenLobby"
            struct.pack_into("<H", p, off + 0x2C, 1)          # race, so the profile is read
        else:
            p[off + 0x0C] = 0x20
    return F.sign(p)


def c2s(cmd, content_id=0, size=0x24):
    p = bytearray(size)
    struct.pack_into("<I", p, 0, size)
    p[4:8] = b"IXFF"
    p[8] = cmd
    struct.pack_into("<I", p, 0x1C, content_id)
    return bytes(p)


def md5_ok(pkt):
    p = bytearray(pkt)
    got = bytes(p[0x0C:0x1C])
    p[0x0C:0x1C] = bytes(16)
    return hashlib.md5(bytes(p)).digest() == got and struct.unpack_from("<I", pkt, 0)[0] == len(pkt)


def records(pkt):
    n = struct.unpack_from("<I", pkt, 0x1C)[0]
    out = []
    for i in range(n):
        off = 0x20 + i * 0x8C
        rec = pkt[off:off + 0x8C]
        out.append({"ffxi_id": struct.unpack_from("<I", rec, 0)[0],
                    "main": struct.unpack_from("<H", rec, 4)[0], "worldid": struct.unpack_from("<H", rec, 6)[0],
                    "status": struct.unpack_from("<H", rec, 8)[0], "tbl": rec[0x0B],
                    "name": rec[0x0C:0x1C].split(b"\0")[0].decode(), "world": rec[0x1C:0x2C].split(b"\0")[0].decode(),
                    "race": struct.unpack_from("<H", rec, 0x2C)[0], "job": rec[0x2E], "sjob": rec[0x2F],
                    "face": struct.unpack_from("<H", rec, 0x30)[0], "nation": rec[0x32], "size": rec[0x35],
                    "grap": struct.unpack_from("<8H", rec, 0x38), "zone": rec[0x48] | ((rec[0x4F] & 1) << 8),
                    "level": rec[0x49]})
    return out


# The client's own create packets (prod capture, 2026-10-04), identifiers as captured.
CAP_22 = bytes.fromhex(
    "600000004958464622000000798287139fcbc73c8ca77fbd4607cff7e7c4c901"
    "4661727279617761790000000000000072bdf6261a0b4ee38ec41c57ab6aac25"
    "4f70656e4c6f6262790000000000000000ffffffffffffffffffffffffffff00")
CAP_21 = bytes.fromhex(
    "9000000049584646210000001da9e618ba4f00097a75122fec76fd36e7c4c901"
    "397b5f27a30455527bc734fe77dd147e01000300020000000102000005010010"
    "0820083008400850006000700001000000000000000000000000000000000000"
    + "00" * 48)


def with_content_id(pkt, cid):
    """The capture names the real player's Content ID; the test member's free slot is another."""
    p = bytearray(pkt)
    struct.pack_into("<I", p, 0x1C, cid)
    return bytes(p)


CAP_22 = with_content_id(CAP_22, 30000702)
CAP_21 = with_content_id(CAP_21, 30000702)

# ================================================================================================
print("1. Federation off: the bridge's output is unchanged")
B.FED = None
wl, cl = lsb_world_list(), lsb_char_list()
check(B.rewrite_s2c(wl, "T", 7, "198.51.100.5:4000") == wl, "0x23 passes through untouched")
B._idmap.clear()
plain = B.rewrite_s2c(cl, "T", 7, "198.51.100.5:4000")
check([r["name"] for r in records(plain)] == ["Kanon", " ", " "], "0x20 as before (local + free slots, rest dropped)")

# ================================================================================================
print("2. Config")
env = {"FFXI_FED_WORLDS": worlds_file, "FFXI_FED_KEYS": TMP,
       "FFXI_FED_CLIENT_IP": "198.18.0.0/15=198.51.100.200"}
FED = F.Federation.from_env(env)
check(FED is not None and FED.issuer.issuer_id == provider_id.server_id, "provider id from FFXI_FED_KEYS")
check([(w.no, w.name) for w in FED.usable()] == [(0x21, "Phoenix")], "world 0x21 named from its key set")
check(FED.worlds[0].search == ("203.0.113.9", 54002), "search server from the key set")
check(FED.client_ip("198.18.3.71") == "198.51.100.200" and FED.client_ip("203.0.113.50") == "203.0.113.50",
      "clients in the mapped range are sent as our public address, others as themselves")
check(F.Federation.from_env({}) is None, "off without FFXI_FED_WORLDS")
bad = os.path.join(TMP, "bad.json")
with open(bad, "w") as f:
    json.dump([{"id": X.SigningKey.generate("i").server_id, "keyset": GW + "/xi/v1/keyset"}], f)
check(F.Federation.from_env({"FFXI_FED_WORLDS": bad, "FFXI_FED_KEYS": TMP}).usable() == [],
      "a world whose key set is not the id we trust is never listed")
B.FED = FED

# ================================================================================================
print("3. World list and character list")
wl2 = B.rewrite_s2c(wl, "T", 7, "198.51.100.5:4000")
check(md5_ok(wl2) and struct.unpack_from("<I", wl2, 0x1C)[0] == 2, "0x23: two worlds, re-signed")
check(struct.unpack_from("<I", wl2, 0x34)[0] == 0x21 and wl2[0x38:0x3F] == b"Phoenix", "remote world entry")
check(B.rewrite_s2c(wl, "T", None, "198.51.100.5:4000") == wl, "no member, no remote worlds")
B._idmap.clear()
SEEN.clear()
out = B.rewrite_s2c(lsb_char_list(), "T", 7, "198.51.100.5:4000")
recs = records(out)
check(md5_ok(out), "0x20 re-signed")
check([r["name"] for r in recs] == ["Kanon", "Ayame", " "], "local character, remote character, one free slot")
rem = recs[1]
check((rem["ffxi_id"], rem["main"], rem["worldid"], rem["status"], rem["tbl"]) == (30000701, 4097, 0x21, 1, 0),
      "remote record: Content ID, charid, world 0x21, available")
check((rem["world"], rem["race"], rem["job"], rem["sjob"], rem["face"], rem["nation"], rem["size"], rem["zone"],
       rem["level"]) == ("Phoenix", 2, 3, 5, 4, 1, 1, 245, 37), "remote record: the gateway's fields")
check(rem["grap"] == (4, 0x1010, 0x2020, 0x3030, 0x4040, 0x5050, 0x6060, 0x7070), "remote record: look, face first")
key = F.remote_key(0x21, 4097)
check(B._idmap.get(str(key)) == 30000701 and B._worldfields.get(str(key)) == B.pack_world_field(4097, 0x21, 0),
      "paired under its remote key with the world field the client will look for")
check(B._charfields.get(str(key), {}).get("world") == "Phoenix", "POL profile tail recorded")
check(F.split_remote_key(key) == (0x21, 4097) and F.split_remote_key(12) is None, "remote keys never look local")
check(("list", "7") in SEEN, "the list was asked for as member 7")

# ================================================================================================
print("4. Select a remote character")
B._client_build["198.51.100.5"] = "30260805_0"
SEEN.clear()
res = B.fed_c2s(c2s(0x07, 30000701), "T", 7, "198.51.100.5:4000")
check(res is not None and res[1] is True, "0x07 answered here, then the connection closes")
pkt = res[0][0]
check(md5_ok(pkt) and pkt[8] == 0x0B and len(pkt) == 0x48, "a signed 0x0B")
check(struct.unpack_from("<II", pkt, 0x1C) == (4097, 4097) and pkt[0x24:0x29] == b"Ayame"
      and struct.unpack_from("<I", pkt, 0x34)[0] == 0x21, "0x0B: the world's charid (UniqueNo), name, world")
check((socket.inet_ntoa(pkt[0x38:0x3C]), struct.unpack_from("<I", pkt, 0x3C)[0],
       socket.inet_ntoa(pkt[0x40:0x44]), struct.unpack_from("<I", pkt, 0x44)[0])
      == ("203.0.113.9", 54232, "203.0.113.9", 54002), "0x0B: the map the gateway named, the key set's search")
entry = [c for k, c in SEEN if k == "entry"]
check(len(entry) == 1, "the world verified a world-entry token")
if entry:
    c = entry[0]
    check((c["sub"], c["char"], c["client"]) == ("7", {"id": 4097, "name": "Ayame"},
                                                 {"ip": "198.51.100.5", "version": "30260805_0", "expansions": 0x0FFF}),
          "token: member, character, client address and build")
    check(X.b64u_decode(c["skey"]) == B.A2_SESSION_KEY, "token: the session key the client uses")
res = B.fed_c2s(c2s(0x07, 30000701), "T", 7, "198.18.3.71:4000")
check(res and [c for k, c in SEEN if k == "entry"][-1]["client"]["ip"] == "198.51.100.200",
      "a client in the mapped range is entered as our public address")
check(B.fed_c2s(c2s(0x07, 30000700), "T", 7, "198.51.100.5:4000") is None, "a local character goes to LSB")
check(B.fed_c2s(c2s(0x26, 0, 0x98), "T", 7, "k") is None and B.fed_c2s(c2s(0x1F), "T", 7, "k") is None,
      "other requests go to LSB")

# ================================================================================================
print("5. Create on the remote world (the client's own packets)")
name, world = F.name_check_fields(CAP_22)
check((name, world) == ("Farryaway", "OpenLobby"), "0x22 names the character and the chosen world")
check(B.fed_c2s(CAP_22, "T", 7, "c1") is None, "choosing our own world goes to LSB")
check(B.fed_c2s(CAP_21, "T", 7, "c1") is None, "and so does its commit")
remote22 = bytearray(CAP_22)
remote22[0x40:0x50] = b"Phoenix".ljust(16, b"\0")
res = B.fed_c2s(bytes(remote22), "T", 7, "c1")
check(res is not None and res[0][0][8] == 0x03 and res[1] is False, "0x22 on Phoenix: OK here")
SEEN.clear()
res = B.fed_c2s(CAP_21, "T", 7, "c1")
check(res is not None and res[0][0][8] == 0x03, "0x21 commits on Phoenix: OK")
made = [s for s in SEEN if s[0] == "create"]
check(made and made[0][2] == {"name": "Farryaway", "race": 1, "face": 5, "size": 2, "job": 3, "nation": 0},
      "the gateway got the fields LSB's createCharacter would have read")
nk = F.remote_key(0x21, 4100)
check(B._idmap.get(str(nk)) == 30000702, "the new character is paired with the Content ID the client named")
check(B._worldfields.get(str(nk)) == B.pack_world_field(4100, 0x21, 0), "world field recorded before POL's 1:3 refetch")
taken = bytearray(remote22)
taken[0x20:0x30] = b"Taken".ljust(16, b"\0")
B.fed_c2s(bytes(taken), "T", 7, "c2")
res = B.fed_c2s(CAP_21, "T", 7, "c2")
check(res and res[0][0][8] == 0x04 and struct.unpack_from("<H", res[0][0], 0x20)[0] == 313,
      "a taken name comes back as error 313")
check(B.fed_c2s(bytes(remote22), "T", None, "c3") is None, "no POL member: nothing remote, LSB decides")
check(B.fed_c2s(bytes(remote22), "T", 7, "c3", ambiguous=True)[0][0][8] == 0x04, "an ambiguous launch: refused")

# ================================================================================================
print("6. Delete a remote character")
SEEN.clear()
res = B.fed_c2s(c2s(0x14, 30000702), "T", 7, "198.51.100.5:4000")
check(res and res[0][0][8] == 0x03 and ("delete", "7", 4100) in SEEN, "0x14: deleted on the world")
check(str(nk) not in B._idmap, "its Content ID is released")

# ================================================================================================
print("7. A remote world that is down never costs the member their own world")
DOWN["on"] = True
FED.forget(7)
B._idmap.clear()
out = B.rewrite_s2c(lsb_char_list(), "T", 7, "198.51.100.5:4000")
check([r["name"] for r in records(out)][0] == "Kanon" and md5_ok(out), "0x20 still lists the local character")
B._idmap[str(key)] = 30000701
B._charnames[str(key)] = "Ayame"
res = B.fed_c2s(c2s(0x07, 30000701), "T", 7, "198.51.100.5:4000")
check(res and res[0][0][8] == 0x04 and res[1] is False, "select while down: an error, the lobby stays open")
DOWN["on"] = False

# ================================================================================================
print("8. Through the relay: the request never reaches LSB")
B._idmap.clear()
B._idmap[str(key)] = 30000701
B._charnames[str(key)] = "Ayame"
client_end, bridge_client = socket.socketpair()
bridge_lsb, lsb_end = socket.socketpair()
t = threading.Thread(target=B.pump, args=(bridge_client, bridge_lsb, True, bytes(16), "VIEW c->s", 7,
                                          "198.51.100.5:4000"), daemon=True)
t.start()
client_end.sendall(c2s(0x1F) + c2s(0x07, 30000701))
client_end.settimeout(5)
try:
    got = client_end.recv(4096)
except OSError:
    got = b""
t.join(5)
lsb_end.settimeout(1)
to_lsb = b""
try:
    while True:
        chunk = lsb_end.recv(4096)
        if not chunk:
            break
        to_lsb += chunk
except OSError:
    pass
check(len(got) > 8 and got[8] == 0x0B and md5_ok(got), "the client got the 0x0B")
check(len(to_lsb) == 0x24 and to_lsb[8] == 0x1F, "LSB got the 0x1F and nothing of the select")
check(not t.is_alive(), "the relay closed after the handoff")

# ================================================================================================
print("9. FFXI_FED_MEMBERS: only listed members see the remote worlds")
env2 = dict(env, FFXI_FED_MEMBERS="7, 9")
FED2 = F.Federation.from_env(env2)
check(FED2.allows(7) and FED2.allows("9") and not FED2.allows(8) and not FED2.allows(None), "allows 7 and 9 only")
check(FED.allows(8) and not FED.allows(None), "unset: every member (but never 'nobody')")
B.FED = FED2
check(B.rewrite_s2c(wl, "T", 8, "198.51.100.5:4000") == wl, "member 8: the world list is untouched")
B._idmap.clear()
POOL[8] = [30000800, 30000801]
CHARS["8"] = [dict(CHARS["7"][0], id=5000, name="Other")]
out8 = B.rewrite_s2c(lsb_char_list(), "T", 8, "198.51.100.5:4000")
check("Other" not in [r["name"] for r in records(out8)], "member 8: no remote characters")
check(B.fed_c2s(bytes(remote22), "T", 8, "c9") is None, "member 8: a create goes to LSB")
B.FED = FED

# ================================================================================================
print("10. Our own world's name is always ours")
B.rewrite_s2c(wl, "T", 7, "198.51.100.5:4000")
check(B._world_name == "OpenLobby", "the local world's name is learned from LSB's 0x23")
clash = os.path.join(TMP, "clash.json")
with open(clash, "w") as f:
    json.dump([{"id": WORLD_ID, "keyset": GW + "/xi/v1/keyset", "name": "OpenLobby"}], f)
B.FED = F.Federation.from_env(dict(env, FFXI_FED_WORLDS=clash))
check(B.fed_c2s(CAP_22, "T", 7, "c10") is None,
      "a remote world misnamed like ours never takes a create meant for ours")
os.remove(clash)
check(F.Federation.from_env(dict(env, FFXI_FED_WORLDS=clash)) is None,
      "a missing world list is 'no worlds yet', not an error")
B.FED = FED

# ================================================================================================
print("11. A world that hangs costs the lobby one timeout, then nothing while it is down")
import time                                                       # noqa: E402
hang = socket.socket()
hang.bind(("127.0.0.1", 0))
hang.listen(8)                                  # accepts, never answers
held = []
threading.Thread(target=lambda: [held.append(hang.accept()) for _ in range(8)], daemon=True).start()
slow = F.RemoteWorld(0x23, X.SigningKey.generate("i").server_id, "unused")
slow.keyset = FED.worlds[0].keyset
slow.gateway = X.WorldGateway("http://127.0.0.1:%d" % hang.getsockname()[1], timeout=getattr(F, "GATEWAY_TIMEOUT", 10))
slow.loaded_at = time.time()
t0 = time.time()
first = FED.characters(70, slow)
t1 = time.time()
second = FED.characters(71, slow)
t2 = time.time()
check(first == [] and t1 - t0 < getattr(F, "GATEWAY_TIMEOUT", 10) + 1.5, "first call: no characters after about one timeout (%.1fs)" % (t1 - t0))
check(getattr(slow, "is_down", lambda: False)(), "the world is marked down")
check(second == [] and t2 - t1 < 0.2, "the next member is not made to wait (%.2fs)" % (t2 - t1))
check(slow.refresh() is True, "a down world keeps its last key set (still listed, characters skipped)")
DOWN["on"] = True
FED.forget(7)
w0 = FED.worlds[0]
FED.characters(7, w0)
check(not getattr(w0, "is_down", lambda: False)(), "a gateway that answers with an error is not 'down'")
DOWN["on"] = False

print()
print("RESULT: %s" % ("%d FAILURE(S)" % len(FAILED) if FAILED else "all passed"))
for f in FAILED:
    print("  - " + f)
sys.exit(1 if FAILED else 0)
