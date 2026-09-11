"""Provisioning continues after isolated failures and removes proven duplicates."""
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

    async def test_bad_token_timeout_missing_member_and_duplicates_do_not_stop_good_accounts(self):
        self.accounts += [dict(self.accounts[0], name='copy-token'), {'name': 'copy-user', 'token': 'different-token', 'channels': []}]
        proxy_manager.save_accounts(self.owner, self.accounts)
        async def identity(owner, account):
            if account['name'] == 'acc1': raise monitor.AccountSetupError('Account token was rejected (HTTP 401).')
            if account['name'] == 'acc2': raise asyncio.TimeoutError()
            if account['name'] == 'copy-user': return '10000'
            return str(10000 + int(account['name'][3:]))
        request = FakeAPI.request
        async def membership(api, method, path, payload=None):
            if path.endswith('/members/10003'): raise monitor.DiscordError(404)
            return await request(api, method, path, payload)
        with patch.object(monitor, '_identify_account', identity), patch.object(FakeAPI, 'request', membership):
            result = await monitor.provision(self.owner)
        self.assertEqual(result, {'channels': 2, 'accounts': 4, 'failed': 1, 'awaiting': 2, 'duplicates_removed': 2})
        saved = proxy_manager.load_accounts(self.owner)
        self.assertEqual([a['name'] for a in saved], [a['name'] for a in self.accounts[:7]])
        self.assertEqual([a['name'] for a in saved if a['channel_setup']['status'] == 'assigned'], ['acc0', 'acc4', 'acc5', 'acc6'])
        # An unidentified account still gets a channel: without one it cannot be
        # started, and only a start can record the id that identifies it.
        self.assertEqual([a['name'] for a in saved if a['channel_setup']['status'] == 'awaiting_identity'], ['acc1', 'acc2'])
        self.assertTrue(all(saved[i]['channels'] for i in (1, 2)))
        self.assertIn('not a member', saved[3]['channel_setup']['reason'])
        rows = monitor._operations[self.owner]['results']
        self.assertEqual(sum(r['status'] == 'duplicate_removed' for r in rows), 2)
        self.assertTrue(all('token' not in r for r in rows))
        calls = [p for _, p, _ in FakeAPI.calls if '/members/' in p]
        self.assertEqual(calls.count('/guilds/22222/members/10000'), 1)

    async def test_unidentified_accounts_are_kept_channelled_and_duplicate_rows_removed(self):
        accounts = [self.accounts[0], dict(self.accounts[0], name='copy')]
        proxy_manager.save_accounts(self.owner, accounts)
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=monitor.AccountSetupError('Refused'))):
            result = await monitor.provision(self.owner)
        self.assertEqual(result, {'channels': 1, 'accounts': 0, 'failed': 0, 'awaiting': 1, 'duplicates_removed': 1})
        saved = proxy_manager.load_accounts(self.owner)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['channel_setup']['status'], 'awaiting_identity')
        self.assertIn('start this account once', saved[0]['channel_setup']['reason'])
        # No overwrite can be written for an account nobody has identified yet.
        group = next(c for c in FakeAPI.channels if ':group:' in c.get('topic', ''))
        self.assertEqual([p['id'] for p in group['permission_overwrites']],
                         ['22222', '99999', '67890', '408785106942164992'])

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

    async def test_recorded_identity_rescues_an_account_discord_refuses_to_confirm(self):
        accounts = [dict(self.accounts[0], user_id='10000'), dict(self.accounts[1], user_id='not-an-id')]
        proxy_manager.save_accounts(self.owner, accounts)
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=monitor.AccountSetupError('Refused'))):
            result = await monitor.provision(self.owner)
        self.assertEqual((result['accounts'], result['awaiting'], result['failed']), (1, 1, 0))
        saved = proxy_manager.load_accounts(self.owner)
        self.assertEqual(saved[0]['channel_setup']['status'], 'assigned')
        self.assertIn('recorded', saved[0]['channel_setup']['reason'])
        group = next(c for c in FakeAPI.channels if ':group:' in c.get('topic', ''))
        self.assertIn('10000', [p['id'] for p in group['permission_overwrites']])

    async def test_recorded_identity_still_has_to_be_in_the_server(self):
        proxy_manager.save_accounts(self.owner, [dict(self.accounts[0], user_id='10000')])
        request = FakeAPI.request
        async def membership(api, method, path, payload=None):
            if path.endswith('/members/10000'): raise monitor.DiscordError(404)
            return await request(api, method, path, payload)
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=monitor.AccountSetupError('Refused'))), \
                patch.object(FakeAPI, 'request', membership):
            result = await monitor.provision(self.owner)
        self.assertEqual((result['accounts'], result['awaiting'], result['failed']), (0, 0, 1))
        self.assertIn('not a member', proxy_manager.load_accounts(self.owner)[0]['channel_setup']['reason'])

    async def test_a_fresh_login_identity_wins_over_a_stale_recorded_one(self):
        proxy_manager.save_accounts(self.owner, [dict(self.accounts[0], user_id='19999')])
        await monitor.provision(self.owner)
        self.assertEqual(proxy_manager.load_accounts(self.owner)[0]['user_id'], '10000')

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

    async def test_full_snapshot_guard_preserves_concurrent_proxy_edits(self):
        async def identity(owner, account):
            changed = proxy_manager.load_accounts(owner)
            changed[0]['proxy_id'] = 'new-proxy'
            proxy_manager.save_accounts(owner, changed)
            return str(10000 + int(account['name'][3:]))
        with patch.object(monitor, '_identify_account', identity):
            with self.assertRaisesRegex(ValueError, 'Accounts changed'): await monitor.provision(self.owner)
        saved = proxy_manager.load_accounts(self.owner)
        self.assertEqual(saved[0]['proxy_id'], 'new-proxy')
        self.assertTrue(all(not a['channels'] for a in saved))
        self.assertEqual(FakeAPI.channels, [])

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

    async def test_cancellation_is_not_mislabeled_as_account_error(self):
        with patch.object(monitor, '_identify_account', AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError): await monitor.provision(self.owner)
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
