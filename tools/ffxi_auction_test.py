#!/usr/bin/env python3
"""lsb/ffxi_auction.py and the Auction house page of lsb/ffxi_fedtool.py.

Offline (always): the checks on what the page sends and the SQL each change runs, over an
in-memory stand-in for the Store, and the tool-port contract on the /ah routes.

Against a real database (--db): every change on LSB's real auction_house, delivery_box and
triggers. Load LSB's sql/ into an empty MariaDB first, triggers.sql included (the purchase
checks need the auction_house_buy trigger); the run adds one
made-up character (id 900011) and listings named AhTest*, checks that a test listing pays
nobody when bought, that a price change pays nobody, that returning a listing puts a full stack
in the seller's delivery box, and that sold rows are refused, then deletes what it added.

  python tools/ffxi_auction_test.py
  python tools/ffxi_auction_test.py --db HOST:PORT:USER:PASSWORD:DATABASE
  python tools/ffxi_auction_test.py --serve N    the page on 127.0.0.1:N over the stand-in
"""
import http.client
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "lsb"))
import ffxi_auction as A                                           # noqa: E402
import ffxi_fedtool as T                                           # noqa: E402

FAILED = []


def check(ok, label):
    print(("  OK   " if ok else "  FAIL ") + label)
    if not ok:
        FAILED.append(label)


def refuses(fn, *a, contains=""):
    try:
        fn(*a)
    except A.AuctionError as exc:
        return contains in str(exc)
    return False


# -- the stand-in ----------------------------------------------------------------------------------

ITEMS = {880: {"itemid": 880, "name": "bone_chip", "stackSize": 12, "flags": 0, "aH": 19},
         1126: {"itemid": 1126, "name": "beastmens_seal", "stackSize": 99, "flags": 0x40, "aH": 0},
         16534: {"itemid": 16534, "name": "onion_sword", "stackSize": 1, "flags": 0x40, "aH": 2}}


def row(id_, seller=11, stack=0, buyer=None, price=1000):
    return {"id": id_, "itemid": 880, "name": "bone_chip", "stack_size": 12, "stack": stack, "seller": seller,
            "seller_name": "Arbor" if seller else "Test", "date": 100, "price": price, "buyer": 12 if buyer else 0,
            "buyer_name": buyer, "sale": price if buyer else 0, "sell_date": 200 if buyer else 0}


class FakeCursor:
    def __init__(self, sql):
        self.sql = sql

    def execute(self, sql, args=()):
        self.sql.append((sql, args))

    def fetchone(self):
        return {"charname": "Arbor"}


class FakeStore:
    def __init__(self):
        self.rows = {1: row(1), 2: row(2, stack=1), 3: row(3, seller=0), 4: row(4, buyer="Birch")}
        self.sql, self.posted, self.logged = [], [], []

    def find_items(self, text, limit=40):
        return [i for i in ITEMS.values() if text.replace(" ", "_") in i["name"] or str(i["itemid"]) == text] + (
            [{"itemid": 65535, "name": "gil", "stackSize": 1, "flags": 0, "aH": 0}] if text == "gil" else [])

    def item(self, itemid):
        return ITEMS.get(int(itemid))

    def listings(self, text="", limit=200):
        return [r for r in self.rows.values() if r["buyer_name"] is None]

    def sold(self, text="", limit=100):
        return [r for r in self.rows.values() if r["buyer_name"] is not None]

    def counts(self):
        return {"selling": 3, "sold": 1, "house": 1}

    def log(self, limit=60):
        return []

    def change(self, listing_id, apply, log):
        r = self.rows.get(int(listing_id))
        if r is None:
            raise A.AuctionError("There is no listing %d." % int(listing_id))
        if r["buyer_name"] is not None:
            raise A.AuctionError("Listing %d already sold to %s." % (int(listing_id), r["buyer_name"]))
        self.sql = []
        self.logged.append((log, apply(FakeCursor(self.sql), r)))
        return r

    def post(self, itemid, stack, price, seller_name, count, who, now, detail):
        self.posted.append((itemid, stack, price, seller_name, count, detail))


def offline():
    print("offline")
    st = FakeStore()
    ah = A.Auction(st)

    s = ah.state({"q": ["bone"]})
    check(s["q"] == "bone" and [r["id"] for r in s["selling"]] == [1, 2, 3] and [r["id"] for r in s["sold"]] == [4],
          "state: for sale and sold apart")
    check(refuses(ah.state, {"q": ["x" * 41]}, contains="longer"), "state: an over-long filter refused")
    check(A._where("4116") == ("a.itemid = %s", (4116,)), "filter: a number is an item id")
    check(A._where("hi potion")[1] == ("%hi\\_potion%", "hi potion%"), "filter: spaces match the item names' underscores")
    check(A._where("50%_x")[1][1] == "50\\_x%", "filter: % dropped and _ escaped in the seller name")
    check(A.quantity(row(1)) == 1 and A.quantity(row(2, stack=1)) == 12, "a stack listing holds a full stack")

    # price
    out = ah.price({"id": 1, "price": "2500"}, "mod", now=300)
    sql, args = st.sql[0]
    check(out["ok"] and "buyer_name IS NULL AND sale = 0" in sql and args == (2500, 1),
          "price: one UPDATE, only on a row still for sale (the buy trigger never fires)")
    check(st.logged[-1][0] == ("mod", "price", 300) and "price 1000 -> 2500" in st.logged[-1][1], "price: logged old -> new")
    check(refuses(ah.price, {"id": 4, "price": 5}, "mod", contains="already sold"), "price: a sold row refused")
    check(refuses(ah.price, {"id": 1, "price": 0}, "mod", contains="1 to 999999999"), "price: 0 refused")
    check(refuses(ah.price, {"id": 1, "price": "abc"}, "mod", contains="whole number"), "price: not a number refused")
    check(refuses(ah.price, {"id": 9, "price": 5}, "mod", contains="no listing 9"), "price: unknown listing refused")

    # taking down
    out = ah.remove({"id": 2, "return": True}, "mod", now=300)
    check(st.sql[0][0].startswith("DELETE FROM auction_house") and "buyer_name IS NULL" in st.sql[0][0],
          "return: the listing deleted while still for sale")
    ins = st.sql[-1]
    check("INSERT INTO delivery_box" in ins[0] and "'AH-Jeuno'" in ins[0] and ins[1] == (11, "Arbor", 880, 12),
          "return: a full stack into the seller's delivery box, as an expired listing")
    check("bone chip x12 is in Arbor's delivery box" in out["msg"], "return: says where the item went")
    ah.remove({"id": 1, "return": False}, "mod", now=300)
    check(len(st.sql) == 1 and st.sql[0][0].startswith("DELETE"), "delete: no delivery box row")
    ah.remove({"id": 3, "return": True}, "mod", now=300)
    check(len(st.sql) == 1 and st.logged[-1][1].endswith("deleted"), "return of a test listing: nobody to return it to")
    check(refuses(ah.remove, {"id": 4}, "mod", contains="already sold"), "remove: a sold row refused")

    # test listings
    out = ah.post({"item": 880, "price": 900, "count": 3, "stack": True, "seller": "Moogle"}, "mod", now=300)
    check(out["ok"] and st.posted[-1][:5] == (880, True, 900, "Moogle", 3), "post: three stacks of bone chips")
    ah.post({"item": 16534, "price": 900}, "mod", now=300)
    check(st.posted[-1][3] == "Test" and st.posted[-1][4] == 1 and "players cannot list" in st.posted[-1][5],
          "post: seller Test by default, an item players cannot list is noted")
    check(refuses(ah.post, {"item": 1126, "price": 5}, "mod", contains="no auction house category"), "post: no AH category refused")
    check(refuses(ah.post, {"item": 16534, "price": 5, "stack": True}, "mod", contains="does not stack"),
          "post: a stack of a single item refused")
    check(refuses(ah.post, {"item": 880, "price": 5, "count": 21}, "mod", contains="1 to 20"), "post: more than 20 refused")
    check(refuses(ah.post, {"item": 880, "price": 5, "seller": "a b"}, "mod", contains="seller's name"), "post: bad seller name")
    check(refuses(ah.post, {"item": 777, "price": 5}, "mod", contains="no item 777"), "post: unknown item refused")
    check(ah.item_search({"q": ["gil"]})["rows"] == [], "item search leaves gil out")

    # the tool port
    T._AH[:] = [A.Auction(FakeStore())]
    tool = T.Tool(types.SimpleNamespace(log=lambda *a: None, FED=None), environ={})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), T.make_handler(tool, "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def req(method, path, body=None, token="tok"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdr = {"X-Devtool-Token": token, "X-Devtool-Actor": "mod"} if token else {}
        if body is not None:
            hdr["Content-Type"] = "application/json"
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdr)
        r = c.getresponse()
        return r.status, r.read()

    s, d = req("GET", "/ah")
    check(s == 200 and b"<title>FFXI auction house</title>" in d, "GET /ah serves the page")
    check(req("GET", "/ah", token=None)[0] == 403, "the page needs the token")
    s, d = req("GET", "/ah.json?q=bone")
    check(s == 200 and len(json.loads(d)["selling"]) == 3, "GET /ah.json")
    s, d = req("GET", "/ah/items.json?q=bone")
    check(s == 200 and json.loads(d)["rows"][0]["itemid"] == 880, "GET /ah/items.json")
    s, d = req("POST", "/ah/price", {"id": 1, "price": 5})
    check(s == 200 and json.loads(d)["ok"], "POST /ah/price")
    s, d = req("POST", "/ah/price", {"id": 4, "price": 5})
    check(s == 200 and not json.loads(d)["ok"] and "already sold" in json.loads(d)["msg"],
          "a refused change comes back ok: false with the reason")
    check(req("POST", "/ah/remove", {"id": 1}, token="bad")[0] == 403, "changes need the token")
    srv.shutdown()


# -- against a real database -------------------------------------------------------------------------

SELLER = 900011


def real(spec):
    print("real database")
    host, port, user, password, database = spec.split(":", 4)
    store = A.Store(host, port, user, password, database)
    ah = A.Auction(store)
    run = store._run

    def one(sql, args=()):
        rows = run(sql, args, fetch=True)
        return rows[0] if rows else None

    def box(charid):
        # list(): pymysql's DictCursor gives () for no rows
        return list(run("SELECT itemid, quantity, sender FROM delivery_box WHERE charid = %s ORDER BY slot", (charid,),
                        fetch=True))

    def cleanup():
        run("DELETE FROM auction_house WHERE seller = %s OR seller_name LIKE 'AhTest%%'", (SELLER,))
        run("DELETE FROM delivery_box WHERE charid IN (0, %s) AND sender = 'AH-Jeuno' AND itemid IN (880, 65535)", (SELLER,))
        # LSB's char_insert trigger gives a new character a row in a dozen char_* tables
        for t in run("SELECT table_name AS t FROM information_schema.columns WHERE table_schema = DATABASE() "
                     "AND column_name = 'charid' AND table_name LIKE 'char%%'", fetch=True):
            run("DELETE FROM `%s` WHERE charid = %%s" % t["t"], (SELLER,))

    cleanup()
    run("INSERT INTO chars (charid, accid, charname, pos_zone, pos_prevzone) VALUES (%s, 1, 'Arbor', 246, 246)", (SELLER,))
    try:
        zero_before = len(box(0))
        ah.post({"item": 880, "price": 777, "count": 2, "stack": True, "seller": "AhTestMoogle"}, "mod-ahtest")
        got = run("SELECT id, itemid, stack, seller, seller_name, price, buyer_name FROM auction_house "
                  "WHERE seller_name = 'AhTestMoogle'", fetch=True)
        check(len(got) == 2 and all((g["itemid"], g["stack"], g["seller"], g["price"], g["buyer_name"])
                                    == (880, 1, 0, 777, None) for g in got), "post: two stack listings, seller 0, for sale")
        check(any(r["id"] == got[0]["id"] for r in ah.state({"q": ["bone chip"]})["selling"]), "state: found by item name")
        check(any(r["id"] == got[0]["id"] for r in ah.state({"q": ["AhTestMoo"]})["selling"]), "state: found by seller name")
        try:
            json.dumps(ah.state({"q": ["bone chip"]}))
            plain = True
        except TypeError:
            plain = False
        check(plain, "state: plain JSON (no Decimal from the item join)")

        ah.price({"id": got[0]["id"], "price": 555}, "mod-ahtest")
        check(one("SELECT price FROM auction_house WHERE id = %s", (got[0]["id"],))["price"] == 555, "price: written")

        # a purchase from a test listing, as the map writes it: the trigger must pay nobody
        run("UPDATE auction_house SET buyer = 1, buyer_name = 'AhBuyer', sale = 555, sell_date = 1 WHERE id = %s",
            (got[0]["id"],))
        check(len(box(0)) == zero_before, "a test listing bought: the buy trigger pays nobody")
        check(any(r["id"] == got[0]["id"] for r in ah.state({"q": ["AhTestMoo"]})["sold"]), "the sale shows as sold")
        check(refuses(ah.price, {"id": got[0]["id"], "price": 9}, "mod-ahtest", contains="already sold"), "price: sold row refused")
        check(refuses(ah.remove, {"id": got[0]["id"]}, "mod-ahtest", contains="already sold"), "remove: sold row refused")

        # a player's listing: a price change pays nothing; returning it delivers the full stack
        run("INSERT INTO auction_house (itemid, stack, seller, seller_name, date, price) VALUES (880, 1, %s, 'Arbor', 1, 1200)",
            (SELLER,))
        lid = one("SELECT id FROM auction_house WHERE seller = %s", (SELLER,))["id"]
        ah.price({"id": lid, "price": 1500}, "mod-ahtest")
        check(box(SELLER) == [], "price change on a player's listing: no payment to the seller")
        out = ah.remove({"id": lid, "return": True}, "mod-ahtest")
        check(out["ok"] and one("SELECT 1 AS x FROM auction_house WHERE id = %s", (lid,)) is None
              and box(SELLER) == [{"itemid": 880, "quantity": 12, "sender": "AH-Jeuno"}],
              "return: listing gone, a stack of 12 in the seller's delivery box from AH-Jeuno")
        ah.remove({"id": got[1]["id"], "return": False}, "mod-ahtest")
        check(one("SELECT 1 AS x FROM auction_house WHERE id = %s", (got[1]["id"],)) is None and len(box(0)) == zero_before,
              "delete: listing gone, nothing delivered")
        log = [e for e in store.log() if e["who"] == "mod-ahtest"]
        check([e["action"] for e in reversed(log)] == ["post", "price", "price", "return", "delete"], "everything logged, in order")
    finally:
        cleanup()
        run("DELETE FROM openlobby_ahtool_log WHERE who = 'mod-ahtest'")


def serve(port):
    T._AH[:] = [A.Auction(FakeStore())]
    tool = T.Tool(types.SimpleNamespace(log=lambda *a: None, FED=None), environ={})
    srv = ThreadingHTTPServer(("127.0.0.1", port), T.make_handler(tool, ""))
    print("http://127.0.0.1:%d/ah" % port)
    srv.serve_forever()


if __name__ == "__main__":
    if "--serve" in sys.argv:
        serve(int(sys.argv[sys.argv.index("--serve") + 1]))
    offline()
    if "--db" in sys.argv:
        real(sys.argv[sys.argv.index("--db") + 1])
    print("%d failed" % len(FAILED) if FAILED else "all passed")
    sys.exit(1 if FAILED else 0)
