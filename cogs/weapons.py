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

Keeps the best weapons we own bolted onto the battle team. `owo weapon` is read on a
timer, the strongest weapon that is not already on an animal is picked for every team
slot that is still bare, and each one is equipped. The equip command is a template in
settings so it can be corrected without a code change if owo renames it.

Every shape below was captured from the live bot, because guessing was wrong twice:

    inventory (a components v2 card - `content` is empty, so the legacy embed path
    never saw a single weapon):
      `FFNA16` <:mythic:..><:pmorb:..> **Decent Orb of Potency [0]** 82.6%
      `FFNA1A` <:epic:..><:erstaff:..> **Resurrection Staff [0]** 68.6% ➤  <a:gsquid:..> gsquid

    equip:      owo equip FFNA16 2
    accepted:   **🗡 | name**, :snowman2: **snowman** is now wielding **Orb of Potency**!
    bad id:     **🚫 | name**, Could not find a weapon with that id!
    bad args:   **🚫 | name**, Invalid arguments! Use `owo weapon {uniqueWeaponId} {animalPos|animal}`

The `➤ <animal>` tail is the only marker of an equipped weapon, and moving one off an
animal to arm another is pointless - so rows carrying it are skipped, and the animals
named in them are the ones that already have a weapon.
"""


import json
import re
import time

from discord.ext import commands

from component_v2_neura.parser import collect_text, parse_v2_message
from cogs.others import canonical_animal

QUALITY_RANK = (
    ("distorted", 8), ("hidden", 7), ("fabled", 6), ("legendary", 5),
    ("mythical", 4), ("mythic", 4), ("epic", 3), ("uncommon", 1), ("rare", 2), ("common", 0),
)
RANK_NAMES = {0: "common", 1: "uncommon", 2: "rare", 3: "epic", 4: "mythical",
              5: "legendary", 6: "fabled", 7: "hidden", 8: "distorted"}
QUALITY_EMOJI = {word: rank for word, rank in QUALITY_RANK}

# a row is `<unique id>` followed by the quality emoji, the name and the wear percent.
# Real ids are six characters ("FFNA16"), which the old 3-4 character pattern could
# not match at all - so even a card that did arrive parsed to zero weapons.
WEAPON_ROW_RE = re.compile(r'^\s*`([0-9A-Za-z]{4,10})`\s+(.*)$')
EMOJI_NAME_RE = re.compile(r'<a?:([A-Za-z0-9_]+):\d+>')
PCT_RE = re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%')
EQUIPPED_MARK = '➤'
EQUIPPED_WORDS = ("equipped", "in use")
WEAPON_LIST_PHRASES = ("'s weapons", "'s weapon", "weapon inventory", "weapons!")
WIELDING_PHRASES = ("is now wielding", "now wielding")
EQUIP_FAIL_PHRASES = ("could not find a weapon with that id", "invalid arguments")


class Weapons(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.team = []
        self._last_plan = []
        self._last_equip_at = 0
        self._awaiting_list = False
        self._asked_at = 0
        self._armed = set()
        self._retry_after = 0
        self._template_warned = False
        self._team_warned_at = 0
        self._handled_v2 = {}

    # ── config ───────────────────────────────────────────────────────────────

    def _cfg(self):
        return self.bot.config.get('commands', {}).get('weapon', {})

    def _slots(self):
        configured = int(self._cfg().get('slots', 3) or 3)
        return max(1, min(configured, len(self.team) or configured))

    def note_team(self, team):
        """Others tells us what is on the team so we know how many slots to arm.

        Returns True when the roster actually changed - a fresh animal starts with no
        weapon, so that is exactly when a re-scan is worth a command.
        """
        new = [canonical_animal(a) or str(a or '').lower() for a in (team or []) if a]
        changed = new != self.team
        self.team = new
        if changed:
            self._last_plan = []
        return changed

    def _equip_command(self, weapon_id, pos):
        """`owo equip <id> <pos>`, repairing the templates that never worked.

        The shipped default used to be `weapon equip {weapon} {slot}`; owo has no
        `weapon equip` subcommand, so it read the literal word "equip" as the weapon
        id and answered "Could not find a weapon with that id!". A template saved in
        an existing settings file still says that, so fix it here rather than leave
        every upgrade silently failing.
        """
        template = str(self._cfg().get('equip_template') or '').strip()
        flat = template.replace('`', '').strip().lower()
        if not template or '{weapon}' not in template or flat.startswith('weapon equip'):
            if template and not self._template_warned:
                self._template_warned = True
                self.bot.log(
                    "WARN",
                    f"commands.weapon.equip_template is `{template}`, which owo rejects "
                    f"- using `equip {{weapon}} {{pos}}` instead"
                )
            template = 'equip {weapon} {pos}'
        template = template.replace('{slot}', '{pos}')
        if '{pos}' not in template:
            template += ' {pos}'
        return template.format(weapon=weapon_id, pos=pos).strip()

    # ── parsing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row_quality(rest):
        """The weapon's rank, read off its first emoji (`<:mythic:...>`)."""
        for name in EMOJI_NAME_RE.findall(rest):
            rank = QUALITY_EMOJI.get(name.lower())
            if rank is not None:
                return rank
        lowered = rest.lower()
        for word, rank in QUALITY_RANK:
            if word in lowered:
                return rank
        return None

    @staticmethod
    def _row_animal(rest):
        """Which animal a row says the weapon is on, from the `➤ <emoji> name` tail."""
        tail = rest.split(EQUIPPED_MARK, 1)[-1]
        tail = EMOJI_NAME_RE.sub(' ', tail)
        words = [w for w in re.split(r'[^A-Za-z]+', tail) if w]
        if not words:
            return None
        return canonical_animal(words[-1]) or words[-1].lower()

    def parse_weapons(self, raw):
        """([(rank, wear, id)] not on an animal, {animals that already hold one})."""
        found, equipped = [], set()
        for line in raw.splitlines():
            row = WEAPON_ROW_RE.match(line)
            if not row:
                continue
            weapon_id, rest = row.group(1), row.group(2)
            rank = self._row_quality(rest)
            if rank is None:
                continue

            lowered = rest.lower()
            if EQUIPPED_MARK in rest or any(word in lowered for word in EQUIPPED_WORDS):
                animal = self._row_animal(rest)
                if animal:
                    equipped.add(animal)
                continue

            percents = PCT_RE.findall(rest)
            try:
                wear = float(percents[-1]) if percents else 0.0
            except ValueError:
                wear = 0.0
            found.append((rank, wear, weapon_id))

        # strongest first, de-duplicated by id
        found.sort(key=lambda item: (-item[0], -item[1]))
        seen = set()
        unique = []
        for rank, wear, weapon_id in found:
            if weapon_id in seen:
                continue
            seen.add(weapon_id)
            unique.append((rank, wear, weapon_id))
        return unique, equipped

    # ── flow ─────────────────────────────────────────────────────────────────

    async def request_weapon_check(self, reason=""):
        cfg = self._cfg()
        if not cfg.get('enabled', True):
            return
        if time.time() - self._last_equip_at < 60 or time.time() < self._retry_after:
            return
        if self._awaiting_list and time.time() - self._asked_at < 300:
            # a read is already queued - the scheduled scan and a team change land
            # within a minute of each other on a fresh boot, and asking twice reads the
            # same card twice while the equips from the first are still going out
            return
        self._awaiting_list = True
        self._asked_at = time.time()
        await self.bot.neura_enqueue(cfg.get('list_command', 'weapon'), priority=4, _cmd_id="weapon")
        if reason:
            self.bot.log("WEAPON", f"Reading the weapon inventory ({reason})")

    async def _equip(self, weapons, equipped_animals):
        slots = self._slots()
        self._armed = set(equipped_animals)

        if not self.team:
            # without the roster we cannot tell an armed animal from a bare one, and
            # equipping blind moves weapons around for nothing. The team scan runs on
            # its own timer; pick this up on the next weapon scan.
            if time.time() - self._team_warned_at > 3600:
                self._team_warned_at = time.time()
                self.bot.log("WEAPON", "Waiting for a team read before handing out weapons")
            return

        free = [pos for pos, animal in enumerate(self.team[:slots], start=1)
                if animal not in equipped_animals]
        if not free:
            # the roster is the authority here, not the count: an animal name we read
            # wrongly out of an equipped row would inflate a count and wrongly report
            # the whole team as armed, whereas an empty `free` means every slot we can
            # name is accounted for.
            self.bot.log("WEAPON", f"Every team animal already holds a weapon ({slots}/{slots})")
            self._last_equip_at = time.time()
            return

        if not weapons:
            bare = ", ".join(self.team[pos - 1] for pos in free)
            self.bot.log(
                "WEAPON",
                f"No spare weapons to give {bare} - every weapon owned is already on an "
                f"animal. Hunt weapon crates or `owo lootbox` for more."
            )
            self._last_equip_at = time.time()
            return

        plan = [(weapon_id, pos) for (_r, _w, weapon_id), pos in zip(weapons, free)]
        if plan == self._last_plan and time.time() - self._last_equip_at < 3600:
            self.bot.log("WEAPON", "Best weapons are already assigned - nothing to do")
            return

        self._last_plan = plan
        self._last_equip_at = time.time()

        for (weapon_id, pos), (rank, wear, _id) in zip(plan, weapons):
            await self.bot.neura_enqueue(self._equip_command(weapon_id, pos), priority=4)
            label = RANK_NAMES.get(rank, 'unknown')
            extra = f" {wear:g}%" if wear else ""
            animal = self.team[pos - 1] if pos <= len(self.team) else '?'
            self.bot.log("WEAPON", f"Arming {animal} (slot {pos}) with {weapon_id} ({label}{extra})")

    async def _handle_list(self, raw):
        self._awaiting_list = False
        weapons, equipped = self.parse_weapons(raw)
        if not weapons and not equipped:
            self.bot.log(
                "WARN",
                "Read the weapon inventory but recognised no weapons in it. If owo "
                "changed the card layout the equip pass has nothing to work from."
            )
            return
        await self._equip(weapons, equipped)

    @commands.Cog.listener('on_owo_gateway_message')
    async def on_owo_gateway_message(self, raw_data):
        """The inventory is a components v2 card, invisible to discord.py-self.

        `owo weapon` answers with type-10 text blocks and an empty `content`, so the
        on_message path below never saw the list at all: the cog asked for it every
        hour and threw the answer away. core.bot hands us the parsed frame.
        """
        if not self._cfg().get('enabled', True):
            return
        if not getattr(self.bot, 'is_ready', False):
            return

        data = raw_data.get("d") or {}
        if str((data.get("author") or {}).get("id")) != self.bot.owo_bot_id:
            return
        if str(data.get("channel_id")) not in [str(c) for c in self.bot.channels]:
            return

        components = parse_v2_message(data)
        if not components:
            return

        raw_text = f"{data.get('content') or ''}\n{collect_text(components)}"
        lowered = raw_text.lower()
        if not any(phrase in lowered for phrase in WEAPON_LIST_PHRASES):
            return
        if not self.bot.identity.text_is_mine(raw_text):
            return
        if not self._awaiting_list:
            return

        key = str(data.get("id"))
        stamp = f"{data.get('edited_timestamp') or ''}|{len(raw_text)}"
        if self._handled_v2.get(key) == stamp:
            return
        if len(self._handled_v2) > 40:
            self._handled_v2.clear()
        self._handled_v2[key] = stamp

        try:
            await self._handle_list(raw_text)
        except Exception as e:
            self.bot.log("ERROR", f"Weapon inventory handling failed: {e}")

    @commands.Cog.listener()
    async def on_message(self, message):
        cfg = self._cfg()
        if not cfg.get('enabled', True):
            return

        monitor_id = str(self.bot.config.get('core', {}).get('monitor_bot_id', '408785106942164992'))
        if str(message.author.id) != monitor_id:
            return
        if str(message.channel.id) not in [str(c) for c in self.bot.channels]:
            return

        content = self.bot.get_full_content(message)

        # the equip confirmation never says "weapon", so it has to be checked before
        # the cheap gate below - which is why "Weapon equipped." never once printed
        if any(phrase in content for phrase in WIELDING_PHRASES):
            if not self.bot.is_message_for_me(message):
                return
            animal = None
            match = re.search(r'\*\*([^*]{2,32})\*\*\s+is now wielding', content)
            if match:
                animal = canonical_animal(match.group(1).strip()) or match.group(1).strip()
                self._armed.add(animal)
            self.bot.log("SUCCESS", f"Weapon equipped to {animal}" if animal else "Weapon equipped.")
            return

        if any(phrase in content for phrase in EQUIP_FAIL_PHRASES) and "weapon" in content:
            if not self.bot.is_message_for_me(message):
                return
            self._last_plan = []
            self._retry_after = time.time() + 900
            self.bot.log(
                "WARN",
                "OwO refused the equip. Expected form is `equip {weapon} {pos}` - check "
                "commands.weapon.equip_template in settings."
            )
            return

        if "weapon" not in content:
            return

        if self._awaiting_list and any(phrase in content for phrase in WEAPON_LIST_PHRASES):
            if not self.bot.is_message_for_me(message, role="header"):
                return
            await self._handle_list(f"{message.content}\n{self._embed_text(message)}")
            return

        if not self.bot.is_message_for_me(message):
            return

        if "you don't have any weapons" in content or "no weapons" in content:
            self._awaiting_list = False
            self._last_equip_at = time.time()
            self.bot.log("WEAPON", "No weapons owned yet - will retry on the next check.")


    @staticmethod
    def _embed_text(message):
        chunks = []
        for embed in message.embeds or []:
            if embed.title:
                chunks.append(embed.title)
            if embed.description:
                chunks.append(embed.description)
            for field in embed.fields:
                chunks.append(f"{field.name}\n{field.value}")
        return "\n".join(chunks)

    async def register_actions(self):
        cfg = self._cfg()
        if not cfg.get('enabled', True):
            self.bot.cmd_states.pop('weapon_scan', None)
            return

        interval = max(600, int(cfg.get('check_interval_min', 60) or 60) * 60)
        await self.bot.neura_register_command(
            "weapon_scan",
            self._weapon_scan_tick,
            priority=self.bot.get_cmd_priority("weapon_scan", 5),
            delay=interval,
            initial_offset=150,
        )
        self.bot.log("SYS", f"Weapon Manager configured (re-check every {interval // 60}m).")

    async def _weapon_scan_tick(self):
        await self.request_weapon_check("scheduled check")
        return None


async def setup(bot):
    await bot.add_cog(Weapons(bot))
