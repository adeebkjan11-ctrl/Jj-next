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
One NeuraBot per Discord account.
"""



import discord
from discord.ext import commands
import json
import os
import time
import random
import asyncio
import re
import sys
import requests
from modules.neura_human import NeuraHuman
from modules.neura_logs import neura_logger
from modules.identity import IdentityManager
from component_v2_neura import setup_interactions
from modules.captcha_solver import setup_solver
from modules.web_solver import setup_web_solver
from modules.browser_solver import setup_browser_solver
import core.state as state
from core import spaces
import aiohttp
import unicodedata
import copy
import logging
from rich.console import Console
from rich.align import Align

_log = logging.getLogger(__name__)

CURRENT_VERSION = "2.5.0"

class NeuraBot(commands.Bot):
    def __init__(self, token=None, channels=None, proxy_url=None, proxy_auth=None, proxy_label="direct",
                 space_owner=spaces.ADMIN_SPACE):
        self.session = None
        self.base_dir = state.BASE_DIR
        # the shared defaults; the per-account override lives in this bot's space
        self.config_file = os.path.join(state.CONFIG_DIR, 'settings.json')
        # NOT self.owner_id: commands.Bot.__init__ owns that name (it is discord.py's
        # "which user owns this bot" option) and unconditionally overwrites it with
        # options.get('owner_id') -> None, which silently detached every bot from its
        # space. Keep the space id on its own attribute.
        self.space_owner = spaces.normalise_owner(space_owner)

        self.console = Console()
        self.aliases = {}
        self.config = {}
        self.accounts = []
        self.token = token
        self.channels = channels or []
        self.proxy_url = proxy_url
        self.proxy_auth = proxy_auth
        self.proxy_label = proxy_label or "direct"
        self._load_config()
        
        if not self.token or not self.channels:
            if self.accounts:
                primary = self.accounts[0]
                self.token = self.token or primary.get('token')
                self.channels = self.channels or primary.get('channels', [])
        
        self.channel_id = int(self.channels[0]) if self.channels else None

        # channel id -> when we last logged a fetch failure for it. Every scheduled
        # command re-resolves its channel, so an unreachable one used to print one
        # ERROR line per command per tick and drown the log. See _resolve_channel.
        self._channel_error_logged = {}

        core_cfg = self.config.get('core', {})
        self.prefix = core_cfg.get('prefix', 'owo ')
        self.user_id = core_cfg.get('user_id')
        self.owo_bot_id = str(core_cfg.get('monitor_bot_id', '408785106942164992'))
        self.owo_user = None
        
        super().__init__(
            command_prefix=self.prefix,
            self_bot=True,
            enable_debug_events=True,
            proxy=proxy_url,
            proxy_auth=proxy_auth,
            # nothing here reads message history out of the cache, and a farm of
            # a few hundred clients paying for one is what fills the box's memory
            max_messages=None,
            # member chunking on connect is a burst of gateway traffic and CPU per
            # account; identity falls back to username/display name when a member
            # is not cached, so the farm does not need it
            chunk_guilds_at_startup=False,
        )
        
        self.username = "Bot"
        self.display_name = "Bot"
        self.nickname = None
        self.identifiers = []
        self.identity = IdentityManager(self)
        self.modules = {}
        self.active = True
        self.paused = False
        self.warmup_until = time.time() + 10
        self.throttle_until = 0.0
        self.last_sent_time = 0
        self.last_sent_command = ""
        self.command_lock = asyncio.Lock()
        self.min_command_interval = 2.2
        self.command_history = []
        self.is_ready = False
        self._systems_started = False
        self.cmd_cooldowns = {}
        self.cmd_states = {}
        self.neura_queue = asyncio.PriorityQueue()
        self.neura_scheduler_task = None
        # Every background worker this bot spawns, so stop_account can cancel
        # the lot instead of only neura_scheduler_task. The others loop on
        # `while self.active` and would wind down on their own, but cancelling
        # them explicitly closes the small window where a worker wakes between
        # active=False and bot.close() and tries to send on a closing gateway.
        self.worker_tasks = []
        self.is_busy = False
        self.grind_active_time = 0.0
        self.last_break_check = 0.0
        self.is_on_break = False
        self.break_lock = asyncio.Lock()


        self.is_mobile = "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", "")
        platform = "Mobile (Termux)" if self.is_mobile else "Desktop"
        _log.info(f"Initialized bot on platform: {platform}")

    # "a long OwO exchange is mid-flight" - currently only huntbot sets it, and only
    # ChannelSwitch reads it, to hold off a channel rotation that would strand the
    # reply in the old channel. The window is seconds: huntbot clears it as soon as
    # OwO answers.
    BUSY_TIMEOUT_S = 120
    _busy_until = 0.0

    @property
    def is_busy(self):
        """Backed by a deadline rather than a plain bool.

        Every writer used to be responsible for clearing the flag, so one reply OwO
        never sent - a dropped message, an unsolvable huntbot captcha, an exception
        in the solver - latched it True for the life of the process and silently
        disabled channel rotation with no log line to say why.
        """
        return time.time() < self._busy_until

    @is_busy.setter
    def is_busy(self, value):
        self._busy_until = (time.time() + self.BUSY_TIMEOUT_S) if value else 0.0

    async def setup_hook(self):
        # login() runs this on every session, including the ones run_bot restarts after a
        # dropped gateway - the workers and cogs from the first session are still alive
        if self._systems_started:
            return
        self._systems_started = True

        if self.proxy_url and self.proxy_url.startswith(("socks4://", "socks5://")):
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(self.proxy_url, rdns=True)
                self.session = aiohttp.ClientSession(connector=connector)
            except Exception:
                self.session = aiohttp.ClientSession()
        else:
            self.session = aiohttp.ClientSession()
        self.interactions = setup_interactions(self)
        self.captcha_solver = setup_solver(self)
        self.web_solver = setup_web_solver(self)
        self.browser_solver = setup_browser_solver(self)
        self.log("SYS", "Initializing systems...")
        
        try:
            # opens (and, on an old database, migrates) this space's history db and
            # closes sessions a previous run left behind. Off the loop: the schema
            # upgrade backfills a unix timestamp for every historical row, and the
            # dashboard is sitting on a 60s future waiting for this account to start.
            await asyncio.to_thread(state.ht.start_session, owner=self.space_owner)
        except Exception as e:
            self.log("ERROR", f"Failed to open history database: {e}")

        self.worker_tasks = [
            asyncio.create_task(self._process_pending_commands()),
            asyncio.create_task(self.neura_queue_worker()),
            asyncio.create_task(self._track_active_time()),
        ]
        self.neura_scheduler_task = asyncio.create_task(self.neura_scheduler_worker())
        self.worker_tasks.append(self.neura_scheduler_task)
        await self._load_cogs()
    
    async def _track_active_time(self):
        await self.wait_until_ready()
        while self.active:
            if not self.paused:
                self.grind_active_time += 1.0
            await asyncio.sleep(1.0)

    async def _process_pending_commands(self):
        await asyncio.sleep(5)
        while self.active:
            if not self.is_ready:
                await asyncio.sleep(1)
                continue
            
            st = self.stats
            if 'pending_commands' in st and st['pending_commands']:
                pending = st['pending_commands'][:]
                for cmd_data in pending:
                    if time.time() - cmd_data['timestamp'] < 300:
                        success = await self.send_message(cmd_data['command'])
                        if success:
                            st['pending_commands'] = [
                                c for c in st['pending_commands'] 
                                if c['timestamp'] != cmd_data['timestamp']
                            ]
                    else:
                        st['pending_commands'] = [
                            c for c in st['pending_commands'] 
                            if c['timestamp'] != cmd_data['timestamp']
                        ]
            await asyncio.sleep(2)
    
    def get_startup_delay(self, offset=0):
        return random.uniform(5, 15) + offset

    async def on_ready(self):
        if getattr(self, '_already_ready', False):
            # A full re-IDENTIFY (RESUME failed) lands us here. on_disconnect
            # already flipped is_ready to False, so we must re-arm it - the old
            # code returned without doing so, leaving the bot stuck "CONNECTING"
            # and silently refusing to send (send_message gates on is_ready).
            if self.user:
                self.is_ready = True
                self.flag_account("ok", None)
            _log.info(f"Reconnected as {self.user.name if self.user else 'unknown'}")
            return

        self.user_id = str(self.user.id)
        self.username = self.user.name
        self.display_name = self.user.display_name
        self.user_display_name = self.display_name

        # The start path already refuses a token that is live somewhere else, but
        # two *different* tokens can belong to one Discord account (a re-login
        # mints a new one, and the old entry keeps the stale string). Until READY
        # there is no way to know that, so this is the second half of the same
        # guard: whoever got here first keeps the session, the newcomer leaves.
        # Two gateways farming one account double every command it sends.
        twin = next((b for b in state.bot_instances
                     if b is not self and str(getattr(b, 'user_id', '') or '') == self.user_id), None)
        if twin:
            other = getattr(twin, 'account_name', None) or 'another entry'
            self.log("ERROR", f"{self.username} is already running as '{other}' - "
                              f"stopping this duplicate instead of farming it twice")
            self.flag_account("duplicate", f"same Discord account as '{other}'")
            self.active = False
            self.is_ready = False
            try:
                await self.close()
            except Exception:
                pass
            return

        # so the dashboard can tell which space a running account belongs to
        state.account_owners[self.user_id] = self.space_owner
        self._persist_user_id()
        await self._claim_setup_channel()
        
        self.identifiers = [
            self.username.lower(),
            self.display_name.lower(),
            f"<@{self.user_id}>",
            f"<@!{self.user_id}>"
        ]

        if self.user_id not in state.account_stats:
            state.account_stats[self.user_id] = state.get_empty_stats()
        
        st = state.account_stats[self.user_id]
        st['username'] = self.username
        st['space_owner'] = self.space_owner
        
        self._load_config()

        from modules.web_solver import setup_web_solver
        self.web_solver = setup_web_solver(self)
        self.browser_solver = setup_browser_solver(self)
        self.log("SYS", "WebSolver reinitialized with account-specific settings.")

        if not st.get('uptime_start'):
            st['uptime_start'] = time.time()
        
        for counter in ['hunt_count', 'battle_count', 'owo_count', 'total_cmd_count', 'other_count', 'captchas_solved', 'bans_detected', 'warnings_detected']:
            if counter not in st: st[counter] = 0
            
        if 'cowoncy_history' not in st: st['cowoncy_history'] = []
        
        self.log("SYS", f"Ready as {self.username} (Display: {self.display_name})")
        
        self.cmd_states.clear()
        
        for cog in self.cogs.values():
            if hasattr(cog, 'register_actions'):
                try:
                    await cog.register_actions()
                except Exception as e:
                    self.log("ERROR", f"Failed to register {type(cog).__name__} actions: {e}")

        active_cmds = [f"{k}({v['delay']}s)" for k, v in self.cmd_states.items()]
        self.log("DEBUG", f"Active Scheduler: {', '.join(active_cmds) if active_cmds else 'None'}")
        
        # The solvers were already rebuilt above, right after _load_config, which
        # is the only thing that changes what they read. Building them a second
        # time here re-ran that work on every ready *and* every reconnect, and
        # threw away any WebSolver state - a queued manual solve, for one - that
        # had been created in between.
        self.interactions = setup_interactions(self)

        self.log("INFO", f"Channel: {self.channel_id}")
        
        self.is_ready = True
        self._already_ready = True
        self.flag_account("ok", None)
        
        if self.session is None:
            self.session = aiohttp.ClientSession()

    async def on_disconnect(self):
        # The gateway dropped. While we are reconnecting the account is not
        # farming, but it is also not gone - flip is_ready so the dashboard can
        # show CONNECTING instead of dropping the card entirely (which made it
        # look like the bot had "stopped by itself").
        self.is_ready = False
        self.log("WARN", "Gateway disconnected - attempting to resume/reconnect.")

    async def on_resumed(self):
        # A clean RESUME restores the session without a fresh on_ready, so
        # re-arm is_ready ourselves - otherwise the card would stay stuck on
        # CONNECTING even though the bot is live again.
        if self.user:
            self.is_ready = True
            self.log("SYS", "Gateway session resumed.")

    async def on_connect(self):
        # The TCP/identify step succeeded but cogs may not be ready yet; leave
        # is_ready to on_ready/on_resumed. This hook exists so discord.py does
        # not warn about an unhandled event during reconnects.
        pass

    async def on_command_error(self, context, exception):
        """Swallow "command not found" for our own outgoing OwO commands.

        With ``self_bot=True`` discord.py's skip check is inverted: the only
        messages it feeds through the command parser are the ones *we* send. Our
        prefix is OwO's (`owo `), so every `owo hunt` / `owo b` / `owo inv` this
        bot sends is re-read as an invocation of a local command named `hunt` /
        `b` / `inv`, none of which exist - and the default handler logs each one
        at ERROR level ("Ignoring exception in command None"). On a ten-account
        deploy that was 42% of the Railway log: two error lines per command sent,
        describing nothing that went wrong.

        This cog set registers no ext.commands commands at all (every trigger is
        an on_message listener), so CommandNotFound here is always that echo.
        Anything else is a real bug in a real command and still gets logged.
        """
        if isinstance(exception, commands.CommandNotFound):
            return
        _log.error("Ignoring exception in command %s", getattr(context, 'command', None),
                   exc_info=exception)

    # How long to stay quiet about a channel we have already complained about.
    # This throttles the *log line only* - the fetch itself still runs every tick.
    # A "Missing Access" is not reliably permanent: during a reconnect, or through
    # a flaky proxy, Discord answers 50001 for a channel the account really is in,
    # and it starts working again on its own a moment later. Suppressing the retry
    # would turn a noisy self-healing failure into a silent one - an account that
    # looks alive on the dashboard and quietly sends nothing.
    _CHANNEL_ERROR_QUIET_S = 300

    async def _resolve_channel(self, c_id):
        channel = self.get_channel(c_id)
        if not channel:
            try:
                channel = await self.fetch_channel(c_id)
            except Exception as e:
                last = self._channel_error_logged.get(c_id, 0)
                if time.time() - last >= self._CHANNEL_ERROR_QUIET_S:
                    self._channel_error_logged[c_id] = time.time()
                    self.log("ERROR", f"Failed to fetch channel {c_id}: {e} - if this "
                                      f"repeats, check that this account is in that "
                                      f"server and that the channel id is right.")
                return None
            self._channel_error_logged.pop(c_id, None)
        return channel

    async def _raw_send(self, content, channel, skip_typing=False):
        """Put a message on the wire. Every safety gate is the caller's job."""
        typing_enabled = self.config.get('stealth', {}).get('typing_enabled', False)
        try:
            if typing_enabled and not skip_typing:
                sent_ok = await NeuraHuman.neura_send(self, channel, content)
                if not sent_ok:
                    return False
            else:
                await channel.send(content)

            self.last_sent_time = time.time()
            short_cmd = content[:30] + "..." if len(content) > 30 else content
            typing_str = ""
            if getattr(self, 'last_typing_time', None):
                typing_str = f" ({self.last_typing_time}s)"
                self.last_typing_time = None
            self.log("CMD", f"Sent: {short_cmd}{typing_str}")
            # the earning ledger has to know the exact figure we handed the huntbot,
            # and this is the only place that knows what actually went out. Wrapped
            # because a bookkeeping bug must never turn into a failed send.
            try:
                cog = self.get_cog("Earning")
                if cog:
                    cog.note_sent(content)
            except Exception:
                pass
            return True
        except Exception as e:
            self.note_send_failure(e)
            return False

    async def _send_safe(self, content, skip_typing=False, target_channel_id=None, priority=False, force=False):
        if not content or not self.is_ready:
            return False

        content = self._fix_command(content)

        # a captcha answer has to reach OwO *while* the account is paused, and it cannot
        # wait for command_lock either: the send that tripped the captcha is usually still
        # parked in the pause loop below, holding it until the captcha is solved
        if force:
            channel = await self._resolve_channel(target_channel_id or self.channel_id)
            if not channel:
                return False
            return await self._raw_send(content, channel, skip_typing=skip_typing)

        async with self.command_lock:
            current_time = time.time()
            if current_time < self.warmup_until:
                 await asyncio.sleep(max(0.1, self.warmup_until - current_time))

            while (self.paused or time.time() < self.throttle_until) and self.active:
                if self.paused or self.throttle_until == float('inf'):
                    self.log("INFO", "Safety Pause: Paused until manually resumed or captcha solved")
                    while (self.paused or self.throttle_until == float('inf')) and self.active:
                        await asyncio.sleep(1)
                else:
                    wait_time = self.throttle_until - time.time()
                    self.log("INFO", f"Safety Pause: Resuming in {round(wait_time, 1)}s (Waiting for OwO Slow-Down)")
                    while time.time() < self.throttle_until and not self.paused and self.throttle_until != float('inf') and self.active:
                        rem = self.throttle_until - time.time()
                        if rem <= 0:
                            break
                        await asyncio.sleep(min(1.0, rem))
                    if not self.paused and self.throttle_until != float('inf') and self.active:
                        await asyncio.sleep(0.1)

            if not self.active or self.paused:
                return False

            stealth_cfg = self.config.get('stealth', {})
            typing_enabled = stealth_cfg.get('typing_enabled', False)
            wait_limit = 0.0 if not typing_enabled else (1.2 if priority else self.min_command_interval)
            
            now = time.time()
            elapsed = now - self.last_sent_time
            if elapsed < wait_limit:
                rem_wait = wait_limit - elapsed
                while rem_wait > 0 and self.active and not self.paused:
                    sleep_dur = min(1.0, rem_wait)
                    await asyncio.sleep(sleep_dur)
                    rem_wait -= sleep_dur

            if not self.active or self.paused:
                return False

            c_id = target_channel_id or self.channel_id
            channel = await self._resolve_channel(c_id)

            if not channel or not self.active or self.paused:
                return False

            return await self._raw_send(content, channel, skip_typing=skip_typing)

    def _fix_command(self, command):
        cmd = command.strip()
        if cmd.lower() == "owo": return "owo"
        if cmd.lower().startswith("owo owo"): cmd = cmd[4:]
        
        if self.shortforms:
            parts = cmd.split()
            if parts:
                base_cmd = parts[0].lower()
                prefix = self.prefix.lower()
                
                actual_cmd = base_cmd[len(prefix):] if base_cmd.startswith(prefix) else base_cmd
                
                if actual_cmd in self.shortforms:
                    if self.config.get('commands', {}).get(actual_cmd, {}).get('use_shortform', False):
                        new_base = self.shortforms[actual_cmd]
                        parts[0] = f"{self.prefix}{new_base}" if base_cmd.startswith(prefix) else new_base
                        cmd = " ".join(parts)

        # anything a cog may enqueue without the prefix has to be listed here, or it
        # is posted as plain chat: owo ignores it, the cog waits forever for a reply
        # that never comes, and the channel fills with bare words like "weapon".
        # `equip` was the live proof of that - the weapon manager's own command was
        # missing from this list, so every equip it sent was chat, not a command.
        known = ['hunt', 'battle', 'curse', 'huntbot', 'daily', 'cookie',
                'quest', 'checklist', 'cf', 'slots', 'bj', 'blackjack', 'autohunt', 'upgrade',
                'sacrifice', 'team', 'zoo', 'use', 'inv', 'sell', 'crate',
                'lootbox', 'run', 'pup', 'piku', 'pray', 'weapon', 'equip']
        
        if self.shortforms:
            for sf in self.shortforms.values():
                if sf not in known:
                    known.append(sf)

        first = cmd.lower().split()[0] if cmd else ""
        if first in known and not cmd.lower().startswith(self.prefix.lower()):
            return f"{self.prefix}{cmd}"
        return cmd
    
    async def send_message(self, content, skip_typing=False, priority=False, target_channel_id=None, force=False):
        if not self.active:
            return False
        # force is for safety-critical replies (captcha answers): paused accounts still
        # have to answer OwO or the pause never lifts
        if self.paused and not force:
            return False

        if state.checking_gems.get(self.user_id) and not force:
            cmd_clean = content.lower().strip()
            if "hunt" in cmd_clean or "battle" in cmd_clean:
                if "huntbot" not in cmd_clean and "autohunt" not in cmd_clean:
                    return False

        fixed_content = self._fix_command(content)
        self.last_sent_command = fixed_content

        success = await self._send_safe(fixed_content, skip_typing=skip_typing, target_channel_id=target_channel_id, priority=priority, force=force)
        return success
    
    @property
    def stats(self):
        if not hasattr(self, '_connection') or not self.user: return {}
        uid = str(self.user.id)
        if uid not in state.account_stats:
            state.account_stats[uid] = state.get_empty_stats()
            state.account_stats[uid]['username'] = self.username
        return state.account_stats[uid]

    def log(self, log_type, message):
        neura_logger.log(self, log_type, message)

    def flag_account(self, status, reason=None):
        """Record token/permission problems against the account in accounts.json."""
        name = getattr(self, 'account_name', None)
        if not name:
            return
        try:
            from utils import proxy_manager
            proxy_manager.set_account_status(self.space_owner, name, status, reason)
        except Exception as e:
            _log.warning("could not record account status: %s", e)

    def _persist_user_id(self):
        """Write this account's discord id into accounts.json.

        `spaces.owner_for_account` falls back to scanning accounts.json when the
        account is not running, and that fallback needs the id to have been saved
        at least once - otherwise the dashboard cannot prove ownership of a
        stopped account and hides its history.
        """
        name = getattr(self, 'account_name', None)
        if not name or not self.user_id:
            return
        try:
            from utils import proxy_manager
            proxy_manager.set_account_user_id(self.space_owner, name, self.user_id)
        except Exception as e:
            _log.warning("could not record account user id: %s", e)

    async def _claim_setup_channel(self):
        """Ask the monitor to open the channel setup assigned this account.

        Channel setup runs with the accounts stopped, so an account that has never
        connected can reach it with no Discord id on record - and with no id there
        is no permission overwrite to write. It is given the channel anyway,
        because an account without one cannot be started at all. The gateway has
        just told us who this account is, so this is the moment the overwrite can
        be added. Does nothing for an account whose channel is already open to it.
        """
        name = getattr(self, 'account_name', None)
        if not name or not self.user_id:
            return
        try:
            from core import monitor
            granted = await asyncio.wait_for(
                monitor.grant_channel_access(self.space_owner, name, self.user_id), timeout=30)
        except Exception as e:
            self.log("WARN", f"Could not claim the channel assigned by setup: {e}")
        else:
            if granted:
                self.log("SUCCESS", "Monitor opened the assigned channel to this account")

    async def on_socket_raw_receive(self, msg):
        """Parse each gateway frame once and hand the result to every cog.

        Five cogs (quest, others, weapons, boss, owner) all want the raw
        components-v2 payload. Each used to decode and json.loads the same frame
        itself, so one message cost five parses per account - the dominant CPU
        cost of the whole process once a farm passes ~20 accounts. Now the frame
        is filtered and parsed here and re-dispatched as ``owo_gateway_message``,
        which the cogs listen for instead.
        """
        if isinstance(msg, (bytes, bytearray)):
            try:
                msg = msg.decode('utf-8', errors='replace')
            except Exception:
                return
        if not isinstance(msg, str):
            return

        # a substring test is ~100x cheaper than a parse, and every listener only
        # ever wants these two frame types
        if '"MESSAGE_CREATE"' not in msg and '"MESSAGE_UPDATE"' not in msg:
            return

        try:
            payload = json.loads(msg)
        except Exception:
            return
        if payload.get("t") not in ("MESSAGE_CREATE", "MESSAGE_UPDATE"):
            return

        data = payload.get('d') or {}
        mid = str(data.get('id', ''))
        cache = getattr(self, '_owo_frames', None)
        if cache is None:
            cache = self._owo_frames = {}
        if str((data.get('author') or {}).get('id')) == str(self.owo_bot_id) or mid in cache:
            merged = dict(cache.get(mid, {}), **data)
            if len(cache) >= 64 and mid not in cache:
                cache.pop(next(iter(cache)))
            cache[mid] = merged
            payload['d'] = merged
        self.dispatch('owo_gateway_message', payload)

    def note_send_failure(self, exc):
        """Classify a failed send, flag the account and stop hammering Discord."""
        detail = f"{type(exc).__name__}: {exc}"
        lowered = detail.lower()

        if "verify your account" in lowered or getattr(exc, 'code', None) == 40002 or "captcha" in lowered:
            self.log("ERROR", f"Send failed - account needs verification: {exc}")
            self.flag_account("needs_verification", detail[:200])
            if not self.paused:
                self.paused = True
                self.log("SECURITY", "Paused: Discord wants this account verified. Verify it, then resume the account from the dashboard.")
        elif isinstance(exc, discord.Forbidden):
            self.log("ERROR", f"Send failed - no permission: {exc}")
            self.flag_account("cannot_send", detail[:200])
            if not self.paused:
                self.paused = True
                self.log("SECURITY", "Paused: this account cannot post in its channel.")
        else:
            self.log("ERROR", f"Send failed: {detail}")

    async def _load_cogs(self):
        cogs_dir = os.path.join(self.base_dir, 'cogs')
        for filename in os.listdir(cogs_dir):
            # a leading underscore marks a non-cog helper; load_extension would
            # raise NoEntryPointError on it and log a failure on every boot
            if filename.endswith('.py') and not filename.startswith('_'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    self.log("SYS", f"Loaded {filename}")
                except Exception as e:
                    self.log("ERROR", f"Failed to load {filename}: {e}")
    
    def _collect_changed_paths(self, old, new, prefix=""):
        """Return dotted paths that differ between two config dicts."""
        changed = set()
        if not isinstance(old, dict) or not isinstance(new, dict):
            p = prefix.rstrip(".")
            if p and old != new:
                changed.add(p)
            return changed
        for key in set(old.keys()) | set(new.keys()):
            path = f"{prefix}{key}" if prefix else key
            ov, nv = old.get(key), new.get(key)
            if isinstance(ov, dict) and isinstance(nv, dict):
                sub = self._collect_changed_paths(ov, nv, f"{path}.")
                if sub:
                    changed.add(path)
                    changed.update(sub)
            elif ov != nv:
                changed.add(path)
        return changed

    def _cogs_for_config_changes(self, changed_paths):
        """Map changed config paths to cog class names that need register_actions."""
        cog_names = set()
        cmd_to_cog = {
            "owo": "Grinding", "hunt": "Grinding", "battle": "Grinding",
            "coinflip": "Gambling", "slots": "Gambling", "blackjack": "Gambling",
            "curse": "NeuraCursePray", "pray": "NeuraCursePray",
            "shop": "Shop", "huntbot": "HuntBot", "daily": "Daily",
            "quest": "Quest", "rpp": "RPP", "cookie": "Cookie",
            "level_grind": "LevelQuotes",
            "team": "Others", "weapon": "Weapons", "custom": "CustomCommands",
            # reactive cogs - they read self.bot.config on every event, so there is
            # nothing to re-register. They are listed so an unknown key does not fall
            # through to the Grinding default and pointlessly re-roll hunt/battle timers.
            "gems": None, "sell_sac": None, "open": None, "giveaway": None,
        }
        top_to_cog = {
            "reactionBot": ["ReactionBot"],
            "security": ["Security"],
            "boss": ["Boss"],
            "utilities": ["ChannelSwitch", "Others"],
            "level_grind": ["LevelQuotes"],
            "coop": ["Coop", "Quest"],
            "owner": ["Owner"],
            # the overlay turns an `earning` change into `commands.*` changes as well,
            # so the grinding cogs are refreshed by cmd_to_cog above - this is only
            # about the ledger cog noticing that the mode itself flipped
            "earning": ["Earning"],
        }
        for path in changed_paths:
            if path == "commands" or path.startswith("commands."):
                parts = path.split(".")
                if len(parts) >= 2:
                    target = cmd_to_cog.get(parts[1], "Grinding")
                    if target:
                        cog_names.add(target)
                else:
                    cog_names.update(c for c in cmd_to_cog.values() if c)
            elif path.split(".")[0] in top_to_cog:
                cog_names.update(top_to_cog[path.split(".")[0]])
        return cog_names

    def _prune_disabled_scheduler_cmds(self):
        """Remove scheduler entries for commands that are now disabled."""
        cmds = self.config.get("commands", {})
        coop = self.config.get("coop", {})

        def enabled(name, default=False):
            return bool(cmds.get(name, {}).get("enabled", default))

        rules = [
            ("owo", enabled("owo")),
            ("hunt", enabled("hunt")),
            ("battle", enabled("battle")),
            ("coinflip", enabled("coinflip")),
            ("slots", enabled("slots")),
            ("blackjack", enabled("blackjack")),
            ("cursepray", enabled("curse") or enabled("pray")),
            ("daily", enabled("daily")),
            ("quest", enabled("quest")),
            ("rpp", enabled("rpp")),
            ("cookie", enabled("cookie")),
            ("huntbot", enabled("huntbot")),
            ("shop_buy", enabled("shop")),
            ("shop_cash_sync", enabled("shop")),
            ("level_quotes", self.config.get("level_grind", {}).get("enabled", False)),
            ("channelswitch", self.config.get("utilities", {}).get("autochannel", {}).get("enabled", False)),
            ("cash_sync", self.config.get("utilities", {}).get("stats_sync", {}).get("balance", True)),
            ("level_sync", self.config.get("utilities", {}).get("stats_sync", {}).get("level", True)),
            ("zoo_sync", self.config.get("utilities", {}).get("stats_sync", {}).get("zoo", True)),
            ("team_scan", enabled("team", True)),
            ("weapon_scan", enabled("weapon", True)),
            # the zoo watcher is event driven (it reacts to hunt results), so it owns no
            # scheduler slot of its own - team_scan above is its periodic backstop.
            # zoo_sync is a separate thing: it is the dashboard's own zoo refresh and
            # runs whether or not the team manager is switched on.
            ("coop_offer", bool(coop.get("enabled", True)) and bool(coop.get("battle", {}).get("enabled", True))),
        ]
        for cmd_id, is_on in rules:
            if not is_on and cmd_id in self.cmd_states:
                del self.cmd_states[cmd_id]

        # every custom command owns a `custom_<n>` scheduler slot; drop the ones that
        # were deleted or switched off so a removed row stops firing without a restart
        custom_cfg = cmds.get("custom", {})
        live_custom = set()
        if custom_cfg.get("enabled", False):
            for index, entry in enumerate(custom_cfg.get("commands", []) or []):
                if not isinstance(entry, dict) or not str(entry.get("command", "")).strip():
                    continue
                if entry.get("enabled", True) is False:
                    continue
                try:
                    interval = float(entry.get("interval_s") or 0)
                except (TypeError, ValueError):
                    interval = 0
                if interval > 0:
                    live_custom.add(f"custom_{index}")
        for cmd_id in [c for c in self.cmd_states if c.startswith("custom_")]:
            if cmd_id not in live_custom:
                del self.cmd_states[cmd_id]

    async def sync_settings(self, new_config):
        """Merge settings and only refresh scheduler modules that actually changed."""
        old_config = copy.deepcopy(self.config)
        self._load_config()
        # Callers persist their changes first. Reload the same layered view used
        # at startup, so a space save cannot temporarily erase account overrides.
        # the POST body is the operator's document, with no overlay in it, so the merge
        # above just undid earning mode. Re-apply before diffing or a save from any tab
        # would quietly hand the account back its gambling timers.
        self._apply_earning_overlay()

        core_cfg = self.config.get("core", {})
        self.prefix = core_cfg.get("prefix", "owo ")
        if hasattr(self, "_connection"):
            self.command_prefix = self.prefix

        changed = self._collect_changed_paths(old_config, self.config)
        if not changed:
            self.log("SYS", "Settings saved (no changes detected).")
            return

        cogs_to_refresh = self._cogs_for_config_changes(changed)
        scheduler_paths = {
            p for p in changed
            if p == "commands" or p.startswith("commands.")
            or p.startswith("utilities.") or p in ("reactionBot", "level_grind", "coop")
            or p.startswith("reactionBot.") or p.startswith("coop.")
        }

        if scheduler_paths:
            self._prune_disabled_scheduler_cmds()
            for cog in self.cogs.values():
                name = type(cog).__name__
                if name in cogs_to_refresh and hasattr(cog, "register_actions"):
                    try:
                        await cog.register_actions()
                    except Exception as e:
                        self.log("ERROR", f"Failed to refresh {name}: {e}")
            self.log("SYS", f"Settings updated ({len(changed)} change(s)). Scheduler modules refreshed: {', '.join(sorted(cogs_to_refresh)) or 'none'}")
        else:
            for cog in self.cogs.values():
                name = type(cog).__name__
                if name in cogs_to_refresh and hasattr(cog, "register_actions"):
                    try:
                        await cog.register_actions()
                    except Exception as e:
                        self.log("ERROR", f"Failed to refresh {name}: {e}")
            self.log("SYS", f"Settings updated ({len(changed)} change(s), no scheduler restart).")

        active_cmds = [f"{k}({round(v['delay'], 1)}s)" for k, v in self.cmd_states.items()]
        self.log("DEBUG", f"Active Scheduler: {', '.join(active_cmds) if active_cmds else 'None'}")

    def _deep_merge(self, base, override):
        for key, value in override.items():
            if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    # switched off by earning mode when `earning.exclusive` is set: each of these
    # either spends cowoncy the run is trying to accumulate (the gambling three) or
    # spends *time* the account could be hunting with.
    # Everything that feeds the hunt - daily, gems, team, battle, weapon, quest,
    # shop - is deliberately absent, and so is `owo` itself: it is what keeps the
    # account ranked and costs nothing.
    EARNING_DISTRACTIONS = ("coinflip", "slots", "blackjack", "curse",
                            "pray", "cookie", "rpp", "giveaway", "sell_sac")

    def _apply_earning_overlay(self):
        """Point every command gate at hunt + huntbot + sell + a growing team.

        This runs *after* all three config layers have merged, so it beats both the
        space default and the per-account file - "redirect all bots to earning" has to
        mean the same thing on an account that had battle switched on by hand. It is
        never written back to disk: the config editor keeps showing what the operator
        chose, and switching the mode off restores it on the next reload.
        """
        earning = self.config.get('earning') or {}
        if not isinstance(earning, dict) or not earning.get('enabled', False):
            return

        cmds = self.config.setdefault('commands', {})

        def section(name):
            sub = cmds.get(name)
            if not isinstance(sub, dict):
                sub = {}
                cmds[name] = sub
            return sub

        section('hunt')['enabled'] = True
        huntbot = section('huntbot')
        huntbot['enabled'] = True
        try:
            cash = int(earning.get('huntbot_cash', 3000))
        except (TypeError, ValueError):
            cash = 3000
        if cash > 0:
            huntbot['cash_to_spend'] = cash

        # battle and the team watcher are part of earning, not a distraction from it:
        # battling levels the animals, the watcher swaps a rarer catch into the team,
        # and a stronger team is what makes the next hunt worth more. Both are free.
        section('battle')['enabled'] = True
        team = section('team')
        team['enabled'] = True
        # so a rarer catch upgrades the team the moment it lands instead of waiting
        # out the scan timer
        team['watch_zoo'] = True

        # sell_sac owns its own loop and reads config on every tick, so the sell half
        # is enabled here and the sacrifice half left exactly as configured: sacrificing
        # an animal destroys the thing we are trying to sell.
        sell_sac = section('sell_sac')
        sell = sell_sac.get('sell')
        if not isinstance(sell, dict):
            sell = {}
            sell_sac['sell'] = sell
        sell['enabled'] = True
        sell['type'] = str(earning.get('sell_type') or 'all')
        try:
            interval = int(earning.get('sell_interval_min', 20))
        except (TypeError, ValueError):
            interval = 20
        sell['interval_min'] = max(1, interval)

        if earning.get('exclusive', True):
            for name in self.EARNING_DISTRACTIONS:
                if name == 'sell_sac':
                    continue
                section(name)['enabled'] = False
            # the NPC battle above already delivers the xp that levels the team, so a
            # coop battle only spends a *second* account's turn on the same reward
            coop = self.config.setdefault('coop', {})
            battle = coop.get('battle')
            if not isinstance(battle, dict):
                battle = {}
                coop['battle'] = battle
            battle['enabled'] = False

    def _load_config(self):
        # a mid-way failure must not leave the bot with an empty config - that would
        # silently drop every command gate down to its hardcoded default
        previous_config = self.config if isinstance(getattr(self, 'config', None), dict) else {}
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    self.config = json.load(f)
            else:
                self.config = {}

            uid = getattr(self, 'user_id', None)
            if not uid and hasattr(self, '_connection') and self.user:
                uid = str(self.user.id)

            # three layers: the shipped defaults, then whatever this space chose
            # for all of its accounts, then this one account's overrides
            space_config_file = spaces.settings_path(self.space_owner)
            if os.path.exists(space_config_file):
                try:
                    with open(space_config_file, 'r') as f:
                        self._deep_merge(self.config, json.load(f))
                except Exception as e:
                    self.log("ERROR", f"Failed to load space settings.json: {e}")

            if uid:
                user_config_file = spaces.settings_path(self.space_owner, uid)
                
                if os.path.exists(user_config_file):
                    try:
                        with open(user_config_file, 'r') as f:
                            user_cfg = json.load(f)
                            self._deep_merge(self.config, user_cfg)
                        self.log("SYS", f"Using account-specific settings: settings_{uid}.json")
                    except Exception as e:
                        self.log("ERROR", f"Failed to load user settings_{uid}.json: {e}")
                else:
                    try:
                        with open(user_config_file, 'w') as f:
                            json.dump({}, f, indent=4)
                        self.log("SYS", f"Created personal settings file: settings_{uid}.json")
                    except Exception as e:
                        self.log("ERROR", f"Failed to create settings_{uid}.json: {e}")
            else:
                self.log("SYS", "Using global settings: settings.json")

            account_file = spaces.accounts_path(self.space_owner)
            if os.path.exists(account_file):
                try:
                    with open(account_file, 'r') as f:
                        self.accounts = json.load(f).get('accounts', [])
                except:
                    self.accounts = []
            else:
                self.accounts = []

            if self.accounts:
                current_acc = None
                if uid:
                    current_acc = next((a for a in self.accounts if str(a.get('id', a.get('user_id', ''))) == uid), None)
                if not current_acc and self.token:
                    current_acc = next((a for a in self.accounts if a.get('token') == self.token), None)
                
                if current_acc:
                    new_channels = current_acc.get('channels', [])
                    if new_channels != self.channels:
                        self.channels = new_channels
                        if self.channels:
                            if str(self.channel_id) not in [str(c) for c in self.channels]:
                                self.channel_id = int(self.channels[0])
                                self.log("SYS", f"Channel rotated to {self.channel_id} (Config Update)")
                        self.log("SYS", f"Channels updated from accounts.json: {len(self.channels)} available")
                
                elif not self.channels:
                    primary = self.accounts[0]
                    self.channels = primary.get('channels', [])
                    self.channel_id = int(self.channels[0]) if self.channels else None

            shortform_file = os.path.join(state.CONFIG_DIR, 'shortform.json')
            if os.path.exists(shortform_file):
                try:
                    with open(shortform_file, 'r') as f:
                        self.shortforms = json.load(f)
                except:
                    self.shortforms = {}
            else:
                self.shortforms = {}

            core_cfg = self.config.get('core', {})
            self.prefix = core_cfg.get('prefix', 'owo ')
            if hasattr(self, '_connection'):
                self.command_prefix = self.prefix

            # last, so it overrides every layer - and after the settings_<uid>.json
            # write above, so the overlay is never persisted as the operator's choice
            self._apply_earning_overlay()

        except Exception as e:
            print(f"Error loading config: {e}")
            self.config = previous_config


    def check_version(self):
        self.log("SYS", f"LazyFarmers {CURRENT_VERSION}")

    async def run_bot(self):
        self.check_version()
        route = f"via {self.proxy_label}" if self.proxy_label != "direct" else "direct connection"
        self.log("SYS", f"Starting bot ({route})...")

        attempt = 0
        while self.active:
            session_start = time.time()
            try:
                if self.is_closed():
                    self.clear()
                # Every login - a Start click or a reconnect after a blip - queues
                # behind the same global gate, so a farm coming back from one
                # dropped uplink hands the gateway one IDENTIFY at a time instead
                # of 200 TLS handshakes at once. Metering here rather than in the
                # start path is the point: the reconnect below is not a start.
                await state.login_slot()
                if not self.active:
                    return
                await self.start(self.token)
            except discord.LoginFailure as e:
                self.log("ERROR", f"Login failed: {e}. Update the token in the dashboard.")
                self.flag_account("invalid_token", str(e)[:200])
                return
            except discord.ConnectionClosed as e:
                if e.code == 4004:
                    self.log("ERROR", "Token rejected by Discord (4004). Update the token in the dashboard.")
                    self.flag_account("invalid_token", "gateway rejected the token (4004)")
                    return
                # discord.py re-raises every close code except 1000, which kills the account
                # until we start a new session ourselves
                self.log("ERROR", f"Gateway closed with code {e.code}: {e}")
            except Exception as e:
                self.log("ERROR", f"Connection lost: {type(e).__name__}: {e}")

            if not self.active:
                return

            # A dropped gateway should look like a brief blip, not a 5-minute
            # outage: the old backoff (15 * attempt, capped at 300s) made an
            # account vanish from the dashboard for ages on a flaky proxy. Keep
            # it short and gentle - Discord tolerates quick retries fine.
            attempt = 1 if time.time() - session_start > 300 else attempt + 1
            # The jitter window grows with the farm. One dead proxy or one dropped
            # uplink drops every account at the same instant, and a fixed 0-3s
            # spread put 200 of them back on the gateway inside three seconds -
            # they then all failed together and retried in lockstep forever.
            # Spreading over roughly login_slot's own drain time breaks the lockstep.
            spread = min(60.0, max(3.0, len(state.bot_instances) * state.LOGIN_MIN_GAP_S))
            delay = min(60, 5 * attempt) + random.uniform(0, spread)
            self.log("WARN", f"Reconnecting in {round(delay)}s (attempt {attempt})...")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                # stop_account cancels the runner to break exactly this sleep
                raise

    def set_cooldown(self, cmd, seconds):
        self.cmd_cooldowns[cmd.lower()] = time.time() + seconds

    def get_cooldown(self, cmd):
        return max(0, self.cmd_cooldowns.get(cmd.lower(), 0) - time.time())

    def get_cmd_priority(self, cmd_id, default=3):
        """load priority from cmd_priorities.json, fallback to default."""
        try:
            prio_file = os.path.join(state.CONFIG_DIR, 'cmd_priorities.json')
            if os.path.exists(prio_file):
                with open(prio_file, 'r') as f:
                    priorities = json.load(f)
                return priorities.get(cmd_id, default)
        except Exception:
            pass
        return default

    def get_command_id_from_content(self, content):
        if not content:
            return None
        cmd_clean = content.lower().strip()
        prefix = self.prefix.lower().strip()
        if cmd_clean.startswith(prefix):
            cmd_clean = cmd_clean[len(prefix):].strip()
        elif cmd_clean.startswith("owo "):
            cmd_clean = cmd_clean[4:].strip()
        elif cmd_clean.startswith("uwu "):
            cmd_clean = cmd_clean[4:].strip()
            
        parts = cmd_clean.split()
        if not parts:
            return "owo"
            
        base = parts[0]
        alias_map = {
            "h": "hunt",
            "hunt": "hunt",
            "b": "battle",
            "battle": "battle",
            "fight": "battle",
            "pray": "cursepray",
            "curse": "cursepray",
            "cookie": "cookie",
            "rep": "cookie",
            "cf": "coinflip",
            "coinflip": "coinflip",
            "slots": "slots",
            "slot": "slots",
            "s": "slots",
            "daily": "daily",
            "rpp": "rpp",
            "owo": "owo"
        }
        return alias_map.get(base, base)


    def get_full_content(self, message):
        if not message: return ""
        content = message.content or ""
        embed_texts = []
        if message.embeds:
            for em in message.embeds:
                parts = [
                    em.title or "",
                    em.author.name if em.author else "",
                    em.description or "",
                    "\n".join([f"{f.name}: {f.value}" for f in em.fields])
                ]
                embed_texts.append("\n".join([p for p in parts if p]))
        return (content + "\n" + "\n".join(embed_texts)).lower()


    def is_message_for_me(self, message, role="any", keyword=None):
        return self.identity.is_message_for_me(message, role, keyword)

    async def neura_enqueue(self, content, priority=3, skip_typing=None, _cmd_id=None, target_channel_id=None):
        options = {"skip_typing": skip_typing, "_cmd_id": _cmd_id, "target_channel_id": target_channel_id}
        item = (priority, time.time(), content, options)
        await self.neura_queue.put(item)

    async def neura_queue_worker(self):
        await self.wait_until_ready()
        self.log("SYS", "Queue worker started.")
        while self.active:
            try:
                priority, ts, content, options = await self.neura_queue.get()
                cmd_id = options.get("_cmd_id")
                target_channel_id = options.get("target_channel_id")

                ran_successfully = False
                
                try:
                    if content == "":
                        if cmd_id == "channelswitch":
                            cog = self.get_cog("ChannelSwitch")
                            if cog: cog.trigger_switch()
                        
                        if cmd_id and cmd_id in self.cmd_states:
                           self.cmd_states[cmd_id]['last_ran'] = time.time()
                        
                        ran_successfully = True
                        continue
                    
                    if self.paused:
                        continue

                    gem_check_val = state.checking_gems.get(self.user_id)
                    if gem_check_val:
                        timestamp = gem_check_val.get("time") if isinstance(gem_check_val, dict) else (time.time() if isinstance(gem_check_val, bool) else gem_check_val)
                        
                        if timestamp and time.time() - timestamp > 20:
                            self.log("WARN", "Gem check timed out. Resuming queue.")
                            state.checking_gems[self.user_id] = False
                            gem_check_val = False
                    
                    if gem_check_val:
                        cmd_clean = content.lower().strip()
                        if "hunt" in cmd_clean or "battle" in cmd_clean:
                             if "huntbot" not in cmd_clean and "autohunt" not in cmd_clean:
                                continue

                    skip_typing = options.get("skip_typing")
                    if skip_typing is None:
                        skip_typing = priority <= 1 or content.lower().strip() == "owo"

                    if not cmd_id and content:
                        cmd_id = self.get_command_id_from_content(content)

                    if cmd_id and cmd_id in self.cmd_states:
                        state_info = self.cmd_states[cmd_id]
                        elapsed = time.time() - state_info['last_ran']
                        if elapsed < state_info['delay']:
                            remaining = state_info['delay'] - elapsed
                            if priority >= 4 and remaining > 60:
                                self.log("WARN", f"Quest Engine: Skipping '{content}' because '{cmd_id}' has a long remaining cooldown of {round(remaining, 1)}s")
                                continue
                            elif remaining <= 60:
                                self.log("INFO", f"Quest Engine: Deferring '{content}' for {round(remaining, 1)}s (Waiting for '{cmd_id}' cooldown)")
                                await asyncio.sleep(remaining + 0.5)

                    self.last_sent_command = content
                    sent = await self._send_safe(content, skip_typing=skip_typing, target_channel_id=target_channel_id, priority=(priority <= 1))
                    if not sent:
                        # Do not book daily or other success hooks for an unsent command.
                        await asyncio.sleep(1)
                        continue
                    ran_successfully = True
                    
                    if cmd_id and cmd_id in self.cmd_states:
                        self.cmd_states[cmd_id]['last_ran'] = time.time()
                    
                    if cmd_id and cmd_id in self.cmd_states:
                        # post-send recompute hooks. One map, not a list plus a map that
                        # could drift apart. Gambling is deliberately absent: its content
                        # is a scheduler callable now, so the next wager is computed at
                        # enqueue time off the freshest balance (see cogs/gambling.py).
                        class_map = {
                            "rpp": "RPP", "quest": "Quest", "level_quotes": "LevelQuotes",
                            "huntbot": "HuntBot", "daily": "Daily", "cookie": "Cookie",
                            "cursepray": "NeuraCursePray", "level_sync": "Others",
                        }
                        cog_name = class_map.get(cmd_id)
                        if cog_name:
                            cog = self.get_cog(cog_name)
                            if cog:
                                cog.trigger_action()
                
                finally:
                    if cmd_id and cmd_id in self.cmd_states:
                        self.cmd_states[cmd_id]['in_queue'] = False
                    self.neura_queue.task_done()

            except Exception as e:
                self.log("ERROR", f"Queue worker error: {e}")
                await asyncio.sleep(1)

    async def neura_register_command(self, cmd_id, content, priority, delay, initial_offset=0):
        existing = self.cmd_states.get(cmd_id, {})
        now = time.time()

        if existing and "last_ran" in existing:
            last_ran = existing["last_ran"]
            in_queue = existing.get("in_queue", False)
            old_delay = existing.get("delay", delay)
            if old_delay > 0 and abs(delay - old_delay) > 0.01:
                elapsed = max(0, now - last_ran)
                remaining_ratio = min(1.0, max(0, 1.0 - (elapsed / old_delay)))
                last_ran = now - (delay * (1.0 - remaining_ratio))
        else:
            last_ran = now - delay + initial_offset
            in_queue = False

        self.cmd_states[cmd_id] = {
            "content": content,
            "priority": priority,
            "delay": delay,
            "last_ran": last_ran,
            "in_queue": in_queue
        }

    async def neura_scheduler_worker(self):
        await self.wait_until_ready()
        self.log("SYS", "Scheduler started.")
        while self.active:
            try:
                if self.paused:
                    await asyncio.sleep(1)
                    continue

                now = time.time()
                # do not name this `state` - it would shadow the core.state module import
                for cmd_id, cmd_state in list(self.cmd_states.items()):
                    if cmd_state["in_queue"]: continue

                    if now - cmd_state["last_ran"] >= cmd_state["delay"]:
                        cmd_state["in_queue"] = True
                        actual_content = cmd_state["content"]
                        if callable(actual_content):
                            try:
                                if asyncio.iscoroutinefunction(actual_content):
                                    actual_content = await actual_content()
                                else:
                                    actual_content = actual_content()
                            except Exception as e:
                                self.log("ERROR", f"Scheduler hook '{cmd_id}' failed: {e}")
                                actual_content = None

                        if actual_content is not None:
                            asyncio.create_task(self.neura_enqueue(actual_content, priority=cmd_state["priority"], _cmd_id=cmd_id))
                        else:
                            cmd_state["in_queue"] = False
                            cmd_state["last_ran"] = time.time()

                await asyncio.sleep(1)
            except Exception as e:
                self.log("ERROR", f"Scheduler error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(1)