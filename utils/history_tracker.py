# This file is part of LazyFarmers.
# Copyright (c) 2025-Present Routo
#
# LazyFarmers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with LazyFarmers. If not, see <https://www.gnu.org/licenses/>.


"""
Session, cash and command history.

Each space (see core/spaces.py) gets its own sqlite database, so a dashboard user
only ever sees their own analytics. Pass owner=None for the admin space.

Two properties this module has to hold that it did not used to:

* **A session belongs to one account.** The old schema had no account column, so
  every account in a space was summed into a single row and the analytics could
  never answer "which account did that". Worse, `start_session` closed whatever
  was open and inserted a fresh row - and NeuraBot.setup_hook calls it - so an N
  account farm produced N+1 rows per boot, all but the last of them closed
  milliseconds after being created, and one reconnect on a flaky proxy shredded
  the day into a dozen empty sessions.

* **Writes never touch the caller's thread.** `track_command` runs from
  `state.log_command`, which runs from `bot.log`, which runs on the asyncio loop.
  A connect + `pragma journal_mode=wal` + UPDATE + COMMIT + close per command is
  tens of milliseconds on a network-backed volume; multiplied by every account
  sending every second it stalled the loop the dashboard waits on, so Start and
  Stop timed out even though the accounts came up fine. All writes are queued to
  one background thread that keeps its connections open.
"""

import json
import os
import queue
import sqlite3
import threading
import time
from datetime import datetime

from core import spaces
from core.paths import DATA_DIR

LEGACY_HISTORY_FILE = os.path.join(DATA_DIR, 'history.json')

# how many cash samples to keep *per account*. The old cap was 100 rows for the
# whole space, so on a three account farm the "Cash Growth Over Time" line was
# 33 samples per account interleaved into one zigzag, and it forgot yesterday.
CASH_HISTORY_PER_ACCOUNT = 500

_ready = set()
_ready_lock = threading.Lock()
# spaces whose leftover open sessions have already been closed this process
_reaped = set()
_reaped_lock = threading.Lock()


def _db_path(owner=None):
    return spaces.history_path(owner or spaces.ADMIN_SPACE)


def _connect(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute('pragma journal_mode=wal')
    return conn


def get_db(owner=None):
    """A fresh read connection. Callers must close it."""
    path = _db_path(owner)
    _ensure(path)
    return _connect(path)


def _ensure(path):
    """Create or upgrade the schema the first time a space's database is touched."""
    with _ready_lock:
        if path in _ready:
            return
        _ready.add(path)
    try:
        init_db(path)
        # the pre-sqlite history.json only ever belonged to the operator
        if path == spaces.history_path(spaces.ADMIN_SPACE):
            migrate_legacy_json(path)
        _reap_once(path)
    except Exception:
        # a broken database must not take the bot down with it; drop the ready
        # mark so the next call retries instead of silently writing nowhere
        with _ready_lock:
            _ready.discard(path)
        raise


def _reap_once(path):
    """Close sessions a previous run left open, the first time we touch this space.

    This is the first thing that happens to a space's database in a process, so
    every open row it finds is necessarily from an earlier run - which is what
    makes it safe to run here rather than only from the boot path. Accounts
    started later from the dashboard belong to spaces `neura.py` never looped
    over, and their stale sessions used to stay open forever: the next event
    reused a row that could be days old and piled the whole week into it.
    """
    with _reaped_lock:
        if path in _reaped:
            return
        _reaped.add(path)
    conn = _connect(path)
    try:
        _close_open(conn.cursor())
        conn.commit()
    finally:
        conn.close()


# ── schema ──────────────────────────────────────────────────────────────────

# name -> DDL, applied with ALTER TABLE when an existing database predates it
_SESSION_COLUMNS = {
    'account_id': 'TEXT',
    'account_name': 'TEXT',
    'start_unix': 'REAL',
    'end_unix': 'REAL',
    # bumped on every tracked event, so a session killed by a redeploy can still
    # be closed at the moment it really stopped instead of at the next boot
    'last_unix': 'REAL',
}
_CASH_COLUMNS = {
    'account_id': 'TEXT',
    'unix': 'REAL',
}


def _existing_columns(c, table):
    try:
        return {row[1] for row in c.execute(f'PRAGMA table_info({table})')}
    except sqlite3.Error:
        return set()


def _add_missing(c, table, wanted):
    have = _existing_columns(c, table)
    if not have:
        return
    for name, decl in wanted.items():
        if name not in have:
            c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')


def _local_unix(date_str, time_str):
    """"YYYY-MM-DD" + "HH:MM:SS" -> unix, read as local time.

    That is how those strings were written (`datetime.now()`), so this is the
    only correct reading of them. The version this replaces used
    `calendar.timegm`, which treats a local wall clock as UTC and shifted every
    point on the analytics charts by the machine's offset.
    """
    if not date_str or not time_str:
        return None
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    try:
        return time.mktime(dt.timetuple())
    except (OverflowError, ValueError):
        return None


def init_db(path=None):
    conn = _connect(path or _db_path())
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            account_name TEXT,
            date TEXT,
            start_time TEXT,
            end_time TEXT,
            start_unix REAL,
            end_unix REAL,
            last_unix REAL,
            hunts INTEGER DEFAULT 0,
            battles INTEGER DEFAULT 0,
            commands INTEGER DEFAULT 0,
            captchas INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS cash_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT,
            timestamp TEXT,
            unix REAL,
            amount INTEGER
        )
    ''')
    _add_missing(c, 'sessions', _SESSION_COLUMNS)
    _add_missing(c, 'cash_history', _CASH_COLUMNS)
    # backfill the unix columns for rows written before they existed, so old
    # sessions keep their place on the timeline instead of dropping off it
    for row_id, date_str, start_str, end_str in c.execute(
            'SELECT id, date, start_time, end_time FROM sessions WHERE start_unix IS NULL').fetchall():
        c.execute('UPDATE sessions SET start_unix = ?, end_unix = ? WHERE id = ?',
                  (_local_unix(date_str, start_str), _local_unix(date_str, end_str), row_id))
    for row_id, stamp in c.execute(
            'SELECT id, timestamp FROM cash_history WHERE unix IS NULL').fetchall():
        parts = str(stamp or '').split(' ')
        c.execute('UPDATE cash_history SET unix = ? WHERE id = ?',
                  (_local_unix(parts[0], parts[1]) if len(parts) == 2 else None, row_id))
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(end_time, account_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_cash_account ON cash_history(account_id, id)')
    conn.commit()
    conn.close()


def migrate_legacy_json(path=None):
    if not os.path.exists(LEGACY_HISTORY_FILE):
        return

    try:
        with open(LEGACY_HISTORY_FILE, 'r') as f:
            data = json.load(f)

        conn = _connect(path or _db_path())
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM sessions')
        if c.fetchone()[0] > 0:
            conn.close()
            return

        for sess in data.get('sessions', []):
            st = sess.get('stats', {})
            date_str = sess.get('date', datetime.now().strftime("%Y-%m-%d"))
            start_str = sess.get('start_time', datetime.now().strftime("%H:%M:%S"))
            end_str = sess.get('end_time')
            c.execute('''
                INSERT INTO sessions (date, start_time, end_time, start_unix, end_unix,
                                      hunts, battles, commands, captchas)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                date_str, start_str, end_str,
                _local_unix(date_str, start_str), _local_unix(date_str, end_str),
                st.get('hunts', 0), st.get('battles', 0),
                st.get('commands', 0), st.get('captchas', 0)
            ))

        for cash in data.get('cash_history', []):
            stamp = cash.get('timestamp')
            parts = str(stamp or '').split(' ')
            c.execute('INSERT INTO cash_history (timestamp, unix, amount) VALUES (?, ?, ?)',
                      (stamp, _local_unix(parts[0], parts[1]) if len(parts) == 2 else None,
                       cash.get('amount', 0)))

        conn.commit()
        conn.close()

        os.rename(LEGACY_HISTORY_FILE, LEGACY_HISTORY_FILE + '.bak')
        print("Successfully migrated legacy history.json to SQLite")
    except Exception as e:
        print(f"Failed to migrate legacy history: {e}")


# ── the writer thread ───────────────────────────────────────────────────────

_writes = queue.Queue()
_writer = None
_writer_lock = threading.Lock()
_conns = {}
# so a caller that wants to read its own writes can wait for the queue to drain
_write_errors = []


def _writer_conn(path):
    conn = _conns.get(path)
    if conn is None:
        _ensure(path)
        conn = _connect(path)
        _conns[path] = conn
    return conn


def _writer_loop():
    while True:
        job = _writes.get()
        try:
            path, apply, done = job
            if apply is None:          # flush marker
                if done is not None:
                    done.set()
                continue
            try:
                conn = _writer_conn(path)
                apply(conn.cursor())
                conn.commit()
            except Exception as e:
                # a write that cannot land must not kill the writer, or every
                # later stat would vanish without a word
                _write_errors.append(f"{type(e).__name__}: {e}")
                del _write_errors[:-20]
                # drop the connection so the next write reopens it; a half
                # applied transaction would otherwise poison every later commit
                stale = _conns.pop(path, None)
                if stale is not None:
                    try:
                        stale.rollback()
                    except Exception:
                        pass
                    try:
                        stale.close()
                    except Exception:
                        pass
            finally:
                if done is not None:
                    done.set()
        finally:
            _writes.task_done()


def _ensure_writer():
    global _writer
    if _writer is not None and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is not None and _writer.is_alive():
            return
        _writer = threading.Thread(target=_writer_loop, name='history-writer', daemon=True)
        _writer.start()


def _submit(owner, apply, wait=False, timeout=5.0):
    """Queue a write. Returns True when it was applied (only meaningful if wait)."""
    _ensure_writer()
    done = threading.Event() if wait else None
    _writes.put((_db_path(owner), apply, done))
    if done is None:
        return True
    return done.wait(timeout)


def flush(owner=None, timeout=5.0):
    """Block until every queued write for this space has been committed.

    Reads go through their own connection, so without this a dashboard request
    could render analytics that are a few hundred milliseconds stale - which
    looks exactly like "the counter did not move".
    """
    _ensure_writer()
    done = threading.Event()
    _writes.put((_db_path(owner), None, done))
    return done.wait(timeout)


def write_errors():
    """The last few write failures, for the debug endpoint."""
    return list(_write_errors)


# ── sessions ────────────────────────────────────────────────────────────────

def load_history():
    """Kept because every call site passes its result back in as the first arg."""
    return {}


def _close_open(c, now=None):
    """Close every open session at the last moment it was known to be active."""
    now = now or time.time()
    c.execute('''
        UPDATE sessions
           SET end_unix = COALESCE(last_unix, start_unix, ?),
               end_time = COALESCE(
                   time(COALESCE(last_unix, start_unix, ?), 'unixepoch', 'localtime'),
                   start_time)
         WHERE end_time IS NULL
    ''', (now, now))


def start_session(history_data=None, owner=None):
    """Open this space's database, closing sessions a previous run left behind.

    This used to insert a row, which is why it could not be called from more than
    one place: NeuraBot.setup_hook called it too (once per account, and again on
    every reconnect), so the boot that was supposed to open one session opened
    N+1 and closed all but the last of them immediately. Rows are now created
    lazily, per account, by whichever event happens first, and the reap is
    `_reap_once` - so calling this twice, or not at all, both do the right thing.
    """
    _ensure(_db_path(owner))
    return {"stats": {"hunts": 0, "battles": 0, "commands": 0, "captchas": 0}}


def end_session(history_data=None, owner=None):
    def apply(c):
        _close_open(c)
    _submit(owner, apply, wait=True)


def _ensure_active_session(c, account_id, account_name, now):
    """The open row for this account, created if it does not exist yet."""
    account_id = str(account_id) if account_id else None
    if account_id:
        c.execute('SELECT id FROM sessions WHERE end_time IS NULL AND account_id = ?'
                  ' ORDER BY id DESC LIMIT 1', (account_id,))
    else:
        # pre-account rows and anything logged before an account is identified
        c.execute('SELECT id FROM sessions WHERE end_time IS NULL AND account_id IS NULL'
                  ' ORDER BY id DESC LIMIT 1')
    row = c.fetchone()
    if row:
        if account_name:
            c.execute('UPDATE sessions SET account_name = ? WHERE id = ?', (account_name, row[0]))
        return row[0]
    stamp = datetime.fromtimestamp(now)
    c.execute('INSERT INTO sessions (account_id, account_name, date, start_time, start_unix, last_unix)'
              ' VALUES (?, ?, ?, ?, ?, ?)',
              (account_id, account_name, stamp.strftime("%Y-%m-%d"),
               stamp.strftime("%H:%M:%S"), now, now))
    return c.lastrowid


_COLUMN_FOR_CMD = {'hunt': 'hunts', 'battle': 'battles'}


def track_command(history_data=None, cmd_type=None, owner=None,
                  account_id=None, account_name=None):
    if not cmd_type:
        return
    column = _COLUMN_FOR_CMD.get(cmd_type)
    now = time.time()

    def apply(c):
        sess_id = _ensure_active_session(c, account_id, account_name, now)
        if column:
            c.execute(f'UPDATE sessions SET commands = commands + 1, {column} = {column} + 1,'
                      f' last_unix = ? WHERE id = ?', (now, sess_id))
        else:
            c.execute('UPDATE sessions SET commands = commands + 1, last_unix = ? WHERE id = ?',
                      (now, sess_id))
    _submit(owner, apply)


def track_captcha(history_data=None, owner=None, account_id=None, account_name=None):
    """One solved captcha.

    The `captchas` column used to be fed by `state.log_command` mapping any
    command containing "autohunt" to cmd_type "captcha" - so the number the
    dashboard labelled "Captchas Solved" was really "times we re-armed
    autohunt", and an actual solve was never recorded at all.
    """
    now = time.time()

    def apply(c):
        sess_id = _ensure_active_session(c, account_id, account_name, now)
        c.execute('UPDATE sessions SET captchas = captchas + 1, last_unix = ? WHERE id = ?',
                  (now, sess_id))
    _submit(owner, apply)


def track_cash(history_data=None, amount=0, owner=None, account_id=None):
    now = time.time()
    account_id = str(account_id) if account_id else None
    stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    def apply(c):
        c.execute('INSERT INTO cash_history (account_id, timestamp, unix, amount)'
                  ' VALUES (?, ?, ?, ?)', (account_id, stamp, now, int(amount or 0)))
        # trim this account's own series, so a busy account can no longer evict
        # every other account's history
        if account_id:
            c.execute('DELETE FROM cash_history WHERE account_id = ? AND id NOT IN'
                      ' (SELECT id FROM cash_history WHERE account_id = ?'
                      '  ORDER BY id DESC LIMIT ?)',
                      (account_id, account_id, CASH_HISTORY_PER_ACCOUNT))
        else:
            c.execute('DELETE FROM cash_history WHERE account_id IS NULL AND id NOT IN'
                      ' (SELECT id FROM cash_history WHERE account_id IS NULL'
                      '  ORDER BY id DESC LIMIT ?)', (CASH_HISTORY_PER_ACCOUNT,))
    _submit(owner, apply)


# ── reads ───────────────────────────────────────────────────────────────────

def get_session_stats(history_data=None, owner=None):
    """This run's totals across every account in the space."""
    flush(owner)
    conn = get_db(owner)
    c = conn.cursor()
    c.execute('SELECT COALESCE(SUM(hunts), 0), COALESCE(SUM(battles), 0),'
              ' COALESCE(SUM(commands), 0), COALESCE(SUM(captchas), 0)'
              ' FROM sessions WHERE end_time IS NULL')
    row = c.fetchone()
    conn.close()
    if row:
        return {"hunts": row[0], "battles": row[1], "commands": row[2], "captchas": row[3]}
    return {"hunts": 0, "battles": 0, "commands": 0, "captchas": 0}


def get_all_time_stats(history_data=None, owner=None, conn=None):
    close = conn is None
    if close:
        flush(owner)
        conn = get_db(owner)
    c = conn.cursor()
    c.execute('SELECT SUM(hunts), SUM(battles), SUM(commands), SUM(captchas), COUNT(id) FROM sessions')
    row = c.fetchone()
    if close:
        conn.close()

    if row and row[4]:
        return {
            "all_time_hunts": row[0] or 0,
            "all_time_battles": row[1] or 0,
            "all_time_commands": row[2] or 0,
            "all_time_captchas": row[3] or 0,
            "total_sessions": row[4]
        }
    return {
        "all_time_hunts": 0,
        "all_time_battles": 0,
        "all_time_commands": 0,
        "all_time_captchas": 0,
        "total_sessions": 0
    }


def get_analytics_data(start_date=None, end_date=None, owner=None):
    flush(owner)
    conn = get_db(owner)
    c = conn.cursor()

    where, params = '', []
    if start_date and end_date:
        where = ' WHERE date >= ? AND date <= ?'
        params = [start_date, end_date]
    elif start_date:
        where = ' WHERE date >= ?'
        params = [start_date]

    c.execute('SELECT id, account_id, account_name, date, start_time, end_time,'
              ' start_unix, end_unix, last_unix, hunts, battles, commands, captchas'
              ' FROM sessions' + where + ' ORDER BY id ASC', params)

    sessions = []
    per_account = {}
    for row in c.fetchall():
        (sess_id, account_id, account_name, date_str, start_str, end_str,
         start_unix, end_unix, last_unix, hunts, battles, commands, captchas) = row
        start_unix = start_unix if start_unix is not None else _local_unix(date_str, start_str)
        end_unix = end_unix if end_unix is not None else _local_unix(date_str, end_str)
        label = account_name or account_id or 'unassigned'
        sessions.append({
            "id": sess_id,
            "account_id": account_id,
            "account": label,
            "date": date_str,
            "start_time": int(start_unix) if start_unix else None,
            "end_time": int(end_unix) if end_unix else None,
            # None while the session is still open, so the UI can say "live"
            "active": end_str is None,
            "stats": {
                "hunts": hunts or 0,
                "battles": battles or 0,
                "commands": commands or 0,
                "captchas": captchas or 0,
            }
        })
        agg = per_account.setdefault(label, {
            "account": label, "account_id": account_id, "sessions": 0,
            "hunts": 0, "battles": 0, "commands": 0, "captchas": 0,
            "first_seen": None, "last_seen": None,
        })
        agg['sessions'] += 1
        agg['hunts'] += hunts or 0
        agg['battles'] += battles or 0
        agg['commands'] += commands or 0
        agg['captchas'] += captchas or 0
        seen = end_unix or last_unix or start_unix
        if start_unix and (agg['first_seen'] is None or start_unix < agg['first_seen']):
            agg['first_seen'] = int(start_unix)
        if seen and (agg['last_seen'] is None or seen > agg['last_seen']):
            agg['last_seen'] = int(seen)

    cash_history = []
    cash_where, cash_params = '', []
    # the cash series used to ignore the date filter entirely, so picking a range
    # moved every chart except that one
    if start_date and end_date:
        cash_where = ' WHERE date(timestamp) >= ? AND date(timestamp) <= ?'
        cash_params = [start_date, end_date]
    elif start_date:
        cash_where = ' WHERE date(timestamp) >= ?'
        cash_params = [start_date]
    c.execute('SELECT account_id, timestamp, unix, amount FROM cash_history'
              + cash_where + ' ORDER BY id ASC', cash_params)
    for account_id, stamp, unix, amount in c.fetchall():
        parts = str(stamp or '').split(' ')
        if unix is None and len(parts) == 2:
            unix = _local_unix(parts[0], parts[1])
        cash_history.append({
            "account_id": account_id,
            "timestamp": stamp,
            "unix": int(unix) if unix else None,
            "amount": amount,
        })

    totals = get_all_time_stats(owner=owner, conn=conn)
    conn.close()

    return {
        "sessions": sessions,
        "per_account": sorted(per_account.values(), key=lambda a: -a['commands']),
        "cash_history": cash_history,
        "totals": totals
    }
