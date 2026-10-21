"""The FFXI seasonal events for the admin panel (Games > FFXI > Seasonal events), served by ffxi_fedtool.py.

Each festival the map-server module runs (not part of stock LandSandBoat) has a GM switch, a
server variable named like [Starlight]Mode: 0 or no row follows the calendar, 1 forces the
festival on, 2 forces it off. The festival's own GM command (!starlight on | off | auto) sets
the same variable. This page sets it in the server_variables table directly. The map reads the
table on every GetServerVariable call (src/map/utils/serverutils.cpp) and each festival re-checks
its switch at least once a minute, so a change reaches every map process within a minute, with
no restart. The switch is server-wide: forcing a festival on opens it to every player, rewards
included, until it is set back to the calendar.

The dates shown are the game's own, from openlobby_game_calendar (the map module's seasonal
rows), for the festivals it lists. A festival it does not list shows its dates as text.

Harvest Festival and the Egg Hunt follow LSB's calendar and settings/main.lua only, with no
server variable to switch, so they are not here.

  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for the notices tool
"""
import json
import os
import time

import ffxi_chars as C

MODE_CALENDAR, MODE_ON, MODE_OFF = 0, 1, 2
MODES = {"auto": MODE_CALENDAR, "on": MODE_ON, "off": MODE_OFF}
MODE_NAMES = {MODE_CALENDAR: "auto", MODE_ON: "on", MODE_OFF: "off"}

#: Every festival with a switch, in calendar order. `var` is the server variable the module reads,
#: `calendar` the prefix of its rows in openlobby_game_calendar (None: the calendar does not list it),
#: `command` its GM command in game.
FESTIVALS = [
    {"id": "new-year", "name": "New Year", "var": "[NewYear]Mode", "when": "Jan 1 - 15",
     "calendar": "new-year-", "command": "!newyear"},
    {"id": "valentione", "name": "Valentione's Day", "var": "[VD]Mode", "when": "Feb 1 - 14",
     "calendar": None, "command": "!valentione"},
    {"id": "doll-festival", "name": "Doll Festival", "var": "[Doll]Mode", "when": "Feb 19 - Mar 4",
     "calendar": "doll-festival-", "command": "!dollfest"},
    {"id": "feast-of-swords", "name": "Feast of Swords", "var": "[FoS]Mode", "when": "Apr 24 - May 8",
     "calendar": "feast-of-swords-", "command": "!feastofswords"},
    {"id": "appreciation", "name": "Adventurer Appreciation Campaign", "var": "[AAC]Mode", "when": "May 16 - Jun 2",
     "calendar": None, "command": "!appreciation"},
    {"id": "celestial-nights", "name": "Celestial Nights", "var": "[CelestialNights]Mode", "when": "Jun 30 - Jul 14",
     "calendar": "celestial-nights-", "command": "!celestial"},
    {"id": "sunbreeze-festival", "name": "Sunbreeze Festival", "var": "[Sunbreeze]Mode", "when": "Aug 11 - 31",
     "calendar": "sunbreeze-festival-", "command": "!sunbreeze"},
    {"id": "blazing-buffaloes", "name": "Blazing Buffaloes", "var": "[Buffalo]Mode", "when": "Sep 25 - Oct 9",
     "calendar": "blazing-buffaloes-", "command": "!buffalo"},
    {"id": "lilies", "name": "Procession of the Lilies", "var": "[Lilies]Mode",
     "when": "with the Harvest Festival, Oct 20 - Nov 1", "calendar": "harvest-festival-", "command": "!lilies"},
    {"id": "starlight", "name": "Starlight Celebration", "var": "[Starlight]Mode", "when": "December",
     "calendar": "lsb-StarlightCelebration-", "command": "!starlight",
     "note": "LSB's own music and decorations stay with December whatever the switch says."},
    {"id": "smilebringer", "name": "Smilebringer Boot Camp", "var": "[SBC]Mode",
     "when": "with the Starlight Celebration", "calendar": "lsb-StarlightCelebration-", "command": None,
     "note": "On gives the sergeants their Starlight run, off their off-season one."},
]
BY_ID = {f["id"]: f for f in FESTIVALS}

LOG_DDL = ("CREATE TABLE IF NOT EXISTS openlobby_eventtool_log (id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY, "
           "at INT UNSIGNED NOT NULL, who VARCHAR(64) NOT NULL, festival VARCHAR(32) NOT NULL, "
           "detail VARCHAR(512) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
#: How far back a window is still shown (it may have just ended) and how far ahead the next one is looked for.
BEHIND = 2 * 86400
AHEAD = 400 * 86400


class EventsError(Exception):
    """A request the panel should show back as it is."""


class Store(C.Store):
    """server_variables, openlobby_game_calendar and the page's log, over the Characters page's settings."""

    def _setup(self):
        if not self._ready:
            self._run(LOG_DDL)
            self._ready = True

    def modes(self):
        """{variable: value} for the festival switches that have a row (no row is the calendar)."""
        names = [f["var"] for f in FESTIVALS]
        rows = self._run("SELECT name, value, expiry FROM server_variables WHERE name IN (%s)" %
                         ", ".join(["%s"] * len(names)), tuple(names), fetch=True)
        now = int(time.time())
        # An expired row reads as 0 in the map (serverutils GetServerVar), whatever its value.
        return {r["name"]: int(r["value"]) for r in rows if not (r["expiry"] and r["expiry"] <= now)}

    def windows(self, now):
        """The seasonal calendar's windows from BEHIND ago to AHEAD; [] before the map has made the table."""
        try:
            return self._run("SELECT event_key, name, starts_at, ends_at, detail FROM openlobby_game_calendar "
                             "WHERE source = 'seasonal' AND ends_at > %s AND starts_at < %s ORDER BY starts_at",
                             (int(now) - BEHIND, int(now) + AHEAD), fetch=True)
        except Exception as exc:
            if (getattr(exc, "args", None) or (None,))[0] == 1146:
                return []
            raise

    def log(self, limit=60):
        self._setup()
        return self._run("SELECT id, at, who, festival, detail FROM openlobby_eventtool_log ORDER BY id DESC LIMIT %s",
                         (int(limit),), fetch=True)

    def set_mode(self, var, value, festival, who, now, detail):
        """Write one switch and its log line in one transaction. 0 deletes the row, as the map does."""
        self._setup()
        conn = self._connect(autocommit=False)
        try:
            with conn.cursor() as cur:
                if value == MODE_CALENDAR:
                    cur.execute("DELETE FROM server_variables WHERE name = %s", (var,))
                else:
                    cur.execute("INSERT INTO server_variables (name, value, expiry) VALUES (%s, %s, 0) "
                                "ON DUPLICATE KEY UPDATE value = %s, expiry = 0", (var, int(value), int(value)))
                cur.execute("INSERT INTO openlobby_eventtool_log (at, who, festival, detail) VALUES (%s, %s, %s, %s)",
                            (int(now), (who or "admin")[:64], festival[:32], detail[:512]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _window(festival, windows, now):
    """(the window running at `now` or else the next one, whether one runs now) from the calendar rows.
    None for the second when the calendar cannot say: it does not list the festival, or the map has
    published nothing yet."""
    prefix = festival["calendar"]
    if not prefix or not windows:
        return None, None
    mine = [w for w in windows if str(w["event_key"]).startswith(prefix)]
    for w in mine:
        if w["starts_at"] <= now < w["ends_at"]:
            return _shown(w), True
    ahead = [w for w in mine if w["starts_at"] > now]
    if ahead:
        return _shown(ahead[0]), False
    return None, False


def _shown(w):
    """A window for the page. The calendar scans a limited span ahead, so a window cut at its edge
    (detail openEnded) has no known end and the page shows only its start."""
    try:
        open_ended = bool(json.loads(w.get("detail") or "{}").get("openEnded"))
    except (ValueError, AttributeError):
        open_ended = False
    return {"starts": int(w["starts_at"]), "ends": None if open_ended else int(w["ends_at"])}


class Events:
    """What the page shows and does, over a Store (or a test's stand-in with the same methods)."""

    def __init__(self, store):
        self.store = store

    def state(self, query=None, now=None):
        now = int(time.time() if now is None else now)
        modes = self.store.modes()
        windows = self.store.windows(now)
        rows = []
        for f in FESTIVALS:
            value = modes.get(f["var"], MODE_CALENDAR)
            window, in_season = _window(f, windows, now)
            if value == MODE_ON:
                running = True
            elif value == MODE_OFF:
                running = False
            else:
                running = in_season
            rows.append({"id": f["id"], "name": f["name"], "when": f["when"], "command": f["command"],
                         "note": f.get("note") or "", "mode": MODE_NAMES.get(value, "auto"), "value": value,
                         "running": running, "in_season": in_season, "window": window})
        return {"now": now, "festivals": rows, "log": self.store.log()}

    def set(self, body, actor, now=None):
        now = int(time.time() if now is None else now)
        f = BY_ID.get(str(body.get("id") or ""))
        if f is None:
            raise EventsError("There is no festival %r here." % str(body.get("id") or "")[:40])
        mode = str(body.get("mode") or "")
        if mode not in MODES:
            raise EventsError("The switch is on, off or auto.")
        value = MODES[mode]
        before = self.store.modes().get(f["var"], MODE_CALENDAR)
        if before == value:
            return {"ok": True, "msg": "%s is already %s." % (f["name"], _says(value))}
        detail = "%s: %s -> %s" % (f["name"], MODE_NAMES.get(before, before), mode)
        self.store.set_mode(f["var"], value, f["id"], actor, now, detail)
        return {"ok": True, "msg": "%s is now %s. Every map process follows within a minute." % (f["name"], _says(value))}


def _says(value):
    return {MODE_ON: "forced on", MODE_OFF: "forced off"}.get(value, "following the calendar")


#: POST routes -> Events methods (body, actor).
ACTIONS = {"/events/set": "set"}


def open_store(environ=os.environ):
    """(Events, None) or (None, why)."""
    store, why = Store.from_env(environ)
    return (Events(store), None) if store else (None, why)


#: The panel page, served by ffxi_fedtool.py at /events (the panel's Games > FFXI > Seasonal events).
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFXI seasonal events</title>
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
.seg{display:inline-flex}
.seg button{border-radius:0;margin-left:-1px}
.seg button:first-child{border-radius:4px 0 0 4px;margin-left:0}
.seg button:last-child{border-radius:0 4px 4px 0}
.seg button.cur{border-color:var(--accent);color:var(--accent);position:relative}
.seg button.cur.on{border-color:var(--good);color:var(--good)}
.seg button.cur.off{border-color:var(--warn);color:var(--warn)}
.pill{display:inline-block;border:1px solid var(--rule);border-radius:10px;padding:0 8px;font-size:12px;color:var(--ink2);white-space:nowrap}
.pill.on{border-color:var(--good);color:var(--good)}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.msg{font-size:13px;border-radius:4px;padding:6px 9px;border:1px solid var(--rule)}
.msg.ok{border-color:var(--good);color:var(--good)}
.msg.bad{border-color:var(--warn);color:var(--warn)}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 7px;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-size:11.5px;font-weight:600;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em}
td.n{font-family:var(--m);font-size:12.5px;white-space:nowrap}
.empty{color:var(--ink3);font-size:13px}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>Seasonal events</h1><span class="sub">Dates are JST, as in game</span></div>
    <div class="sub">Each switch is the one the festival's GM command sets, and it is server-wide. On opens the festival to every
      player, rewards included, until it goes back to auto. Every map process follows within a minute; nobody needs to zone.</div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
  </div>

  <div class="card">
    <div class="tw"><table><thead><tr><th>Festival</th><th>Season</th><th>Now</th><th>Switch</th></tr></thead>
      <tbody id="rows"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Log</h2><span class="sub">Newest first. Changes made in game with the GM commands are not listed.</span></div>
    <div class="tw"><table><thead><tr><th>When (UTC)</th><th>Who</th><th>Change</th></tr></thead><tbody id="log"></tbody></table></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = t => !t ? '' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ');
// A window runs from 00:00 JST of its first day to 00:00 JST after its last.
const jstDay = t => new Date((t + 9 * 3600) * 1000).toLocaleDateString('en-US', {timeZone: 'UTC', month: 'short', day: 'numeric'});
const span = w => w.ends ? jstDay(w.starts) + ' - ' + jstDay(w.ends - 86400) : 'from ' + jstDay(w.starts);
let S = null, BUSY = false;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }

async function load(){
  try{
    const r = await fetch(qs('events.json'));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || r.statusText);
    S = j; draw();
  }catch(e){ say('Could not load: ' + e.message, false); }
}

function now(f){
  if(f.running === null) return f.mode === 'auto' ? '<span class="pill">follows its dates</span>' : '';
  const why = f.mode === 'on' ? ' (forced)' : f.mode === 'off' ? (f.in_season ? ' (forced, in season)' : ' (forced)') : '';
  return (f.running ? '<span class="pill on">running</span>' : '<span class="pill">not running</span>') +
    (why ? ` <span class="sub">${why.trim()}</span>` : '');
}
function season(f){
  let s = esc(f.when);
  if(f.window) s += `<div class="sub">${f.in_season ? 'now' : 'next'}: ${span(f.window)}</div>`;
  if(f.note) s += `<div class="sub">${esc(f.note)}</div>`;
  return s;
}
function draw(){
  q('#rows').innerHTML = S.festivals.map(f => `<tr>
    <td>${esc(f.name)}${f.command ? `<div class="sub">${esc(f.command)} in game</div>` : ''}</td>
    <td>${season(f)}</td><td>${now(f)}</td>
    <td><span class="seg">${['auto', 'on', 'off'].map(m => `<button data-id="${esc(f.id)}" data-mode="${m}"
      data-name="${esc(f.name)}" class="${f.mode === m ? 'cur ' + m : ''}">${m === 'auto' ? 'Auto' : m === 'on' ? 'On' : 'Off'}</button>`).join('')}</span></td>
    </tr>`).join('');
  q('#log').innerHTML = S.log.length ? S.log.map(e => `<tr><td class="n">${when(e.at)}</td><td>${esc(e.who)}</td>
    <td>${esc(e.detail)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">Nothing changed from this page yet.</td></tr>';
}

async function set(id, mode, name){
  if(BUSY) return;
  const f = S.festivals.find(x => x.id === id);
  if(f && f.mode === mode) return;
  if(mode === 'on' && !confirm('Turn ' + name + ' on for every player now? It stays on until you set it back to Auto.')) return;
  if(mode === 'off' && f && f.in_season && !confirm(name + ' is in season. Stop it for every player now?')) return;
  BUSY = true; document.querySelectorAll('button').forEach(b => b.disabled = true);
  try{
    const r = await fetch(qs('events/set'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, mode})});
    const j = await r.json();
    say(j.msg || (j.ok ? 'Done.' : 'Refused.'), !!j.ok);
  }catch(e){ say('Failed: ' + e.message, false); }
  BUSY = false; await load();
}

document.addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if(b) set(b.dataset.id, b.dataset.mode, b.dataset.name);
});
load();
setInterval(() => { if(!BUSY && document.visibilityState === 'visible') load(); }, 30000);
</script>
</body></html>
"""
