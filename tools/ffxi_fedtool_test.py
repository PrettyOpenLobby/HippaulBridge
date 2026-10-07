#!/usr/bin/env python3
"""lsb/ffxi_fedtool.py, the admin panel's FFXI federation tool, through its own HTTP port.

Holds it to the tool-port contract the panel relies on (the token, the actor, never an open
wide bind) and to what it may write: the world list, and only <server id>.keyset files in a
trust directory that also holds our world's identity.key. No network beyond loopback.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_federation as F                                        # noqa: E402
import ffxi_fedtool as T                                           # noqa: E402
import xitoken as X                                                # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


TMP = tempfile.mkdtemp(prefix="ffxi-fedtool-test-")
KEYS, WORLDS, TRUST = (os.path.join(TMP, d) for d in ("keys", "worlds", "trust"))
for d in (KEYS, WORLDS, TRUST):
    os.makedirs(d)
ident, signer = X.SigningKey.generate("identity"), X.SigningKey.generate("2026-10")
for name, key in (("identity.key", ident), ("signing-2026-10.key", signer)):
    with open(os.path.join(KEYS, name), "w") as f:
        f.write(key.paserk_secret() + "\n")
# Our WORLD's identity key lives in the trust directory, as on prod (lsb/federation).
with open(os.path.join(TRUST, "identity.key"), "w") as f:
    f.write("k4.secret.world-identity-stays-put\n")

# A remote world serving its key set.
world = X.SigningKey.generate("identity")
WORLD_KS = {"text": ""}


class GW(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = WORLD_KS["text"].encode()
        self.send_response(200 if self.path == "/xi/v1/keyset" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


gw = ThreadingHTTPServer(("127.0.0.1", 0), GW)
threading.Thread(target=gw.serve_forever, daemon=True).start()
GW_URL = "http://127.0.0.1:%d" % gw.server_port
WORLD_KS["text"] = X.make_keyset(world, "Phoenix", [], world={"gateway": GW_URL, "expansions": 4095,
                                                            "search": "203.0.113.9:54002"})

ENV = {"FFXI_FED_WORLDS": os.path.join(WORLDS, "worlds.json"), "FFXI_FED_KEYS": KEYS,
       "FFXI_FED_TRUST_DIR": TRUST, "FFXI_FED_WORLD_KEYSET_URL": GW_URL,
       "FFXI_FEDTOOL_PORT": "0", "FFXI_FEDTOOL_TOKEN": "s3cret"}
LOGS = []
bridge = types.SimpleNamespace(FED=None, _world_name="OpenLobby", log=lambda tag, msg: LOGS.append(msg))
bridge.reload_federation = lambda: setattr(bridge, "FED", F.Federation.from_env(ENV)) or bridge.FED

srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(T.Tool(bridge, ENV), "s3cret"))
threading.Thread(target=srv.serve_forever, daemon=True).start()


def call(method, path, body=None, token="s3cret", actor="mod1"):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = {"X-Devtool-Actor": actor}
    if token:
        h["X-Devtool-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode("utf-8", "replace")


# ================================================================================================
print("1. The tool-port contract")
check(call("GET", "/", token=None)[0] == 403, "no token: refused")
check(call("GET", "/", token="wrong")[0] == 403, "a wrong token: refused")
st, page = call("GET", "/")
check(st == 200 and "FFXI federation" in page and "<script>" in page, "the page, with the token")
check(call("GET", "/fed.json?t=s3cret", token=None)[0] == 200, "?t= works when opened directly")
check(T.start(bridge, dict(ENV, FFXI_FEDTOOL_PORT="1", FFXI_FEDTOOL_BIND="0.0.0.0", FFXI_FEDTOOL_TOKEN="")) is None,
      "a wide bind without a token is refused, not served open")
check(T.start(bridge, {}) is None, "off without FFXI_FEDTOOL_PORT")

print("2. Our ids")
st, s = call("GET", "/fed.json")
check(s["provider"]["id"] == ident.server_id, "our provider id")
check(X.load_keyset(s["provider"]["keyset"], ident.server_id).keys == {"2026-10": signer.public},
      "our provider key set, ready to hand over, lists our signing key")
check(s["world"]["id"] == world.server_id and s["world"]["url"] == GW_URL + "/xi/v1/keyset",
      "our world's id, read and checked from its gateway URL")
check(s["federation"] == "off" and s["worlds"] == [] and s["providers"] == [], "nothing configured yet")

# ================================================================================================
print("3. Worlds our players can visit")
other = X.SigningKey.generate("identity").server_id
r = call("POST", "/worlds/add", {"id": other, "keyset": GW_URL + "/xi/v1/keyset"})[1]
check(not r["ok"] and "does not check out" in r["msg"], "a key set that is not that id's: refused")
check(not os.path.exists(ENV["FFXI_FED_WORLDS"]), "...and nothing written")
r = call("POST", "/worlds/add", {"id": "xi1.short", "keyset": GW_URL})[1]
check(not r["ok"], "a malformed id: refused")
r = call("POST", "/worlds/add", {"id": world.server_id, "keyset": GW_URL + "/xi/v1/keyset", "name": "openlobby"})[1]
check(not r["ok"] and "own world" in r["msg"], "our own world's name (any case): refused")
r = call("POST", "/worlds/add", {"id": world.server_id, "keyset": GW_URL + "/xi/v1/keyset"})[1]
check(r["ok"], "a checked world is added")
check(F.read_worlds(ENV["FFXI_FED_WORLDS"]) == [{"id": world.server_id, "keyset": GW_URL + "/xi/v1/keyset"}],
      "worlds.json holds it")
check(bridge.FED is not None and [w.name for w in bridge.FED.usable()] == ["Phoenix"],
      "the bridge reloaded: federation on, the world usable, no restart")
st, s = call("GET", "/fed.json")
check(s["worlds"][0]["ok"] and s["worlds"][0]["no"] == 0x21 and s["worlds"][0]["gateway"] == GW_URL,
      "the page shows it checked, as 0x21, with its gateway")
check(not call("POST", "/worlds/add", {"id": world.server_id, "keyset": GW_URL})[1]["ok"], "listed twice: refused")
check(call("POST", "/worlds/check", {"id": world.server_id})[1]["ok"], "check: the key set still checks out")
check(any("mod1" in line and "added world" in line for line in s["log"]), "the change is logged with the panel's actor")
r = call("POST", "/worlds/remove", {"id": world.server_id})[1]
check(r["ok"] and F.read_worlds(ENV["FFXI_FED_WORLDS"]) == [] and bridge.FED is None,
      "removed: the list is empty and federation is off again")

# ================================================================================================
print("4. Providers whose players can visit us")
prov, pkey = X.SigningKey.generate("identity"), X.SigningKey.generate("k1")
pks = X.make_keyset(prov, "Crystal", [pkey])
r = call("POST", "/providers/add", {"id": other, "keyset": pks})[1]
check(not r["ok"], "a key set under another id: refused")
r = call("POST", "/providers/add", {"id": prov.server_id, "keyset": X.make_keyset(prov, "Crystal", [])})[1]
check(not r["ok"] and "no signing keys" in r["msg"], "a key set with no signing keys: refused")
r = call("POST", "/providers/add", {"id": prov.server_id, "keyset": pks})[1]
path = os.path.join(TRUST, prov.server_id + ".keyset")
check(r["ok"] and os.path.isfile(path), "trusted: <id>.keyset written")
with open(path) as f:
    check(X.load_keyset(f.read().strip(), prov.server_id).name == "Crystal", "the file is the checked key set")
st, s = call("GET", "/fed.json")
check([(p["id"], p["name"], p["keys"], p["ok"]) for p in s["providers"]] == [(prov.server_id, "Crystal", ["k1"], True)],
      "the page lists it")
# A real target outside the trust directory: without the id check, "../decoy" + ".keyset"
# would delete it (and the same shape could reach any *.keyset the bridge can write).
DECOY = os.path.join(TMP, "decoy.keyset")
with open(DECOY, "w") as f:
    f.write("not ours to delete\n")
for bad in ("../decoy", "identity", "xi1.AAAAAAAAAAAAAAAAAAAAAA/../../decoy"):
    check(not call("POST", "/providers/remove", {"id": bad})[1]["ok"], "remove %r: refused" % bad)
check(os.path.isfile(DECOY), "a .keyset outside the trust directory survives an escape attempt")
check(call("POST", "/providers/remove", {"id": prov.server_id})[1]["ok"] and not os.path.exists(path),
      "untrusted: the file is gone")
with open(os.path.join(TRUST, "identity.key")) as f:
    check(f.read() == "k4.secret.world-identity-stays-put\n", "our world's identity.key was never touched")
check(sorted(os.listdir(TRUST)) == ["identity.key"], "nothing else left in the trust directory")

print()
print("RESULT: %s" % ("%d FAILURE(S)" % len(FAILED) if FAILED else "all passed"))
for f in FAILED:
    print("  - " + f)
sys.exit(1 if FAILED else 0)
