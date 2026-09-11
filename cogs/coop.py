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


import asyncio
import random
import time
from discord.ext import commands
from lazy_engines import coop


class Coop(commands.Cog):
    """Makes the accounts in this process work for each other.

    Two jobs. Friendly battles on a timer, so the battle quests and the "battle
    with a friend" quest both make progress without a human in the loop, and the
    bookkeeping the quest engine needs to hand a social quest to a sibling.

    All accounts run on one event loop, so a peer's queue is awaited directly.
    """

    def __init__(self, bot):
        self.bot = bot
        self._last_partner = None
        self._no_peer_warned = 0.0

    async def register_actions(self):
        cfg = self.bot.config.get('coop', {}) or {}
        battle_cfg = cfg.get('battle') or {}

        if not cfg.get('enabled', True) or not battle_cfg.get('enabled', True):
            self.bot.cmd_states.pop('coop_offer', None)
            return

        interval = max(120, int(battle_cfg.get('interval_min', 20) or 20) * 60)
        await self.bot.neura_register_command(
            "coop_offer",
            self._battle_tick,
            priority=self.bot.get_cmd_priority("coop_offer", 5),
            delay=interval,
            initial_offset=random.randint(60, 200),
        )
        self.bot.log("SYS", f"Coop configured (friendly battle with a sibling every {interval // 60}m).")

    def _warn_no_peer(self):
        if time.time() - self._no_peer_warned < 1800:
            return
        self._no_peer_warned = time.time()
        self.bot.log(
            "WARN",
            "Coop: no other account is online and in a shared channel, so friendly "
            "battles and social quests cannot be paired up."
        )

    def _pick_partner(self, candidates):
        """Rotate partners so three or more accounts do not all pair with the same one."""
        partner = next(
            (peer for peer in candidates if str(peer.user.id) != self._last_partner),
            candidates[0],
        )
        self._last_partner = str(partner.user.id)
        return partner

    async def _battle_tick(self):
        """Challenge one sibling, then let the other side accept on its own.

        response_handler already accepts any duel it is mentioned in, so the only
        real work here is picking a partner and making sure exactly one of the pair
        sends the challenge - otherwise both do and one of the two is thrown away.

        Returns None: the challenge is enqueued here so it can be aimed at the
        channel both accounts share rather than whatever channel_id happens to be
        current for this one.
        """
        cfg = self.bot.config.get('coop', {}) or {}
        battle_cfg = cfg.get('battle') or {}
        if not cfg.get('enabled', True) or not battle_cfg.get('enabled', True):
            return None

        candidates = coop.peers(self.bot)
        if not candidates:
            self._warn_no_peer()
            return None

        if battle_cfg.get('arbitrate', True):
            candidates = [peer for peer in candidates if coop.is_initiator(self.bot, peer)]
            if not candidates:
                return None  # a sibling with a lower id is the one that starts it

        partner = self._pick_partner(candidates)
        channel = coop.shared_channel(self.bot, partner)
        if channel is None:
            return None
        if not coop.may_ask(partner, self.bot, "friendly_battle", max(90, int(battle_cfg.get('min_gap_s', 120) or 120))):
            return None
        coop.note_ask(partner, self.bot, "friendly_battle")

        self.bot.log("SYS", f"Coop: challenging {partner.user.name} to a friendly battle")
        await self.bot.neura_enqueue(
            f"owo battle <@{partner.user.id}>",
            priority=self.bot.get_cmd_priority("coop_offer", 5),
            target_channel_id=channel,
            _cmd_id="coop_battle",
        )
        self.arm_accept_fallback(partner, channel)
        return None

    def arm_accept_fallback(self, partner, channel):
        """Nudge the partner if the challenge never gets accepted.

        response_handler spots a duel by looking for a real mention, and owo now
        renders some challenges as a components v2 card that discord.py-self cannot
        see at all - so the accept can silently never happen. The old engine sent
        `owo ab` on a flat four second timer instead, which fired whether or not a
        challenge had actually landed.
        """
        async def nudge():
            await asyncio.sleep(random.uniform(6.5, 9.5))
            if time.time() - float(getattr(partner, 'last_duel_accept', 0) or 0) < 20:
                return  # already accepted it on its own
            if not coop.is_available(partner):
                return
            await partner.neura_enqueue("owo ab", priority=4, target_channel_id=channel)
            self.bot.log("SYS", f"Coop: nudged {partner.user.name} to accept the battle")

        asyncio.create_task(nudge())


async def setup(bot):
    await bot.add_cog(Coop(bot))
