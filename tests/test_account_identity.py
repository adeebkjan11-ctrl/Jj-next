"""Setup identity must use the dashboard login stack, not bare HTTP probes."""
import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from support import extract
from core import monitor, state
from utils import proxy_manager


class LoginFailure(Exception): pass
class HTTPException(Exception):
    def __init__(self, status): self.status = status


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(login=AsyncMock(), close=AsyncMock(),
            user=SimpleNamespace(id=12345, bot=False))
        self.factory = Mock(return_value=self.client)
        self.discord = SimpleNamespace(Client=self.factory, LoginFailure=LoginFailure, HTTPException=HTTPException)
        self.account = {'name': 'working-account', 'token': '  synthetic-token  ', 'proxy_id': 'proxy-1', 'user_id': 'stale-id'}
        self.patches = [patch.dict(sys.modules, {'discord': self.discord}),
            patch.object(state, 'login_slot', AsyncMock()),
            patch.object(proxy_manager, 'resolve_account_proxy', return_value=('http://proxy.invalid:8080', 'synthetic-auth', 'proxy-1'))]
        for p in self.patches: p.start(); self.addCleanup(p.stop)

    async def test_uses_dashboard_client_proxy_and_fresh_login_identity(self):
        with patch.object(monitor.aiohttp, 'ClientSession', side_effect=AssertionError('Bare HTTP must not be used')):
            result = await monitor._identify_account('admin', self.account)
        self.assertEqual(result, '12345')
        self.factory.assert_called_once_with(proxy='http://proxy.invalid:8080', proxy_auth='synthetic-auth')
        self.client.login.assert_awaited_once_with('synthetic-token')
        state.login_slot.assert_awaited_once()
        self.client.close.assert_awaited_once()
        self.assertFalse(hasattr(self.client, 'start'))  # No gateway or farming workers.

    async def test_login_failure_is_sanitized_and_closes_client(self):
        self.client.login.side_effect = LoginFailure('private-token-or-proxy')
        with self.assertRaises(monitor.AccountSetupError) as caught:
            await monitor._identify_account('admin', self.account)
        self.assertNotIn('private', str(caught.exception))
        self.assertNotIn('Replace its token', str(caught.exception))
        self.assertIn('login check was rejected', str(caught.exception))
        self.client.close.assert_awaited_once()

    async def test_http_error_is_not_reported_as_proof_of_invalid_token(self):
        self.client.login.side_effect = HTTPException(401)
        with self.assertRaisesRegex(monitor.AccountSetupError, 'does not establish'):
            await monitor._identify_account('admin', self.account)
        self.client.close.assert_awaited_once()

    async def test_timeout_closes_client(self):
        self.client.login.side_effect = asyncio.TimeoutError()
        with self.assertRaises(asyncio.TimeoutError):
            await monitor._identify_account('admin', self.account)
        self.client.close.assert_awaited_once()

    async def test_cancellation_closes_client_and_propagates(self):
        self.client.login.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await monitor._identify_account('admin', self.account)
        self.client.close.assert_awaited_once()

    async def test_does_not_trust_cached_identity_when_login_returns_none(self):
        self.client.user = None
        with self.assertRaisesRegex(monitor.AccountSetupError, 'no account identity'):
            await monitor._identify_account('admin', self.account)
        self.client.close.assert_awaited_once()

    async def test_bot_identity_is_rejected_and_closed(self):
        self.client.user.bot = True
        with self.assertRaisesRegex(monitor.AccountSetupError, 'belongs to a bot'):
            await monitor._identify_account('admin', self.account)
        self.client.close.assert_awaited_once()

    async def test_missing_token_does_not_attempt_login(self):
        self.account['token'] = ''
        with self.assertRaisesRegex(monitor.AccountSetupError, 'no token saved'):
            await monitor._identify_account('admin', self.account)
        self.factory.assert_not_called()

    async def test_cleanup_error_does_not_discard_successful_identity(self):
        self.client.close.side_effect = RuntimeError('close failure')
        self.assertEqual(await monitor._identify_account('admin', self.account), '12345')
