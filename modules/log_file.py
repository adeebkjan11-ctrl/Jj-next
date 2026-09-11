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
The only copy of the log that survives the process dying.

Everything else is in memory: the dashboard's log view is a 1000-entry deque and
the host console is a pipe into the provider's viewer. So when the process went
away - OOM kill, a fatal abort, a host migration - it took the explanation with
it, and the farm came back empty with nothing anywhere saying why. "It stopped
and showed nothing" is that, not a silent bug.

This appends every line to DATA_ROOT/data/neura.log, which is on the persistent
volume, and keeps the previous run's file as neura.log.1 so a restart cannot
overwrite the evidence of what caused it.

Two more things live here because they answer the same question:

  - faulthandler dumps native crashes into neura_crash.log. An abort like
    `std::system_error: Resource temporarily unavailable` unwinds outside
    Python's exception machinery, so nothing but faulthandler ever sees it.
  - a clean-shutdown marker, written by _shutdown and cleared at boot. If it is
    missing when we start, the previous run did not exit on purpose, and the boot
    banner says so instead of leaving the operator to guess.
"""


import os
import queue
import threading
import time

MAX_BYTES = 8 * 1024 * 1024

_queue = queue.Queue(maxsize=20000)
_writer = None
_lock = threading.Lock()
_path = None
_marker = None
_dropped = 0


def _paths():
    from core.paths import DATA_DIR
    return (os.path.join(DATA_DIR, 'neura.log'),
            os.path.join(DATA_DIR, 'clean_shutdown'))


def _rotate_if_needed():
    try:
        if os.path.exists(_path) and os.path.getsize(_path) > MAX_BYTES:
            os.replace(_path, _path + '.1')
    except OSError:
        pass


def _writer_loop():
    """Batch whatever has queued up and append it in one write.

    A thread, not a direct write from the caller: log_command runs on the asyncio
    loop, and disk I/O there stalls every bot behind it. Nothing is fsynced - a
    handful of buffered lines lost in a hard kill is an acceptable trade for not
    paying a flush per log line.
    """
    global _dropped
    while True:
        line = _queue.get()
        if line is None:
            return
        batch = [line]
        while len(batch) < 200:
            try:
                nxt = _queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                batch.append(None)
                break
            batch.append(nxt)

        done = batch and batch[-1] is None
        if done:
            batch.pop()

        if _dropped:
            batch.append(f"[!] {_dropped} log line(s) dropped - writer fell behind\n")
            _dropped = 0

        try:
            _rotate_if_needed()
            with open(_path, 'a', encoding='utf-8', errors='replace') as f:
                f.write(''.join(batch))
        except OSError:
            pass

        if done:
            return


def start():
    """Open the log file, roll the previous run's, and arm the crash dumper.

    Returns (previous_run_was_clean, path_to_previous_run_log or None).
    """
    global _writer, _path, _marker
    with _lock:
        if _writer is not None:
            return None, None
        _path, _marker = _paths()
        os.makedirs(os.path.dirname(_path) or '.', exist_ok=True)

        # Keep the run that just ended. It is the one that holds the answer, and a
        # crash-restart would otherwise start appending to it and bury the tail
        # under the new run's startup chatter.
        had_previous = os.path.exists(_path)
        previous = None
        if had_previous:
            try:
                os.replace(_path, _path + '.1')
                previous = _path + '.1'
            except OSError:
                # could not roll it; append rather than lose the new run's lines
                previous = None

        marked = os.path.exists(_marker)
        try:
            if marked:
                os.remove(_marker)
        except OSError:
            pass

        # None, not False, when there is nothing to judge. A first boot has no
        # previous log and no marker, which is indistinguishable from a crash by
        # the marker alone - and telling a fresh install that it crashed is how a
        # useful signal gets learned as noise and ignored.
        clean = True if marked else (None if not had_previous else False)

        _writer = threading.Thread(target=_writer_loop, name='neura-logfile', daemon=True)
        _writer.start()

        try:
            import faulthandler
            # Its own file, not neura.log. faulthandler needs a live fd for the
            # whole run (it writes from a signal handler, after the interpreter is
            # already too broken to open anything), and on Windows an open handle
            # makes the log unrenameable - so sharing the file meant rotation
            # failed silently and neura.log grew without bound.
            faulthandler.enable(open(os.path.join(os.path.dirname(_path), 'neura_crash.log'),
                                     'a', encoding='utf-8', errors='replace'))
        except Exception:
            pass

        write('SYS', 'LazyFarmers', '─' * 60)
        write('SYS', 'LazyFarmers', f'Process started (pid {os.getpid()})')
        if clean is False:
            write('WARN', 'LazyFarmers',
                  'The previous run did not shut down cleanly - it was killed or it '
                  'crashed. See neura.log.1 for its last lines.')
        return clean, previous


def write(log_type, who, message):
    global _dropped
    if _writer is None:
        return
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{who}] [{log_type}] {message}\n"
    try:
        _queue.put_nowait(line)
    except queue.Full:
        # Never block the event loop on a full queue. Losing lines is bad; a farm
        # frozen behind a log write is worse, and the count is reported.
        _dropped += 1


def mark_clean_shutdown():
    """Record that this exit was on purpose, for the next boot to read."""
    if not _marker:
        return
    try:
        with open(_marker, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
    except OSError:
        pass


def tail(lines=200, previous=False):
    """The last N lines of this run's log, or of the run before it."""
    path = (_path + '.1') if previous else _path
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.readlines()[-lines:]
    except OSError:
        return []


def flush(timeout=3.0):
    """Drain the queue on the way out so the last lines actually reach disk."""
    if _writer is None:
        return
    try:
        _queue.put_nowait(None)
    except queue.Full:
        return
    _writer.join(timeout=timeout)
