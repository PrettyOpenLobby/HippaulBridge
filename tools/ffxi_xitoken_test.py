#!/usr/bin/env python3
"""lsb/xitoken.py against the PASETO vectors and against tokens upstream's C++ signed.

A token library that only agrees with itself proves nothing: sign and verify can share one
mistake and still round-trip. So the anchors here come from elsewhere:

  1. the PASETO v4.public test vectors (paseto-standard/test-vectors v4.json), which the
     xitoken spec says every implementation must pass first;
  2. a key set signed by LSB's xi_world (upstream ext/xitoken C++), served by our world on
     2026-10-04, with the server id xi_world itself logged for that key
     ("Federation gateway: world xi1.oBcsuiE82Nu2Mg1YrlBH3g");
  3. both Ed25519 backends (pycryptodome, cryptography) producing the same bytes.

The rest holds the issuer and the verifier to the spec's claim shapes and its verification
order. No network, no database.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import xitoken as X                                                # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def error_of(fn):
    try:
        fn()
    except X.XiTokenError as e:
        return e.name
    except ValueError as e:
        return "ValueError: %s" % e
    return None


# --------------------------------------------------------------------------------------------
print("1. PASETO v4.public vectors (backend: %s)" % X.BACKEND)
SEED = bytes.fromhex("b4cbfb43df4ce210727d953e4a713307fa19bb7d9f85041438d9e11b942a3774")
PUB = bytes.fromhex("1eb9dbbbbc047c03fd70604e0071f0987e16b28b757225c11f00415d0e20b1a2")
MSG = b'{"data":"this is a signed message","exp":"2022-01-01T00:00:00+00:00"}'
FOOT = b'{"kid":"zVhMiPBP9fRf2snEcT7gFTioeA9COcNy9DfgL1W60haN"}'
VECTORS = [
    ("4-S-1", b"", b"",
     "v4.public.eyJkYXRhIjoidGhpcyBpcyBhIHNpZ25lZCBtZXNzYWdlIiwiZXhwIjoiMjAyMi0wMS0wMVQwMDowMDowMCswMDowMCJ9"
     "bg_XBBzds8lTZShVlwwKSgeKpLT3yukTw6JUz3W4h_ExsQV-P0V54zemZDcAxFaSeef1QlXEFtkqxT1ciiQEDA"),
    ("4-S-2", FOOT, b"",
     "v4.public.eyJkYXRhIjoidGhpcyBpcyBhIHNpZ25lZCBtZXNzYWdlIiwiZXhwIjoiMjAyMi0wMS0wMVQwMDowMDowMCswMDowMCJ9"
     "v3Jt8mx_TdM2ceTGoqwrh4yDFn0XsHvvV_D0DtwQxVrJEBMl0F2caAdgnpKlt4p7xBnx1HcO-SPo8FPp214HDw."
     "eyJraWQiOiJ6VmhNaVBCUDlmUmYyc25FY1Q3Z0ZUaW9lQTlDT2NOeTlEZmdMMVc2MGhhTiJ9"),
    ("4-S-3", FOOT, b'{"test-vector":"4-S-3"}',
     "v4.public.eyJkYXRhIjoidGhpcyBpcyBhIHNpZ25lZCBtZXNzYWdlIiwiZXhwIjoiMjAyMi0wMS0wMVQwMDowMDowMCswMDowMCJ9"
     "NPWciuD3d0o5eXJXG5pJy-DiVEoyPYWs1YSTwWHNJq6DZD3je5gf-0M4JR9ipdUSJbIovzmBECeaWmaqcaP0DQ."
     "eyJraWQiOiJ6VmhNaVBCUDlmUmYyc25FY1Q3Z0ZUaW9lQTlDT2NOeTlEZmdMMVc2MGhhTiJ9"),
]
check(X._public_of(SEED) == PUB, "public key from the vectors' seed")
for name, footer, implicit, token in VECTORS:
    check(X.sign_raw(SEED, MSG, footer, implicit) == token, name + " signs to the vector's exact token")
    try:
        got = X.verify_raw(PUB, token, implicit)
        check(got == (MSG, footer), name + " verifies and returns its payload and footer")
    except X.XiTokenError as e:
        check(False, name + " verifies (%s)" % e)
# 4-F-2: a v4.public token that must never decode under this key.
F2 = ("v4.public.eyJpbnZhbGlkIjoidGhpcyBzaG91bGQgbmV2ZXIgZGVjb2RlIn22Sp4gjCaUw0c7EH84ZSm_jN_Qr41MrgLNu5LIBCzUr1pn3Z"
      "-Wukg9h3ceplWigpoHaTLcwxj0NsI1vjTh67YB.eyJraWQiOiJ6VmhNaVBCUDlmUmYyc25FY1Q3Z0ZUaW9lQTlDT2NOeTlEZmdMMVc2MGhhTiJ9")
check(error_of(lambda: X.verify_raw(PUB, F2, b'{"test-vector":"4-F-2"}')) == "bad_signature", "4-F-2 is rejected")
check(error_of(lambda: X.verify_raw(PUB, VECTORS[2][3], b"")) == "bad_signature",
      "4-S-3 without its implicit assertion is rejected")

# --------------------------------------------------------------------------------------------
print("2. Ed25519 backends agree")
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    other = Ed25519PrivateKey.from_private_bytes(SEED).sign(b"xitoken backend parity")
    check(X._sign(SEED, b"xitoken backend parity") == other, "same signature from both backends")
    check(X._verify(PUB, b"xitoken backend parity", other), "this backend verifies the other's signature")
except ImportError:
    print("  SKIP cryptography not installed; only %s checked" % X.BACKEND)

# --------------------------------------------------------------------------------------------
print("3. A key set signed by upstream's C++ (our world, 2026-10-04)")
WORLD_ID = "xi1.oBcsuiE82Nu2Mg1YrlBH3g"            # as xi_world logged it for this key
WORLD_KEYSET = (
    "v4.public.eyJpYXQiOiIyMDI2LTEwLTA0VDA1OjIyOjQxWiIsImlzcyI6InhpMS5vQmNzdWlFODJOdTJNZzFZcmxCSDNnIiwia2V5cyI6W10s"
    "Im5hbWUiOiJPcGVuTG9iYnkiLCJ0eXAiOiJ4aS5rZXlzZXQvMSIsIndvcmxkIjp7ImV4cGFuc2lvbnMiOjQwOTUsImdhdGV3YXkiOiJodHRw"
    "czovL29wZW5sb2JieS5meWkiLCJzZWFyY2giOiI4Mi4yMjEuMTAwLjEyNTo1NDAwMiJ9fftRubZZnnJe8t5LhSF7zn7N0Yl9hCRdkd1ceYHqakS"
    "-zn18R-9EQebvSMfJCpxcIEjHLUYyqwrj0O7YYYagQAA.eyJpZGsiOiJrNC5wdWJsaWMuWnItTE1FaXFoZXlpYkJYaTl2VTFadzBFZlk4Y1J6"
    "a1B4emNTQ0FRVE5IWSJ9")
try:
    ks = X.load_keyset(WORLD_KEYSET, WORLD_ID)
    check(ks.server_id == WORLD_ID, "our server id derivation matches the one xi_world logged")
    check(ks.name == "OpenLobby" and ks.keys == {}, "name and (empty) signing keys read")
    check(ks.world == {"expansions": 4095, "gateway": "https://openlobby.fyi", "search": "82.221.100.125:54002"},
          "world description read")
    check(ks.issued == X.parse_time("2026-10-04T05:22:41Z"), "iat read")
except X.XiTokenError as e:
    check(False, "C++-signed key set loads (%s)" % e)
check(error_of(lambda: X.load_keyset(WORLD_KEYSET, "xi1.AAAAAAAAAAAAAAAAAAAAAA")) == "wrong_issuer",
      "a key set is refused when it is not the id we chose to trust")
body = WORLD_KEYSET[len(X.HEADER):].split(".")
flipped = X.HEADER + body[0][:30] + ("A" if body[0][30] != "A" else "B") + body[0][31:] + "." + body[1]
check(error_of(lambda: X.load_keyset(flipped, WORLD_ID)) in ("bad_signature", "malformed"),
      "one changed payload character breaks the C++ signature")

# --------------------------------------------------------------------------------------------
print("4. Keys and key sets we make")
ident = X.SigningKey.generate("identity")
again = X.SigningKey.from_paserk("identity", ident.paserk_secret() + "\n")
check(again.public == ident.public and again.server_id == ident.server_id, "k4.secret round trip (with a newline)")
check(X.is_server_id(ident.server_id) and len(ident.server_id) == 26, "server id is xi1. + 22 characters")
other_pub = X.SigningKey.generate("x").public
mismatched = X.SECRET_PREFIX + X.b64u(X.b64u_decode(ident.paserk_secret()[len(X.SECRET_PREFIX):])[:32] + other_pub)
check(error_of(lambda: X.SigningKey.from_paserk("identity", mismatched)) is not None,
      "a k4.secret whose public half is not the seed's is refused")
signer = X.SigningKey.generate("2026-10")
mine = X.make_keyset(ident, "OpenLobby", [signer], issued=1_800_000_000)
ks = X.load_keyset(mine, ident.server_id)
check(ks.keys == {"2026-10": signer.public} and ks.world is None, "our provider key set loads with its signing key")
check(error_of(lambda: X.make_keyset(ident, None, [signer, X.SigningKey("2026-10", os.urandom(32))])) is None
      and error_of(lambda: X.load_keyset(X.make_keyset(ident, None, [signer, X.SigningKey("2026-10", os.urandom(32))]),
                                         ident.server_id)) == "bad_claims",
      "a key set with a duplicate kid is refused")
expired = X.make_keyset(ident, None, [signer], issued=1_800_000_000, expires=1_800_000_100)
check(error_of(lambda: X.load_keyset(expired, ident.server_id, now=1_800_000_200)) == "expired",
      "an expired key set is refused")
ring = X.KeyRing()
ring.add(ks)
newer = X.load_keyset(X.make_keyset(ident, None, [], issued=1_800_000_500), ident.server_id)
ring.add(newer)
check(ring.key(ident.server_id, "2026-10") is None, "a newer key set replaces the older (key revoked)")
ring.add(ks)
check(ring.key(ident.server_id, "2026-10") is None, "an older key set does not come back")

# --------------------------------------------------------------------------------------------
print("5. Tokens: shape and the verification order")
provider = X.SigningKey.generate("provider-identity")
pkey = X.SigningKey.generate("2026-10")
ring = X.KeyRing()
ring.add(X.load_keyset(X.make_keyset(provider, "OpenLobby", [pkey]), provider.server_id))
issuer = X.Issuer(provider.server_id, pkey)
NOW = 1_800_000_000
SKEY = bytes(16) + bytes.fromhex("58e05dad")       # the bridge's A2_SESSION_KEY
entry = issuer.world_entry(WORLD_ID, "1003", 4097, "203.0.113.7", "30260805_0", 0x0FFF, SKEY, now=NOW)
payload, _, footer = X.split_token(entry)
claims, footer = json.loads(payload), json.loads(footer)
check(footer == {"iss": provider.server_id, "kid": "2026-10"}, "footer is {iss, kid}")
check(sorted(claims) == sorted(["iss", "aud", "sub", "typ", "jti", "iat", "exp", "char", "client", "skey"]),
      "world-entry carries exactly the spec's claims")
check(claims["char"] == {"id": 4097} and claims["client"] == {"ip": "203.0.113.7", "version": "30260805_0",
                                                             "expansions": 4095},
      "char and client claims as upstream's WorldEntry::toClaims writes them")
check(X.b64u_decode(claims["skey"]) == SKEY and len(claims["jti"]) == 22, "skey round-trips; jti is 16 random bytes")
check(claims["exp"] == "2027-01-15T08:01:00Z" and claims["iat"] == "2027-01-15T08:00:00Z", "60 s lifetime, Z times")
got = X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW + 5, replay=X.MemoryReplayGuard())
check(got["sub"] == "1003", "the world-entry verifies")
acct = issuer.account(WORLD_ID, "1003", now=NOW)
check(json.loads(X.split_token(acct)[0])["exp"] == "2027-01-15T08:02:00Z", "account token lives 120 s")

guard = X.MemoryReplayGuard()
cases = [
    ("unknown_key", lambda: X.verify(entry, X.KeyRing(), WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW)),
    ("bad_signature", lambda: X.verify(X.HEADER + X.b64u(X.split_token(entry)[0] + bytes(64)) + "." +
                                       entry.split(".")[3], ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW)),
    # Signed with this provider's key, footer naming this provider, payload claiming another:
    # one provider's key minting in another's name (spec step 5).
    ("wrong_issuer", lambda: X.verify(pkey.sign(
        X._json(dict(json.loads(X.split_token(acct)[0]), iss="xi1.AAAAAAAAAAAAAAAAAAAAAA")),
        X._json({"iss": provider.server_id, "kid": "2026-10"})), ring, WORLD_ID, X.ACCOUNT_TYPE, now=NOW)),
    ("wrong_audience", lambda: X.verify(entry, ring, "xi1.AAAAAAAAAAAAAAAAAAAAAA", X.WORLD_ENTRY_TYPE, now=NOW)),
    ("wrong_type", lambda: X.verify(entry, ring, WORLD_ID, X.ACCOUNT_TYPE, now=NOW)),
    ("not_yet_valid", lambda: X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW - 31)),
    ("expired", lambda: X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW + 90)),
    ("lifetime_too_long", lambda: X.verify(issuer.issue(X.ACCOUNT_TYPE, WORLD_ID, "1", 301, now=NOW), ring,
                                           WORLD_ID, X.ACCOUNT_TYPE, now=NOW)),
    ("malformed", lambda: X.verify("v4.public.@@", ring, WORLD_ID, X.ACCOUNT_TYPE, now=NOW)),
]
for want, fn in cases:
    check(error_of(fn) == want, "rejected as %s" % want)
check(error_of(lambda: X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW + 1, replay=guard)) is None
      and error_of(lambda: X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW + 2, replay=guard)) == "replayed",
      "a token is accepted once, then rejected as replayed")
check(error_of(lambda: X.verify(entry, ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW + 29)) is None,
      "within the 30 s skew of iat it is accepted")
for label, fn in [
        ("an IPv6 or partial client address", lambda: issuer.world_entry(WORLD_ID, "1", 1, "10.1", "v", 0, SKEY)),
        ("a client version over 16 characters", lambda: issuer.world_entry(WORLD_ID, "1", 1, "198.51.100.7", "x" * 17, 0, SKEY)),
        ("a session key that is not 20 bytes", lambda: issuer.world_entry(WORLD_ID, "1", 1, "198.51.100.7", "v", 0, bytes(16))),
        ("a claim the issuer sets", lambda: issuer.issue(X.ACCOUNT_TYPE, WORLD_ID, "1", 60, {"aud": "x"}))]:
    check((error_of(fn) or "").startswith("ValueError"), "the issuer refuses " + label)
check(error_of(lambda: X.verify(entry.replace(entry.split(".")[2], X.b64u(
    b'{"a":1,"a":2}' + X.split_token(entry)[1])), ring, WORLD_ID, X.WORLD_ENTRY_TYPE, now=NOW)) == "bad_signature",
      "a duplicate-key payload never gets past the signature")
check(X.parse_time("2026-10-04T01:22:41.999-04:00") == X.parse_time("2026-10-04T05:22:41Z"),
      "RFC 3339 offsets and fractions are accepted and truncated")

# --------------------------------------------------------------------------------------------
print("6. Gateway client, against a fake gateway on loopback")
import datetime                                                    # noqa: E402
import socket                                                      # noqa: E402
import ssl                                                         # noqa: E402
import tempfile                                                    # noqa: E402
import threading                                                   # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

SEEN = []


class FakeGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, status, obj, ctype="application/json"):
        body = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        SEEN.append((self.command, self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), body))
        if self.path == "/w/xi/v1/keyset":
            return self._reply(200, WORLD_KEYSET, "text/plain")
        if self.path == "/w/xi/v1/characters" and self.command == "GET":
            return self._reply(200, {"ok": True, "characters": [{"id": 4097, "name": "Ayame"}]})
        if self.path == "/w/xi/v1/characters" and self.command == "POST":
            return self._reply(200, {"ok": True, "id": 4098})
        if self.path == "/w/xi/v1/world-entry":
            if body.startswith(b"v4.public.taken"):
                return self._reply(409, {"ok": False, "error": "already_logged_in"})
            return self._reply(200, {"ok": True, "map": {"ip": "203.0.113.7", "port": 54232}})
        return self._reply(200, {"ok": True})

    do_GET = do_POST = do_DELETE = _any


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_port

# The name resolves to an unroutable IPv6 address FIRST, the way Cloudflare names do; prod has
# no IPv6 route. The client must try IPv4 first instead of waiting out the IPv6 timeout.
real_gai, attempts = socket.getaddrinfo, []
real_socket = socket.socket


class LoggingSocket(real_socket):
    def connect(self, addr):
        attempts.append(addr[0])
        return super().connect(addr)


socket.getaddrinfo = lambda host, p, *a, **k: (
    [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("100::1", p, 0, 0)),
     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))] if host == "gw.test" else real_gai(host, p, *a, **k))
socket.socket = LoggingSocket
try:
    gw = X.WorldGateway("http://gw.test:%d/w" % port)
    t0 = time.time()
    check(gw.keyset() == WORLD_KEYSET and attempts[:1] == ["127.0.0.1"] and time.time() - t0 < 2,
          "IPv4 is tried before an IPv6 address listed first")
finally:
    socket.getaddrinfo, socket.socket = real_gai, real_socket

gw = X.WorldGateway("http://127.0.0.1:%d/w" % port)
check(gw.characters("v4.public.acct") == [{"id": 4097, "name": "Ayame"}]
      and SEEN[-1][:3] == ("GET", "/w/xi/v1/characters", "XiToken v4.public.acct"),
      "characters: GET with 'Authorization: XiToken <token>'")
check(gw.create_character("v4.public.acct", "Ayame", 2, 4, 1, 1, 0) == 4098
      and json.loads(SEEN[-1][4]) == {"name": "Ayame", "race": 2, "face": 4, "size": 1, "job": 1, "nation": 0},
      "create: POST the spec's JSON body, returns the id")
gw.delete_character("v4.public.acct", 4098)
check(SEEN[-1][:2] == ("DELETE", "/w/xi/v1/characters/4098"), "delete: DELETE /characters/<id>")
check(gw.world_entry("v4.public.entry") == ("203.0.113.7", 54232)
      and SEEN[-1][1:] == ("/w/xi/v1/world-entry", None, "text/plain", b"v4.public.entry"),
      "world-entry: the token as a text/plain body, the map address back")
try:
    gw.world_entry("v4.public.taken")
    check(False, "a refusal raises")
except X.GatewayError as e:
    check((e.status, e.error) == (409, "already_logged_in"), "a refusal raises with the gateway's status and error")

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "world.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp(prefix="xitoken-tls-")
    with open(os.path.join(tmp, "c.pem"), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(os.path.join(tmp, "k.pem"), "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    tls = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem"))
    tls.socket = sctx.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    url = "https://127.0.0.1:%d/w" % tls.server_port
    pin = X.certificate_pin(cert.public_bytes(serialization.Encoding.DER))
    check(X.WorldGateway(url, pin=pin).keyset() == WORLD_KEYSET, "https: a self-signed world is reached by its pin")
    try:
        X.WorldGateway(url, pin="sha256:" + "A" * 43).keyset()
        check(False, "https: a wrong pin is refused")
    except X.GatewayError as e:
        check(e.error == "pin_mismatch", "https: a wrong pin is refused")
    try:
        X.WorldGateway(url).keyset()
        check(False, "https: without a pin, a self-signed certificate is refused")
    except ssl.SSLError:
        check(True, "https: without a pin, a self-signed certificate is refused")
except ImportError:
    print("  SKIP cryptography not installed; https pinning not checked")

print()
if FAILED:
    print("RESULT: %d FAILURE(S)" % len(FAILED))
    for f in FAILED:
        print("  - " + f)
else:
    print("RESULT: all passed")
sys.exit(1 if FAILED else 0)
