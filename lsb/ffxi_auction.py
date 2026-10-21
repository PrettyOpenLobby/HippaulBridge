"""The FFXI auction house for the admin panel (Games > FFXI > Auction house), served by ffxi_fedtool.py.

LSB keeps every listing in auction_house, one row per item or stack: the row is for sale while
buyer_name is NULL and sold once the map fills in the buyer, the sale price and sell_date. The
map and the search server read the table on every request, so a change here shows at once, with
the seller and the buyers logged in. This page:

  - lists what is for sale and what sold, by item name or id, or by seller;
  - changes the price of a listing for sale;
  - takes a listing down and returns its item to the seller's delivery box, as LSB does when a
    listing expires (LSB's src/search/data_loader.cpp: sender AH-Jeuno, a stack's full
    count), or deletes it outright;
  - posts test listings that nobody sold. Their seller is 0, as for the server's own stock (the
    "Auctioneer" listings), which the auction_house_buy trigger pays nothing to
    (sql/triggers.sql), so a purchase from one only takes the buyer's gil.

Only rows still for sale are ever updated. The auction_house_buy trigger runs on every UPDATE
whose new sale is not 0 and puts the sale price in the seller's delivery box, so touching a sold
row would pay its seller a second time. Sold rows are only read here.

A listing with no seller left up until it expires is "returned" to character 0's delivery box, which no
character opens; take test listings down when done. There is no Undo here, because a returned
item may already have been collected; the log below says who changed what.

  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for the notices tool
"""
import os
import re
import time

import ffxi_chars as C

MAX_PRICE = C.MAX_GIL
MAX_TEST_LISTINGS = 20
#: auction_house.seller_name is VARCHAR(15); the client shows it in the sale history
_SELLER = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,14}\Z")
LOG_DDL = ("CREATE TABLE IF NOT EXISTS openlobby_ahtool_log (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, "
           "at INT UNSIGNED NOT NULL, who VARCHAR(64) NOT NULL, action VARCHAR(16) NOT NULL, "
           "detail VARCHAR(512) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
LISTING_COLUMNS = ("a.id, a.itemid, COALESCE(b.name, '?') AS name, CAST(COALESCE(b.stackSize, 1) AS UNSIGNED) AS stack_size, a.stack, "
                   "a.seller, a.seller_name, a.date, a.price, a.buyer, a.buyer_name, a.sale, a.sell_date")


class AuctionError(Exception):
    """A request the panel should show back as it is."""


def _where(text):
    """(SQL condition, args) for the page's filter: an item id, or part of an item or seller name."""
    text = (text or "").strip()
    if not text:
        return "1 = 1", ()
    if text.isdigit():
        return "a.itemid = %s", (int(text),)
    like = text.replace("\\", "").replace("%", "")
    item = "%" + like.lower().replace("_", " ").replace(" ", "\\_") + "%"
    return "(b.name LIKE %s OR a.seller_name LIKE %s)", (item, like.replace("_", "\\_") + "%")


def quantity(row):
    """How many items a listing holds: a full stack when it was listed as a stack."""
    return int(row["stack_size"] or 1) if row["stack"] else 1


class Store(C.Store):
    """auction_house and delivery_box, over the Characters page's connection settings."""

    def _setup(self):
        if not self._ready:
            self._run(LOG_DDL)
            self._ready = True

    def listings(self, text="", limit=200):
        where, args = _where(text)
        return self._run("SELECT " + LISTING_COLUMNS + " FROM auction_house a LEFT JOIN item_basic b ON b.itemid = a.itemid "
                         "WHERE a.buyer_name IS NULL AND " + where + " ORDER BY a.date DESC, a.id DESC LIMIT %s",
                         args + (int(limit),), fetch=True)

    def sold(self, text="", limit=100):
        where, args = _where(text)
        return self._run("SELECT " + LISTING_COLUMNS + " FROM auction_house a LEFT JOIN item_basic b ON b.itemid = a.itemid "
                         "WHERE a.buyer_name IS NOT NULL AND " + where + " ORDER BY a.sell_date DESC, a.id DESC LIMIT %s",
                         args + (int(limit),), fetch=True)

    def counts(self):
        rows = self._run("SELECT SUM(buyer_name IS NULL) AS selling, SUM(buyer_name IS NOT NULL) AS sold, "
                         "SUM(buyer_name IS NULL AND seller = 0) AS house FROM auction_house", fetch=True)
        return {k: int(rows[0][k] or 0) for k in ("selling", "sold", "house")}

    def log(self, limit=60):
        self._setup()
        return self._run("SELECT id, at, who, action, detail FROM openlobby_ahtool_log ORDER BY id DESC LIMIT %s",
                         (int(limit),), fetch=True)

    def change(self, listing_id, apply, log):
        """Lock listing `listing_id` while it is for sale, run apply(cursor, row) and log, in one
        transaction. `log` is (who, action, now); apply returns the log's detail. -> the row."""
        self._setup()
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT " + LISTING_COLUMNS + " FROM auction_house a LEFT JOIN item_basic b "
                            "ON b.itemid = a.itemid WHERE a.id = %s FOR UPDATE", (int(listing_id),))
                row = cur.fetchone()
                if row is None:
                    raise AuctionError("There is no listing %d." % int(listing_id))
                if row["buyer_name"] is not None or row["sale"]:
                    raise AuctionError("Listing %d already sold to %s." % (int(listing_id), row["buyer_name"] or "someone"))
                detail = apply(cur, row)
                who, action, now = log
                cur.execute("INSERT INTO openlobby_ahtool_log (at, who, action, detail) VALUES (%s, %s, %s, %s)",
                            (int(now), (who or "admin")[:64], action, detail[:512]))
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def post(self, itemid, stack, price, seller_name, count, who, now, detail):
        """Insert `count` test listings (seller 0) and log them, in one transaction."""
        self._setup()
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                for _ in range(count):
                    cur.execute("INSERT INTO auction_house (itemid, stack, seller, seller_name, date, price) "
                                "VALUES (%s, %s, 0, %s, %s, %s)", (int(itemid), 1 if stack else 0, seller_name,
                                                                   int(now), int(price)))
                cur.execute("INSERT INTO openlobby_ahtool_log (at, who, action, detail) VALUES (%s, %s, 'post', %s)",
                            (int(now), (who or "admin")[:64], detail[:512]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _set_price(price):
    def apply(cur, row):
        # `sale = 0` keeps the auction_house_buy trigger from paying anyone
        cur.execute("UPDATE auction_house SET price = %s WHERE id = %s AND buyer_name IS NULL AND sale = 0",
                    (int(price), row["id"]))
        return "listing %d, %s: price %d -> %d" % (row["id"], _what(row), row["price"], int(price))
    return apply


def _take_down(give_back):
    def apply(cur, row):
        cur.execute("DELETE FROM auction_house WHERE id = %s AND buyer_name IS NULL", (row["id"],))
        if give_back and row["seller"]:
            cur.execute("SELECT charname FROM chars WHERE charid = %s", (row["seller"],))
            got = cur.fetchone()
            cur.execute("INSERT INTO delivery_box (charid, charname, box, itemid, itemsubid, quantity, senderid, sender) "
                        "VALUES (%s, %s, 1, %s, 0, %s, 0, 'AH-Jeuno')",
                        (row["seller"], (got or {}).get("charname") or row["seller_name"], row["itemid"], quantity(row)))
            return "listing %d, %s: returned to %s's delivery box" % (row["id"], _what(row), row["seller_name"])
        return "listing %d, %s: deleted" % (row["id"], _what(row))
    return apply


def _iname(name):
    return str(name or "?").replace("_", " ")


def _label(row):
    return "listing %d (%s)" % (row["id"], _iname(row["name"]))


def _what(row):
    return "%s x%d by %s at %d" % (_iname(row["name"]), quantity(row), row["seller_name"] or row["seller"], row["price"])


def _int(body, key, label, lo, hi, default=None):
    raw = body.get(key)
    if raw in (None, "") and default is not None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise AuctionError("%s must be a whole number." % label)
    if not lo <= n <= hi:
        raise AuctionError("%s must be %d to %d." % (label, lo, hi))
    return n


class Auction:
    """What the page shows and does, over a Store (or a test's stand-in with the same methods)."""

    def __init__(self, store):
        self.store = store

    def state(self, query):
        text = (query.get("q") or [""])[0].strip()
        if len(text) > 40:
            raise AuctionError("That is longer than any item or seller name.")
        return {"q": text, "selling": self.store.listings(text), "sold": self.store.sold(text),
                "counts": self.store.counts(), "log": self.store.log()}

    def item_search(self, query):
        text = (query.get("q") or [""])[0].strip()
        if not text:
            return {"rows": []}
        if len(text) > 40:
            raise AuctionError("That is longer than any item name.")
        return {"rows": [r for r in self.store.find_items(text) if r["itemid"] != C.GIL_ITEM]}

    def price(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        listing = _int(body, "id", "The listing", 1, 2 ** 32 - 1)
        price = _int(body, "price", "The price", 1, MAX_PRICE)
        row = self.store.change(listing, _set_price(price), (actor, "price", now))
        return {"ok": True, "msg": "%s now sells for %d gil (was %d)." % (_label(row), price, row["price"])}

    def remove(self, body, actor, now=None):
        """Take a listing down: "return": true sends the item back to its seller, otherwise it is deleted."""
        now = int(time.time() if now is None else now)
        listing = _int(body, "id", "The listing", 1, 2 ** 32 - 1)
        give_back = bool(body.get("return"))
        row = self.store.change(listing, _take_down(give_back), (actor, "return" if give_back else "delete", now))
        if give_back and row["seller"]:
            return {"ok": True, "msg": "Took down listing %d. %s x%d is in %s's delivery box." % (
                row["id"], _iname(row["name"]), quantity(row), row["seller_name"] or "the seller")}
        return {"ok": True, "msg": "Deleted %s; the item is gone." % _label(row)}

    def post(self, body, actor, now=None):
        """Post test listings: seller 0, which the auction_house_buy trigger pays nothing to."""
        now = int(time.time() if now is None else now)
        itemid = _int(body, "item", "The item", 1, 65534)
        it = self.store.item(itemid)
        if it is None:
            raise AuctionError("There is no item %d." % itemid)
        if not it["aH"]:
            raise AuctionError("%s has no auction house category, so nobody could find it there." % _iname(it["name"]))
        stack = bool(body.get("stack"))
        if stack and (it["stackSize"] or 1) <= 1:
            raise AuctionError("%s does not stack." % _iname(it["name"]))
        price = _int(body, "price", "The price", 1, MAX_PRICE)
        count = _int(body, "count", "How many listings", 1, MAX_TEST_LISTINGS, default=1)
        seller = str(body.get("seller") or "").strip() or "Test"
        if not _SELLER.match(seller):
            raise AuctionError("The seller's name is 2 to 15 letters, digits or dashes, starting with a letter.")
        what = "%s%s" % (_iname(it["name"]), " x%d" % it["stackSize"] if stack else "")
        detail = "%d test listing%s: %s at %d gil, seller %s" % (count, "" if count == 1 else "s", what, price, seller)
        if it["flags"] & C.FLAG_NO_AUCTION:
            detail += " (an item players cannot list)"
        self.store.post(itemid, stack, price, seller, count, actor, now, detail)
        return {"ok": True, "msg": "Posted %s. Buying one pays nobody; take them down when done." % detail}


#: POST routes -> Auction methods (body, actor).
ACTIONS = {"/ah/price": "price", "/ah/remove": "remove", "/ah/post": "post"}


def open_store(environ=os.environ):
    """(Auction, None) or (None, why)."""
    store, why = Store.from_env(environ)
    return (Auction(store), None) if store else (None, why)


#: The panel page, served by ffxi_fedtool.py at /ah (the panel's Games > FFXI > Auction house).
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFXI auction house</title>
<style>
:root{
  --bg:#0f1317;--panel:#161c23;--sunk:#0b0f13;--ink:#e5eaf1;--ink2:#98a4b4;
  --ink3:#66727f;--rule:#232d38;--accent:#74a8ea;--warn:#d59450;--good:#59ab80;
  --m:ui-monospace,"IBM Plex Mono",Consolas,monospace;
}
@media (prefers-color-scheme:light){
  :root{--bg:#edeff3;--panel:#fafbfd;--sunk:#e3e7ee;--ink:#171b21;--ink2:#4b5563;
        --ink3:#7b8798;--rule:#d5dae3;--accent:#2b5ea6;--warn:#96551a;--good:#256b4c;}
}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:16px 14px 60px;display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}
h1{font-size:1.1rem;margin:0;font-weight:600}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:5px;padding:13px 15px;min-width:0}
.hd{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:9px;flex-wrap:wrap}
.hd h2{font-size:.95rem;margin:0;font-weight:600}
.sub{font-size:12px;color:var(--ink3)}
button{font:inherit;font-size:13px;cursor:pointer;color:var(--ink);background:var(--sunk);
       border:1px solid var(--rule);border-radius:4px;padding:4px 10px;white-space:nowrap}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.go{border-color:var(--good);color:var(--good)}
button.rm:hover:not(:disabled){border-color:var(--warn);color:var(--warn)}
input,select{font:inherit;font-size:13px;background:var(--sunk);color:var(--ink);
  border:1px solid var(--rule);border-radius:4px;padding:5px 8px;width:100%;min-width:0}
label{font-size:12px;color:var(--ink2);display:grid;gap:3px}
.pill{display:inline-block;border:1px solid var(--rule);border-radius:10px;padding:0 8px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill.on{border-color:var(--good);color:var(--good)}
.pill.acc{border-color:var(--accent);color:var(--accent)}
#sstate{white-space:normal;max-width:100%}
.msg{font-size:13px;border-radius:4px;padding:6px 9px;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good);color:var(--good)}
.msg.bad{border-color:var(--warn);color:var(--warn)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11.5px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
td.n{font-family:var(--m);font-size:12.5px;white-space:nowrap}
tr.pick{cursor:pointer}
tr.pick:hover td{background:var(--sunk)}
.acts{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.grid{display:grid;gap:8px;grid-template-columns:minmax(0,1fr)}
@media(min-width:760px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}.grid .w2{grid-column:span 2}}
.row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:end}
.row label{flex:1 1 140px}
.facts{display:grid;gap:6px 18px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));font-size:13px}
.facts b{font-weight:600;color:var(--ink2);font-size:12px;display:block}
.empty{color:var(--ink3);font-size:13px}
.vars,.heavy{max-height:320px;overflow:auto}
.checks{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:10px}
label.ck{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink)}
label.ck input{width:auto}
.flags{display:grid;gap:8px}
.flag{display:flex;gap:10px;align-items:flex-start}
.flag input{width:auto;margin-top:4px}
.flag b{font-weight:600;font-size:13px}
tr.diff td{background:color-mix(in srgb, var(--warn) 12%, transparent)}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
}
td.r{text-align:right}
td input{width:110px}
@media(max-width:700px){td.r{text-align:left}}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>Auction house</h1><span id="counts" class="sub"></span></div>
    <div class="sub">Changes show in game at once, with the seller and buyers logged in. Only listings still for sale can be changed.
      Return to seller puts the item in the seller's delivery box, as an expired listing does. Delete takes it down and the item is gone.</div>
    <div class="row">
      <label>Show listings for an item (name or id) or a seller<input id="q" autocomplete="off" maxlength="40" placeholder="Hi-Potion, 4116, Arbor"></label>
      <button id="find">Show</button>
    </div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
  </div>

  <div class="card">
    <div class="hd"><h2>For sale</h2><span class="sub" id="sellnote"></span></div>
    <div class="tw vars"><table><thead><tr><th>Listed (UTC)</th><th>Item</th><th>Seller</th><th>Price</th><th></th></tr></thead>
      <tbody id="selling"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Post test listings</h2><span class="sub">No seller, like the server's own Auctioneer stock: buying one takes the buyer's gil and pays no one.</span></div>
    <div class="row">
      <label>Item (name or id)<input id="p_q" autocomplete="off" maxlength="40" placeholder="Hi-Potion, 4116"></label>
      <button id="p_find">Find</button>
    </div>
    <div class="row" id="p_pick" hidden>
      <label style="flex:2 1 260px">Item<select id="p_item"></select></label>
      <label style="flex:0 1 130px">Price (gil)<input id="p_price" inputmode="numeric"></label>
      <label style="flex:0 1 90px">Listings<input id="p_count" inputmode="numeric" value="1"></label>
      <label style="flex:0 1 150px">Seller's name<input id="p_seller" maxlength="15" value="Test"></label>
      <label class="ck" style="flex:0 0 auto"><input type="checkbox" id="p_stack"> As a stack</label>
      <button id="p_go" class="go">Post</button>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Sold</h2><span class="sub">Newest first. Read only: the price history buyers see comes from these rows.</span></div>
    <div class="tw vars"><table><thead><tr><th>Sold (UTC)</th><th>Item</th><th>Seller</th><th>Buyer</th><th>Paid</th></tr></thead>
      <tbody id="sold"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Log</h2><span class="sub">Newest first.</span></div>
    <div class="tw"><table><thead><tr><th>When (UTC)</th><th>Who</th><th>Change</th></tr></thead><tbody id="log"></tbody></table></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = t => !t ? '' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');
const iname = n => String(n ?? '?').replace(/_/g, ' ');
const gil = n => Number(n).toLocaleString('en-US');
const qty = r => r.stack ? r.stack_size : 1;
const what = r => `${esc(iname(r.name))}${qty(r) > 1 ? ' x' + qty(r) : ''} <span class="sub">${r.itemid}</span>`;
let S = null, BUSY = false;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }
async function getj(path){ const r = await fetch(qs(path)); const j = await r.json(); if(!r.ok) throw new Error(j.error || r.statusText); return j; }

async function load(){
  try{ S = await getj('ah.json?q=' + encodeURIComponent(q('#q').value.trim())); draw(); }catch(e){ say('Could not load: ' + e.message, false); }
}
function draw(){
  const c = S.counts;
  q('#counts').textContent = `${gil(c.selling)} for sale (${gil(c.house)} with no seller), ${gil(c.sold)} sold`;
  q('#sellnote').textContent = S.q ? 'matching ' + S.q : 'newest first, up to 200';
  q('#selling').innerHTML = S.selling.length ? S.selling.map(r => `<tr>
    <td class="n">${when(r.date)}</td><td>${what(r)}</td>
    <td>${esc(r.seller_name || '')}${r.seller ? ' <span class="sub">' + r.seller + '</span>' : ' <span class="pill">no seller</span>'}</td>
    <td class="n r">${gil(r.price)}</td>
    <td><span class="acts"><input data-pricefor="${r.id}" inputmode="numeric" value="${r.price}" aria-label="New price">
      <button data-price="${r.id}">Set price</button>
      ${r.seller ? `<button data-return="${r.id}">Return to seller</button>` : ''}
      <button class="rm" data-delete="${r.id}" data-name="${esc(iname(r.name))}">Delete</button></span></td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">Nothing for sale' + (S.q ? ' matching that.' : '.') + '</td></tr>';
  q('#sold').innerHTML = S.sold.length ? S.sold.map(r => `<tr>
    <td class="n">${when(r.sell_date)}</td><td>${what(r)}</td>
    <td>${esc(r.seller_name || '')}${r.seller ? '' : ' <span class="pill">no seller</span>'}</td>
    <td>${esc(r.buyer_name)} <span class="sub">${r.buyer}</span></td><td class="n r">${gil(r.sale)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No sales' + (S.q ? ' matching that.' : ' yet.') + '</td></tr>';
  q('#log').innerHTML = S.log.length ? S.log.map(e => `<tr><td class="n">${when(e.at)}</td><td>${esc(e.who)}</td>
    <td>${esc(e.detail)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">Nothing changed yet.</td></tr>';
}

async function post(path, body, confirmText){
  if(BUSY) return;
  if(confirmText && !confirm(confirmText)) return;
  BUSY = true;
  try{
    const r = await fetch(qs(path), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    await load();
    say(j.msg || (j.ok ? 'Done.' : 'Refused.'), !!j.ok);
  }catch(e){ say('Failed: ' + e.message, false); }
  BUSY = false;
}

let ITEMS = [];
async function findItem(){
  const text = q('#p_q').value.trim();
  if(!text) return;
  try{
    ITEMS = (await getj('ah/items.json?q=' + encodeURIComponent(text))).rows;
    q('#p_pick').hidden = !ITEMS.length;
    if(!ITEMS.length) return say('No item by that name or id.', false);
    q('#p_item').innerHTML = ITEMS.map(r => `<option value="${r.itemid}">${esc(iname(r.name))} (${r.itemid})${r.stackSize > 1 ?
      ', stacks to ' + r.stackSize : ''}${r.aH ? '' : ', no AH category'}${r.flags & 0x40 ? ', players cannot list' : ''}</option>`).join('');
    pickItem();
    say('', true);
  }catch(e){ say(e.message, false); }
}
function pickItem(){
  const it = ITEMS.find(r => r.itemid === +q('#p_item').value);
  const stacks = it && it.stackSize > 1;
  q('#p_stack').disabled = !stacks;
  if(!stacks) q('#p_stack').checked = false;
}

q('#find').onclick = load;
q('#q').onkeydown = e => { if(e.key === 'Enter') load(); };
q('#p_find').onclick = findItem;
q('#p_q').onkeydown = e => { if(e.key === 'Enter') findItem(); };
q('#p_item').onchange = pickItem;
q('#p_go').onclick = () => post('ah/post', {item: +q('#p_item').value, price: q('#p_price').value.trim(), count: q('#p_count').value.trim(),
  seller: q('#p_seller').value.trim(), stack: q('#p_stack').checked});
document.addEventListener('click', e => {
  const t = e.target.closest('[data-price],[data-return],[data-delete]');
  if(!t) return;
  const d = t.dataset;
  if(d.price) return post('ah/price', {id: +d.price, price: q(`[data-pricefor="${d.price}"]`).value.trim()});
  if(d.return) return post('ah/remove', {id: +d.return, return: true}, 'Take listing ' + d.return + ' down and return the item to its seller?');
  if(d.delete) return post('ah/remove', {id: +d.delete, return: false}, 'Delete listing ' + d.delete + ' (' + d.name + ')? The item does not go back to the seller.');
});
load();
</script>
</body></html>
"""
