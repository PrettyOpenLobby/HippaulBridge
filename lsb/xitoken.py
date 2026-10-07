"""xitoken: the short-lived signed tokens between PlayOnline providers and FFXI worlds.

Python implementation of PhoenixPS2's `ext/xitoken/SPEC.md` (version 1), for the provider side:
the bridge signs in our POL members, so to send one of them to another operator's world it has
to mint the tokens that world's federation gateway accepts. The world side runs in LSB's
xi_world (upstream C++, `src/world/federation_gateway.cpp`); this module also verifies, so the
selftest can hold it to the same rules, and so key sets fetched from worlds can be checked.

Wire format, all from the spec:

  token      v4.public.<b64url(payload || sig64)>.<b64url(footer)>   (PASETO v4.public, Ed25519)
  signed     PAE("v4.public.", payload, footer, "")                   (no implicit assertion)
  footer     {"iss": <server id>, "kid": <key id>} on tokens, {"idk": <k4.public>} on key sets
  server id  "xi1." + b64url(SHA-256("xitoken server id\\0" || identity public key)[:16])
  keys       k4.secret.<b64url(seed32 || pub32)>, k4.public.<b64url(pub32)>   (PASERK)

The identity key only signs our key set; tokens are signed with signing keys the key set
lists, so the identity key can stay offline. A world trusts a provider by its server id alone.

Ed25519 comes from pycryptodome (in the core image) or, failing that, `cryptography`. Both
are deterministic, so either produces the same bytes; the selftest checks they agree.
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import socket
import ssl
import struct
import time
import urllib.parse
from datetime import datetime, timezone

HEADER = "v4.public."
SECRET_PREFIX = "k4.secret."
PUBLIC_PREFIX = "k4.public."
KEYSET_TYPE = "xi.keyset/1"
WORLD_ENTRY_TYPE = "xi.world-entry/1"
ACCOUNT_TYPE = "xi.account/1"
#: Recommended lifetimes (spec, "Token types").
WORLD_ENTRY_LIFETIME = 60
ACCOUNT_LIFETIME = 120
MAX_TOKEN = 8192
MAX_FOOTER = 512
_KID = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z")
_SERVER_ID = re.compile(r"xi1\.[A-Za-z0-9_-]{22}\Z")


class XiTokenError(Exception):
    """A rejected token or document. `name` is the spec's error name (malformed,
    unknown_key, bad_signature, bad_claims, wrong_issuer, wrong_audience, wrong_type,
    not_yet_valid, expired, lifetime_too_long, replayed)."""

    def __init__(self, name, detail=""):
        super().__init__("%s%s" % (name, ": " + detail if detail else ""))
        self.name = name


# --------------------------------------------------------------------------------------------
# Ed25519

try:
    from Crypto.PublicKey import ECC as _ECC
    from Crypto.Signature import eddsa as _eddsa

    def _public_of(seed):
        return _ECC.construct(curve="Ed25519", seed=seed).public_key().export_key(format="raw")

    def _sign(seed, msg):
        return _eddsa.new(_ECC.construct(curve="Ed25519", seed=seed), "rfc8032").sign(msg)

    def _verify(pub, msg, sig):
        try:
            _eddsa.new(_eddsa.import_public_key(pub), "rfc8032").verify(msg, sig)
            return True
        except ValueError:
            return False

    BACKEND = "pycryptodome"
except ImportError:                                           # pragma: no cover - host Python
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (Ed25519PrivateKey,
                                                                   Ed25519PublicKey)

    def _public_of(seed):
        return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
            _ser.Encoding.Raw, _ser.PublicFormat.Raw)

    def _sign(seed, msg):
        return Ed25519PrivateKey.from_private_bytes(seed).sign(msg)

    def _verify(pub, msg, sig):
        try:
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
            return True
        except (InvalidSignature, ValueError):
            return False

    BACKEND = "cryptography"


# --------------------------------------------------------------------------------------------
# Encodings

def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text):
    """Strict base64url without padding: anything else is malformed."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", text) or len(text) % 4 == 1:
        raise XiTokenError("malformed", "not base64url")
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u(raw) != text:                       # non-canonical trailing bits
        raise XiTokenError("malformed", "not canonical base64url")
    return raw


def pae(*pieces):
    """PASETO pre-authentication encoding."""
    out = struct.pack("<Q", len(pieces))
    for p in pieces:
        out += struct.pack("<Q", len(p)) + p
    return out


def _json(obj):
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key %r" % k)
        out[k] = v
    return out


def _depth(obj, limit=32):
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if d > limit:
            return False
        if isinstance(o, dict):
            stack.extend((v, d + 1) for v in o.values())
        elif isinstance(o, list):
            stack.extend((v, d + 1) for v in o)
    return True


def _load_object(raw, what):
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes)
    except (UnicodeDecodeError, ValueError) as e:
        raise XiTokenError("bad_claims" if what == "payload" else "malformed", "%s: %s" % (what, e))
    if not isinstance(obj, dict) or not _depth(obj):
        raise XiTokenError("bad_claims" if what == "payload" else "malformed", what + " is not an object")
    return obj


def format_time(t):
    """RFC 3339 as issuers write it: YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """Any RFC 3339 time, fractional seconds truncated. None if it is not one."""
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)[Tt](\d\d):(\d\d):(\d\d)(?:\.\d+)?([Zz]|[+-]\d\d:\d\d)", text)
    if not m:
        return None
    y, mo, d, h, mi, s, off = m.groups()
    try:
        t = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    if off not in ("Z", "z"):
        sign = 1 if off[0] == "+" else -1
        t -= sign * (int(off[1:3]) * 3600 + int(off[4:6]) * 60)
    return int(t)


# --------------------------------------------------------------------------------------------
# PASETO v4.public

def sign_raw(seed, payload, footer=b"", implicit=b""):
    sig = _sign(seed, pae(HEADER.encode(), payload, footer, implicit))
    token = HEADER + b64u(payload + sig)
    return token + "." + b64u(footer) if footer else token


def split_token(token):
    """(payload, signature, footer) of a v4.public token, unverified."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN or not token.startswith(HEADER):
        raise XiTokenError("malformed", "not a v4.public token")
    parts = token[len(HEADER):].split(".")
    if len(parts) > 2:
        raise XiTokenError("malformed", "too many parts")
    body = b64u_decode(parts[0])
    footer = b64u_decode(parts[1]) if len(parts) == 2 else b""
    if len(body) < 64:
        raise XiTokenError("malformed", "too short")
    return body[:-64], body[-64:], footer


def verify_raw(pub, token, implicit=b""):
    """The payload of a v4.public token signed by `pub`, and its footer."""
    payload, sig, footer = split_token(token)
    if not _verify(pub, pae(HEADER.encode(), payload, footer, implicit), sig):
        raise XiTokenError("bad_signature")
    return payload, footer


# --------------------------------------------------------------------------------------------
# Keys and server ids

def server_id(identity_public):
    return "xi1." + b64u(hashlib.sha256(b"xitoken server id\0" + identity_public).digest()[:16])


def is_server_id(text):
    return isinstance(text, str) and bool(_SERVER_ID.match(text))


def _decode_paserk(text, prefix, size):
    if not isinstance(text, str) or not text.startswith(prefix):
        return None
    try:
        raw = b64u_decode(text[len(prefix):])
    except XiTokenError:
        return None
    return raw if len(raw) == size else None


def public_from_paserk(text):
    return _decode_paserk(text, PUBLIC_PREFIX, 32)


class SigningKey:
    """An Ed25519 key with a key id: a server's identity key, or a signing key it endorses."""

    def __init__(self, kid, seed):
        if not _KID.match(kid):
            raise ValueError("key id must be 1-64 of [A-Za-z0-9._:-]")
        self.kid = kid
        self._seed = bytes(seed)
        self.public = _public_of(self._seed)

    @classmethod
    def generate(cls, kid):
        return cls(kid, os.urandom(32))

    @classmethod
    def from_paserk(cls, kid, text):
        """From a k4.secret. Trailing whitespace (a key file's newline) is allowed; a public
        half that does not match the seed is refused, as upstream does."""
        raw = _decode_paserk(text.strip(), SECRET_PREFIX, 64) if isinstance(text, str) else None
        if raw is None:
            raise ValueError("not a k4.secret key")
        key = cls(kid, raw[:32])
        if not hmac.compare_digest(key.public, raw[32:]):
            raise ValueError("k4.secret seed and public key do not match")
        return key

    @classmethod
    def from_file(cls, kid, path):
        with open(path) as f:
            return cls.from_paserk(kid, f.read())

    def paserk_secret(self):
        return SECRET_PREFIX + b64u(self._seed + self.public)

    def paserk_public(self):
        return PUBLIC_PREFIX + b64u(self.public)

    @property
    def server_id(self):
        return server_id(self.public)

    def sign(self, payload, footer):
        return sign_raw(self._seed, payload, footer)

    def __repr__(self):
        return "SigningKey(%r, %s)" % (self.kid, self.paserk_public())


# --------------------------------------------------------------------------------------------
# Key sets

class KeySet:
    """A server's checked key set: its id, name, signing keys by kid, and world description."""

    def __init__(self, server_id, name, issued, expires, keys, world, token):
        self.server_id = server_id
        self.name = name
        self.issued = issued
        self.expires = expires
        self.keys = keys            # {kid: 32-byte public key}
        self.world = world          # {"gateway", "expansions", "search"?} or None
        self.token = token

    def __repr__(self):
        return "KeySet(%s %r, %d key(s)%s)" % (self.server_id, self.name, len(self.keys),
                                                ", world" if self.world else "")


def make_keyset(identity, name=None, signing_keys=(), issued=None, expires=None, world=None):
    """Our signed key set. `world` = {"gateway": url, "expansions": n, "search"?: "ip:port"}."""
    payload = {"typ": KEYSET_TYPE, "iss": identity.server_id,
               "iat": format_time(time.time() if issued is None else issued)}
    if name:
        payload["name"] = name
    if expires is not None:
        payload["exp"] = format_time(expires)
    payload["keys"] = [{"kid": k.kid, "public": k.paserk_public()} for k in signing_keys]
    if world is not None:
        payload["world"] = dict(world)
    return identity.sign(_json(payload), _json({"idk": identity.paserk_public()}))


def load_keyset(token, trusted_id=None, now=None):
    """Check a key set (spec, "Keys and key sets", steps 1-4). `trusted_id` is the server id
    the operator chose to trust; None checks the document alone, for display."""
    _, _, footer_raw = split_token(token)
    footer = _load_object(footer_raw, "footer")
    idk = public_from_paserk(footer.get("idk"))
    if idk is None:
        raise XiTokenError("malformed", "key set footer has no k4.public idk")
    sid = server_id(idk)
    payload, _ = verify_raw(idk, token)
    body = _load_object(payload, "payload")
    if body.get("typ") != KEYSET_TYPE:
        raise XiTokenError("wrong_type", "not an %s document" % KEYSET_TYPE)
    if body.get("iss") != sid:
        raise XiTokenError("wrong_issuer", "iss does not match the identity key")
    if trusted_id is not None and sid != trusted_id:
        raise XiTokenError("wrong_issuer", "signed by %s, not %s" % (sid, trusted_id))
    issued = parse_time(body.get("iat"))
    if issued is None:
        raise XiTokenError("bad_claims", "iat")
    expires = None
    if "exp" in body:
        expires = parse_time(body["exp"])
        if expires is None:
            raise XiTokenError("bad_claims", "exp")
        if (time.time() if now is None else now) >= expires:
            raise XiTokenError("expired", "key set")
    keys = {}
    entries = body.get("keys")
    if not isinstance(entries, list):
        raise XiTokenError("bad_claims", "keys")
    for entry in entries:
        kid = entry.get("kid") if isinstance(entry, dict) else None
        pub = public_from_paserk(entry.get("public")) if isinstance(entry, dict) else None
        if not isinstance(kid, str) or not _KID.match(kid) or pub is None or kid in keys:
            raise XiTokenError("bad_claims", "key entry %r" % (entry,))
        keys[kid] = pub
    world = body.get("world")
    if world is not None:
        ok = (isinstance(world, dict) and isinstance(world.get("gateway"), str)
              and isinstance(world.get("expansions"), int) and 0 <= world["expansions"] <= 0xFFFFFFFF
              and (world.get("search") is None or isinstance(world.get("search"), str)))
        if not ok:
            raise XiTokenError("bad_claims", "world")
    name = body.get("name") if isinstance(body.get("name"), str) else None
    return KeySet(sid, name, issued, expires, keys, world, token)


class KeyRing:
    """Trusted key sets by server id. A newer key set (later iat) for an id replaces the older."""

    def __init__(self):
        self.sets = {}

    def add(self, keyset):
        old = self.sets.get(keyset.server_id)
        if old is None or keyset.issued >= old.issued:
            self.sets[keyset.server_id] = keyset
        return self.sets[keyset.server_id]

    def key(self, iss, kid):
        ks = self.sets.get(iss)
        return ks.keys.get(kid) if ks else None


# --------------------------------------------------------------------------------------------
# Tokens

class Issuer:
    """Mints tokens as provider `issuer_id`, signed with `key` (which our key set lists)."""

    def __init__(self, issuer_id, key):
        if not is_server_id(issuer_id):
            raise ValueError("%r is not a server id" % issuer_id)
        self.issuer_id = issuer_id
        self.key = key

    def issue(self, typ, audience, subject, lifetime, claims=None, now=None):
        now = int(time.time() if now is None else now)
        payload = {"iss": self.issuer_id, "aud": audience, "sub": str(subject), "typ": typ,
                   "jti": b64u(os.urandom(16)), "iat": format_time(now), "exp": format_time(now + lifetime)}
        for name, value in (claims or {}).items():
            if name in payload or name == "nbf":
                raise ValueError("claim %r is set by the issuer" % name)
            payload[name] = value
        return self.key.sign(_json(payload), _json({"iss": self.issuer_id, "kid": self.key.kid}))

    def account(self, world_id, subject, now=None):
        """xi.account/1: list, create, delete or rename the player's characters on a world."""
        return self.issue(ACCOUNT_TYPE, world_id, subject, ACCOUNT_LIFETIME, now=now)

    def world_entry(self, world_id, subject, char_id, client_ip, client_version, client_expansions,
                    session_key, char_name=None, now=None):
        """xi.world-entry/1: admit one character. `session_key` is the 20-byte map session key
        the client will use; this token carries it in clear, so it travels over TLS only and
        is never logged."""
        try:
            if len(socket.inet_aton(client_ip)) != 4 or client_ip.count(".") != 3:
                raise OSError
        except (OSError, TypeError):
            raise ValueError("client ip must be a dotted IPv4 address")
        if len(client_version) > 16:
            raise ValueError("client version is at most 16 characters")
        if len(session_key) != 20:
            raise ValueError("session key is 20 bytes")
        char = {"id": int(char_id)}
        if char_name is not None:
            char["name"] = char_name
        claims = {"char": char,
                  "client": {"ip": client_ip, "version": client_version, "expansions": int(client_expansions)},
                  "skey": b64u(bytes(session_key))}
        return self.issue(WORLD_ENTRY_TYPE, world_id, subject, WORLD_ENTRY_LIFETIME, claims, now=now)


class MemoryReplayGuard:
    def __init__(self):
        self.used = {}

    def try_consume(self, issuer, jti, keep_until, now):
        self.used = {k: v for k, v in self.used.items() if v > now}
        if (issuer, jti) in self.used:
            return False
        self.used[(issuer, jti)] = keep_until
        return True


def verify(token, ring, audience, expected_type, now=None, skew=30, max_lifetime=300, replay=None):
    """Check a token the way a world does (spec, "Verification", steps 1-9, in order)."""
    now = int(time.time() if now is None else now)
    payload_raw, sig, footer_raw = split_token(token)
    footer = _load_object(footer_raw, "footer") if footer_raw else None
    if not footer or not isinstance(footer.get("iss"), str) or not isinstance(footer.get("kid"), str):
        raise XiTokenError("malformed", "footer needs iss and kid")
    if len(footer_raw) > MAX_FOOTER:
        raise XiTokenError("malformed", "footer too long")
    pub = ring.key(footer["iss"], footer["kid"])
    if pub is None:
        raise XiTokenError("unknown_key", "%s/%s" % (footer["iss"], footer["kid"]))
    if not _verify(pub, pae(HEADER.encode(), payload_raw, footer_raw, b""), sig):
        raise XiTokenError("bad_signature")
    claims = _load_object(payload_raw, "payload")
    for name in ("iss", "aud", "sub", "typ", "jti"):
        if not isinstance(claims.get(name), str) or not claims[name]:
            raise XiTokenError("bad_claims", name)
    iat, exp = parse_time(claims.get("iat")), parse_time(claims.get("exp"))
    if iat is None or exp is None or len(claims["jti"]) < 22:
        raise XiTokenError("bad_claims", "iat/exp/jti")
    nbf = None
    if "nbf" in claims:
        nbf = parse_time(claims["nbf"])
        if nbf is None:
            raise XiTokenError("bad_claims", "nbf")
    if claims["iss"] != footer["iss"]:
        raise XiTokenError("wrong_issuer")
    if claims["aud"] != audience:
        raise XiTokenError("wrong_audience")
    if claims["typ"] != expected_type:
        raise XiTokenError("wrong_type")
    if (nbf is not None and now + skew < nbf) or now + skew < iat:
        raise XiTokenError("not_yet_valid")
    if now - skew >= exp:
        raise XiTokenError("expired")
    if exp - iat > max_lifetime:
        raise XiTokenError("lifetime_too_long")
    if replay is not None and not replay.try_consume(claims["iss"], claims["jti"], exp + skew, now):
        raise XiTokenError("replayed")
    return claims


# --------------------------------------------------------------------------------------------
# World gateway client (spec, "World gateway API")

def certificate_pin(der):
    return "sha256:" + b64u(hashlib.sha256(der).digest())


class GatewayError(Exception):
    def __init__(self, status, error, detail=""):
        super().__init__("%s %s%s" % (status, error, " (%s)" % detail if detail else ""))
        self.status = status
        self.error = error


class WorldGateway:
    """Calls one world's gateway. With `pin` ("sha256:<b64url of the DER>") the certificate is
    checked against the pin instead of a CA chain, as the spec allows for self-signed worlds;
    without it, normal verification. Plain http:// only for loopback or private links."""

    def __init__(self, base_url, pin=None, timeout=10):
        u = urllib.parse.urlsplit(base_url.rstrip("/"))
        if u.scheme not in ("https", "http") or not u.hostname:
            raise ValueError("gateway must be an http(s) URL")
        self.scheme, self.host, self.prefix = u.scheme, u.hostname, u.path
        self.port = u.port or (443 if u.scheme == "https" else 80)
        self.pin, self.timeout = pin, timeout

    def _socket(self):
        """A TCP connection, IPv4 addresses first. Prod has no working IPv6 route, and a name
        with AAAA records first (anything behind Cloudflare) otherwise costs a full timeout on
        every call before the IPv4 attempt."""
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        last = OSError("no address for %s" % self.host)
        for family, kind, proto, _, addr in infos:
            s = socket.socket(family, kind, proto)
            s.settimeout(min(self.timeout, 5) if len(infos) > 1 else self.timeout)
            try:
                s.connect(addr)
                s.settimeout(self.timeout)
                return s
            except OSError as e:
                s.close()
                last = e
        raise last

    def _conn(self):
        if self.scheme == "http":
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.sock = self._socket()
            return conn
        ctx = ssl.create_default_context()
        if self.pin:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=ctx)
        conn.sock = ctx.wrap_socket(self._socket(), server_hostname=self.host)
        if self.pin:
            got = certificate_pin(conn.sock.getpeercert(binary_form=True))
            if not hmac.compare_digest(got, self.pin):
                conn.close()
                raise GatewayError(0, "pin_mismatch", "%s presented %s" % (self.host, got))
        return conn

    def _call(self, method, path, body=None, token=None, ctype=None):
        headers = {}
        if token is not None:
            headers["Authorization"] = "XiToken " + token
        if body is not None:
            headers["Content-Type"] = ctype or "application/json"
        conn = self._conn()
        try:
            conn.request(method, self.prefix + path, body=body, headers=headers)
            r = conn.getresponse()
            status, raw = r.status, r.read(1 << 20)
        finally:
            conn.close()
        return status, raw

    def _json_call(self, method, path, body=None, token=None, ctype=None):
        status, raw = self._call(method, path, body, token, ctype)
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise GatewayError(status, "bad_reply", raw[:80].decode("latin-1"))
        if status != 200 or not isinstance(reply, dict) or not reply.get("ok"):
            raise GatewayError(status, reply.get("error", "error") if isinstance(reply, dict) else "error")
        return reply

    def keyset(self):
        status, raw = self._call("GET", "/xi/v1/keyset")
        if status != 200:
            raise GatewayError(status, "keyset")
        return raw.decode("ascii").strip()

    def characters(self, account_token):
        return self._json_call("GET", "/xi/v1/characters", token=account_token)["characters"]

    def create_character(self, account_token, name, race, face, size, job, nation):
        body = _json({"name": name, "race": race, "face": face, "size": size, "job": job, "nation": nation})
        return self._json_call("POST", "/xi/v1/characters", body, account_token)["id"]

    def delete_character(self, account_token, char_id):
        self._json_call("DELETE", "/xi/v1/characters/%d" % int(char_id), token=account_token)

    def rename_character(self, account_token, char_id, name):
        self._json_call("POST", "/xi/v1/characters/%d/name" % int(char_id), _json({"name": name}),
                        account_token)

    def world_entry(self, entry_token):
        """POST the world-entry token; the map server (ip, port) the client goes to."""
        reply = self._json_call("POST", "/xi/v1/world-entry", entry_token.encode(), ctype="text/plain")
        m = reply.get("map") or {}
        return m["ip"], int(m["port"])
