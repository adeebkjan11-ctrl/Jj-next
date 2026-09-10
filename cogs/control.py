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
from discord.ext import commands
from core import state

class Control(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if str(message.author.id) != str(self.bot.user_id):
            return
            
        content = message.content.lower().strip()
        
        if content == '.stop':
            if not self.bot.paused:
                self.bot.paused = True
                self.bot.log("SYS", "Bot PAUSED via Chat cmd")
                
        elif content == '.start' or content == '.resume':
            if self.bot.paused:
                self.bot.paused = False
                # only this tenant's accounts: state.bot_instances is every space at
                # once, so walking it let one operator's `.resume` restart somebody
                # else's farm.
                for bot in state.bots_for(self.bot.space_owner):
                    bot.paused = False
                    # float('inf') means "parked until a captcha is solved" (see
                    # cogs/security.py). Zeroing it here sent an unverified account
                    # straight back to spamming owo, which is how a warning turns
                    # into a ban.
                    if bot.throttle_until != float('inf'):
                        bot.throttle_until = 0
                    else:
                        bot.paused = True
                        self.bot.log("SECURITY",
                                     f"{bot.username} is still waiting on a captcha - left paused")
                self.bot.log("SYS", "Bot RESUMED via Chat cmd")


        elif content == '.status':
            status = "PAUSED " if self.bot.paused else "RUNNING "

            uptime = time.time() - state.stats['uptime_start']
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            status += f"| Uptime: {hours}h {minutes}m"
            self.bot.log("SYS", status)

async def setup(bot):
    cog = Control(bot)
    await bot.add_cog(cog)