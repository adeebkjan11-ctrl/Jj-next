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


"""Cross-account coordination.

Every NeuraBot in this process shares one asyncio loop - neura.py binds it and
supervisor.start_account creates each runner as a task on that same loop - so
awaiting a peer's neura_enqueue from here is safe. No run_coroutine_threadsafe.

Everything an account needs to know before leaning on a sibling lives here, so
the quest engine and the Coop cog cannot drift apart on who is available.
"""

import random
import time
import core.state as state

# (giver, receiver, action) -> timestamp, shared by every account in the process so
# two bots cannot independently decide to do the same favour at the same moment
_favours = {}


def _cfg(bot):
    return bot.config.get('coop', {}) or {}


def enabled(bot, section=None):
    cfg = _cfg(bot)
    if not cfg.get('enabled', True):
        return False
    if section is None:
        return True
    return bool((cfg.get(section) or {}).get('enabled', True))


def shared_channel(bot, peer):
    """A channel both accounts post in.

    owo only credits a social interaction it can see happening between the two, so
    a favour fired into a channel the asker does not read is wasted. The old code
    always used the asker's channel_id and never checked the peer was in it.
    """
    peer_channels = {str(c) for c in (getattr(peer, 'channels', None) or [])}
    if getattr(peer, 'channel_id', None):
        peer_channels.add(str(peer.channel_id))

    mine = []
    if getattr(bot, 'channel_id', None):
        mine.append(str(bot.channel_id))
    mine += [str(c) for c in (getattr(bot, 'channels', None) or [])]

    for channel in mine:
        if channel in peer_channels:
            try:
                return int(channel)
            except (TypeError, ValueError):
                return None
    return None


def is_available(peer):
    """Can this account act on someone else's behalf right now?"""
    if not getattr(peer, 'active', False) or not getattr(peer, 'is_ready', False):
        return False
    if getattr(peer, 'paused', False):
        return False

    now = time.time()
    # throttle_until is inf while a captcha is unsolved, so asking now just piles
    # commands onto an account that cannot send anything
    if now < (getattr(peer, 'throttle_until', 0) or 0):
        return False
    if now < (getattr(peer, 'warmup_until', 0) or 0):
        return False
    if state.checking_gems.get(getattr(peer, 'user_id', None)):
        return False
    return True


def peers(bot, require_channel=True):
    """Live sibling accounts that can help, in an order every account agrees on."""
    out = []
    for inst in list(getattr(state, 'bot_instances', None) or []):
        if inst is bot or getattr(inst, 'user', None) is None:
            continue
        if str(inst.user.id) == str(bot.user_id):
            continue
        if not is_available(inst):
            continue
        if require_channel and shared_channel(bot, inst) is None:
            continue
        out.append(inst)

    out.sort(key=lambda inst: str(inst.user.id))
    return out


def is_initiator(bot, peer):
    """For a two-sided action, only the lower account id starts it.

    Both accounts run this same code against their own quest list, so without an
    arbitration rule they both send the challenge and one of the two is wasted.
    """
    return str(bot.user_id) < str(peer.user.id)


def may_ask(peer, bot, action, cooldown):
    key = (str(peer.user.id), str(bot.user_id), action)
    return time.time() - _favours.get(key, 0) >= cooldown


def note_ask(peer, bot, action):
    now = time.time()
    _favours[(str(peer.user.id), str(bot.user_id), action)] = now
    if len(_favours) > 400:
        for key in [k for k, v in _favours.items() if now - v > 3600]:
            _favours.pop(key, None)


async def ask_peer(bot, peer, command, action, cooldown=45, priority=5):
    """Have `peer` run `command` somewhere this account can actually see it."""
    channel = shared_channel(bot, peer)
    if channel is None:
        return False
    if not may_ask(peer, bot, action, cooldown):
        return False

    note_ask(peer, bot, action)
    await peer.neura_enqueue(command, priority=priority, target_channel_id=channel)
    bot.log("SYS", f"Coop: asked {peer.user.name} to run [{command}]")
    return True


def fallback_target(bot):
    """A configured stand-in for when no sibling account is online.

    Deliberately not defaulted to owo's own user id, which is what used to sit in
    FALLBACK_TARGETS: owo refuses action commands aimed at a bot, so every emote
    quest that fell through to it burned a command and made no progress at all.
    """
    raw = _cfg(bot).get('fallback_targets') or []
    # a hand-edited settings.json can leave a comma separated string here, and
    # iterating that yields single characters - every one of which is a "digit"
    if isinstance(raw, str):
        raw = raw.split(',')

    targets = [str(t).strip() for t in raw if str(t).strip().isdigit()]
    return random.choice(targets) if targets else None
