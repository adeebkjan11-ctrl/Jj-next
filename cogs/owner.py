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
import datetime
import re
import time
from discord.ext import commands
import core.state as state
from core.withdrawal_limits import send_limit
from core.transfers import recipient_lock, matches_prompt, confirmed_receipt, payload_text
from component_v2_neura.parser import parse_v2_message, buttons


class Owner(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self._cash = None
        self._prompt = None
        self._receipt = None
        self._channel_id = None
        self._recipient = None
        self._amount = 0
        self._prompt_id = None
        self._raw = {}

    def _config(self):
        return self.bot.config.get('owner', {})

    def _owner_id(self):
        cfg = self._config()
        uid = str(cfg.get('user_id', ''))
        return uid if cfg.get('enabled') and uid.isdigit() else None

    def _allowance(self):
        rec = self.bot.stats.setdefault('owner_send', state.empty_owner_send())
        today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        if rec.get('day') != today:
            rec.clear()
            rec.update(day=today, sent=0)
        return rec

    def _daily_limit(self):
        return send_limit(self._config(), self.bot.stats)

    def _remaining_today(self):
        rec = self._allowance()
        if rec.get('server_remaining') is not None:
            return max(0, int(rec['server_remaining']))
        limit = self._daily_limit()
        return None if limit is None else max(0, limit - int(rec.get('sent', 0)))

    def _status(self, status, **extra):
        record = self.bot.stats.setdefault('withdrawal', {})
        record.update(status=status, updated_at=time.time(), recipient=self._recipient, **extra)
        state.save_account_stats(force=True, strict=True)
        return dict(record)

    def _book_sent(self, amount):
        receipts = self.bot.stats.setdefault('withdrawal_receipts', [])
        if self._prompt_id and any(r.get('message_id') == self._prompt_id for r in receipts):
            return
        rec = self._allowance()
        rec['sent'] = int(rec.get('sent', 0)) + amount
        if rec.get('server_remaining') is not None:
            rec['server_remaining'] = max(0, int(rec['server_remaining']) - amount)
        # Performance graphs must not interpret a withdrawal as negative earnings.
        self.bot.stats.setdefault('withdrawal_receipts', []).append({'at': time.time(), 'amount': amount, 'message_id': self._prompt_id})
        self.bot.stats['withdrawal_receipts'] = self.bot.stats['withdrawal_receipts'][-1000:]
        state.save_account_stats(force=True, strict=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if str(message.author.id) == str(self.bot.owo_bot_id):
            await self._observe_object(message)
            return
        owner = self._owner_id()
        if not owner or str(message.author.id) != owner:
            return
        if str(message.channel.id) not in map(str, self.bot.channels):
            return
        text = (message.content or '').strip()
        trigger = str(self._config().get('trigger', 'farmers')).strip()
        if not trigger or not (text.lower() == trigger.lower() or text.lower().startswith(trigger.lower() + ' ')):
            return
        action = text[len(trigger):].strip()
        parts = action.split(None, 1)
        names = {str(a.get('name', '')).lower() for a in getattr(self.bot, 'accounts', [])}
        if len(parts) == 2 and (parts[0].isdigit() or parts[0].lower() in names):
            if parts[0] not in (str(self.bot.user_id), str(getattr(self.bot, 'account_name', '')).lower()):
                return
            action = parts[1]
        if action.lower() == 'send':
            task = asyncio.create_task(self.withdraw(owner, message.channel.id))
            self.bot.worker_tasks.append(task)
            task.add_done_callback(lambda t: self.bot.worker_tasks.remove(t) if t in self.bot.worker_tasks else None)
        elif action.lower() == 'pay':
            await self.bot.neura_enqueue(f'{self.bot.prefix}pray <@{owner}>', priority=2, target_channel_id=message.channel.id)
        elif action.lower() in ('showbal', 'bal'):
            await self.bot.neura_enqueue(f'{self.bot.prefix}cash', priority=2, target_channel_id=message.channel.id)
        elif action:
            if action.lower().startswith(self.bot.prefix.lower()):
                action = action[len(self.bot.prefix):]
            await self.bot.neura_enqueue(f'{self.bot.prefix}{action}', priority=2, target_channel_id=message.channel.id)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        await self._observe_object(after)

    async def _observe_object(self, message):
        if str(message.author.id) != str(self.bot.owo_bot_id):
            return
        components = []
        for row in getattr(message, 'components', []) or []:
            components.append({'type': 1, 'components': [
                {'type': 2, 'custom_id': getattr(b, 'custom_id', None),
                 'label': getattr(b, 'label', ''), 'disabled': getattr(b, 'disabled', False)}
                for b in getattr(row, 'children', []) or []]})
        await self._observe({'id': str(message.id), 'channel_id': str(message.channel.id),
            'guild_id': str(message.guild.id) if message.guild else None,
            'author': {'id': str(message.author.id)}, 'content': message.content or '',
            'embeds': [e.to_dict() for e in message.embeds], 'components': components,
            'flags': getattr(getattr(message, 'flags', None), 'value', 0)})

    @commands.Cog.listener('on_owo_gateway_message')
    async def on_owo_gateway_message(self, payload):
        data = payload.get('d') or {}
        mid = str(data.get('id', ''))
        # MESSAGE_UPDATE may omit author, channel and content. Merge only a
        # message observed in this transaction, never infer ownership by amount.
        prior = self._raw.get(mid, {})
        merged = dict(prior, **data)
        if str((merged.get('author') or {}).get('id')) != str(self.bot.owo_bot_id):
            return
        if len(self._raw) >= 50:
            self._raw.pop(next(iter(self._raw)))
        self._raw[mid] = merged
        await self._observe(merged)

    async def _observe(self, data):
        if str(data.get('channel_id')) != str(self._channel_id):
            return
        text = payload_text(data)
        if self._cash is not None and not self._cash.done() and self.bot.identity.text_is_mine(text):
            found = re.search(r'you currently have[^\d]*([\d,]+)', text, re.I)
            if found:
                self._cash.set_result(int(found.group(1).replace(',', '')))
        mid = str(data.get('id', ''))
        if self._prompt is not None and not self._prompt.done() and matches_prompt(
                text, self.bot.user_id, self._recipient, self._amount):
            entries = [b for b in buttons(parse_v2_message(data)) if not b.disabled]
            # The transaction has already been matched to both parties and its
            # amount. Accept the official ID or an explicit affirmative label;
            # never fall back to an arbitrary first/emoji-only button.
            target = next((b for b in entries if b.custom_id == 'give_accept' or
                           (re.sub(r'[^a-z ]', '', (b.label or '').lower()).strip() in
                            ('confirm', 'send', 'accept', 'yes') and b.custom_id and
                            not re.search(r'(?:cancel|decline|reject|deny)', b.custom_id, re.I))), None)
            if target:
                self._prompt_id = mid
                self._prompt.set_result((data, target.custom_id))
        if self._receipt is None or self._receipt.done():
            return
        if mid == self._prompt_id and confirmed_receipt(text, self._amount):
            self._receipt.set_result({'status': 'confirmed'})
        elif self.bot.identity.text_is_mine(text):
            plain = re.sub(r'[*`_]', '', text).lower()
            sender_remaining = re.search(r'you can only send\s+([\d,]+)\s+more cowoncy today', plain)
            if sender_remaining:
                self._allowance()['server_remaining'] = int(sender_remaining.group(1).replace(',', ''))
                self._receipt.set_result({'status': 'limited', 'reason': 'Sender allowance reported by OwO'})
            elif 'cannot send any more cowoncy today' in plain or 'daily cowoncy give limit' in plain:
                self._allowance()['server_remaining'] = 0
                self._receipt.set_result({'status': 'limited', 'reason': 'Daily sender limit reached'})
            elif 'receive' in plain and ('limit' in plain or 'more cowoncy today' in plain):
                self._receipt.set_result({'status': 'recipient_limited', 'reason': 'Recipient receive limit reached'})
            elif mid == self._prompt_id and any(x in plain for x in ('declined', 'inactive', 'not have enough', "don't have enough")):
                self._receipt.set_result({'status': 'failed', 'reason': 'OwO rejected or expired the transfer'})

    async def _fetch_payload(self, message_id):
        manager = self.bot.interactions
        session = await manager._get_session()
        async with session.get(
                f'https://discord.com/api/v9/channels/{self._channel_id}/messages/{message_id}',
                headers={'Authorization': self.bot.token}, **manager._proxy_kwargs()) as response:
            if response.status == 200:
                return await response.json()
        return None

    async def _poll_receipt(self):
        if not self._prompt_id:
            return
        try:
            data = await self._fetch_payload(self._prompt_id)
            if data:
                await self._observe(data)
        except Exception:
            pass

    async def reconcile(self):
        """Re-read the original prompt without ever issuing a second give command."""
        if self._lock.locked():
            return {'status': 'busy'}
        async with self._lock:
            rec = self.bot.stats.get('withdrawal', {})
            if rec.get('status') not in ('unknown', 'sending', 'awaiting_confirmation', 'verifying'):
                return dict(rec)
            if not rec.get('prompt_id'):
                return dict(rec, reason='No prompt ID was received; check the Discord channel manually')
            self._prompt_id = rec.get('prompt_id')
            self._channel_id = rec.get('channel_id')
            self._recipient = rec.get('recipient')
            try:
                data = await self._fetch_payload(rec['prompt_id'])
            except Exception:
                data = None
            if data and str((data.get('author') or {}).get('id')) == str(self.bot.owo_bot_id):
                text = payload_text(data)
                if confirmed_receipt(text, rec.get('amount', 0)):
                    self._book_sent(int(rec['amount']))
                    return self._status('confirmed', confirmed_total=int(rec.get('confirmed_total', 0)) + int(rec['amount']), reason=None)
                plain = text.lower()
                if any(word in plain for word in ('declined the transaction', 'message is now inactive')):
                    return self._status('failed', reason='OwO confirms this prompt was cancelled or expired')
            return dict(rec, reason='No conclusive receipt yet; no second transfer was sent')

    async def withdraw(self, recipient, channel_id=None, percent=None):
        if self._lock.locked():
            return {'status': 'busy', 'reason': 'This account already has a withdrawal in progress'}
        async with self._lock, recipient_lock(recipient):
            prior = self.bot.stats.get('withdrawal', {})
            if prior.get('status') in ('sending', 'awaiting_confirmation', 'verifying', 'unknown'):
                # A restart after submit cannot establish whether money moved.
                return {'status': 'unknown', 'reason': 'Previous transfer needs receipt reconciliation', **prior}
            if not self.bot.active or not self.bot.is_ready or self.bot.paused:
                return {'status': 'skipped', 'reason': 'Account is stopped, connecting, or paused'}
            if str(recipient) == str(self.bot.user_id):
                return {'status': 'skipped', 'reason': 'Recipient is this account'}
            self._recipient = str(recipient)
            self._channel_id = channel_id or self.bot.channel_id
            loop = asyncio.get_running_loop()
            total = 0
            try:
                self._cash = loop.create_future()
                self._status('checking_balance', amount=0, confirmed_total=0)
                if not await asyncio.wait_for(self.bot.send_message(f'{self.bot.prefix}cash', priority=True,
                        target_channel_id=self._channel_id), 60):
                    return self._status('failed', reason='Could not request balance')
                balance = await asyncio.wait_for(asyncio.shield(self._cash), 45)
                self.bot.stats.update(current_cash=balance, last_cash_update=time.time())
                remaining = self._remaining_today()
                if remaining is None:
                    return self._status('skipped', reason='Level unknown; configure a fixed limit or refresh the level')
                pct = max(1, min(100, float(percent if percent is not None else self._config().get('send_percent', 90))))
                target = min(int(balance * pct / 100), remaining)
                while total < target:
                    if not self.bot.active or self.bot.paused:
                        return self._status('partial' if total else 'skipped', confirmed_total=total, reason='Account paused')
                    self._amount = min(100000, target - total)
                    self._prompt_id = None
                    self._prompt, self._receipt = loop.create_future(), loop.create_future()
                    self._status('sending', amount=self._amount, confirmed_total=total, prompt_id=None,
                                 channel_id=str(self._channel_id))
                    sent = await asyncio.wait_for(self.bot.send_message(
                        f'{self.bot.prefix}give <@{recipient}> {self._amount}', priority=True,
                        target_channel_id=self._channel_id), 60)
                    if not sent:
                        return self._status('unknown', reason='Give send failed; check for a receipt before retrying')
                    self._status('awaiting_confirmation')
                    done, _ = await asyncio.wait([self._prompt, self._receipt], timeout=90, return_when=asyncio.FIRST_COMPLETED)
                    if self._receipt in done:
                        return self._status(**self._receipt.result(), confirmed_total=total)
                    if self._prompt not in done:
                        return self._status('unknown', reason='Confirmation prompt not received; no automatic resend')
                    data, custom_id = self._prompt.result()
                    self._status('verifying', prompt_id=self._prompt_id)
                    for attempt in range(3):
                        if self._receipt.done():
                            break
                        try:
                            await self.bot.interactions.click_button_raw(custom_id=custom_id,
                                message_id=self._prompt_id, channel_id=int(self._channel_id),
                                author_id=self.bot.owo_bot_id, guild_id=data.get('guild_id'), flags=data.get('flags', 0))
                        except Exception:
                            pass
                        # An accepted interaction is not proof of a completed transfer.
                        try:
                            await asyncio.wait_for(asyncio.shield(self._receipt), 8)
                        except asyncio.TimeoutError:
                            await self._poll_receipt()
                    if not self._receipt.done():
                        return self._status('unknown', reason='No final receipt; transfer was not resent')
                    result = self._receipt.result()
                    if result['status'] != 'confirmed':
                        return self._status(**result, confirmed_total=total)
                    self._book_sent(self._amount)
                    total += self._amount
                    self.bot.stats['current_cash'] = balance - total
                    self._status('confirmed', confirmed_total=total)
                    if total < target:
                        await asyncio.sleep(6)
                return self._status('confirmed' if total else 'limited', confirmed_total=total,
                                    reason=None if total else 'Nothing withdrawable today')
            except asyncio.CancelledError:
                current = self.bot.stats.get('withdrawal', {}).get('status')
                self._status('unknown' if current in ('sending', 'awaiting_confirmation', 'verifying') else 'cancelled')
                raise
            except Exception as exc:
                current = self.bot.stats.get('withdrawal', {}).get('status')
                return self._status('unknown' if current in ('sending', 'awaiting_confirmation', 'verifying') else 'failed',
                                    reason=type(exc).__name__, confirmed_total=total)
            finally:
                self._cash = self._prompt = self._receipt = None
                self._raw.clear()

    async def register_actions(self):
        pass


async def setup(bot):
    await bot.add_cog(Owner(bot))
