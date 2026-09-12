"""Slash commands and destructive channel rebuilds using synthetic Discord APIs."""
import asyncio
import copy
import sys
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from support import extract
from core import monitor, spaces
from utils import proxy_manager
from test_monitor import FakeAPI


class CommandAPI(FakeAPI):
    channels = []
    calls = []
    next_id = 90000

    async def request(self, method, path, payload=None):
        if path == '/oauth2/applications/@me':
            self.calls.append((method, path, copy.deepcopy(payload)))
            return {'id': '99999'}
        if path == '/guilds/22222/channels' and method == 'POST':
            self.calls.append((method, path, copy.deepcopy(payload)))
            CommandAPI.next_id += 1
            channel = dict(payload, id=str(CommandAPI.next_id), guild_id='22222')
            self.channels.append(channel)
            return channel
        if path.startswith('/channels/') and method in ('GET', 'DELETE'):
            self.calls.append((method, path, copy.deepcopy(payload)))
            channel = next((c for c in self.channels if c['id'] == path.split('/')[2]), None)
            if channel is None:
                raise monitor.DiscordError(404)
            if method == 'DELETE':
                self.channels.remove(channel)
            return copy.deepcopy(channel)
        if path.endswith('/messages') and method == 'POST':
            self.calls.append((method, path, copy.deepcopy(payload)))
            return {'id': '99998'}
        return await super().request(method, path, payload)


class MonitorCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = 'u_acacac'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.supervisor = SimpleNamespace(running_names=lambda _: [])
        self.stack.enter_context(patch.dict(sys.modules, {'core.supervisor': self.supervisor}))
        self.stack.enter_context(patch.object(monitor, 'BotAPI', CommandAPI))
        self.stack.enter_context(patch.object(monitor, 'configured_owner_ids', return_value=['77777']))
        self.stack.enter_context(patch.object(monitor, 'restart', AsyncMock()))
        CommandAPI.calls = []
        CommandAPI.channels = []
        self.accounts = [{'name': f'acc{i}', 'token': f'synthetic-{i}', 'channels': []} for i in range(4)]
        proxy_manager.save_accounts(self.owner, self.accounts)
        monitor.save_config(self.owner, {'token': 'synthetic', 'guild_id': '22222', 'channel_id': None})
        monitor._operations[self.owner] = {'results': []}
        self.service = monitor.Monitor(self.owner)

    def interaction(self, name, actor='77777', confirm=True):
        return {'id': '123456', 'type': 2, 'token': 'synthetic', 'guild_id': '22222',
                'channel_id': '55555', 'member': {'user': {'id': actor}},
                'data': {'name': name, 'options': [{'name': 'confirm', 'value': confirm}]}}

    async def test_commands_are_upserted_without_bulk_replacing_other_commands(self):
        await self.service.register_commands(CommandAPI('synthetic'))
        writes = [(method, path, data) for method, path, data in CommandAPI.calls if method != 'GET']
        self.assertEqual([row[2]['name'] for row in writes], ['panel', 'setup_channels', 'recreate_channels'])
        self.assertTrue(all(row[0] == 'POST' and row[1] == '/applications/99999/guilds/22222/commands' for row in writes))

    async def test_rebuild_only_starts_after_successful_acknowledgement(self):
        original = CommandAPI.request
        rebuilding = AsyncMock(return_value={'accounts': 4})
        async def request(api, method, path, payload=None):
            if path.endswith('/callback'):
                rebuilding.assert_not_awaited()
                self.assertFalse(monitor._tasks[self.owner].done())
            return await original(api, method, path, payload)
        with patch.object(monitor, 'recreate_channels', rebuilding), patch.object(CommandAPI, 'request', request):
            await self.service.interact(None, self.interaction('recreate_channels'))
            await monitor._tasks[self.owner]
        rebuilding.assert_awaited_once_with(self.owner)

    async def test_failed_ack_cancels_job_before_deleting_anything(self):
        with patch.object(CommandAPI, 'request', AsyncMock(side_effect=monitor.DiscordError(403))), \
                patch.object(monitor, 'recreate_channels', AsyncMock()) as rebuild:
            with self.assertRaises(monitor.DiscordError):
                await self.service.interact(None, self.interaction('recreate_channels'))
            await asyncio.gather(monitor._tasks[self.owner], return_exceptions=True)
        rebuild.assert_not_awaited()
        self.assertEqual(monitor._operations[self.owner]['status'], 'cancelled')

    async def test_unconfigured_owner_wrong_guild_and_unconfirmed_delete_are_rejected(self):
        cases = [self.interaction('recreate_channels', actor='67890'),
                 self.interaction('recreate_channels', confirm=False),
                 dict(self.interaction('setup_channels'), guild_id='44444')]
        with patch.object(monitor, 'start_operation') as start:
            for i, data in enumerate(cases):
                data['id'] = str(i)
                await self.service.interact(None, data)
            start.assert_not_called()

    async def test_running_accounts_block_setup_and_rebuild(self):
        self.supervisor.running_names = lambda _: ['acc0']
        with patch.object(monitor, 'start_operation') as start:
            for i, name in enumerate(('setup_channels', 'recreate_channels')):
                data = self.interaction(name)
                data['id'] = str(i)
                await self.service.interact(None, data)
            start.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Stop the accounts'):
            await monitor.recreate_channels(self.owner)
        self.assertFalse(any(call[0] == 'DELETE' for call in CommandAPI.calls))

    async def test_rebuild_creates_new_ids_and_preserves_unmanaged_and_other_space_channels(self):
        await monitor.provision(self.owner)
        old_ids = {c['id'] for c in CommandAPI.channels}
        CommandAPI.channels.extend([
            {'id': '55555', 'name': 'general', 'type': 0, 'guild_id': '22222'},
            {'id': '55556', 'name': 'farm-other', 'type': 0, 'topic': 'LazyFarmers:u_bbbbbb:group:1'},
        ])
        result = await monitor.recreate_channels(self.owner)
        deleted = {path.split('/')[-1] for method, path, _ in CommandAPI.calls if method == 'DELETE'}
        self.assertEqual(deleted, old_ids)
        self.assertEqual(result['accounts'], 4)
        self.assertEqual(result['deleted_channels'], 3)
        for before, after in zip(self.accounts, proxy_manager.load_accounts(self.owner)):
            self.assertEqual(before['token'], after['token'])
            self.assertEqual(after['channel_setup']['status'], 'assigned')
            self.assertTrue(set(after['channels']).isdisjoint(old_ids))
        self.assertTrue({'55555', '55556'}.issubset({c['id'] for c in CommandAPI.channels}))

    async def test_category_with_unmanaged_child_is_preserved(self):
        await monitor.provision(self.owner)
        category = next(c for c in CommandAPI.channels if c['type'] == 4)
        CommandAPI.channels.append({'id': '55555', 'name': 'keep-me', 'type': 0, 'parent_id': category['id']})
        await monitor.recreate_channels(self.owner)
        self.assertNotIn(('DELETE', f"/channels/{category['id']}", None), CommandAPI.calls)

    async def test_partial_deletion_failure_clears_only_deleted_account_assignments(self):
        await monitor.provision(self.owner)
        groups = [c for c in CommandAPI.channels if ':group:' in c.get('topic', '')]
        original = CommandAPI.request
        async def request(api, method, path, payload=None):
            if method == 'DELETE' and path.endswith(groups[1]['id']):
                raise monitor.DiscordError(403)
            return await original(api, method, path, payload)
        with patch.object(CommandAPI, 'request', request):
            with self.assertRaises(monitor.DiscordError):
                await monitor.recreate_channels(self.owner)
        saved = proxy_manager.load_accounts(self.owner)
        self.assertTrue(all(a['channels'] == [] for a in saved[:3]))
        self.assertEqual(saved[3]['channels'], [groups[1]['id']])

    async def test_panel_posts_in_current_channel_and_becomes_the_active_panel(self):
        CommandAPI.channels.append({'id': '55555', 'name': 'general', 'type': 0, 'guild_id': '22222'})
        with patch.object(monitor, 'snapshot', return_value={}), patch.object(monitor, 'build_message', return_value={'embeds': []}):
            await self.service.interact(None, self.interaction('panel'))
            await monitor._tasks[self.owner]
        cfg = monitor.load_config(self.owner)
        self.assertEqual((cfg['channel_id'], cfg['message_id']), ('55555', '99998'))
        self.assertTrue(any(path == '/channels/55555/messages' for _, path, _ in CommandAPI.calls))

    async def test_account_start_is_blocked_during_rebuild(self):
        method = extract('core/supervisor.py', ['start_account'], {'spaces': spaces})['start_account']
        monitor._operations[self.owner] = {'status': 'running', 'kind': 'recreate'}
        success, reason = await method(self.accounts[0], self.owner)
        self.assertFalse(success)
        self.assertIn('channel assignment', reason)

    def test_start_all_cannot_queue_stale_channel_ids_during_rebuild(self):
        method = extract('core/supervisor.py', ['start_sequence'], {'spaces': spaces})['start_sequence']
        monitor._operations[self.owner] = {'status': 'running', 'kind': 'recreate'}
        success, reason = method(self.owner, self.accounts)
        self.assertFalse(success)
        self.assertIn('channel assignment', reason)

    async def test_pending_start_queue_blocks_rebuild_before_first_account_connects(self):
        self.supervisor._sequences = {self.owner: {'task': SimpleNamespace(done=lambda: False)}}
        with self.assertRaisesRegex(ValueError, 'Stop the accounts'):
            await monitor.recreate_channels(self.owner)
        self.assertFalse(any(call[0] == 'DELETE' for call in CommandAPI.calls))
