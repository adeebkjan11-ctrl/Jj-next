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


from discord.ext import commands
import asyncio
import time
import random
import re
import json
import os

import core.state as state

class NeuraCursePray(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # one file per account on the volume - a relative "data/" path lands in the
        # ephemeral repo copy and every account would overwrite the same key
        self.state_file = os.path.join(state.DATA_DIR, f"cp_state_{bot.user_id or 'default'}.json")
        self.last_run = self._load_last_run()

    def _load_last_run(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    return data.get("cp_last_run", 0)
            except Exception:
                pass
        return 0

    def _save_last_run(self):
        data = {}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
            except Exception:
                pass
        data["cp_last_run"] = self.last_run
        try:
            os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            self.bot.log("ERROR", f"Could not save curse/pray state: {e}")

    def trigger_action(self):
        if 'cursepray' not in self.bot.cmd_states:
            return
        cmds_cfg = self.bot.config.get("commands", {})
        curse_cfg = cmds_cfg.get("curse", {})
        pray_cfg = cmds_cfg.get("pray", {})
        
        available = []
        if curse_cfg.get("enabled", False): available.append("curse")
        if pray_cfg.get("enabled", False): available.append("pray")
        
        if available:
            choice = random.choice(available)
            cfg = curse_cfg if choice == "curse" else pray_cfg
            
            raw_targets = cfg.get("targets", [])
            if not isinstance(raw_targets, list):
                raw_targets = [raw_targets]
                
            targets = [str(t).strip() for t in raw_targets if t and str(t).strip()]
                
            if targets:
                target = random.choice(targets)
                if cfg.get("ping", True):
                    full_cmd = f"{choice} <@{target}>"
                else:
                    full_cmd = f"{choice} {target}"
            else:
                full_cmd = choice
            
            self.bot.cmd_states['cursepray']['content'] = full_cmd

            cooldown_range = cfg.get("cooldown", [305, 310])
            cur_cooldown = random.uniform(cooldown_range[0], cooldown_range[1])
            self.bot.cmd_states['cursepray']['delay'] = cur_cooldown


    @commands.Cog.listener()
    async def on_message(self, message):
        await self._process_response(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        await self._process_response(after)

    async def _process_response(self, message):
        core_config = self.bot.config.get("core", {})
        monitor_id = str(core_config.get("monitor_bot_id", "408785106942164992"))
        if str(message.author.id) != monitor_id:
            return
        if str(message.channel.id) not in [str(c) for c in self.bot.channels]:
            return
            
        full_content = self.bot.get_full_content(message)
        
        is_for_me = self.bot.is_message_for_me(message)
        
        success_triggers = [
            "puts a curse on", "is now cursed.",
            "prays for", "prays..."
        ]
        
        if is_for_me and any(t in full_content for t in success_triggers):
            self.last_run = time.time()
            self._save_last_run()
            self.bot.log("SUCCESS", "Curse/Pray action confirmed, cooldown reset.")

    async def register_actions(self):
        cmds_cfg = self.bot.config.get("commands", {})
        if cmds_cfg.get("curse", {}).get("enabled", False) or cmds_cfg.get("pray", {}).get("enabled", False):
            self.bot.log("SYS", "NeuraCursePray Module configured.")
            
            delay = 305
            rb_cfg = self.bot.config.get('reactionBot', {})
            if rb_cfg.get('enabled', False) and rb_cfg.get('pray_and_curse', False):
                delay += 5
                
            await self.bot.neura_register_command("cursepray", "curse", priority=self.bot.get_cmd_priority("cursepray", 3), delay=delay, initial_offset=20)
            self.trigger_action()

async def setup(bot):
    cog = NeuraCursePray(bot)
    await bot.add_cog(cog)
