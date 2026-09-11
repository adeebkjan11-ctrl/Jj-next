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
import random
import json
import core.state as state
from discord.ext import commands
from lazy_engines.quest_engine import LazyQuestEngine
from component_v2_neura import parse_v2_message, collect_text, buttons, media_image_url

# a quest line owo numbers itself. The separator is not always a dot: cards in the
# wild use "1) ", "1- " and "quest 1: ", and the leading \W run absorbs any markdown
# or bullet emoji owo puts in front of the number.
QUEST_TITLE_RE = re.compile(r'^\W{0,4}(?:quest\s*)?(\d{1,2})\s*[.):\-]\s*(.+)$', re.I)
PROGRESS_RE = re.compile(r'\b(\d+)\s*/\s*(\d+)\b')
# words owo puts on a claim control, on the custom_id or on the visible label
CLAIM_HINTS = ("claim", "reward")
# owo swaps the "N/M" counter for a tick once a quest is finished
DONE_MARKERS = ("✅", "☑", "✔", "🎉", "completed!", "quest complete")
# OwO titles the quest card differently across updates ("Quest Log", "Quests",
# "Your Quests", "Daily Quests", "Checklist"...). Matching only the literal
# "quest log" used to miss the card entirely, leaving the dashboard on
# "No active quests tracked" forever. Both listeners share this list so the legacy
# embed path cannot recognise fewer cards than the v2 path; the identity guard that
# follows each match is what keeps another account's card out.
QUEST_HEADERS = (
    "quest log", "quest list", "your quest", "daily quest",
    "active quest", "quest card", "checklist", "quests",
)

# OwO writes the next-quest time as a discord *relative timestamp*: the live card
# says "Next quest: **<t:1787727600:R>**", never "next quest in 2h 30m". The old
# pattern demanded the literal word "in" followed by digits, so it matched nothing on
# a modern card and the dashboard countdown sat blank forever. Storing the absolute
# unix time also lets the panel tick down live instead of freezing a string.
DISCORD_TS_RE = re.compile(r'<t:(\d{9,12})(?::[a-zA-Z])?>')
# "Balance: 11 <:seal:...> Quest Seals" - a real number the card still carries as
# text even when the quest rows themselves are a picture, and which was discarded.
SEAL_RE = re.compile(r'balance:\s*([\d,]+)\s*(?:<a?:\w+:\d+>\s*)?quest\s*seals?', re.I)

class Quest(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active = True
        self.task = None
        self.engine = LazyQuestEngine(self.bot)
        self._claimed = {}
        self._claim_lock = asyncio.Lock()
        self._last_recheck = 0.0
        # so the "rows are an image" note is logged once per card, not every 6 hours
        self._card_logged = None

    async def register_actions(self):
        cfg = self.bot.config.get('commands', {}).get('quest', {})
        if cfg.get('enabled', True):
            self.bot.log("SYS", "Quest Module configured.")
            ih = cfg.get('interval_h', 6)
            await self.bot.neura_register_command("quest", "quest", priority=self.bot.get_cmd_priority("quest", 4), delay=ih * 3600, initial_offset=10)
            self.trigger_action()
            
            self.engine.start()

    def trigger_action(self):
        cfg = self.bot.config.get('commands', {}).get('quest', {})
        ih = cfg.get('interval_h', 6)
        
        if 'quest' in self.bot.cmd_states:
            self.bot.cmd_states['quest']['delay'] = ih * 3600

    @commands.Cog.listener('on_owo_gateway_message')
    async def on_owo_gateway_message(self, raw_data):
        # core.bot decodes, filters and parses the frame once for every cog.
        # deliberately not gated on bot.paused: reading a card is passive, and an
        # unsolved captcha holds `paused` for as long as it takes the operator to
        # answer it - dropping quest cards for that whole stretch is exactly when the
        # dashboard went stale. The outgoing side (_claim_rewards) checks paused itself.
        if not self.active:
            return

        data = raw_data.get("d") or {}
        if str((data.get("author") or {}).get("id")) != self.bot.owo_bot_id:
            return

        if str(data.get("channel_id")) not in [str(c) for c in self.bot.channels]:
            return

        components = parse_v2_message(data)
        if not components:
            return

        v2_text = collect_text(components)
        content = data.get("content") or ""
        full_text = f"{content}\n{v2_text}".lower()

        # OwO titles the quest card differently across updates - see QUEST_HEADERS.
        # The _v2_text_is_mine guard below still keeps us from grabbing another
        # account's card in a shared channel.
        if any(header in full_text for header in QUEST_HEADERS):
            if not self._v2_text_is_mine(full_text):
                return

            await self._parse_quests_v2(components, data, v2_text)

    def _v2_text_is_mine(self, full_text):
        """components v2 messages are invisible to discord.py-self, so match by name."""
        return self.bot.identity.text_is_mine(full_text)

    async def _parse_quests_v2(self, components, message_data, v2_text=None):
        text = v2_text if v2_text is not None else collect_text(components)
        text = text.replace('*', '').replace('`', '')
        text_lines = [line.strip().lower() for line in text.split('\n') if line.strip()]

        quests = []
        current_quest = None
        for line in text_lines:
            title_match = QUEST_TITLE_RE.match(line)
            if title_match:
                title = title_match.group(2).strip()
                current_quest = {
                    'slot': int(title_match.group(1)),
                    'description': title,
                    'title': title,
                    'current': 0,
                    'total': 1,
                    'completed': False,
                }
                quests.append(current_quest)
                self._apply_progress(current_quest, title)
                continue

            if current_quest is not None and self._apply_progress(current_quest, line):
                current_quest = None

        st = self.bot.stats
        old_quests = st.get('quest_data', [])

        cleaned_quests = []
        for q in quests:
            desc_text = q['description']
            cleaned_quests.append({
                'description': desc_text,
                'current': q['current'],
                'total': q['total'],
                'completed': q['completed'],
            })

            if q['completed']:
                was_completed = any(
                    oq.get('description', '').lower() == desc_text.lower() and oq.get('completed')
                    for oq in old_quests
                )
                if not was_completed:
                    self.bot.log("SUCCESS", f"QUEST COMPLETED: {desc_text}")

        # OwO now renders the quest rows as a picture ("quest-rows.png") and the url is
        # only inside the media_gallery component - `attachments` is empty on these
        # cards. Everything around the rows (header, seal balance, next-quest stamp) is
        # still text, which is why the card is recognised but no quest line ever parses.
        card_url = media_image_url(components, name_contains=("quest", "checklist", "rows"))

        if cleaned_quests:
            st['quest_data'] = cleaned_quests
            st.pop('quest_source', None)
            st.pop('quest_card_url', None)
            self.bot.log("SYS", f"Dashboard synced: {len(cleaned_quests)} V2 quests tracked.")
        elif card_url:
            # Keeping the previous card's rows would be worse than keeping none: the
            # quest engine drives sibling pings and Force Lucky Gems off this list, so
            # stale rows make it act on quests that may have finished days ago. Empty
            # plus quest_source='image' is the truth - the rows genuinely are not on the
            # wire any more, and inventing progress numbers is not an option.
            st['quest_data'] = []
            st['quest_source'] = 'image'
            st['quest_card_url'] = card_url
            if self._card_logged != card_url:
                self._card_logged = card_url
                self.bot.log(
                    "INFO",
                    "OwO drew the quest rows as an image (quest-rows.png), so the "
                    "descriptions and N/M counters are pixels, not text. The card itself "
                    "is shown on the dashboard. Auto-claim is unaffected - it follows the "
                    "Claim button, not the text - but quest-text automation (social quest "
                    "routing, Force Lucky Gems) has nothing to read."
                )
        else:
            # the card was recognised as a quest log of ours, yet not one line parsed.
            # This used to fall through in silence, so the panel kept showing stale (or
            # no) quests with nothing in the log to point at the real cause. Print a
            # sample of what owo actually sent so the shape can be matched.
            sample = " | ".join(line.strip() for line in text_lines if line.strip())[:300]
            self.bot.log(
                "WARN",
                f"Quest card recognised but no quest lines parsed - dashboard left unchanged. "
                f"Card text: {sample}"
            )

        # the seal balance survives as text even on an image card, and was thrown away
        seal_match = SEAL_RE.search(text)
        if seal_match:
            try:
                st['quest_seals'] = int(seal_match.group(1).replace(',', ''))
            except ValueError:
                pass

        #  global timer - a discord relative timestamp on modern cards, plain text on old
        ts_match = DISCORD_TS_RE.search(text)
        if ts_match:
            st['next_quest_at'] = int(ts_match.group(1))
            st['next_quest_timer'] = None
        else:
            st.pop('next_quest_at', None)
            timer_pattern = r'next quest.*?\bin\s*(\d+\w+(?:\s*\d+\w+)*)'
            for line in text_lines:
                timer_match = re.search(timer_pattern, line)
                if timer_match:
                    st['next_quest_timer'] = timer_match.group(1).upper()
                    break

        await self._claim_rewards(components, message_data, sum(1 for q in quests if q['completed']))

    @staticmethod
    def _claim_targets(components):
        """Every *enabled* claim control on the card.

        `buttons()` already drops the disabled ones and owo only enables a claim
        button while the reward is actually waiting, so this is a far better signal
        than re-deriving completion from the "N/M" text. Claiming used to be gated on
        that text plus a slot number scraped out of the custom_id, and when either
        guess missed - a finished quest rendered with a tick instead of a counter, a
        custom_id whose trailing digits were not the slot - the reward was left
        sitting there. That is the "completes but never claims" bug.
        """
        found = []
        for comp in buttons(components):
            haystack = f"{comp.custom_id} {comp.label or ''}".lower()
            if any(hint in haystack for hint in CLAIM_HINTS):
                found.append((comp.custom_id, comp.label or comp.custom_id))
        return found

    async def _claim_rewards(self, components, message_data, completed_count):
        cfg = self.bot.config.get('commands', {}).get('quest', {})
        if not cfg.get('auto_claim', True):
            return
        # reading the card is passive, clicking a claim button is not. `paused` covers an
        # unsolved captcha and an operator stop, and neither should produce traffic.
        if self.bot.paused:
            return

        channel_id = message_data.get("channel_id")
        message_id = str(message_data.get("id") or "")
        if not channel_id or not message_id:
            return

        targets = self._claim_targets(components)
        if not targets:
            if completed_count:
                await self._recheck_for_claim(completed_count)
            return

        for custom_id, label in targets:
            key = f"{message_id}:{custom_id}"
            # owo edits the quest card after every claim and MESSAGE_UPDATE brings us
            # straight back here, so without this the same button is clicked on a loop
            if key in self._claimed:
                continue
            self._claimed[key] = time.time()

            async with self._claim_lock:
                # awaited, not fire-and-forget: click_button_raw returns whether discord
                # accepted the interaction, and throwing that away meant a rejected claim
                # looked identical to a successful one
                try:
                    ok = await self.bot.interactions.click_button_raw(
                        custom_id=custom_id,
                        message_id=message_data.get("id"),
                        channel_id=int(channel_id),
                        author_id=(message_data.get("author") or {}).get("id"),
                        guild_id=message_data.get("guild_id"),
                        flags=message_data.get("flags", 0)
                    )
                except Exception as e:
                    ok = False
                    self.bot.log("ERROR", f"Quest claim raised: {e}")

                if ok:
                    self.bot.log("SUCCESS", f"Quest reward claimed ({label}).")
                else:
                    # forget the key so the next card retries instead of skipping forever
                    self._claimed.pop(key, None)
                    self.bot.log("ERROR", f"Quest reward claim rejected ({label}) - retrying on the next quest card.")

                await asyncio.sleep(random.uniform(1.1, 2.2))

        if len(self._claimed) > 200:
            self._claimed.clear()

    async def _recheck_for_claim(self, completed_count):
        """Finished quests, but the card carried no claim button - ask for a fresh one.

        Deliberately enqueued under its own id: the scheduled `quest` slot sits on a
        six-hour cooldown, and waiting that long to collect a finished reward is the
        very thing being fixed here.
        """
        if time.time() - self._last_recheck < 300:
            return
        self._last_recheck = time.time()
        self.bot.log(
            "WARN",
            f"{completed_count} quest(s) finished but the card had no claim button - "
            "requesting a fresh quest log."
        )
        await self.bot.neura_enqueue("owo quest", priority=4, _cmd_id="quest_claim_recheck")

    @staticmethod
    def _apply_progress(quest, line):
        progress_match = PROGRESS_RE.search(line)
        if not progress_match:
            if any(marker in line for marker in DONE_MARKERS):
                quest['current'] = quest['total']
                quest['completed'] = True
                return True
            return False
        current, total = int(progress_match.group(1)), int(progress_match.group(2))
        if total <= 0:
            return False
        quest['current'] = current
        quest['total'] = total
        quest['completed'] = current >= total or any(marker in line for marker in DONE_MARKERS)
        return True

    @commands.Cog.listener()
    async def on_message(self, message):
        core_config = self.bot.config.get('core', {})
        monitor_id = str(core_config.get('monitor_bot_id', '408785106942164992'))
        
        if str(message.author.id) != monitor_id:
            return
        if self.bot.owo_user is None:
            self.bot.owo_user = message.author
        all_channels = [str(c) for c in self.bot.channels]
        if str(message.channel.id) not in all_channels:
            return

        full_text = self.bot.get_full_content(message)
        if not any(header in full_text for header in QUEST_HEADERS):
            return
        if not self.bot.is_message_for_me(message, role="header"):
            return

        completed = self._parse_quests_legacy(full_text)

        # a legacy embed can still carry real buttons, and the old code skipped the
        # whole message whenever it did - so a claimable reward on an embed card was
        # never even looked at
        if message.components:
            await self._claim_legacy(message, completed)

    async def _claim_legacy(self, message, completed_count):
        """Claim from an embed-style card using discord.py-self's own button click."""
        cfg = self.bot.config.get('commands', {}).get('quest', {})
        if not cfg.get('auto_claim', True):
            return
        # same rule as the v2 claim path: no outgoing clicks while paused
        if self.bot.paused:
            return

        for row in message.components:
            for btn in getattr(row, 'children', []):
                if getattr(btn, 'disabled', False):
                    continue
                haystack = f"{getattr(btn, 'custom_id', '') or ''} {getattr(btn, 'label', '') or ''}".lower()
                if not any(hint in haystack for hint in CLAIM_HINTS):
                    continue

                key = f"{message.id}:{getattr(btn, 'custom_id', '') or haystack}"
                if key in self._claimed:
                    continue
                self._claimed[key] = time.time()
                try:
                    await asyncio.sleep(random.uniform(0.8, 1.8))
                    await btn.click()
                    self.bot.log("SUCCESS", f"Quest reward claimed ({getattr(btn, 'label', None) or 'claim'}).")
                except Exception as e:
                    self._claimed.pop(key, None)
                    self.bot.log("ERROR", f"Quest reward claim failed: {e}")
                return

        if completed_count:
            await self._recheck_for_claim(completed_count)

    def _parse_quests_legacy(self, text):
        progress_pattern = r'progress:\s*\[(\d+)/(\d+)\]'
        timer_pattern = r'next quest in:\s*(\d+h \d+m \d+s)'
        
        clean_text = text.replace(':blank:', '').replace('*', '')
        lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
        
        new_quest_data = []
        current_description = None
        
        st = self.bot.stats
        old_quests = st.get('quest_data', [])
        
        for i, line in enumerate(lines):
            if "reward:" in line.lower():
                desc_part = re.split(r'reward:', line, flags=re.IGNORECASE)[0].strip()
                desc_part = desc_part.replace('‣', '').strip()
                
                if desc_part:
                    raw_desc = desc_part
                else:
                    raw_desc = lines[i-1] if i > 0 else ""
                
                clean_desc = re.sub(r'^\d+[\)\.]\s*', '', raw_desc)
                clean_desc = re.sub(r'<[^>]*>', '', clean_desc)
                clean_desc = clean_desc.replace('`', '').strip()
                
                if clean_desc and 'quest log' not in clean_desc.lower() and 'quests belong' not in clean_desc.lower():
                    current_description = clean_desc
            
            progress_match = re.search(progress_pattern, line, re.IGNORECASE)
            if progress_match and current_description:
                current = int(progress_match.group(1))
                total = int(progress_match.group(2))
                
                is_completed = current >= total
                quest_item = {
                    'description': current_description,
                    'current': current,
                    'total': total,
                    'completed': is_completed
                }
                new_quest_data.append(quest_item)
                
                if is_completed:
                    was_completed = any(q['description'] == current_description and q.get('completed') for q in old_quests)
                    if not was_completed:
                        self.bot.log("SUCCESS", f"QUEST COMPLETED: {current_description}")
                
                current_description = None

        timer_match = re.search(timer_pattern, text, re.IGNORECASE)
        next_timer = timer_match.group(1).upper() if timer_match else None

        valid_quests = [q for q in new_quest_data if 'progress' not in q['description'].lower()]

        if valid_quests or "quest log" in text.lower():
            st['quest_data'] = valid_quests
        if valid_quests:
            # a readable embed card supersedes any image card we stored earlier
            st.pop('quest_source', None)
            st.pop('quest_card_url', None)

        seal_match = SEAL_RE.search(text)
        if seal_match:
            try:
                st['quest_seals'] = int(seal_match.group(1).replace(',', ''))
            except ValueError:
                pass

        # embeds carry the same relative timestamp as v2 cards, so read it here too
        ts_match = DISCORD_TS_RE.search(text)
        if ts_match:
            st['next_quest_at'] = int(ts_match.group(1))
            next_timer = None
        elif next_timer:
            st.pop('next_quest_at', None)

        st['next_quest_timer'] = next_timer
        return sum(1 for q in valid_quests if q.get('completed'))

async def setup(bot):
    cog = Quest(bot)
    await bot.add_cog(cog)