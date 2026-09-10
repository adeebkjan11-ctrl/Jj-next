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
import time
import random
import core.state as state
from lazy_engines import coop

# quest intelligence is still under testing , errors and bugs can occur


# ─────────────────────────────────────────────────────────────────────────────
#        owo quest type (taken from owobot src ) 
# ─────────────────────────────────────────────────────────────────────────────
# self quests (no alt needed):
#   hunt         → "manually hunt X times!"
#   battle       → "battle X times!"             
#   gamble       → "gamble X times!"             
#   owo          → "say 'owo' X times!"        
#   find         → "hunt 3 animals that are X rank!"  
#   xp           → "earn X xp from hunting and battling!"  (normal grinding)
#   emoteTo      → "use an action command on someone X times!"  hug/f*ck(owobot)/etc
#
# alt quests (requires alt account):
#   emoteBy      → "have a friend use an action command on you X times!"
#   prayBy       → "have a friend pray to you X times!"
#   curseBy      → "have a friend curse you X times!"
#   cookieBy     → "receive a cookie from X friends!"
#   friendlyBattle → "battle with a friend X times!"
# ─────────────────────────────────────────────────────────────────────────────


EMOTE_COMMANDS = ["hug", "poke", "pat", "cuddle", "kiss"]

class LazyQuestEngine:
    def __init__(self, bot):
        self.bot = bot
        self.last_solver_run = 0
        self.solver_task = None
        self._alt_warned = False
        self._target_warned = False
        self.last_signaled = {}
        self.last_queued = {}

    def start(self):
        if not self.solver_task:
            self.solver_task = asyncio.create_task(self._quest_solver_loop())

    async def _quest_solver_loop(self):
        await asyncio.sleep(15)  
        while True:
            if not getattr(self.bot, 'is_ready', False) or getattr(self.bot, 'paused', False):
                await asyncio.sleep(5)
                continue

            now = time.time()
            if now - self.last_solver_run < 20: 
                await asyncio.sleep(2)
                continue

            self.last_solver_run = now
            st = self.bot.stats
            quests = st.get('quest_data', [])

            has_rarity_quest = any(
                "hunt 3 animals that are" in q.get('description', '')
                and not q.get('completed', False)
                for q in quests
            )
            if not has_rarity_quest and st.get('force_lucky_gems'):
                st['force_lucky_gems'] = False
                self.bot.log("SYS", "Quest Engine: Rarity quest done — disabled Force Lucky Gems.")

            for q in quests:
                if q.get('completed', False):
                    continue

                desc = q.get('description', '').lower()
                current = q.get('current', 0)
                total = q.get('total', 1)
                remaining = total - current

                # priorty 1: social quests that need an alt account
                if self.is_alt_quest(desc):
                    now = time.time()
                    if now - self.last_signaled.get(desc, 0) < 60:
                        continue
                    cfg = self.bot.config.get('commands', {}).get('quest', {})
                    if not cfg.get('use_alt_account', True) or not coop.enabled(self.bot, 'quests'):
                        continue

                    helpers = coop.peers(self.bot)
                    if helpers:
                        self.last_signaled[desc] = now
                        self._alt_warned = False
                        await self._signal_alt(desc, helpers, remaining)
                    elif not self._alt_warned:
                        self.bot.log(
                            "WARN",
                            "Quest Engine: Social quest active but no alt account is online in a "
                            f"shared channel. Quest: '{q.get('description', '')}'"
                        )
                        self._alt_warned = True
                    # a second social quest is somebody else's command budget, not ours,
                    # so keep going instead of dropping out of the whole pass
                    continue

                # priorty 2: self-contained quests we automate directly ──

                elif "gamble" in desc:
                    await self._queue_quest_command("owo cf 1", "Gamble Quest", cooldown=12)
                    break

                elif "use an action command on someone" in desc or "use an emote command on someone" in desc:
                    target_id = self._get_sibling_or_fallback()
                    if target_id is None:
                        self._warn_no_target(q.get('description', ''))
                        break
                    emote = random.choice(EMOTE_COMMANDS)
                    await self._queue_quest_command(
                        f"owo {emote} <@{target_id}>",
                        "Action/Emote Quest",
                        cooldown=8
                    )
                    break

                elif "hunt 3 animals that are" in desc and not st.get('force_lucky_gems'):
                    st['force_lucky_gems'] = True
                    self.bot.log("SYS", "Quest Engine: Enabled Force Lucky Gems for rarity quest.")

                elif "say 'owo'" in desc or "say \"owo\"" in desc:
                    if remaining > 5:
                        await self._queue_quest_command("owo", "OWO Quest", cooldown=6)
                    break

                # hunt / battle / xp quests are handled by the grinding loop.


            await asyncio.sleep(5)

    # ──────────────────────────────────────────────────────────────────────────
    #  helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _get_sibling_or_fallback(self):
        """Somebody we can aim an action command at, or None if there is nobody.

        Returning None matters: this used to fall back to owo's own user id, and owo
        rejects action commands aimed at a bot, so the quest never moved and the
        wasted command looked like it had worked.
        """
        helpers = coop.peers(self.bot)
        if helpers:
            return random.choice(helpers).user.id
        return coop.fallback_target(self.bot)

    def _warn_no_target(self, description):
        if self._target_warned:
            return
        self._target_warned = True
        self.bot.log(
            "WARN",
            f"Quest Engine: '{description}' needs somebody to act on, but no sibling account "
            "is online and coop.fallback_targets is empty - add a friend's user id there."
        )

    async def _queue_quest_command(self, cmd, reason, cooldown=10):
        now = time.time()
        if now - self.last_queued.get(cmd, 0) < cooldown:
            return
        self.last_queued[cmd] = now
        self.bot.log("SYS", f"Quest Engine: Queueing [{cmd}] for {reason}")
        await self.bot.neura_enqueue(cmd, priority=5)

    def is_alt_quest(self, desc):
        socials = [
            "have a friend use an action command on you",
            "have a friend use an emote command on you",
            "have a friend pray to you",
            "have a friend curse you",
            "receive a cookie from",
            "battle with a friend",
        ]
        return any(s in desc for s in socials)

    async def _signal_alt(self, desc, helpers, remaining=1):
        """Get sibling accounts to do the social half of a quest for us.

        Every ask goes through coop.ask_peer, which drops it unless the peer is
        actually able to send (not paused, not sitting on an unsolved captcha) and
        shares a channel with us - owo only credits what it sees us both in.
        """
        my_id = self.bot.user_id

        if "battle with a friend" in desc:
            await self._friendly_battle(helpers)
            return

        target_cmd = None
        if "pray to you" in desc:
            target_cmd = f"owo pray <@{my_id}>"
        elif "curse you" in desc:
            target_cmd = f"owo curse <@{my_id}>"
        elif "action command on you" in desc or "emote command on you" in desc:
            target_cmd = f"owo {random.choice(EMOTE_COMMANDS)} <@{my_id}>"
        elif "cookie from" in desc:
            target_cmd = f"owo cookie <@{my_id}>"

        if not target_cmd:
            return

        # "receive a cookie from 3 friends" wants three *different* people, so ask as
        # many siblings as the quest still needs. The old code stopped after the first
        # peer no matter what the quest asked for.
        distinct_needed = max(1, int(remaining)) if "cookie from" in desc else 1
        action = desc[:40]

        asked = 0
        for peer in helpers:
            if asked >= distinct_needed:
                break
            if await coop.ask_peer(self.bot, peer, target_cmd, action, cooldown=90):
                asked += 1
                await asyncio.sleep(random.uniform(1.0, 2.5))

        if not asked:
            self.bot.log("INFO", f"Quest Engine: no sibling was free to help with '{desc[:50]}'")

    async def _friendly_battle(self, helpers):
        """A friendly battle needs both sides, so only one account may start it."""
        partner = next((peer for peer in helpers if coop.is_initiator(self.bot, peer)), None)
        if partner is None:
            # a sibling with a lower id owns this pairing and will send the challenge;
            # our own response_handler accepts it when it arrives
            return

        channel = coop.shared_channel(self.bot, partner)
        if channel is None:
            return
        if not coop.may_ask(partner, self.bot, "friendly_battle", 90):
            return
        coop.note_ask(partner, self.bot, "friendly_battle")

        await self.bot.neura_enqueue(
            f"owo battle <@{partner.user.id}>",
            priority=5,
            target_channel_id=channel,
            _cmd_id="coop_battle",
        )
        cog = self.bot.get_cog("Coop")
        if cog:
            cog.arm_accept_fallback(partner, channel)
        self.bot.log("SYS", f"Quest Engine: Coordinated friendly battle with {partner.user.name}")
