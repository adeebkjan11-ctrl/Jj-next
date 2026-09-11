"""An account identified only at its first login still gets into its channel."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from support import extract
from core import monitor
from utils import proxy_manager


class FakeChannelAPI:
    """Just the two channel calls grant_channel_access makes."""
    def __init__(self, token):
        self.token = token
        FakeChannelAPI.calls = getattr(FakeChannelAPI, 'calls', [])
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def request(self, method, path, payload=None):
        FakeChannelAPI.calls.append((method, path, payload))
        if method == 'GET':
            return {'id': '80001', 'permission_overwrites': [
                {'id': '22222', 'type': 0, 'deny': '1024', 'allow': '0'},
                {'id': '99999', 'type': 1, 'allow': str(monitor.ACCESS), 'deny': '0', 'unexpected': 'field'}]}
        return {}


class ChannelAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = 'u_bbccdd'
        monitor.save_config(self.owner, {'token': 'synthetic', 'guild_id': '22222'})
        proxy_manager.save_accounts(self.owner, [{'name': 'acc1', 'token': 'synthetic-1', 'channels': ['80001'],
            'channel_setup': {'status': 'awaiting_identity', 'reason': 'Refused', 'channel_id': '80001'}}])
        FakeChannelAPI.calls = []
        patcher = patch.object(monitor, 'BotAPI', FakeChannelAPI)
        patcher.start(); self.addCleanup(patcher.stop)

    def setup_row(self):
        return proxy_manager.load_accounts(self.owner)[0]['channel_setup']

    async def test_overwrite_is_added_without_dropping_the_existing_ones(self):
        self.assertTrue(await monitor.grant_channel_access(self.owner, 'acc1', '10000'))
        method, path, payload = FakeChannelAPI.calls[-1]
        self.assertEqual((method, path), ('PATCH', '/channels/80001'))
        self.assertEqual([o['id'] for o in payload['permission_overwrites']], ['22222', '99999', '10000'])
        self.assertEqual(payload['permission_overwrites'][-1], {'id': '10000', 'type': 1,
                                                               'allow': str(monitor.ACCESS), 'deny': '0'})
        # Discord rejects unknown keys on the way in, so they are not echoed back.
        self.assertTrue(all(set(o) == {'id', 'type', 'allow', 'deny'} for o in payload['permission_overwrites']))
        self.assertEqual(self.setup_row(), dict(self.setup_row(), status='assigned', reason='', channel_id='80001'))

    async def test_second_grant_is_a_no_op_once_the_account_is_assigned(self):
        await monitor.grant_channel_access(self.owner, 'acc1', '10000')
        FakeChannelAPI.calls = []
        self.assertFalse(await monitor.grant_channel_access(self.owner, 'acc1', '10000'))
        self.assertEqual(FakeChannelAPI.calls, [])

    async def test_channel_is_taken_from_the_account_when_setup_recorded_none(self):
        accounts = proxy_manager.load_accounts(self.owner)
        accounts[0]['channel_setup'] = {'status': 'awaiting_identity', 'reason': 'Refused'}
        proxy_manager.save_accounts(self.owner, accounts)
        self.assertTrue(await monitor.grant_channel_access(self.owner, 'acc1', '10000'))
        self.assertEqual(FakeChannelAPI.calls[-1][1], '/channels/80001')

    async def test_nothing_happens_without_a_monitor_bot_an_id_or_a_known_account(self):
        self.assertFalse(await monitor.grant_channel_access(self.owner, 'acc1', 'not-an-id'))
        self.assertFalse(await monitor.grant_channel_access(self.owner, 'no-such-account', '10000'))
        monitor.save_config(self.owner, {'token': '', 'guild_id': ''})
        self.assertFalse(await monitor.grant_channel_access(self.owner, 'acc1', '10000'))
        self.assertEqual(FakeChannelAPI.calls, [])


class ResolveIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_fresh_login_is_preferred_over_the_recorded_id(self):
        with patch.object(monitor, '_identify_account', AsyncMock(return_value='10000')):
            self.assertEqual(await monitor.resolve_identity('admin', {'user_id': '19999'}), ('10000', 'login'))

    async def test_a_refused_probe_falls_back_to_the_recorded_id(self):
        for failure in (monitor.AccountSetupError('Refused'), asyncio.TimeoutError(), OSError('proxy')):
            with patch.object(monitor, '_identify_account', AsyncMock(side_effect=failure)):
                self.assertEqual(await monitor.resolve_identity('admin', {'user_id': '10000'}), ('10000', 'recorded'))

    async def test_a_refused_probe_with_nothing_recorded_still_raises(self):
        for account in ({}, {'user_id': ''}, {'user_id': 'not-an-id'}):
            with patch.object(monitor, '_identify_account', AsyncMock(side_effect=monitor.AccountSetupError('No'))):
                with self.assertRaises(monitor.AccountSetupError):
                    await monitor.resolve_identity('admin', account)

    async def test_cancellation_is_never_answered_from_the_recorded_id(self):
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.resolve_identity('admin', {'user_id': '10000'})


class BotClaimTests(unittest.IsolatedAsyncioTestCase):
    """core.bot asks for the grant on ready, and a failure never reaches on_ready."""
    def setUp(self):
        self.bot = extract('core/bot.py', ['_claim_setup_channel'], class_name='NeuraBot')()
        self.bot.account_name = 'acc1'
        self.bot.user_id = '10000'
        self.bot.space_owner = 'u_bbccdd'
        self.logged = []
        self.bot.log = lambda kind, message: self.logged.append((kind, message))

    async def test_ready_asks_the_monitor_to_open_the_channel(self):
        grant = AsyncMock(return_value=True)
        with patch.object(monitor, 'grant_channel_access', grant):
            await self.bot._claim_setup_channel()
        grant.assert_awaited_once_with('u_bbccdd', 'acc1', '10000')
        self.assertEqual(self.logged[0][0], 'SUCCESS')

    async def test_a_monitor_failure_is_logged_rather_than_raised(self):
        with patch.object(monitor, 'grant_channel_access', AsyncMock(side_effect=monitor.DiscordError(403))):
            await self.bot._claim_setup_channel()
        self.assertEqual(self.logged[0][0], 'WARN')

    async def test_nothing_is_asked_for_an_account_with_no_identity_yet(self):
        self.bot.user_id = None
        grant = AsyncMock()
        with patch.object(monitor, 'grant_channel_access', grant):
            await self.bot._claim_setup_channel()
        grant.assert_not_called()
        self.assertEqual(self.logged, [])
