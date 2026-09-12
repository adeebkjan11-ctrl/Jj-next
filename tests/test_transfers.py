import asyncio
import datetime
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from support import extract
from core import state
from core.withdrawal_limits import daily_limits, send_limit
from core.transfers import amount_in, matches_prompt, confirmed_receipt, payload_text, recipient_lock
from component_v2_neura.parser import parse_v2_message, buttons

Owner = extract('cogs/owner.py', None, dict(state=state, re=re, datetime=datetime, send_limit=send_limit,
    matches_prompt=matches_prompt, confirmed_receipt=confirmed_receipt, payload_text=payload_text,
    recipient_lock=recipient_lock, parse_v2_message=parse_v2_message, buttons=buttons), 'Owner')


class LimitTests(unittest.TestCase):
    def test_published_formula_and_unknown_levels(self):
        self.assertEqual(daily_limits(0), {'send': 50000, 'receive': 50000})
        self.assertEqual(daily_limits(1)['send'], 64000)
        self.assertEqual(daily_limits(2)['send'], 78000)
        self.assertEqual(daily_limits(10), {'send': 5190000, 'receive': 7785000})
        for level in (None, True, 'unknown', -1, 1.5):
            self.assertIsNone(daily_limits(level))
        self.assertEqual(send_limit({'limit_mode': 'fixed', 'daily_send_limit': 64000}, {'level': None}), 64000)

    def test_prompt_requires_both_parties_and_exact_amount(self):
        self.assertTrue(matches_prompt('<@12345> will give <@67890>: 64,000 cowoncy', '12345', '67890', 64000))
        self.assertFalse(matches_prompt('<@11111> will give <@67890>: 64,000 cowoncy', '12345', '67890', 64000))
        self.assertFalse(matches_prompt('<@12345> will give <@99999>: 64,000 cowoncy', '12345', '67890', 64000))
        self.assertFalse(amount_in('164,000 cowoncy', 64000))
        self.assertFalse(confirmed_receipt('Confirm 64,000 cowoncy', 64000))
        self.assertTrue(confirmed_receipt('sender sent **64,000 cowoncy** to **receiver**!', 64000))


class TransferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.save_patch = patch.object(state, 'save_account_stats'); self.save_patch.start()
        self.addCleanup(self.save_patch.stop)
        self.bot = SimpleNamespace(user_id='12345', space_owner='admin', active=True, is_ready=True, paused=False,
            channel_id=11111, channels=['11111'], owo_bot_id='408785106942164992', prefix='owo ',
            config={'owner': {'limit_mode': 'level', 'send_percent': 100}},
            stats={'level': 1}, identity=SimpleNamespace(text_is_mine=lambda text: '<@12345>' in text),
            interactions=SimpleNamespace(click_button_raw=AsyncMock()), log=lambda *args: None)
        self.cog = Owner(self.bot)

    def prompt(self, amount=64000):
        return {'id': '90000', 'author': {'id': self.bot.owo_bot_id}, 'channel_id': '11111', 'guild_id': '22222',
            'content': f'<@12345> will give <@67890>: {amount:,} cowoncy',
            'components': [{'type': 1, 'components': [{'type': 2, 'custom_id': 'give_accept', 'label': 'Confirm'}]}]}

    async def setup_send(self, text, **kwargs):
        if text == 'owo cash':
            await self.cog._observe({'id': '80000', 'channel_id': '11111',
                'content': '<@12345> you currently have 64,000 cowoncy'})
        else:
            await self.cog.on_owo_gateway_message({'d': self.prompt()})
        return True

    async def test_classic_and_v2_prompt_and_partial_update_receipt(self):
        self.bot.send_message = AsyncMock(side_effect=self.setup_send)
        async def click(**kwargs):
            # OwO's final update omits the author/channel; merge original metadata.
            await self.cog.on_owo_gateway_message({'d': {'id': '90000',
                'content': '<@12345> sent **64,000 cowoncy** to <@67890>!', 'components': []}})
            return True
        self.bot.interactions.click_button_raw.side_effect = click
        result = await self.cog.withdraw('67890')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['confirmed_total'], 64000)
        self.assertEqual(self.bot.stats['owner_send']['sent'], 64000)
        self.assertEqual(self.bot.stats['current_cash'], 0)
        self.assertEqual(len(self.bot.stats['withdrawal_receipts']), 1)
        self.bot.interactions.click_button_raw.assert_awaited_once()

    async def test_rejected_click_retries_without_resending_give(self):
        self.bot.send_message = AsyncMock(side_effect=self.setup_send)
        count = 0
        async def click(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                await self.cog._observe({'id': '90000', 'channel_id': '11111',
                    'content': '<@12345> sent 64,000 cowoncy to <@67890>!'})
                return True
            return False
        self.bot.interactions.click_button_raw.side_effect = click
        # Only shorten the protocol's wait interval, not the withdrawal logic.
        original = asyncio.wait_for
        async def fast_wait(awaitable, timeout):
            return await original(awaitable, .01 if timeout == 8 else timeout)
        with patch('asyncio.wait_for', fast_wait):
            result = await self.cog.withdraw('67890')
        self.assertEqual(result['status'], 'confirmed'); self.assertEqual(count, 2)
        self.assertEqual(self.bot.send_message.await_count, 2)  # one cash, one give

    async def test_accepted_click_without_receipt_is_not_success_or_retried_transfer(self):
        self.bot.send_message = AsyncMock(side_effect=self.setup_send)
        self.bot.interactions.click_button_raw.return_value = True
        original = asyncio.wait_for
        async def fast_wait(awaitable, timeout):
            return await original(awaitable, .001 if timeout == 8 else timeout)
        with patch('asyncio.wait_for', fast_wait):
            result = await self.cog.withdraw('67890')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.bot.stats['owner_send']['sent'], 0)
        before = self.bot.send_message.await_count
        await self.cog.withdraw('67890')
        self.assertEqual(self.bot.send_message.await_count, before)

    async def test_actual_remaining_allowance_is_not_marked_fully_spent(self):
        async def send(text, **kwargs):
            if text == 'owo cash':
                return await self.setup_send(text, **kwargs)
            await self.cog._observe({'channel_id': '11111', 'id': '91000',
                'content': '<@12345>, you can only send **12,000** more cowoncy today!'})
            return True
        self.bot.send_message = AsyncMock(side_effect=send)
        result = await self.cog.withdraw('67890')
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(self.cog._remaining_today(), 12000)
        self.assertEqual(self.bot.stats['owner_send']['sent'], 0)

    async def test_unknown_level_sends_and_paused_account_does_not(self):
        self.bot.send_message = AsyncMock(side_effect=self.setup_send)
        self.bot.stats['level'] = None
        async def click(**kwargs):
            await self.cog.on_owo_gateway_message({'d': {'id': '90000',
                'content': '<@12345> sent 64,000 cowoncy to <@67890>!'}})
        self.bot.interactions.click_button_raw.side_effect = click
        result = await self.cog.withdraw('67890')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.bot.send_message.await_args_list[1].args[0], 'owo send <@67890> 64000')
        self.bot.paused = True
        await self.cog.withdraw('67890')
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_unknown_limit_rejection_retries_reported_allowance(self):
        self.bot.stats['level'] = None
        async def send(text, **kwargs):
            if text == 'owo cash':
                return await self.setup_send(text, **kwargs)
            if text.endswith('64000'):
                await self.cog._observe({'channel_id': '11111', 'id': '91000',
                    'content': '<@12345>, you can only send **12,000** more cowoncy today!'})
            else:
                self.assertEqual(text, 'owo send <@67890> 12000')
                await self.cog.on_owo_gateway_message({'d': self.prompt(12000)})
            return True
        async def click(**kwargs):
            await self.cog.on_owo_gateway_message({'d': {'id': '90000',
                'content': '<@12345> sent 12,000 cowoncy to <@67890>!'}})
        self.bot.send_message = AsyncMock(side_effect=send)
        self.bot.interactions.click_button_raw.side_effect = click
        with patch('asyncio.sleep', AsyncMock()):
            result = await self.cog.withdraw('67890')
        self.assertEqual(result['confirmed_total'], 12000)
        self.assertEqual(self.cog._remaining_today(), 0)
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_balance_does_not_match_another_account_or_emoji_id(self):
        self.cog._channel_id = '11111'
        self.cog._cash = asyncio.get_running_loop().create_future()
        await self.cog._observe({'channel_id': '11111',
            'content': '<@54321> you currently have 99,000 cowoncy'})
        self.assertFalse(self.cog._cash.done())
        await self.cog._observe({'channel_id': '11111', 'components': [
            {'type': 10, 'content': '<@12345> you currently have <:cowoncy:987654321> **__64,000__ cowoncy!**'}]})
        self.assertEqual(self.cog._cash.result(), 64000)

    async def test_nested_components_v2_prompt_is_confirmed(self):
        self.bot.send_message = AsyncMock(side_effect=self.setup_send)
        original = self.prompt
        def v2(amount=64000):
            raw = original(amount)
            text = raw.pop('content')
            buttons_row = raw['components'][0]
            raw['components'] = [{'type': 17, 'components': [{'type': 10, 'content': text}, buttons_row]}]
            return raw
        self.prompt = v2
        async def click(**kwargs):
            await self.cog.on_owo_gateway_message({'d': {'id': '90000',
                'components': [{'type': 17, 'components': [{'type': 10,
                    'content': '<@12345> sent 64,000 cowoncy to <@67890>!'}]}]}})
            return True
        self.bot.interactions.click_button_raw.side_effect = click
        result = await self.cog.withdraw('67890')
        self.assertEqual(result['confirmed_total'], 64000)
        self.assertEqual(result['status'], 'confirmed')

    async def test_reconciliation_books_receipt_once_without_sending(self):
        self.bot.stats['withdrawal'] = {'status': 'unknown', 'amount': 64000, 'prompt_id': '90000',
            'channel_id': '11111', 'recipient': '67890', 'confirmed_total': 0}
        self.bot.send_message = AsyncMock()
        self.cog._fetch_payload = AsyncMock(return_value={'id': '90000', 'author': {'id': self.bot.owo_bot_id},
            'content': '<@12345> sent 64,000 cowoncy to <@67890>!'})
        result = await self.cog.reconcile()
        self.assertEqual(result['status'], 'confirmed')
        await self.cog.reconcile()
        self.assertEqual(self.bot.stats['owner_send']['sent'], 64000)
        self.bot.send_message.assert_not_called()
