"""In-game notices for FFXI: the login message lines and the system-chat announcements.

LSB's map server needs a notices module (not part of stock LandSandBoat, and not in this
repository) that reads one table in the LSB database and does the in-game half:

  kind 'motd'      a line added to the server message a player sees at login (and on
                   /servmes) while starts_at <= now < ends_at (NULL = no end).
  kind 'announce'  sent once, as system chat, to everyone online when starts_at comes. The
                   module claims a row by setting sent_at before sending it. A row it only
                   sees at or after ends_at (NULL = starts_at + 600) is claimed and skipped,
                   which is also how a pending announcement is cancelled from here.

This module is the writing half, used by the bridge:

  - the admin panel's Notices tool (served by ffxi_fedtool.py): post an announcement now or
    later, a maintenance countdown, a manual login line;
  - the calendar sync, a thread that keeps the login message and the announcements in step
    with the event calendar the PlayOnline side runs on (OpenLobby's eventcal.py, the same
    calendar Tetra Master and JongHoLow read and eventremind.py posts to Discord from), and
    with FFXI's own calendar, which the map server publishes.

The sync owns every row tagged calendar:*. It keeps one login line per game
(calendar:motd:<game>) and inserts its announcements ahead of time with INSERT IGNORE on
their tag (calendar:start:<window id>, calendar:remind:<window id>:<minutes>), so a re-run
or a restart never queues one twice and an announcement already sent is never re-sent. A
pending calendar announcement whose window left the calendar is deleted; one cancelled in
the panel stays in the table, so the next pass does not put it back.

The map sends an announcement whose tag starts with one of its urgent prefixes
(admin:down:, admin:urgent:, deploy:) in the urgent chat colour and every other one in the
news colour, so the panel's countdown and its urgent announcements are the only rows here
in the urgent one.

The module polls every 15 s, so nothing here needs to be exact to the second. Times are
unix seconds and shown as UTC. The text is ASCII; the module maps typographic punctuation
and drops anything else it cannot draw.

  FFXI_NOTICES=0             everything here off (the panel tool says so)
  FFXI_NOTICES_CALENDAR=0    the calendar sync off; the panel tool still works
  FFXI_NOTICES_GAMES         calendar sections to follow (tm,jan)
  FFXI_NOTICES_PERIOD        seconds between sync passes (300); a pass also runs at the next
                             start or end it knows of
  FFXI_NOTICES_REMIND_MIN    minutes before a window its reminders go out (180,15)
  FFXI_NOTICES_NEAR_H        hours before a start the login line names the next event (24)
  FFXI_NOTICES_FFXI=0        leave FFXI's own calendar out (campaigns, login campaign,
                             festivals; see "FFXI's own calendar" below)
  FFXI_NOTICES_DIGEST_UTC    UTC hours of the "Now in Vana'diel" shout (2,14)
  LSB_DB_HOST/PORT/NAME/USER/PASSWORD   the LSB database, as for the rest of the bridge;
                             without a password set this module stays off
"""
import calendar
import importlib
import os
import re
import secrets
import sys
import threading
import time

TABLE = "openlobby_notices"

#: The table as the map module creates it. Either side may be first.
DDL = """CREATE TABLE IF NOT EXISTS openlobby_notices (
  id INT UNSIGNED NOT NULL AUTO_INCREMENT,
  kind ENUM('motd', 'announce') NOT NULL,
  tag VARCHAR(96) NULL,
  message VARCHAR(1024) NOT NULL,
  starts_at INT UNSIGNED NOT NULL DEFAULT 0,
  ends_at INT UNSIGNED NULL,
  sent_at INT UNSIGNED NULL,
  created_by VARCHAR(64) NULL,
  created_at INT UNSIGNED NOT NULL DEFAULT 0,
  sort INT NOT NULL DEFAULT 0,
  PRIMARY KEY (id), UNIQUE KEY tag (tag), KEY due (kind, sent_at, starts_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

#: The module's default life of an announce with no ends_at (NOTICES_ANNOUNCE_TTL).
ANNOUNCE_TTL = 600
#: A login line has room for about this much before it wraps badly.
MOTD_MAX = 120
#: An announcement this long is three chat lines; anything longer is a mail, not a shout.
ANNOUNCE_MAX = 440
#: The game's name in the text players read (eventnews.GAMES uses the same names).
GAME_NAMES = {"tm": "Tetra Master", "jan": "JongHoLow"}
CALENDAR_TAG = "calendar:"
#: An announcement tagged with this goes out in the map's urgent chat colour.
URGENT_TAG = "admin:urgent:"
DAY = 86400


class NoticeError(Exception):
    """A request the panel should show back as it is."""


def _env_list(environ, name, default):
    raw = (environ.get(name) or "").strip() or default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_num(environ, name, default):
    try:
        return float((environ.get(name) or "").strip() or default)
    except ValueError:
        return float(default)


def utc(t, fmt="%a %H:%M"):
    return time.strftime(fmt, time.gmtime(t)) + " UTC"


def clean(text, limit):
    """One line of text, whitespace collapsed, at most `limit` characters."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise NoticeError("The text is empty.")
    if len(text) > limit:
        raise NoticeError("Keep it to %d characters (this is %d)." % (limit, len(text)))
    return text


def parse_time(value, now):
    """A start time from the panel: empty = now, unix seconds, or 'YYYY-MM-DDTHH:MM' (UTC,
    what a datetime-local input sends)."""
    if value in (None, "", 0):
        return int(now)
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return calendar.timegm(time.strptime(s, fmt))
        except ValueError:
            pass
    raise NoticeError("Give the time as YYYY-MM-DD HH:MM (UTC).")


# -- the table ----------------------------------------------------------------------------------

class Store:
    """openlobby_notices over pymysql. A connection per call: the sync runs every few minutes
    and the panel now and then, and a held connection would only add a reconnect path."""

    def __init__(self, host, port, user, password, database):
        self._args = dict(host=host, port=int(port), user=user, password=password,
                          database=database, autocommit=True, connect_timeout=5,
                          charset="utf8mb4")
        self._ready = False

    @classmethod
    def from_env(cls, environ=os.environ):
        """A Store, or (None, why) when there is nothing to connect with."""
        if not (environ.get("LSB_DB_PASSWORD") or "").strip():
            return None, "LSB_DB_PASSWORD is not set"
        try:
            import pymysql  # noqa: F401
        except ImportError:
            return None, "pymysql is not in this image"
        return cls(environ.get("LSB_DB_HOST") or "db", environ.get("LSB_DB_PORT") or 3306,
                   environ.get("LSB_DB_USER") or "xiadmin", environ.get("LSB_DB_PASSWORD"),
                   environ.get("LSB_DB_NAME") or "xidb"), None

    def _run(self, sql, args=(), fetch=False):
        import pymysql
        conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self._args)
        try:
            with conn.cursor() as cur:
                if not self._ready:
                    cur.execute(DDL)
                    self._ready = True
                n = cur.execute(sql, args)
                return cur.fetchall() if fetch else n
        finally:
            conn.close()

    def rows(self, now):
        """The login lines that are on or still to come (and those ended in the last day),
        and the announcements pending or sent in the last two days."""
        return list(self._run(
            "SELECT id, kind, tag, message, starts_at, ends_at, sent_at, created_by, created_at, sort "
            "FROM openlobby_notices WHERE (kind = 'motd' AND (ends_at IS NULL OR ends_at > %s)) "
            "OR (kind = 'announce' AND (sent_at IS NULL OR sent_at > %s)) "
            "ORDER BY kind, starts_at, sort, id LIMIT 300",
            (int(now) - DAY, int(now) - 2 * DAY), fetch=True))

    def insert(self, row):
        """INSERT IGNORE: 1 when added, 0 when its tag is already there."""
        return self._run(
            "INSERT IGNORE INTO openlobby_notices "
            "(kind, tag, message, starts_at, ends_at, created_by, created_at, sort) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (row["kind"], row.get("tag"), row["message"], int(row["starts_at"]),
             row.get("ends_at"), row.get("created_by"), int(row.get("created_at") or time.time()),
             int(row.get("sort") or 0)))

    def put_motd(self, row):
        """Insert or rewrite the login line with this tag: 0 unchanged, otherwise changed."""
        return self._run(
            "INSERT INTO openlobby_notices "
            "(kind, tag, message, starts_at, ends_at, created_by, created_at, sort) "
            "VALUES ('motd', %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE message = VALUES(message), starts_at = VALUES(starts_at), "
            "ends_at = VALUES(ends_at), sort = VALUES(sort)",
            (row["tag"], row["message"], int(row["starts_at"]), row.get("ends_at"),
             row.get("created_by"), int(row.get("created_at") or time.time()),
             int(row.get("sort") or 0)))

    def delete_tag(self, tag):
        return self._run("DELETE FROM openlobby_notices WHERE tag = %s AND (kind = 'motd' OR sent_at IS NULL)",
                         (tag,))

    def pending_calendar(self, now):
        """(id, tag) of every calendar announcement still to come."""
        return [(r["id"], r["tag"]) for r in self._run(
            "SELECT id, tag FROM openlobby_notices WHERE kind = 'announce' AND sent_at IS NULL "
            "AND starts_at > %s AND tag LIKE 'calendar:%%'", (int(now),), fetch=True)]

    def delete_id(self, rid):
        """Remove a login line, or an announcement not sent yet."""
        return self._run("DELETE FROM openlobby_notices WHERE id = %s AND (kind = 'motd' OR sent_at IS NULL)",
                         (int(rid),))

    def cancel_id(self, rid):
        """Stop an announcement from going out while keeping its row (and its tag)."""
        return self._run("UPDATE openlobby_notices SET ends_at = starts_at "
                         "WHERE id = %s AND kind = 'announce' AND sent_at IS NULL", (int(rid),))

    def game_calendar(self, now, behind=DAY, ahead=3 * DAY):
        """FFXI's calendar rows around `now`; none while the map has not made the table."""
        try:
            return list(self._run(FFXI_CALENDAR_SQL, (int(now) - behind, int(now) + ahead), fetch=True))
        except Exception as exc:                 # noqa: BLE001 -- pymysql's error classes
            if (getattr(exc, "args", None) or (None,))[0] == 1146:
                return []
            raise

    def motd_tags(self, prefix):
        """The tags of the login lines whose tag starts with `prefix`."""
        return [r["tag"] for r in self._run("SELECT tag FROM openlobby_notices WHERE kind = 'motd' AND tag LIKE %s",
                                            (prefix.replace("%", "") + "%",), fetch=True)]

    def end_motd(self, rid, now):
        return self._run("UPDATE openlobby_notices SET ends_at = %s WHERE id = %s AND kind = 'motd' "
                         "AND (ends_at IS NULL OR ends_at > %s)", (int(now), int(rid), int(now)))


def describe(row, now):
    """Where a row stands, for the panel."""
    if row["kind"] == "motd":
        if row.get("ends_at") is not None and row["ends_at"] <= now:
            return "ended"
        return "showing" if row["starts_at"] <= now else "scheduled"
    if row.get("sent_at") is not None:
        ends = row["ends_at"] if row.get("ends_at") is not None else row["starts_at"] + ANNOUNCE_TTL
        return "skipped" if row["sent_at"] >= ends else "sent"
    if row.get("ends_at") is not None and row["ends_at"] <= row["starts_at"]:
        return "cancelled"
    return "due" if row["starts_at"] <= now else "scheduled"


# -- the panel's actions ------------------------------------------------------------------------

def add_announce(store, text, at, actor, now, every_min=0, times=1, urgent=False):
    """One announcement, or `times` of it `every_min` minutes apart, in the news colour or
    (urgent) the maintenance one. Returns the starts."""
    text = clean(text, ANNOUNCE_MAX)
    try:
        every, times = int(every_min or 0), int(times or 1)
    except (TypeError, ValueError):
        raise NoticeError("Repeat needs whole numbers.")
    if times < 1 or times > 12 or (times > 1 and not 1 <= every <= 24 * 60):
        raise NoticeError("Repeat up to 12 times, 1 to 1440 minutes apart.")
    start = parse_time(at, now)
    if start < now - 60:
        raise NoticeError("That time has passed.")
    batch = secrets.token_hex(4)
    out = []
    for i in range(times):
        t = max(int(now), start + i * every * 60)
        store.insert({"kind": "announce", "tag": "%s%s:%d" % (URGENT_TAG if urgent else "admin:announce:", batch, i),
                      "message": text,
                      "starts_at": t, "ends_at": t + ANNOUNCE_TTL, "created_by": actor or None,
                      "created_at": int(now)})
        out.append(t)
    return out


#: Minutes before a shutdown the countdown speaks: every minute of the last five, so a
#: player who missed one warning gets the next a minute later.
COUNTDOWN_MIN = (30, 15, 10, 5, 4, 3, 2, 1)


def countdown_rows(down_at, now, leads=COUNTDOWN_MIN, actor=None):
    """The announcements of a countdown to `down_at`. Leads already past are left out; each
    row lapses at the shutdown, so a late one is never sent after it."""
    rows = []
    for m in sorted({int(x) for x in leads if int(x) > 0}, reverse=True):
        at = int(down_at) - m * 60
        if at < now - 30:
            continue
        rows.append({"kind": "announce", "tag": "admin:down:%d:%d" % (down_at, m),
                     "message": "The FFXI server goes down for maintenance in %d minute%s. "
                                "Please log out safely." % (m, "" if m == 1 else "s"),
                     "starts_at": max(int(now), at), "ends_at": int(down_at),
                     "created_by": actor or None, "created_at": int(now)})
    return rows


def add_countdown(store, down_at, actor, now, leads=COUNTDOWN_MIN):
    down = parse_time(down_at, now)
    if down <= now + 60:
        raise NoticeError("Pick a time at least a minute from now.")
    if down > now + 7 * DAY:
        raise NoticeError("Pick a time within the next week.")
    rows = countdown_rows(down, now, leads, actor)
    if not rows:
        raise NoticeError("Every warning time has already passed.")
    added = sum(store.insert(r) for r in rows)
    return down, rows, added


def add_motd(store, text, until, actor, now, sort=0):
    text = clean(text, MOTD_MAX)
    end = None
    if until not in (None, ""):
        end = parse_time(until, now)
        if end <= now:
            raise NoticeError("The end time has passed.")
    store.insert({"kind": "motd", "tag": "admin:motd:%s" % secrets.token_hex(6), "message": text,
                  "starts_at": int(now), "ends_at": end, "created_by": actor or None,
                  "created_at": int(now), "sort": sort})
    return end


# -- the calendar sync --------------------------------------------------------------------------

def _short_name(name):
    """A cup's name without the week number the calendar adds ("Weekend Cup, week 41")."""
    return re.sub(r",?\s*week\s+\d+\s*$", "", str(name or "Cup")).strip() or "Cup"


def _when(t, now):
    """'20:00 UTC' later today, else 'Sat 19:00 UTC'."""
    same = time.gmtime(t)[:3] == time.gmtime(now)[:3]
    return utc(t, "%H:%M") if same else utc(t)


def _lead_words(m):
    if m % 60 == 0:
        h = m // 60
        return "%d hour%s" % (h, "" if h == 1 else "s")
    return "%d minute%s" % (m, "" if m == 1 else "s")


def plan(now, cal, games, ec, remind_min=(180, 15), near_s=DAY):
    """What the table should hold for the calendar at `now`.

    -> {"motd": {game: row or None}, "announce": [rows], "next": unix time of the next start
    or end it knows of}. `ec` is the eventcal module. Pure: the test runs it on a fixed
    calendar and clock."""
    now = int(now)
    leads = sorted({int(m) for m in remind_min if int(m) > 0}, reverse=True)
    reach = now + int(near_s) + (max(leads) * 60 if leads else 0) + DAY
    motd, announce, edges = {}, [], []
    for game in games:
        label = GAME_NAMES.get(game, game)
        wins = ec.windows(game, now - DAY, reach, cal)
        edges += [t for w in wins for t in (w["start"], w["end"]) if t > now]
        cur = next((w for w in wins if w["start"] <= now < w["end"]), None)
        nxt = next((w for w in wins if w["start"] > now), None)
        tag = "calendar:motd:" + game
        row = None
        if cur:
            row = {"message": "Current Event: %s %s, until %s. Check community channels for details."
                              % (label, _short_name(cur.get("name")), _when(cur["end"], now)),
                   "starts_at": cur["start"], "ends_at": cur["end"]}
        elif nxt:
            # Between two sessions of a season already under way, or close to the next start.
            season = bool(nxt.get("season")) and nxt.get("season_start", nxt["start"]) <= now
            if season or nxt["start"] - now <= near_s:
                # starts_at only has to be past and the same on every pass, so a pass that
                # changes nothing writes nothing.
                row = {"message": "Next Event: %s %s, %s. Check community channels for details."
                                  % (label, _short_name(nxt.get("name")), _when(nxt["start"], now)),
                       "starts_at": nxt["season_start"] if season else max(0, nxt["start"] - int(near_s)),
                       "ends_at": nxt["start"]}
        if row:
            row.update(kind="motd", tag=tag, created_by="calendar", sort=-10 + len(motd))
            if len(row["message"]) > MOTD_MAX:
                row["message"] = row["message"].replace(" Check community channels for details.", "")
        motd[game] = row
        for w in wins:
            if w.get("remind") is False or w["end"] <= now:
                continue
            name = _short_name(w.get("name"))
            length = w["end"] - w["start"]
            if w["start"] + min(ANNOUNCE_TTL, length) > now:
                announce.append({"kind": "announce", "tag": "calendar:start:%s" % w["id"],
                                 "message": "%s: the %s starts now! Join from PlayOnline." % (label, name),
                                 "starts_at": w["start"], "ends_at": w["start"] + min(ANNOUNCE_TTL, length)})
            for m in leads:
                at = w["start"] - m * 60
                until = min(w["start"], at + ANNOUNCE_TTL)
                if until <= now:
                    continue
                announce.append({"kind": "announce", "tag": "calendar:remind:%s:%d" % (w["id"], m),
                                 "message": "%s: the %s starts in %s, at %s." % (label, name, _lead_words(m),
                                                                                utc(w["start"], "%H:%M")),
                                 "starts_at": at, "ends_at": until})
    for r in announce:
        r.update(created_by="calendar", created_at=now)
        edges.append(r["starts_at"])
    for r in motd.values():
        if r:
            r["created_at"] = now
    later = [t for t in edges if t > now]
    return {"motd": motd, "announce": announce, "next": min(later) if later else None}


def apply(store, p, now, horizon_s=2 * DAY):
    """Make the table match a plan. -> counts of what changed."""
    got = {"motd": 0, "motd_off": 0, "queued": 0, "dropped": 0}
    for game, row in p["motd"].items():
        if row:
            got["motd"] += 1 if store.put_motd(row) else 0
        else:
            got["motd_off"] += store.delete_tag("calendar:motd:" + game) or 0
    if "motd_ffxi" in p:
        keep = set()
        for row in p["motd_ffxi"]:
            keep.add(row["tag"])
            got["motd"] += 1 if store.put_motd(row) else 0
        for tag in store.motd_tags("calendar:motd:ffxi:"):
            if tag not in keep:
                got["motd_off"] += store.delete_tag(tag) or 0
    want = set()
    for row in p["announce"]:
        want.add(row["tag"])
        if row["starts_at"] <= now + horizon_s:
            got["queued"] += store.insert(row) or 0
    for rid, tag in store.pending_calendar(now):
        if tag not in want:
            got["dropped"] += store.delete_id(rid) or 0
    return got


# -- FFXI's own calendar ------------------------------------------------------------------------
#
# The map module's calendar (not part of stock LandSandBoat) publishes what the game itself has
# scheduled to openlobby_game_calendar: the Adventurer Campaign windows, the Repeat Login
# Campaign and the festivals, each worked out by the game's own code. The sync reads it straight
# from the LSB database and keeps, for every event:
#
#   calendar:motd:ffxi:<source>:<key>         a login line while it runs
#   calendar:ffxi:start:<source>:<key>        a shout when it starts
#   calendar:ffxi:closing:<source>:<key>      a shout a day before it ends (the login campaign:
#                                             before its points stop), for events of 3 days or more
#   calendar:ffxi:digest:<YYYYMMDDHH>         at each FFXI_NOTICES_DIGEST_UTC hour, one shout
#                                             naming everything that runs, for whoever missed the
#                                             start
#
# The Mog Bonanza is not here: it writes its own announcements.

FFXI_SOURCES = ("campaigns", "login", "seasonal")
FFXI_CALENDAR_SQL = ("SELECT source, event_key, name, starts_at, ends_at, detail FROM openlobby_game_calendar "
                     "WHERE ends_at > %s AND starts_at < %s ORDER BY starts_at, source, event_key")
FFXI_LOGIN_MOOGLES = "the Greeter Moogles in Port San d'Oria, Port Bastok and Windurst Walls"
#: Events shorter than this get no closing shout.
FFXI_CLOSING_MIN_S = 3 * DAY
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def ffxi_events(rows):
    """openlobby_game_calendar rows as events: {source, key, name, start, end, detail}."""
    import json
    out = []
    for r in rows:
        if r["source"] not in FFXI_SOURCES:
            continue
        try:
            detail = json.loads(r.get("detail") or "{}")
        except ValueError:
            detail = {}
        out.append({"source": r["source"], "key": str(r["event_key"]), "name": str(r.get("name") or ""),
                    "start": int(r["starts_at"]), "end": int(r["ends_at"]),
                    "detail": detail if isinstance(detail, dict) else {}})
    return out


def _at(t, now):
    """'15:00 UTC' later today, 'Sat 15:00 UTC' within the week, else 'Oct 31 15:00 UTC'."""
    g = time.gmtime(t)
    if g[:3] == time.gmtime(now)[:3]:
        return utc(t, "%H:%M")
    if 0 <= t - now < 6 * DAY:
        return utc(t)
    return "%s %d %s" % (_MONTHS[g.tm_mon - 1], g.tm_mday, utc(t, "%H:%M"))


def _and_list(names):
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return "%s and %s" % (", ".join(names[:-1]), names[-1])


def _fit(head, items, tail, limit=ANNOUNCE_MAX):
    """head + the items joined + tail, leaving items off ('and 3 more') until it fits."""
    items = list(items)
    for keep in range(len(items), -1, -1):
        shown = items[:keep] + (["%d more" % (len(items) - keep)] if keep < len(items) else [])
        text = head + _and_list(shown) + tail
        if len(text) <= limit:
            return text
    return (head + tail)[:limit]


def _cap(s):
    return s[:1].upper() + s[1:]


def _ffxi_name(ev):
    """The event as it reads mid-sentence: 'the October Login Campaign', 'the Harvest Festival'."""
    if ev["source"] == "campaigns":
        return "Adventurer Campaigns"
    if ev["source"] == "login":
        month = ev["detail"].get("month") or time.strftime("%B", time.gmtime(ev["start"]))
        return "the %s Login Campaign" % month
    name = ev["name"] or "festival"
    return name if name.lower().startswith("the ") else "the " + name


def _ffxi_ends(ev):
    """When the part players act on ends: the login campaign's points, else the event."""
    if ev["source"] == "login":
        return int(ev["detail"].get("earnEnd") or ev["end"])
    return ev["end"]


def _ffxi_motd(ev, now):
    name = _cap(_ffxi_name(ev))
    if ev["source"] == "login":
        earn = _ffxi_ends(ev)
        if now < earn:
            text = "Now on: %s. Earn points until %s." % (name, _at(earn, now))
        else:
            text = "Now on: %s. Trade your points until %s." % (name, _at(ev["end"], now))
    elif ev["detail"].get("openEnded"):
        text = "Now on: %s." % name
    else:
        text = "Now on: %s, until %s." % (name, _at(ev["end"], now))
    return text[:MOTD_MAX]


def _ffxi_start(ev):
    d, at = ev["detail"], ev["start"]
    if ev["source"] == "campaigns":
        tail = "."
        if d.get("plus"):
            tail += " Dedication and Commitment count double."
        return _fit("Adventurer Campaigns have begun and run until %s: " % _at(ev["end"], at),
                    [str(n) for n in d.get("campaigns") or []], tail)
    if ev["source"] == "login":
        return ("%s has begun! Log in each day until %s to earn points, and trade them with %s until %s."
                % (_cap(_ffxi_name(ev)), _at(_ffxi_ends(ev), at), FFXI_LOGIN_MOOGLES, _at(ev["end"], at)))
    text = "%s has begun!" % _cap(_ffxi_name(ev))
    if not d.get("openEnded"):
        text += " It runs until %s." % _at(ev["end"], at)
    blurb = str(d.get("blurb") or "")
    if blurb and len(text) + 1 + len(blurb) <= ANNOUNCE_MAX:
        text += " " + blurb
    return text


def _ffxi_closing(ev, at):
    if ev["source"] == "login":
        return ("%s: one day left to earn login points, until %s. Trade them with %s."
                % (_cap(_ffxi_name(ev)), _at(_ffxi_ends(ev), at), FFXI_LOGIN_MOOGLES))
    verb = "end" if ev["source"] == "campaigns" else "ends"
    return "%s %s in one day, at %s." % (_cap(_ffxi_name(ev)), verb, _at(ev["end"], at))


def _ffxi_digest(evs, at):
    def one(ev):
        if ev["source"] == "login":
            ends = _ffxi_ends(ev)
            if at < ends:
                return "%s (points until %s)" % (_ffxi_name(ev), _at(ends, at))
            return "%s (trade points until %s)" % (_ffxi_name(ev), _at(ev["end"], at))
        if ev["detail"].get("openEnded"):
            return _ffxi_name(ev)
        return "%s (until %s)" % (_ffxi_name(ev), _at(ev["end"], at))
    return _fit("Now in Vana'diel: ", [one(ev) for ev in evs], ". Type /servmes to see them again.")


def _running(events, t):
    return sorted((ev for ev in events if ev["start"] <= t < ev["end"]),
                  key=lambda ev: (ev["start"], ev["source"], ev["key"]))


def plan_ffxi(now, events, closing_s=DAY, digest_hours=(2, 14), horizon_s=2 * DAY):
    """What the table should hold for FFXI's own calendar at `now`: {"motd": [rows],
    "announce": [rows], "next": unix time of the next change}. Pure, like plan()."""
    now = int(now)
    motd, announce, edges = [], [], []
    for i, ev in enumerate(_running(events, now)):
        motd.append({"kind": "motd", "tag": "calendar:motd:ffxi:%s:%s" % (ev["source"], ev["key"]),
                     "message": _ffxi_motd(ev, now), "starts_at": ev["start"], "ends_at": ev["end"],
                     "created_by": "calendar", "created_at": now, "sort": -20 + i})
    for ev in events:
        edges += [t for t in (ev["start"], ev["end"], _ffxi_ends(ev)) if t > now]
        tag = "%s:%s" % (ev["source"], ev["key"])
        until = ev["start"] + min(ANNOUNCE_TTL, ev["end"] - ev["start"])
        if until > now:
            announce.append({"kind": "announce", "tag": "calendar:ffxi:start:" + tag,
                             "message": _ffxi_start(ev), "starts_at": ev["start"], "ends_at": until})
        ends = _ffxi_ends(ev)
        at = ends - int(closing_s)
        if (ev["end"] - ev["start"] >= FFXI_CLOSING_MIN_S and not ev["detail"].get("openEnded")
                and at > ev["start"] and min(at + ANNOUNCE_TTL, ends) > now):
            announce.append({"kind": "announce", "tag": "calendar:ffxi:closing:" + tag,
                             "message": _ffxi_closing(ev, at), "starts_at": at,
                             "ends_at": min(at + ANNOUNCE_TTL, ends)})
    # The digests over the next two days, each naming what runs then. One within the hour after
    # an event's start shout is left out: the shout just said it.
    day0 = now - now % DAY
    for t in sorted({day0 + d * DAY + h * 3600 for d in range(3) for h in digest_hours}):
        if t + ANNOUNCE_TTL <= now or t > now + horizon_s:
            continue
        evs = _running(events, t)
        if not evs or any(t - ev["start"] < 3600 for ev in evs):
            continue
        announce.append({"kind": "announce", "tag": "calendar:ffxi:digest:" + time.strftime("%Y%m%d%H", time.gmtime(t)),
                         "message": _ffxi_digest(evs, t), "starts_at": t, "ends_at": t + ANNOUNCE_TTL})
    for r in announce:
        r.update(created_by="calendar", created_at=now)
        edges.append(r["starts_at"])
    later = [t for t in edges if t > now]
    return {"motd": motd, "announce": announce, "next": min(later) if later else None}


def find_eventcal(environ=os.environ):
    """OpenLobby's eventcal module. The bridge image is built on the OpenLobby image, so it
    is normally importable as is (/app); a checkout finds it beside this repository."""
    try:
        return importlib.import_module("eventcal")
    except ImportError:
        pass
    import ffxidb
    for cand in ffxidb._openlobby_candidates():
        if os.path.isfile(os.path.join(cand, "eventcal.py")):
            if cand not in sys.path:
                sys.path.append(cand)
            return importlib.import_module("eventcal")
    return None


class Sync:
    """The calendar sync's thread body and the state the panel shows."""

    def __init__(self, store, ec, environ=os.environ, log=print):
        self.store, self.ec, self.log = store, ec, log
        self.games = [g for g in _env_list(environ, "FFXI_NOTICES_GAMES", "tm,jan")]
        self.period = max(30.0, _env_num(environ, "FFXI_NOTICES_PERIOD", 300))
        self.remind = [int(x) for x in _env_list(environ, "FFXI_NOTICES_REMIND_MIN", "180,15") if x.isdigit()]
        self.near = max(0.0, _env_num(environ, "FFXI_NOTICES_NEAR_H", 24)) * 3600
        self.ffxi = (environ.get("FFXI_NOTICES_FFXI") or "").strip() != "0"
        self.digest = [int(x) % 24 for x in _env_list(environ, "FFXI_NOTICES_DIGEST_UTC", "2,14") if x.isdigit()]
        self.last = None
        self.error = None
        self._said = None

    def once(self, now=None, cal=None):
        now = int(time.time() if now is None else now)
        cal = self.ec.load() if cal is None else cal
        p = plan(now, cal, self.games, self.ec, self.remind, self.near)
        if self.ffxi:
            f = plan_ffxi(now, ffxi_events(self.store.game_calendar(now)), digest_hours=self.digest)
            p["motd_ffxi"] = f["motd"]
            p["announce"] += f["announce"]
            p["next"] = min([t for t in (p["next"], f["next"]) if t] or [None])
        got = apply(self.store, p, now)
        self.last, self.error = now, None
        if any(got.values()):
            self.log("calendar sync: %d login line(s) written, %d removed; %d announcement(s) "
                     "queued, %d dropped" % (got["motd"], got["motd_off"], got["queued"], got["dropped"]))
        return p, got

    def run(self, sleep=time.sleep):
        while True:
            wait = self.period
            try:
                p, _ = self.once()
                self._said = None
                if p["next"]:
                    # Wake for the next start or end so the login line turns over on time.
                    wait = max(5.0, min(wait, p["next"] - time.time() + 2))
            except Exception as exc:
                self.error = "%s: %s" % (type(exc).__name__, exc)
                if self.error != self._said:
                    self.log("calendar sync failed (%s); retrying every %ds" % (self.error, self.period))
                    self._said = self.error
            sleep(wait)


def start(bridge, environ=os.environ):
    """Set bridge.NOTICES (the Store, or None) and bridge.NOTICES_SYNC, and start the
    calendar sync. Never raises: notices are an extra, the lobby must come up without them."""
    bridge.NOTICES, bridge.NOTICES_SYNC = None, None
    bridge.NOTICES_OFF = None
    log = lambda m: bridge.log("notices", m)
    if (environ.get("FFXI_NOTICES") or "").strip() == "0":
        bridge.NOTICES_OFF = "FFXI_NOTICES=0"
        log("in-game notices OFF (FFXI_NOTICES=0)")
        return None
    store, why = Store.from_env(environ)
    if store is None:
        bridge.NOTICES_OFF = why
        log("in-game notices OFF: %s" % why)
        return None
    bridge.NOTICES = store
    if (environ.get("FFXI_NOTICES_CALENDAR") or "").strip() == "0":
        log("in-game notices on; calendar sync OFF (FFXI_NOTICES_CALENDAR=0)")
        return store
    ec = find_eventcal(environ)
    if ec is None:
        log("in-game notices on; calendar sync OFF: eventcal.py (OpenLobby's services/) not found")
        return store
    sync = Sync(store, ec, environ, log)
    bridge.NOTICES_SYNC = sync
    threading.Thread(target=sync.run, daemon=True).start()
    log("in-game notices on; calendar sync for %s every %ds, calendar %s"
        % (",".join(sync.games + (["ffxi"] if sync.ffxi else [])), sync.period, ec.calendar_path()))
    return store


#: The panel page, served by ffxi_fedtool.py at /notices (the panel's Games > FFXI > Notices).
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FFXI notices</title>
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
       border:1px solid var(--rule);border-radius:4px;padding:4px 10px}
button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.go{border-color:var(--good);color:var(--good)}
button.rm:hover:not(:disabled){border-color:var(--warn);color:var(--warn)}
input,textarea,select{font:inherit;font-size:13px;background:var(--sunk);color:var(--ink);
  border:1px solid var(--rule);border-radius:4px;padding:5px 8px;width:100%;min-width:0}
textarea{min-height:58px;resize:vertical}
label{font-size:12px;color:var(--ink2);display:grid;gap:3px}
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
td.a{width:1%;white-space:nowrap}
td.t{white-space:nowrap}
.acts{display:flex;gap:4px}
.form{display:grid;gap:8px;grid-template-columns:minmax(0,1fr);margin-top:10px;padding-top:10px;border-top:1px solid var(--rule)}
.form.top{margin-top:0;padding-top:0;border-top:0}
@media(min-width:760px){.form.an{grid-template-columns:minmax(0,4fr) minmax(0,2fr) minmax(0,1fr) minmax(0,1fr) minmax(0,1fr) auto;align-items:end}
                        .form.cd{grid-template-columns:minmax(0,2fr) minmax(0,2fr) auto;align-items:end}
                        .form.ml{grid-template-columns:minmax(0,4fr) minmax(0,2fr) auto;align-items:end}}
.preview{font-size:12.5px;color:var(--ink2);margin-top:8px}
.log{font-family:var(--m);font-size:12px;color:var(--ink2);max-height:200px;overflow:auto;white-space:pre-wrap}
.empty{color:var(--ink3);font-size:13px}
@media(max-width:700px){
  table,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{border-bottom:1px solid var(--rule);padding:8px 0}
  td{border:0;padding:2px 0}
  td.a{width:auto;padding-top:6px}
}
</style></head><body>
<div class="wrap">
  <div class="card">
    <div class="hd"><h1>FFXI notices</h1><span id="state" class="pill"></span></div>
    <div class="sub" id="facts"></div>
    <div id="msg" class="msg" style="margin-top:10px" hidden></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Announce</h2><span class="sub">Sent as a system message to everyone in the game</span></div>
    <div class="form an top">
      <label>Text<textarea id="a_text" rows="2" maxlength="440" placeholder="The Chocobo Cup starts at 19:00 UTC in Tetra Master!"></textarea></label>
      <label>Send at, UTC (empty for now)<input id="a_at" type="datetime-local"></label>
      <label>Repeat every (minutes)<input id="a_every" type="number" min="1" max="1440" placeholder="no repeat"></label>
      <label>Times<input id="a_times" type="number" min="1" max="12" value="1"></label>
      <label>Kind<select id="a_kind"><option value="">News</option><option value="1">Urgent</option></select></label>
      <button class="go" id="a_go">Post</button>
    </div>
    <p class="sub" style="margin:8px 0 0">One line holds about 140 characters; longer text is sent as several lines. Players who are not logged in when it goes out do not see it. Urgent goes out in the maintenance colour; keep it for things players must act on.</p>
  </div>

  <div class="card">
    <div class="hd"><h2>Maintenance countdown</h2><span class="sub">Warnings before the server goes down</span></div>
    <div class="form cd top">
      <label>Server goes down at, UTC<input id="c_at" type="datetime-local"></label>
      <label>Warn this many minutes before<input id="c_leads" value="30, 15, 10, 5, 4, 3, 2, 1" autocomplete="off"></label>
      <button class="go" id="c_go">Schedule countdown</button>
    </div>
    <div class="preview" id="c_prev"></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Login message</h2><span class="sub">Lines added to the server message players see when they log in</span></div>
    <div class="tw"><table><thead><tr><th>Line</th><th>From</th><th>Until</th><th>Status</th><th>By</th><th></th></tr></thead>
    <tbody id="motd"></tbody></table></div>
    <div class="form ml">
      <label>Line<input id="m_text" maxlength="120" autocomplete="off" placeholder="Double EXP weekend until Sunday!"></label>
      <label>Until, UTC (optional)<input id="m_until" type="datetime-local"></label>
      <button class="go" id="m_go">Add line</button>
    </div>
  </div>

  <div class="card">
    <div class="hd"><h2>Announcements</h2><span class="sub" id="asub"></span></div>
    <div class="tw"><table><thead><tr><th>When</th><th>Text</th><th>Status</th><th>By</th><th></th></tr></thead>
    <tbody id="ann"></tbody></table></div>
  </div>

  <div class="card">
    <div class="hd"><h2>Changes</h2></div>
    <div class="log" id="log"></div>
  </div>
</div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const qs = p => { p = p.replace(/^\//, ''); return T ? p + (p.includes('?') ? '&' : '?') + 't=' + encodeURIComponent(T) : p; };
const q = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when = t => t == null ? '' : new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
const local = t => new Date(t * 1000).toISOString().slice(0, 16);
const TONE = {showing:'on', due:'on', sent:'on', cancelled:'warn', skipped:'warn'};
const WORDS = {showing:'showing', scheduled:'scheduled', due:'sending', sent:'sent', ended:'ended',
               cancelled:'cancelled', skipped:'skipped, too late'};
let S = null, BUSY = false;

function say(text, ok){ const m = q('#msg'); m.hidden = !text; m.textContent = text || ''; m.className = 'msg ' + (ok ? 'ok' : 'bad'); }

async function load(){
  try{
    const r = await fetch(qs('notices.json'));
    const j = await r.json();
    if(!r.ok) throw new Error(j.error || r.statusText);
    S = j; draw();
  }catch(e){ say('Could not load: ' + e.message, false); }
}

const pill = st => `<span class="pill ${TONE[st] || ''}">${esc(WORDS[st] || st)}</span>`;
const by = r => r.calendar ? 'Event calendar' : esc(r.created_by || '');
const open = r => r.state === 'scheduled' || r.state === 'due';

function draw(){
  const st = q('#state');
  if(S.off){ st.textContent = 'off'; st.className = 'pill warn'; }
  else { st.textContent = S.calendar.on ? 'calendar sync on' : 'calendar sync off'; st.className = 'pill ' + (S.calendar.on ? 'on' : ''); }
  const bits = [];
  if(S.off) bits.push('In-game notices are off on the bridge (' + S.off + ').');
  else if(S.calendar.on) bits.push('Events for ' + S.calendar.games.join(', ') + ' are announced from the event calendar'
                                   + (S.calendar.last ? ', last checked ' + when(S.calendar.last) : ''));
  if(S.calendar.error) bits.push('Calendar sync failed: ' + S.calendar.error);
  if(S.error) bits.push(S.error);
  q('#facts').textContent = bits.join(' · ');
  q('#motd').innerHTML = S.motd.length ? S.motd.map(r => `<tr>
      <td>${esc(r.message)}</td><td class="t">${esc(when(r.starts_at))}</td>
      <td class="t">${r.ends_at == null ? 'no end' : esc(when(r.ends_at))}</td>
      <td>${pill(r.state)}</td><td>${by(r)}</td>
      <td class="a"><div class="acts">${r.calendar ? '' : (r.state === 'ended'
          ? `<button class="rm" data-del="${r.id}">Remove</button>`
          : `<button class="rm" data-end="${r.id}">End</button>`)}</div></td>
    </tr>`).join('') : '<tr><td colspan="6" class="empty">No lines added. Players see the server message on its own.</td></tr>';
  q('#asub').textContent = S.announce.filter(open).length + ' to go out, and the last two days';
  q('#ann').innerHTML = S.announce.length ? S.announce.map(r => `<tr>
      <td class="t">${esc(when(r.starts_at))}</td><td>${esc(r.message)}</td>
      <td>${pill(r.state)}${r.sent_at ? `<div class="sub">${esc(when(r.sent_at))}</div>` : ''}</td><td>${by(r)}</td>
      <td class="a"><div class="acts">${open(r) ? `<button class="rm" data-cancel="${r.id}">Cancel</button>`
          : (r.state === 'cancelled' && !r.calendar ? `<button class="rm" data-del="${r.id}">Remove</button>` : '')}</div></td>
    </tr>`).join('') : '<tr><td colspan="5" class="empty">Nothing announced in the last two days.</td></tr>';
  q('#log').textContent = (S.log || []).slice().reverse().join('\n') || 'Nothing changed since the bridge started.';
  if(!q('#c_at').value) q('#c_at').value = local(Math.ceil((S.now + 35 * 60) / 1800) * 1800);
  preview();
}

function preview(){
  const leads = q('#c_leads').value.split(/[\s,]+/).filter(Boolean);
  const m = leads.length ? leads[Math.min(1, leads.length - 1)] : '';
  q('#c_prev').textContent = m ? 'Players see: "The FFXI server goes down for maintenance in ' + m + ' minute'
                                 + (m === '1' ? '' : 's') + '. Please log out safely."' : '';
}
q('#c_leads').oninput = preview;

async function act(path, body, confirmText){
  if(BUSY) return;
  if(confirmText && !confirm(confirmText)) return;
  BUSY = true; document.querySelectorAll('button').forEach(b => b.disabled = true);
  try{
    const r = await fetch(qs(path), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    say(j.msg || (j.ok ? 'Done.' : 'Failed.'), j.ok);
    if(j.ok) return true;
  }catch(e){ say('Failed: ' + e.message, false); }
  finally{ BUSY = false; document.querySelectorAll('button').forEach(b => b.disabled = false); await load(); }
}

q('#a_go').onclick = async () => {
  const text = q('#a_text').value.trim(), at = q('#a_at').value;
  if(!text) return say('Write the announcement first.', false);
  const body = {text, at, every: q('#a_every').value, times: q('#a_times').value || 1, urgent: q('#a_kind').value === '1'};
  if(await act('notices/announce', body, at ? null : 'Send this to everyone in the game now?'))
    ['#a_text','#a_at','#a_every'].forEach(s => q(s).value = '');
};
q('#c_go').onclick = () => act('notices/countdown', {down_at: q('#c_at').value, leads: q('#c_leads').value},
                               'Schedule the warnings for a shutdown at ' + q('#c_at').value.replace('T', ' ') + ' UTC?');
q('#m_go').onclick = async () => {
  if(await act('notices/motd', {text: q('#m_text').value, until: q('#m_until').value}))
    ['#m_text','#m_until'].forEach(s => q(s).value = '');
};
document.addEventListener('click', e => {
  const b = e.target.closest('button'); if(!b) return;
  if(b.dataset.cancel) act('notices/cancel', {id: +b.dataset.cancel}, 'Cancel this announcement?');
  else if(b.dataset.end) act('notices/end', {id: +b.dataset.end}, 'Stop showing this line at login?');
  else if(b.dataset.del) act('notices/delete', {id: +b.dataset.del});
});
load();
setInterval(() => { if(!BUSY && document.visibilityState === 'visible') load(); }, 15000);
</script>
</body></html>
"""
