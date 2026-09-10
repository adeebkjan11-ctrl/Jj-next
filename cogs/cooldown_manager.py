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
import re
import time
import core.state as state

# command word as it appears on the wire -> the cmd_id the scheduler tracks it under.
# Only entries where the two differ need listing; everything else falls through as
# itself and is then filtered by `in bot.cmd_states`. This used to be two separate
# literals, and the one on the slow-down path was missing cf/s/bj/hb - so an owo
# "slow down" for a gamble or a huntbot never back-dated the timer it belonged to
# and the account walked straight back into the same cooldown.
CMD_ALIASES = {
    "h": "hunt",
    "b": "battle",
    "curse": "cursepray",
    "pray": "cursepray",
    "cf": "coinflip",
    "s": "slots",
    "bj": "blackjack",
    "hb": "huntbot",
}


def _cmd_id_from_text(text, prefix):
    """The cmd_id for a command we sent, or None when the text is not one.

    `text` and `prefix` are already lowercased and stripped. A bare prefix ("owo")
    is the owo command itself; splitting it used to raise IndexError and abort the
    whole slow-down sync.
    """
    if not text.startswith(prefix):
        return None
    remaining = text[len(prefix):].strip()
    if not remaining:
        return prefix or "owo"
    return CMD_ALIASES.get(remaining.split()[0], remaining.split()[0])


class CooldownManager:
    def __init__(self, bot):
        self.bot = bot
        self.last_manual_cmd = None
        self.last_manual_time = 0.0

    async def on_message(self, message):
        # self.bot.user is None before the first READY; guard so a pre-ready
        # dispatch does not raise AttributeError on None.id and take down the
        # cooldown tracker.
        if self.bot.user is None:
            return
        if message.author.id == self.bot.user.id:
            content = message.content.lower().strip()
            prefix = self.bot.prefix.lower().strip()

            if content.startswith(prefix):
                cmd_id = _cmd_id_from_text(content, prefix)

                if cmd_id and cmd_id in self.bot.cmd_states:
                    self.bot.cmd_states[cmd_id]['last_ran'] = time.time()

                    is_echo = (content == self.bot.last_sent_command.lower().strip() and
                              time.time() - self.bot.last_sent_time < 1.2)

                    if not is_echo:
                        silent_cmds = ['hunt', 'battle', 'owo']
                        if cmd_id not in silent_cmds:
                            self.bot.log("SYS", f"Manual command sync: {cmd_id}")

                self.last_manual_cmd = cmd_id
                self.last_manual_time = time.time()
            return

        monitor_id = str(self.bot.config.get('core', {}).get('monitor_bot_id', '408785106942164992'))
        if str(message.author.id) != monitor_id:
            return
            
        if message.channel.id != self.bot.channel_id:
            return
        
        content = message.content.lower()
        is_for_me = self.bot.is_message_for_me(message)
        if not is_for_me:
            return


        if "slow down" in content or "try the command again" in content:
            wait_seconds = 0
            
            ts_match = re.search(r'<t:(\d+):r>', content)
            if ts_match:
                target_ts = int(ts_match.group(1))
                wait_seconds = max(0, target_ts - int(time.time()))
                self.bot.log("COOLDOWN", f"Syncing via Timestamp: {wait_seconds}s")

            else:
                sec_match = re.search(r'(\d+)\s*seconds?', content)
                if sec_match:
                    wait_seconds = int(sec_match.group(1))
                    self.bot.log("COOLDOWN", f"Syncing via Seconds: {wait_seconds}s")
                else:
                    wait_seconds = 5
                    self.bot.log("COOLDOWN", f"Generic slow-down detected. Applying 5s safety pause.")

            if wait_seconds > 0:
                cmd_id = None
                if self.last_manual_cmd and time.time() - self.last_manual_time < 5:
                    cmd_id = self.last_manual_cmd
                else:
                    cmd_id = _cmd_id_from_text(
                        self.bot.last_sent_command.lower().strip(),
                        self.bot.prefix.lower().strip(),
                    )

                if wait_seconds <= 15:
                    self.bot.throttle_until = time.time() + wait_seconds + 0.5
                else:
                    pass

                if cmd_id and cmd_id in self.bot.cmd_states:
                    delay = self.bot.cmd_states[cmd_id]['delay']
                    self.bot.cmd_states[cmd_id]['last_ran'] = time.time() - delay + wait_seconds + 1

                    if wait_seconds > 15:
                        self.bot.log("COOLDOWN", f"Synced {cmd_id} cooldown: {wait_seconds}s (Grinding continues)")
                    else:
                        silent_cmds = ['hunt', 'battle', 'owo']
                        if cmd_id not in silent_cmds:
                            self.bot.log("COOLDOWN", f"Refined {cmd_id} timer (Global Pause: {wait_seconds}s)")

        elif "too tired to run" in content:
            self.bot.log("COOLDOWN", "Too tired to run (Synced)")

async def setup(bot):
    cog = CooldownManager(bot)
    bot.add_listener(cog.on_message, 'on_message')