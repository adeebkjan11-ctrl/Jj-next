"""Website owner authorization and complete fleet withdrawal dispatch, offline."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import support  # Isolate runtime data before importing core modules.
from core import monitor, spaces, state
from utils import proxy_manager
from test_monitor import FakeAPI


class WebsiteOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = 'u_bcdefa'
        self.settings = Path(spaces.settings_path(self.owner))
        self.set_owner('77777')
        monitor.save_config(self.owner, {'token': 'synthetic', 'owner_id': '67890',
            'recipient_id': '67890', 'guild_id': '22222', 'channel_id': '33333', 'message_id': '44444'})

    def set_owner(self, uid):
        self.settings.write_text(json.dumps({'owner': {'user_id': uid}}))

    async def test_configured_owner_allowed_and_server_owner_denied(self):
        service = monitor.Monitor(self.owner)
        data = {'id': '55555', 'token': 'synthetic', 'guild_id': '22222', 'channel_id': '33333',
                'message': {'id': '44444'}, 'member': {'user': {'id': '67890'}},
                'data': {'custom_id': 'lf:withdraw'}}
        with patch.object(monitor, 'BotAPI', FakeAPI), patch.object(monitor, 'start_operation', return_value={'success': True}) as start:
            await service.interact(None, data)
            start.assert_not_called()
            data['id'] = '55556'
            data['member']['user']['id'] = '77777'
            await service.interact(None, data)
            start.assert_called_once_with(self.owner, 'withdraw')
            start.reset_mock()
            self.set_owner('88888')
            data['id'] = '55557'
            await service.interact(None, data)
            start.assert_not_called()

    async def test_guild_selection_does_not_change_website_recipient(self):
        with patch.object(monitor, 'BotAPI', FakeAPI), patch.object(monitor, 'restart', AsyncMock()):
            result = await monitor.select_guild(self.owner, '22222')
        self.assertEqual(result['recipient_id'], '77777')
        self.set_owner('')
        self.assertIsNone(monitor.public_config(self.owner)['recipient_id'])
        with self.assertRaisesRegex(ValueError, 'owner.user_id'):
            await monitor.withdraw_all(self.owner)

    async def test_wrong_message_channel_and_guild_do_not_authorize(self):
        service = monitor.Monitor(self.owner)
        with patch.object(monitor, 'BotAPI', FakeAPI), patch.object(monitor, 'start_operation') as start:
            for index, field in enumerate(('guild_id', 'channel_id', 'message')):
                data = {'id': str(60000 + index), 'token': 'synthetic', 'guild_id': '22222',
                    'channel_id': '33333', 'message': {'id': '44444'}, 'user': {'id': '77777'},
                    'data': {'custom_id': 'lf:withdraw'}}
                data[field] = {'id': '99999'} if field == 'message' else '99999'
                await service.interact(None, data)
            start.assert_not_called()

    async def test_every_saved_account_gets_result_despite_failed_account(self):
        accounts = [{'name': f'acc{i}'} for i in range(5)]
        proxy_manager.save_accounts(self.owner, accounts)
        first = SimpleNamespace(withdraw=AsyncMock(side_effect=RuntimeError('secret must not appear')))
        second = SimpleNamespace(withdraw=AsyncMock(return_value={'status': 'confirmed', 'confirmed_total': 12000}))
        third = SimpleNamespace(withdraw=AsyncMock(return_value={'status': 'confirmed', 'confirmed_total': 7000}))
        bots = [SimpleNamespace(account_name='acc0', get_cog=lambda _: first),
                SimpleNamespace(account_name='acc1', get_cog=lambda _: second),
                SimpleNamespace(account_name='acc2', get_cog=lambda _: None),
                SimpleNamespace(account_name='acc4', get_cog=lambda _: third)]
        monitor._operations[self.owner] = {'results': []}
        with patch.object(state, 'bots_for', return_value=bots):
            result = await monitor.withdraw_all(self.owner)
        self.assertEqual(result, {'attempted': 5, 'confirmed': 19000})
        for cog in (first, second, third):
            cog.withdraw.assert_awaited_once_with('77777', percent=100)
        progress = monitor._operations[self.owner]
        self.assertEqual(progress['done'], 5)
        self.assertEqual([r['status'] for r in progress['results']], ['failed', 'confirmed', 'skipped', 'skipped', 'confirmed'])
        self.assertNotIn('secret', str(progress))

    async def test_recipient_limit_reports_remaining_accounts_without_sending(self):
        proxy_manager.save_accounts(self.owner, [{'name': 'acc0'}, {'name': 'acc1'}])
        first = SimpleNamespace(withdraw=AsyncMock(return_value={'status': 'recipient_limited'}))
        second = SimpleNamespace(withdraw=AsyncMock())
        bots = [SimpleNamespace(account_name='acc0', get_cog=lambda _: first),
                SimpleNamespace(account_name='acc1', get_cog=lambda _: second)]
        monitor._operations[self.owner] = {'results': []}
        with patch.object(state, 'bots_for', return_value=bots):
            await monitor.withdraw_all(self.owner)
        second.withdraw.assert_not_called()
        self.assertEqual(monitor._operations[self.owner]['done'], 2)
        self.assertEqual(monitor._operations[self.owner]['results'][1]['reason'], 'Recipient receive limit reached')
