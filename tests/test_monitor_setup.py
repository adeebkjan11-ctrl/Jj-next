"""Offline regressions for monitor authentication and configuration writes."""
import os
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from support import extract
from core import monitor, spaces


class Response:
    status = 200
    headers = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return {'id': '99999', 'username': 'Monitor', 'bot': True}


class MonitorSetupTests(unittest.IsolatedAsyncioTestCase):
    def test_normalize_copied_tokens_and_reject_invalid_input(self):
        for value in ['synthetic.token', ' "synthetic.token" ', 'Bot synthetic.token',
                      '`Bot synthetic.token`', 'Bot "synthetic.token"', '\nsynthetic.token\n']:
            self.assertEqual(monitor.normalize_token(value), 'synthetic.token')
        for value in [None, 123, '', 'Bot ', 'token\nfragment']:
            with self.assertRaises(ValueError): monitor.normalize_token(value)

    async def test_request_sends_exactly_one_bot_prefix(self):
        response = Response()
        from unittest.mock import Mock
        session = SimpleNamespace(request=Mock(return_value=response))
        api = monitor.BotAPI('"Bot synthetic.token"')
        api.session = session
        await api.request('GET', '/users/@me')
        self.assertEqual(session.request.call_args.kwargs['headers']['Authorization'], 'Bot synthetic.token')

    async def test_discord_401_explains_bot_credentials_without_exposing_response(self):
        response = Response()
        response.status = 401
        response.json = AsyncMock(return_value={'message': 'synthetic-private-response'})
        api = monitor.BotAPI('synthetic.token')
        api.session = SimpleNamespace(request=lambda *a, **k: response)
        with self.assertRaises(monitor.DiscordError) as caught:
            await api.request('GET', '/users/@me')
        self.assertEqual(caught.exception.status, 401)
        self.assertIn('monitor bot token', str(caught.exception))
        self.assertNotIn('synthetic', str(caught.exception))

    async def test_invalid_replacement_preserves_saved_config_and_running_bot(self):
        owner = 'u_ccaaaa'
        monitor.save_config(owner, {'token': 'old-token', 'withdraw_percent': 75})
        with patch.object(monitor.BotAPI, 'request', AsyncMock(side_effect=monitor.DiscordError(401))), \
                patch.object(monitor, 'restart', AsyncMock()) as restart:
            with self.assertRaises(monitor.DiscordError):
                await monitor.configure(owner, {'token': 'new-token', 'withdraw_percent': 25})
            restart.assert_not_called()
        self.assertEqual(monitor.load_config(owner)['token'], 'old-token')
        self.assertEqual(monitor.load_config(owner)['withdraw_percent'], 75)

    async def test_valid_replacement_overrides_environment_and_returns_no_token(self):
        with patch.dict(os.environ, {'LAZYFARMERS_MONITOR_TOKEN': 'old-env-token'}), \
                patch.object(monitor, 'restart', AsyncMock()), \
                patch.object(monitor.BotAPI, 'request', AsyncMock(return_value={'id': '99999', 'username': 'Monitor', 'bot': True})):
            public = await monitor.configure(spaces.ADMIN_SPACE, {'token': ' "Bot new-token" '})
            self.assertEqual(monitor.load_config(spaces.ADMIN_SPACE)['token'], 'new-token')
            self.assertTrue(public['token_set'])
            self.assertNotIn('token', public)
            self.assertEqual(public['bot_name'], 'Monitor')

    async def test_nonbot_identity_is_not_saved(self):
        with patch.object(monitor.BotAPI, 'request', AsyncMock(return_value={'id': '99999', 'bot': False})), \
                patch.object(monitor, 'save_config') as save:
            with self.assertRaisesRegex(ValueError, 'official Discord bot'):
                await monitor.configure('u_ccbbbb', {'token': 'synthetic'})
            save.assert_not_called()

    async def test_stop_still_works_when_saved_token_is_invalid(self):
        owner = 'u_cccccc'
        monitor.save_config(owner, {'token': 'invalid-old-token', 'enabled': True})
        with patch.object(monitor.BotAPI, 'request', AsyncMock()) as request, patch.object(monitor, 'restart', AsyncMock()):
            result = await monitor.configure(owner, {'enabled': False})
            request.assert_not_called()
            self.assertFalse(result['enabled'])

    def test_route_does_not_turn_discord_401_into_dashboard_logout(self):
        class Configure:
            def close(self): pass
        fake = SimpleNamespace(_operations={}, configure=lambda *args: Configure())
        env = {'request': SimpleNamespace(method='POST'), 'g': SimpleNamespace(owner='admin'),
               '_payload': lambda: {'token': 'synthetic'}, 'jsonify': lambda data: data,
               '_bot_loop_call': lambda coro: (None, str(monitor.DiscordError(401)))}
        route = extract('dashboard/app.py', ['monitor_config_api'], env)['monitor_config_api']
        with patch('core.monitor', fake):
            response, status = route()
        self.assertEqual(status, 400)
        self.assertFalse(response['success'])
        self.assertIn('HTTP 401', response['error'])
