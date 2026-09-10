import asyncio
import copy
import json
import sqlite3
import sys
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from support import extract
from core import monitor, spaces, state
from utils import proxy_manager, history_tracker


class FakeAPI:
    channels = []
    calls = []
    def __init__(self, token): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == '/users/@me': return {'id': '99999', 'username': 'monitor', 'bot': True}
        if path == '/guilds/22222': return {'id': '22222', 'name': 'Test server', 'owner_id': '67890'}
        if '/members/' in path: return {'user': {'id': path.split('/')[-1]}}
        if path == '/guilds/22222/channels' and method == 'GET': return copy.deepcopy(self.channels)
        if path == '/guilds/22222/channels' and method == 'POST':
            data = dict(payload, id=str(80000 + len(self.channels)))
            self.channels.append(data); return data
        if path.startswith('/channels/') and method == 'PATCH':
            ident = path.split('/')[2]
            row = next(c for c in self.channels if c['id'] == ident)
            row.update(payload); return row
        return {}


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_seven_accounts_get_three_private_channels_and_rerun_reuses_them(self):
        owner = 'u_abcdef'
        accounts = [{'name': f'acc{i}', 'token': f'synthetic-{i}', 'channels': []} for i in range(7)]
        proxy_manager.save_accounts(owner, accounts)
        monitor.save_config(owner, {'guild_id': '22222', 'token': 'synthetic'})
        FakeAPI.channels = []; FakeAPI.calls = []
        fake_supervisor = SimpleNamespace(running_names=lambda owner: [])
        async def identity(owner, account): return str(10000 + int(account['name'][3:]))
        monitor._operations[owner] = {}
        with patch.dict(sys.modules, {'core.supervisor': fake_supervisor}), patch.object(monitor, 'BotAPI', FakeAPI), \
                patch.object(monitor, '_identify_account', identity), patch.object(monitor, 'restart', AsyncMock()):
            await monitor.provision(owner)
            creation_count = len(FakeAPI.channels)
            await monitor.provision(owner)
        self.assertEqual(len(FakeAPI.channels), creation_count)
        saved = proxy_manager.load_accounts(owner)
        assignments = [a['channels'][0] for a in saved]
        self.assertEqual(sorted(assignments.count(c) for c in set(assignments)), [1, 3, 3])
        groups = [c for c in FakeAPI.channels if ':group:' in c.get('topic', '')]
        self.assertEqual(len(groups), 3)
        for c in groups:
            overwrites = c['permission_overwrites']
            self.assertTrue(any(p['id'] == '22222' and p['deny'] == '1024' for p in overwrites))
            self.assertTrue(any(p['id'] == '408785106942164992' for p in overwrites))
        self.assertNotIn('token', monitor.public_config(owner))
        self.assertEqual(monitor.load_config(owner)['recipient_id'], '67890')

    async def test_nonowner_button_cannot_start_withdrawal(self):
        owner = 'u_ababab'
        monitor.save_config(owner, {'token': 'synthetic', 'owner_id': '67890', 'recipient_id': '67890',
            'guild_id': '22222', 'channel_id': '33333', 'message_id': '44444'})
        service = monitor.Monitor(owner)
        data = {'id': '55555', 'token': 'synthetic-interaction', 'guild_id': '22222',
                'channel_id': '33333', 'message': {'id': '44444'}, 'member': {'user': {'id': '77777'}},
                'data': {'custom_id': 'lf:withdraw'}}
        with patch.object(monitor, 'BotAPI', FakeAPI), patch.object(monitor, 'start_operation') as start:
            await service.interact(FakeAPI('test'), data)
            start.assert_not_called()
            data['id'] = '55556'; data['member']['user']['id'] = '67890'
            start.return_value = {'success': True}
            await service.interact(FakeAPI('test'), data)
            start.assert_called_once_with(owner, 'withdraw')
            await service.interact(FakeAPI('test'), data)
            self.assertEqual(start.call_count, 1)

    async def test_running_accounts_block_reassignment(self):
        fake_supervisor = SimpleNamespace(running_names=lambda owner: ['acc1'])
        with patch.dict(sys.modules, {'core.supervisor': fake_supervisor}):
            with self.assertRaisesRegex(ValueError, 'Stop the accounts'):
                await monitor.provision('admin')

    def test_embed_has_balances_and_real_newlines(self):
        sample = {key: 0 for key in ('captcha', 'missing', 'configured', 'connected', 'ready', 'cached',
            'low_cash', 'paused', 'failed', 'queued', 'total_owo', 'withdrawable', 'unknown_limits',
            'hour_net', 'day_net', 'sampled_accounts')}
        sample.update(total_owo=64000, withdrawable=64000, best={'name': 'acc1', 'net_24h': 123})
        body = monitor.build_message(sample)
        self.assertIn('\n', body['embeds'][0]['fields'][0]['value'])
        self.assertNotIn('\\n', body['embeds'][0]['fields'][0]['value'])
        self.assertEqual(body['allowed_mentions'], {'parse': []})
        self.assertIn('64,000', body['embeds'][0]['fields'][2]['value'])
        self.assertEqual(body['components'][0]['components'][0]['custom_id'], 'lf:withdraw')

    def test_snapshot_counts_all_accounts_and_compensates_confirmed_withdrawals(self):
        owner = 'u_abcdab'
        spaces.ensure_space(owner)
        proxy_manager.save_accounts(owner, [{'name': 'acc1', 'user_id': '12345'}, {'name': 'acc2', 'user_id': '22222'}])
        monitor.save_config(owner, {'recipient_id': '67890', 'withdraw_percent': 100})
        now = time.time()
        stats = {'level': 1, 'current_cash': 90000, 'last_cash_update': now, 'space_owner': owner,
                 'withdrawal_receipts': [{'at': now - 5, 'amount': 5000}]}
        bot = SimpleNamespace(account_name='acc1', is_ready=True, paused=False, stats=stats,
            space_owner=owner, user=SimpleNamespace(id='12345'), config={'owner': {}})
        def db(_):
            conn = sqlite3.connect(':memory:')
            conn.execute('CREATE TABLE cash_history(account_id TEXT, unix REAL, amount INTEGER)')
            conn.executemany('INSERT INTO cash_history VALUES(?,?,?)', [('12345', now - 1000, 80000), ('12345', now - 1, 90000)])
            return conn
        with patch.object(state, 'bots_for', return_value=[bot]), patch.object(history_tracker, 'get_db', db):
            result = monitor.snapshot(owner)
        self.assertEqual(result['configured'], 2); self.assertEqual(result['ready'], 1)
        self.assertEqual(result['total_owo'], 90000); self.assertEqual(result['withdrawable'], 64000)
        self.assertEqual(result['best'], {'name': 'acc1', 'net_24h': 15000})
        self.assertEqual(result['unknown_limits'], 1)

    def test_withdrawal_journal_survives_stats_reload(self):
        original = copy.deepcopy(state.account_stats)
        try:
            state.account_stats.clear()
            state.account_stats['12345'] = dict(state.get_empty_stats(), withdrawal={'status': 'unknown', 'amount': 64000},
                withdrawal_receipts=[{'at': 1, 'amount': 10}], space_owner='admin', last_cash_update=123)
            with tempfile.TemporaryDirectory() as root, patch.object(state, 'STATS_FILE', str(Path(root) / 'stats.json')):
                state._write_account_stats(); state.account_stats.clear(); state.load_account_stats()
                row = state.account_stats['12345']
                self.assertEqual(row['withdrawal']['status'], 'unknown')
                self.assertEqual(row['withdrawal_receipts'][0]['amount'], 10)
                self.assertEqual(row['last_cash_update'], 123)
        finally:
            state.account_stats.clear(); state.account_stats.update(original)
