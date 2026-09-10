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


class CustomCommands(commands.Cog):
    """Runs operator-supplied commands, either on a timer or on demand.

    Config lives at ``commands.custom``::

        "custom": {
            "enabled": true,
            "commands": [
                {"command": "cash", "interval_s": 600, "enabled": true},
                {"command": "inv", "interval_s": 0}
            ]
        }

    ``interval_s`` of 0 (or missing) means "never schedule it" - the entry is
    only there so the dashboard can offer it as a one-click button.

    The OwO prefix is added when it is missing, so ``cash`` and ``owo cash``
    both send ``owo cash``. Start the line with a backslash to send it exactly
    as typed instead.
    """

    def __init__(self, bot):
        self.bot = bot

    def _cfg(self):
        return self.bot.config.get('commands', {}).get('custom', {})

    def _with_prefix(self, command):
        """Prepend the account prefix unless it is already there.

        ``NeuraBot._fix_command`` only auto-prefixes a hardcoded list of known
        commands, so anything the operator types outside that list (``cash``,
        ``shop``, ``my``, ...) would otherwise go out bare.
        """
        command = command.strip()
        if not command:
            return ''
        if command.startswith('\\'):
            return command[1:].strip()

        prefix = (self.bot.prefix or 'owo ')
        bare = prefix.strip().lower()
        if not bare or command.lower().startswith(bare):
            return command
        return f"{prefix}{command}"

    def entries(self, only_enabled=True):
        """Normalise the configured list into [{index, command, interval, enabled}]."""
        raw = self._cfg().get('commands', []) or []
        # a plain textarea in the dashboard would hand us one command per line
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]

        out = []
        for index, item in enumerate(raw):
            if isinstance(item, str):
                item = {'command': item}
            if not isinstance(item, dict):
                continue

            command = str(item.get('command', '')).strip()
            if not command:
                continue

            is_on = item.get('enabled', True) is not False
            if only_enabled and not is_on:
                continue

            try:
                interval = float(item.get('interval_s', item.get('interval', 0)) or 0)
            except (TypeError, ValueError):
                interval = 0.0

            out.append({
                'index': index,
                'command': self._with_prefix(command),
                'raw': command,
                'interval': interval,
                'enabled': is_on,
            })
        return out

    async def register_actions(self):
        if not self._cfg().get('enabled', False):
            return

        scheduled = 0
        for entry in self.entries():
            if entry['interval'] <= 0:
                continue
            # keep the id tied to the config index so _prune_disabled_scheduler_cmds
            # can drop the slot when the row is deleted
            await self.bot.neura_register_command(
                f"custom_{entry['index']}",
                entry['command'],
                priority=self.bot.get_cmd_priority('custom', 5),
                delay=max(5.0, entry['interval']),
                initial_offset=15 + (scheduled * 4),
            )
            scheduled += 1

        if scheduled:
            self.bot.log("SYS", f"Custom commands: {scheduled} on a timer.")

    async def run_now(self, command):
        """Send one command straight away, jumping the scheduler."""
        command = self._with_prefix(command or '')
        if not command:
            return False
        return await self.bot.send_message(command, skip_typing=True, priority=True)


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
