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
from discord.ext import commands

class RPP(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active = True
        self.task = None
        self.last_run = 0
        self.command_availability = {"run": 0, "pup": 0, "piku": 0}
        
    def trigger_action(self):
        cfg = self.bot.config.get('commands', {}).get('rpp', {})
        interval = cfg.get('interval_s', 60)
        
        commands_list = cfg.get('active_commands', ["run", "pup", "piku"])
        available = [c for c in commands_list if time.time() > self.command_availability.get(c, 0)]

        if 'rpp' in self.bot.cmd_states:
             self.bot.cmd_states['rpp']['delay'] = interval

        if available:
            cmd = random.choice(available)
            return f"owo {cmd}"
        else:
            return None 

    async def register_actions(self):
        cfg = self.bot.config.get('commands', {}).get('rpp', {})
        if cfg.get('enabled', False):
            self.bot.log("SYS", "RPP Module configured.")
            interval = cfg.get('interval_s', 60)
            
            def rpp_dispatch():
                return self.trigger_action()
                
            await self.bot.neura_register_command("rpp", rpp_dispatch, priority=self.bot.get_cmd_priority("rpp", 3), delay=interval, initial_offset=15)

    @commands.Cog.listener()
    async def on_message(self, message):
        monitor_id = str(self.bot.config.get('core', {}).get('monitor_bot_id', '408785106942164992'))
        if str(message.author.id) != monitor_id:
            return
        if self.bot.owo_user is None:
            self.bot.owo_user = message.author
        if str(message.channel.id) not in [str(c) for c in self.bot.channels]:
            return

        content = message.content.lower()
        full_text = self.bot.get_full_content(message)

        # these are direct replies, so they must be ours before we mark a command spent
        # for the rest of the day
        if not self.bot.is_message_for_me(message):
            return

        if "too tired to run" in full_text:
            self.command_availability["run"] = time.time() + 86400
            self.bot.log("COOLDOWN", "RPP: run exhausted. Next available tomorrow....")
            return

        if "garden is out of carrots" in full_text:
            self.command_availability["piku"] = time.time() + 86400
            self.bot.log("COOLDOWN", "RPP: piku exhausted (no carrots). Next available tomorrow..")
            return

        if "no puppies" in full_text:
            self.command_availability["pup"] = time.time() + 86400
            self.bot.log("COOLDOWN", "RPP: pup exhausted (no puppies). Next available tomorrow...")
            return

        for cmd in ["run", "pup", "piku"]:
            if f":9{cmd}" in content or f"o {cmd}" in content:
                 if "tomorrow" in content or "too many" in content:
                    self.command_availability[cmd] = time.time() + 86400
                    self.bot.log("COOLDOWN", f"RPP: {cmd} exhausted. Next available tomorrow.")

async def setup(bot):
    cog = RPP(bot)
    await bot.add_cog(cog)
