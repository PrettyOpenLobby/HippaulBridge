#!/usr/bin/env python3
"""Our side of FFXI federation as a PROVIDER: keys, our key set, and a self-test against a world.

A provider (the POL service a player signed in to) sends its players to other operators' worlds
with signed tokens (lsb/xitoken.py, PhoenixPS2 ext/xitoken/SPEC.md). A world admits them once it
trusts the provider's server id, which it learns from our key set.

  ffxi_provider.py keygen DIR [--kid 2026-10]
      Make DIR/identity.key and DIR/signing-<kid>.key (k4.secret, mode 0600) and print the
      provider's server id. Refuses to overwrite. The identity key only signs the key set.
  ffxi_provider.py keyset DIR [--name OpenLobby] [--out FILE]
      Print (or write) our signed key set, listing every DIR/signing-*.key. Give it to a world
      operator; on our own world it goes in lsb/federation/<provider id>.keyset.
  ffxi_provider.py selftest DIR --gateway URL --world-id ID [--client-ip IP] [--full]
      Call a world's gateway as a player the world has never seen. The world must trust us.
      Without --full nothing is created on the world: an account-token character list (an
      empty list), a world-entry for a character that is not ours (409 not_permitted or unknown_character),
      the same token again (replayed) and a token signed by a key the world does not
      trust (unknown_key). With --full: create a character, enter the world with it (the
      world writes a session row and answers with a map server), then delete it.
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import xitoken as X                                                # noqa: E402

#: The map session key the FFXI client uses with our lobby (ffxi_bridge.A2_SESSION_KEY):
#: zero Blowfish bytes and the counter seed. A world-entry token must carry the key the
#: client will actually use, so it is this constant, not a random one.
CLIENT_SESSION_KEY = bytes(16) + bytes.fromhex("58e05dad")


def write_secret(path, key):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.paserk_secret() + "\n")


def load_dir(d):
    identity = X.SigningKey.from_file("identity", os.path.join(d, "identity.key"))
    signers = []
    for path in sorted(glob.glob(os.path.join(d, "signing-*.key"))):
        kid = os.path.basename(path)[len("signing-"):-len(".key")]
        signers.append(X.SigningKey.from_file(kid, path))
    if not signers:
        sys.exit("%s has no signing-*.key" % d)
    return identity, signers


def cmd_keygen(a):
    os.makedirs(a.dir, mode=0o700, exist_ok=True)
    ident_path = os.path.join(a.dir, "identity.key")
    sign_path = os.path.join(a.dir, "signing-%s.key" % a.kid)
    for p in (ident_path, sign_path):
        if os.path.exists(p):
            sys.exit("%s exists; not overwriting it" % p)
    identity, signer = X.SigningKey.generate("identity"), X.SigningKey.generate(a.kid)
    write_secret(ident_path, identity)
    write_secret(sign_path, signer)
    print("provider server id: " + identity.server_id)
    print("signing key %s:   %s" % (a.kid, signer.paserk_public()))


def cmd_keyset(a):
    identity, signers = load_dir(a.dir)
    token = X.make_keyset(identity, a.name, signers)
    X.load_keyset(token, identity.server_id)          # check what we hand out
    if a.out:
        with open(a.out, "w") as f:
            f.write(token + "\n")
        print("%s -> %s (%s)" % (identity.server_id, a.out, ", ".join(k.kid for k in signers)))
    else:
        print(token)


def cmd_selftest(a):
    identity, signers = load_dir(a.dir)
    issuer = X.Issuer(identity.server_id, signers[-1])
    gw = X.WorldGateway(a.gateway, pin=a.pin)
    failed = []

    def check(ok, label):
        print(("  OK   " if ok else "  FAIL ") + label)
        if not ok:
            failed.append(label)

    def refused(fn):
        try:
            fn()
        except X.GatewayError as e:
            return e.status, e.error
        return None

    print("provider %s -> world %s at %s" % (identity.server_id, a.world_id, a.gateway))
    ks = X.load_keyset(gw.keyset(), a.world_id)
    check(ks.world is not None, "the world's key set checks out against %s (%s)" % (a.world_id, ks.name))
    subject = a.subject or "selftest-%d" % int(time.time())

    try:
        chars = gw.characters(issuer.account(a.world_id, subject))
        check(chars == [], "account token accepted; a new player has no characters")
    except X.GatewayError as e:
        check(False, "account token accepted (%s)" % e)

    entry = issuer.world_entry(a.world_id, subject, 1, a.client_ip, "30260805_0", 0x0FFF, CLIENT_SESSION_KEY)
    check(refused(lambda: gw.world_entry(entry)) in ((409, "not_permitted"), (409, "unknown_character")),
          "world-entry token accepted, entry refused: charid 1 is not this player's")
    check(refused(lambda: gw.world_entry(entry)) == (400, "replayed"), "the same token again is replayed")
    stranger = X.Issuer(identity.server_id, X.SigningKey.generate(signers[-1].kid))
    check(refused(lambda: gw.characters(stranger.account(a.world_id, subject))) == (400, "bad_signature"),
          "our id with a key not in our key set is refused")
    other = X.SigningKey.generate("identity")
    check(refused(lambda: gw.characters(X.Issuer(other.server_id, signers[-1]).account(a.world_id, subject)))
          == (400, "unknown_key"), "a provider the world does not trust is refused")

    if a.full:
        name = a.name or "Xitest" + "".join(chr(97 + int(c)) for c in str(int(time.time()))[-6:])
        created = []
        try:
            char_id = gw.create_character(issuer.account(a.world_id, subject), name, 1, 0, 1, 1, 0)
            created.append(char_id)
            check(True, "created %s (charid %d)" % (name, char_id))
            listed = gw.characters(issuer.account(a.world_id, subject))
            check([c["id"] for c in listed] == [char_id], "it is listed for this player")
            entry = issuer.world_entry(a.world_id, subject, char_id, a.client_ip, "30260805_0", 0x0FFF,
                                       CLIENT_SESSION_KEY, char_name=name)
            ip, port = gw.world_entry(entry)
            check(bool(ip) and port > 0, "admitted: the client would go to map %s:%d" % (ip, port))
            check(refused(lambda: gw.world_entry(issuer.world_entry(
                a.world_id, subject, char_id, a.client_ip, "30260805_0", 0x0FFF, CLIENT_SESSION_KEY)))
                == (409, "already_logged_in"), "a second entry while the session stands is refused")
        finally:
            # By the ids we created, not by the list: on 10-04 the list itself was broken
            # (upstream read chars.doRename) and a list-driven cleanup deleted nothing.
            listed = []
            try:
                listed = [c["id"] for c in gw.characters(issuer.account(a.world_id, subject))]
            except X.GatewayError:
                pass
            for char_id in sorted(set(created) | set(listed)):
                try:
                    gw.delete_character(issuer.account(a.world_id, subject), char_id)
                    print("  deleted charid %d (and its session row)" % char_id)
                except X.GatewayError as e:
                    check(False, "delete charid %d (%s)" % (char_id, e))
        check(gw.characters(issuer.account(a.world_id, subject)) == [], "nothing left behind but the account mapping")
        print("  note: the world keeps an account for %s:%s (accounts_federated)" % (identity.server_id, subject))

    print("RESULT: %s" % ("%d FAILURE(S)" % len(failed) if failed else "all passed"))
    return 1 if failed else 0


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("dir")
    k.add_argument("--kid", default=time.strftime("%Y-%m"))
    s = sub.add_parser("keyset")
    s.add_argument("dir")
    s.add_argument("--name", default="OpenLobby")
    s.add_argument("--out")
    t = sub.add_parser("selftest")
    t.add_argument("dir")
    t.add_argument("--gateway", required=True)
    t.add_argument("--world-id", required=True)
    t.add_argument("--pin")
    t.add_argument("--client-ip", default="203.0.113.7")
    t.add_argument("--subject")
    t.add_argument("--name", help="character name for --full")
    t.add_argument("--full", action="store_true")
    a = p.parse_args()
    return {"keygen": cmd_keygen, "keyset": cmd_keyset, "selftest": cmd_selftest}[a.cmd](a) or 0


if __name__ == "__main__":
    sys.exit(main())
