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
Runs the bot instances so the dashboard can start and stop accounts while the
process keeps running.

Accounts are addressed by (owner, name) - two dashboard users may both call an
account "acc1", so a bare name is never enough to find the right bot.

Nothing in here starts an account on its own. Every bot that exists was asked
for by an explicit click on the dashboard; a restart, a redeploy or a crash
brings the process back with an empty farm.

Starting many accounts is a queue, never a fan-out - see start_sequence.
"""


import asyncio
import os

import core.state as state
import utils.history_tracker as ht
from core import spaces
from core.bot import NeuraBot
from utils import proxy_manager

# Spaces whose history db has been opened this run. Boot used to do this for
# every space up front; now the first Start in a space opens it.
started_spaces = set()

_loop = None


def bind_loop(loop):
    global _loop
    _loop = loop


def get_loop():
    return _loop


def account_name(bot):
    return getattr(bot, 'account_name', None)


def bot_owner(bot):
    # the space id lives on space_owner, never on owner_id - commands.Bot owns that name
    return getattr(bot, 'space_owner', spaces.ADMIN_SPACE)


def running_names(owner=None):
    return [
        account_name(bot) for bot in state.bot_instances
        if account_name(bot) and (owner is None or bot_owner(bot) == owner)
    ]


def running_states(owner=None):
    """name -> True once the account reached READY, False while it is still connecting.

    "has an instance" and "is logged in" are different things: a bot with a dead
    proxy sits in the reconnect loop forever, so the UI needs to say CONNECTING
    rather than claim it is farming.
    """
    return {
        account_name(bot): bool(getattr(bot, 'is_ready', False))
        for bot in state.bot_instances
        if account_name(bot) and (owner is None or bot_owner(bot) == owner)
    }


def find_bot(owner, name):
    for bot in state.bot_instances:
        if account_name(bot) == name and bot_owner(bot) == owner:
            return bot
    return None


def is_placeholder(value):
    text = str(value or '')
    return not text or "YOUR_TOKEN_HERE" in text or "YOUR_CHANNEL_ID_HERE" in text or "PLACEHOLDER" in text


def running_duplicate(token, user_id=None, ignore=None):
    """The live bot already using this token or discord id, in *any* space.

    The only guard used to be (owner, name), which answers "is this config row
    running" - not "is this Discord account running". The same token pasted
    under a second name, or added by two dashboard users, sailed straight past
    it and logged in twice: two gateway sessions farming one account, sending
    every command twice. That is the shape of a selfbot ban.

    Token is the pre-login identity (it is all we have before READY); user_id
    catches the case where two config rows hold different tokens for the same
    Discord account.
    """
    token = str(token or '').strip()
    uid = str(user_id or '').strip()
    for bot in state.bot_instances:
        if bot is ignore:
            continue
        if token and str(getattr(bot, 'token', '') or '').strip() == token:
            return bot
        if uid and str(getattr(bot, 'user_id', '') or '').strip() == uid:
            return bot
    return None


def _open_space_history(owner):
    """Open this space's history db the first time it starts something.

    Boot used to do this for every space before starting its accounts. With
    nothing auto-starting, the first Start click is the moment a space becomes
    active, so that is where the db is opened. start_session is idempotent.
    """
    if owner in started_spaces:
        return
    try:
        ht.start_session(owner=owner)
    except Exception:
        return
    started_spaces.add(owner)


async def start_account(account, owner=spaces.ADMIN_SPACE, proxies=None):
    owner = spaces.normalise_owner(owner)
    name = account.get('name') or 'unnamed'
    if find_bot(owner, name):
        return False, f"{name} is already running"

    token = account.get('token')
    if is_placeholder(token):
        return False, f"{name} has no usable token"

    channels = [c for c in (account.get('channels') or []) if not is_placeholder(c)]
    if not channels:
        return False, f"{name} needs channel setup. Open Monitor Bot and create & assign channels first."

    # No await between here and the append below, and every caller is on the one
    # shared loop - so a double-clicked Start cannot slip a second instance in
    # between the check and the registration.
    twin = running_duplicate(token, account.get('user_id'))
    if twin:
        twin_name = account_name(twin) or 'another entry'
        if bot_owner(twin) != owner:
            return False, (f"{name} is the same Discord account as one already "
                           f"running in another space")
        return False, f"{name} is the same Discord account as {twin_name}, already running"

    _open_space_history(owner)

    proxy_url, proxy_auth, proxy_label = proxy_manager.resolve_account_proxy(
        owner, account, proxies=proxies)
    bot = NeuraBot(
        token=token,
        channels=channels,
        proxy_url=proxy_url,
        proxy_auth=proxy_auth,
        proxy_label=proxy_label,
        space_owner=owner,
    )
    bot.account_name = name
    state.bot_instances.append(bot)
    bot.runner_task = asyncio.create_task(_run(bot))
    return True, f"{name} is starting"


async def _run(bot):
    """Every exit path tears down the account, including permanent login errors."""
    try:
        await bot.run_bot()
    finally:
        from core.captcha_jobs import jobs
        jobs.cancel(bot.space_owner, getattr(bot, 'user_id', ''))
        bot.active = False
        bot.paused = True
        bot.is_ready = False
        tasks = [t for t in getattr(bot, 'worker_tasks', [])
                 if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _release_captcha_hold(bot)
        for obj in (getattr(bot, 'interactions', None), bot, getattr(bot, 'session', None)):
            if obj is not None and hasattr(obj, 'close'):
                try:
                    await asyncio.wait_for(obj.close(), 5)
                except Exception:
                    pass
        if bot in state.bot_instances:
            state.bot_instances.remove(bot)


def _release_captcha_hold(bot):
    """Withdraw anything this account still holds in the captcha machinery.

    A pending challenge is a claim that this account is waiting on a human right now.
    Once the bot is gone nothing can solve for it, so leaving the claim behind put a
    permanent entry in the dashboard's notification bell whose Solve button could only
    ever answer "that account is not running" - and left the single manual-solve slot
    held against every other account.
    """
    user = getattr(bot, 'user', None)
    bot_id = str(user.id) if user else None
    if not bot_id:
        return
    try:
        from modules.web_solver import WebSolver
        WebSolver.abandon_manual_solve(bot_id)
    except Exception:
        pass
    try:
        from dashboard.app import clear_captcha_challenge
        clear_captcha_challenge(bot_id)
    except Exception:
        pass


async def stop_account(owner, name):
    bot = find_bot(owner, name)
    if not bot:
        return False, f"{name} is not running"

    # Signal every loop in run_bot / workers to bail out before we touch the
    # gateway, otherwise a bot caught mid-reconnect-sleep (up to 300s) would
    # ignore the stop and the dashboard would keep listing it as running.
    bot.active = False
    bot.paused = True

    runner = getattr(bot, 'runner_task', None)

    # Cancel the runner first so a long reconnect backoff can not out-live the
    # close() call. close() alone leaves the task parked in asyncio.sleep().
    if runner is not None and not runner.done():
        runner.cancel()
        try:
            # shield so a wait_for timeout does not eat the cancel we just sent;
            # if it is still winding down after 10s, close() below finishes it.
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass

    try:
        await bot.close()
    except Exception as e:
        bot.log("ERROR", f"Error while closing session: {e}")

    # The runner's finally block also removes the bot; guard the double remove.
    if bot in state.bot_instances:
        state.bot_instances.remove(bot)
    _release_captcha_hold(bot)

    # Belt and suspenders: cancel any stray background workers the bot spawned
    # (_track_active_time, _process_pending_commands, neura_queue_worker,
    # neura_scheduler_worker) so they do not keep firing after the account is
    # gone. They loop on `while self.active` and would exit on their own, but
    # cancelling closes the race where a worker wakes between active=False and
    # close() and tries to send on a shutting gateway.
    for task in (getattr(bot, 'worker_tasks', None) or []):
        if task is not None and not task.done():
            task.cancel()
    return True, f"{name} stopped"


# ── starting a whole space, one account at a time ───────────────────────────
#
# There is no *simultaneous* start. Firing every account at once is what put ~16
# of them on the gateway inside eight seconds, buried the host's log viewer under
# the resulting cog-loading spam and took the process out. So a "start
# everything" is a queue, not a fan-out: one account is started, waited on until
# it is actually READY, then a gap, then the next.
#
# The gap is the point. state.login_slot only spaces the gateway handshakes
# 0.35s apart, which is a rate limit for Discord, not a budget for the ~26 cogs
# and the config load each account does behind it.
START_GAP_S = 8.0            # LAZYFARMERS_START_GAP_S
START_READY_TIMEOUT_S = 60.0  # give up waiting for READY and move on

# One sequence per space, and a second click while one is running is refused
# rather than queued - that is what "don't start them again and again" means. A
# second pass over a farm that is already coming up does nothing but re-check
# accounts the first pass has not reached yet.
_sequences = {}


def _env_float(name, default, low, high):
    try:
        value = float(os.environ.get(name, '').strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def sequence_status(owner):
    """What the Start-all queue for this space is doing, for the UI to poll."""
    seq = _sequences.get(owner)
    if not seq:
        return {'active': False}
    return dict(seq['progress'], active=not seq['task'].done())


def cancel_sequence(owner):
    seq = _sequences.get(owner)
    if not seq or seq['task'].done():
        return False
    seq['progress']['cancelled'] = True
    seq['task'].cancel()
    return True


async def _wait_ready(bot, timeout):
    """Block until the account is logged in, gave up, or ran out of patience."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if getattr(bot, 'is_ready', False):
            return 'ready'
        if bot not in state.bot_instances:
            # _run removed it: the login failed or the token was rejected
            return 'gone'
        await asyncio.sleep(0.5)
    return 'timeout'


async def _run_sequence(owner, accounts, gap):
    progress = _sequences[owner]['progress']
    proxies = proxy_manager.load_proxies(owner)
    try:
        for i, account in enumerate(accounts):
            name = account.get('name') or 'unnamed'
            progress['current'] = name
            # One account must not be able to take the queue down with it. Without
            # this, a single raising start_account left the remaining accounts
            # never started and nothing said why - the progress line simply froze.
            try:
                ok, message = await start_account(account, owner, proxies=proxies)
                if not ok:
                    progress['skipped'].append(message)
                else:
                    bot = find_bot(owner, name)
                    outcome = await _wait_ready(bot, START_READY_TIMEOUT_S) if bot else 'gone'
                    if outcome == 'ready':
                        progress['started'].append(name)
                    elif outcome == 'gone':
                        progress['failed'].append(f"{name} could not log in")
                    else:
                        # still connecting - a slow proxy, not a failure. Leave it
                        # to the reconnect loop and carry on rather than stalling
                        # the rest of the farm behind it.
                        progress['started'].append(f"{name} (still connecting)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                progress['failed'].append(f"{name}: {e}")
            progress['done'] = i + 1

            if i + 1 < len(accounts):
                await asyncio.sleep(gap)
    except asyncio.CancelledError:
        progress['cancelled'] = True
        raise
    except Exception as e:
        # asyncio swallows a task exception until the task is garbage collected,
        # so without this the queue would just stop with no explanation anywhere.
        progress['failed'].append(f"start queue stopped: {e}")
        state.log_command("ERROR", f"Start-all queue stopped: {e}", "error", owner=owner)
    finally:
        progress['current'] = None
        state.log_command(
            "SYS",
            f"Start-all finished: {len(progress['started'])} started, "
            f"{len(progress['skipped'])} skipped, {len(progress['failed'])} failed"
            + (" (cancelled)" if progress['cancelled'] else ""),
            "info", owner=owner)


def start_sequence(owner, accounts):
    """Queue every account in `accounts` to start one at a time.

    Returns (started, message). Does not wait for the queue to drain: sixteen
    accounts at an eight second gap is over two minutes, and no HTTP request
    should be held open for that.
    """
    owner = spaces.normalise_owner(owner)
    seq = _sequences.get(owner)
    if seq and not seq['task'].done():
        return False, "this space is already starting accounts - let it finish"

    pending = [a for a in accounts if not find_bot(owner, a.get('name'))]
    if not pending:
        return False, "every account is already running"

    gap = _env_float('LAZYFARMERS_START_GAP_S', START_GAP_S, low=1.0, high=120.0)
    progress = {'total': len(pending), 'done': 0, 'current': None,
                'started': [], 'skipped': [], 'failed': [], 'cancelled': False,
                'gap': gap}
    _sequences[owner] = {'progress': progress, 'task': None}
    _sequences[owner]['task'] = asyncio.ensure_future(_run_sequence(owner, pending, gap))

    minutes = (len(pending) - 1) * gap / 60.0
    return True, (f"starting {len(pending)} account(s), one every {gap:.0f}s "
                  f"(about {minutes:.0f} min)")


# Teardown is bounded rather than unbounded: each stop_account may wait up to 10s
# on a runner task and then close a gateway connection, so serial teardown of 200
# accounts could not finish inside any sane HTTP timeout. Stopping is cheap in a
# way starting is not - no logins, no cog loading - so it may go wide.
STOP_CONCURRENCY = 16


async def stop_all(owner=None):
    # A queue still feeding accounts in would undo this as fast as it works.
    for space in ([owner] if owner is not None else list(_sequences)):
        cancel_sequence(space)

    targets = [
        bot for bot in list(state.bot_instances)
        if account_name(bot) and (owner is None or bot_owner(bot) == owner)
    ]

    async def _stop(bot):
        try:
            return await stop_account(bot_owner(bot), account_name(bot))
        except Exception as e:
            return (False, f"{account_name(bot)}: {e}")

    results = []
    for i in range(0, len(targets), STOP_CONCURRENCY):
        batch = targets[i:i + STOP_CONCURRENCY]
        results.extend(await asyncio.gather(*(_stop(bot) for bot in batch)))
    return results
