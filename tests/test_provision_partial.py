"""Provisioning assigns public channels without testing or removing accounts."""
import asyncio
import copy
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from support import extract
from core import monitor
from utils import proxy_manager
from test_monitor import FakeAPI


class PartialProvisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = 'u_aaddee'
        self.accounts = [{'name': f'acc{i}', 'token': f'token-{i}', 'channels': []} for i in range(7)]
        proxy_manager.save_accounts(self.owner, self.accounts)
        monitor.save_config(self.owner, {'guild_id': '22222', 'token': 'synthetic'})
        monitor._operations[self.owner] = {}
        FakeAPI.channels = []; FakeAPI.calls = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(sys.modules, {'core.supervisor': SimpleNamespace(running_names=lambda _: [])}))
        self.stack.enter_context(patch.object(monitor, 'BotAPI', FakeAPI))
        self.stack.enter_context(patch.object(monitor.asyncio, 'sleep', AsyncMock()))
        self.restart = self.stack.enter_context(patch.object(monitor, 'restart', AsyncMock()))
        async def identity(owner, account): return str(10000 + int(account['name'][3:]))
        self.stack.enter_context(patch.object(monitor, '_identify_account', identity))



    async def test_monitor_is_connected_even_when_no_account_was_assigned(self):
        proxy_manager.save_accounts(self.owner, [self.accounts[0]])
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=monitor.DiscordError(404))):
            await monitor.provision(self.owner)
        overview = next(c for c in FakeAPI.channels if c.get('topic', '').endswith(':monitor'))
        self.assertTrue(monitor.load_config(self.owner)['enabled'])
        self.assertEqual(monitor.load_config(self.owner)['channel_id'], overview['id'])
        self.restart.assert_awaited_once_with(self.owner)

    async def test_overview_channel_is_reused_rather_than_duplicated(self):
        await monitor.provision(self.owner)
        created = len(FakeAPI.channels)
        await monitor.provision(self.owner)
        self.assertEqual(len(FakeAPI.channels), created)
        self.assertEqual(sum(c.get('topic', '').endswith(':monitor') for c in FakeAPI.channels), 1)




    async def test_channel_group_failure_does_not_stop_later_groups(self):
        request = FakeAPI.request
        async def fail_first_group(api, method, path, payload=None):
            if payload and payload.get('name') == 'farm-001': raise monitor.DiscordError(403)
            return await request(api, method, path, payload)
        with patch.object(FakeAPI, 'request', fail_first_group):
            result = await monitor.provision(self.owner)
        self.assertEqual(result['accounts'], 4)
        self.assertEqual(result['failed'], 3)
        saved = proxy_manager.load_accounts(self.owner)
        self.assertTrue(all(a['channels'] for a in saved[3:]))
        self.assertTrue(all(a['channel_setup']['status'] == 'failed' for a in saved[:3]))

    async def test_assignments_survive_later_overview_failure(self):
        request = FakeAPI.request
        async def fail_overview(api, method, path, payload=None):
            if payload and payload.get('name') == 'operations-overview': raise monitor.DiscordError(403)
            return await request(api, method, path, payload)
        with patch.object(FakeAPI, 'request', fail_overview):
            with self.assertRaises(monitor.DiscordError): await monitor.provision(self.owner)
        self.assertTrue(all(a['channels'] for a in proxy_manager.load_accounts(self.owner)))


    async def test_retry_clears_previous_setup_failure(self):
        async def broken(owner, account):
            if account['name'] == 'acc0': raise monitor.AccountSetupError('Invalid token')
            return str(10000 + int(account['name'][3:]))
        with patch.object(monitor, '_identify_account', broken): await monitor.provision(self.owner)
        result = await monitor.provision(self.owner)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['accounts'], 7)
        saved = proxy_manager.load_accounts(self.owner)
        self.assertEqual(saved[0]['channel_setup']['status'], 'assigned')
        self.assertEqual(saved[0]['channel_setup']['reason'], '')

    async def test_shared_bot_auth_failure_does_not_delete_accounts(self):
        with patch.object(FakeAPI, 'request', AsyncMock(side_effect=monitor.DiscordError(401))):
            with self.assertRaises(monitor.DiscordError): await monitor.provision(self.owner)
        self.assertEqual(proxy_manager.load_accounts(self.owner), self.accounts)


    async def test_job_reports_partial_completion(self):
        with patch.object(monitor, 'provision', AsyncMock(return_value={'accounts': 4, 'failed': 1})):
            result = monitor.start_operation(self.owner, 'provision')
            self.assertTrue(result['success'])
            await monitor._tasks[self.owner]
        self.assertEqual(monitor._operations[self.owner]['status'], 'complete_with_errors')

    def test_error_messages_do_not_expose_proxy_credentials(self):
        reason = monitor.setup_error(ValueError('https://user:private@proxy.invalid'), 'verification')
        self.assertNotIn('private', reason)
        self.assertNotIn('proxy.invalid', reason)

    async def test_preserves_all_rows_without_any_account_checks(self):
        self.accounts += [dict(self.accounts[0], name='duplicate')]
        self.accounts[1]['user_id'] = 'stale-id'
        self.accounts[2]['token'] = ''
        self.accounts[3]['proxy_id'] = 'unchanged-proxy'
        proxy_manager.save_accounts(self.owner, self.accounts)
        with patch.object(monitor, 'resolve_identity', AsyncMock(side_effect=AssertionError('No probing'))) as identify:
            result = await monitor.provision(self.owner)
        identify.assert_not_called()
        self.assertEqual(result['accounts'], 8)
        saved = proxy_manager.load_accounts(self.owner)
        for before, after in zip(self.accounts, saved):
            self.assertEqual(before, {k: v for k, v in after.items() if k != 'channel_setup'} | {'channels': []})
            self.assertEqual(after['channel_setup']['status'], 'assigned')
        self.assertFalse(any('/members/' in path for _, path, _ in FakeAPI.calls))

    async def test_migrates_private_categories_channels_and_overview(self):
        await monitor.provision(self.owner)
        original_ids = [c['id'] for c in FakeAPI.channels]
        for channel in FakeAPI.channels:
            channel['permission_overwrites'] = [{'id': '22222', 'type': 0, 'allow': '0', 'deny': '1024'}]
        await monitor.provision(self.owner)
        self.assertEqual(original_ids, [c['id'] for c in FakeAPI.channels])
        for channel in FakeAPI.channels:
            everyone = next(p for p in channel['permission_overwrites'] if p['id'] == '22222')
            self.assertEqual(everyone['deny'], '0')
            self.assertTrue(int(everyone['allow']) & monitor.VIEW)

    async def test_concurrent_config_edits_are_not_overwritten(self):
        request = FakeAPI.request
        async def concurrent(api, method, path, payload=None):
            if payload and payload.get('name') == 'farm-001':
                changed = proxy_manager.load_accounts(self.owner)
                changed[0]['proxy_id'] = 'new-proxy'
                proxy_manager.save_accounts(self.owner, changed)
            return await request(api, method, path, payload)
        with patch.object(FakeAPI, 'request', concurrent):
            with self.assertRaisesRegex(ValueError, 'Accounts changed'):
                await monitor.provision(self.owner)
        self.assertEqual(proxy_manager.load_accounts(self.owner)[0]['proxy_id'], 'new-proxy')

    async def test_cancellation_preserves_saved_accounts(self):
        with patch.object(FakeAPI, 'request', AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await monitor.provision(self.owner)
        self.assertEqual(proxy_manager.load_accounts(self.owner), self.accounts)
