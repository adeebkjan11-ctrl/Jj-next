import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from support import extract
from core import spaces, state
from dashboard import users
from core.captcha_jobs import CaptchaJobs


class MigrationTests(unittest.TestCase):
    def test_recovers_sources_even_after_old_migration_marker(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / 'config'; cfg.mkdir()
            store = Path(root) / 'users'; store.mkdir()
            (store / '.legacy_migrated').write_text('')
            (cfg / 'accounts.json').write_text(json.dumps({'accounts': [{'name': 'old'}]}))
            (cfg / 'proxies.json').write_text(json.dumps({'proxies': [{'id': 'old'}]}))
            with patch.multiple(spaces, CONFIG_DIR=str(cfg), USERS_DIR=str(store),
                                _MIGRATION_MARKER=str(store / '.legacy_migrated_v2')):
                spaces.ensure_space('admin')
                self.assertTrue(spaces.migrate_legacy())
                self.assertEqual(json.loads(Path(spaces.accounts_path('admin')).read_text())['accounts'][0]['name'], 'old')
                self.assertEqual(json.loads(Path(spaces.proxies_path('admin')).read_text())['proxies'][0]['id'], 'old')
                self.assertFalse(spaces.migrate_legacy())
                self.assertTrue((cfg / 'accounts.json.migrated').exists())

    def test_never_overwrites_populated_destination(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = Path(root) / 'config'; cfg.mkdir()
            store = Path(root) / 'users'; store.mkdir()
            (cfg / 'accounts.json').write_text('{"accounts":[{"name":"legacy"}]}')
            with patch.multiple(spaces, CONFIG_DIR=str(cfg), USERS_DIR=str(store), _MIGRATION_MARKER=str(store / '.done')):
                spaces.ensure_space('admin')
                destination = Path(spaces.accounts_path('admin'))
                destination.write_text('{"accounts":[{"name":"current"}]}')
                spaces.migrate_legacy()
                self.assertEqual(json.loads(destination.read_text())['accounts'][0]['name'], 'current')


class AuthTests(unittest.TestCase):
    def test_password_reset_revokes_cookie_and_old_password(self):
        with tempfile.TemporaryDirectory() as root, patch.object(users, 'STORE_FILE', str(Path(root) / 'users.json')):
            keys, _ = users.generate_keys(7)
            user, error = users.redeem_key(keys[0]['key'], 'test@example.invalid', 'before-test')
            self.assertIsNone(error)
            cookie = {'logged_in': True, 'user_id': user['id'], 'is_admin': False, 'user_version': 0}
            auth = extract('dashboard/app.py', {'_current_user', '_session_valid'}, {'session': cookie, 'dash_users': users})
            self.assertTrue(auth['_session_valid']())
            users.set_password(user['id'], 'after-test')
            self.assertFalse(auth['_session_valid']())
            self.assertIsNone(users.authenticate('test@example.invalid', 'before-test')[0])
            new_user, _ = users.authenticate('test@example.invalid', 'after-test')
            cookie['user_version'] = new_user['session_version']
            self.assertTrue(auth['_session_valid']())


class BotRegressionTests(unittest.IsolatedAsyncioTestCase):
    def bot(self):
        cls = extract('core/bot.py', {'_load_config', '_deep_merge', '_apply_earning_overlay',
            '_collect_changed_paths', '_cogs_for_config_changes', '_prune_disabled_scheduler_cmds',
            'sync_settings', '_send_safe', 'neura_queue_worker'}, {'spaces': spaces, 'state': state}, 'NeuraBot')
        bot = cls()
        bot.config_file = str(Path(state.CONFIG_DIR) / 'settings.json')
        bot.config = {}; bot.space_owner = 'u_aabbcc'; bot.user_id = '123456789'
        bot.token = 'synthetic'; bot.channels = ['12345']; bot.channel_id = 12345
        bot.cogs = {}; bot.cmd_states = {}; bot.log = lambda *a: None
        return bot

    async def test_default_save_and_restart_have_identical_behavior(self):
        bot = self.bot()
        for path in spaces.settings_files(bot.space_owner):
            Path(path).unlink(missing_ok=True)
        bot._load_config()
        self.assertEqual(json.loads(Path(spaces.settings_path(bot.space_owner, bot.user_id)).read_text()), {})
        update = copy.deepcopy(bot.config)
        update['commands']['hunt']['enabled'] = False
        Path(spaces.settings_path(bot.space_owner)).write_text(json.dumps(update))
        await bot.sync_settings(update)
        self.assertFalse(bot.config['commands']['hunt']['enabled'])
        bot._load_config()
        self.assertFalse(bot.config['commands']['hunt']['enabled'])
        # Explicit account overrides remain in force both live and on reload.
        Path(spaces.settings_path(bot.space_owner, bot.user_id)).write_text('{"commands":{"hunt":{"enabled":true}}}')
        await bot.sync_settings(update)
        self.assertTrue(bot.config['commands']['hunt']['enabled'])
        bot._load_config()
        self.assertTrue(bot.config['commands']['hunt']['enabled'])

    async def test_unsent_command_does_not_advance_daily(self):
        bot = self.bot(); bot.active = True; bot.paused = False; bot.is_ready = False
        bot.neura_queue = asyncio.PriorityQueue(); bot.wait_until_ready = AsyncMock()
        bot.cmd_states = {'daily': {'last_ran': 0, 'delay': 10, 'in_queue': True}}
        hook = SimpleNamespace(trigger_action=unittest.mock.Mock())
        bot.get_cog = lambda _: hook
        await bot.neura_queue.put((4, time.time(), 'daily', {'_cmd_id': 'daily'}))
        task = asyncio.create_task(bot.neura_queue_worker())
        await asyncio.wait_for(bot.neura_queue.join(), 3)
        self.assertEqual(bot.cmd_states['daily']['last_ran'], 0)
        self.assertEqual(bot.cmd_states['daily']['delay'], 10)
        hook.trigger_action.assert_not_called()
        task.cancel(); await asyncio.gather(task, return_exceptions=True)

    async def test_permanent_exit_closes_all_workers_and_sessions(self):
        worker = asyncio.create_task(asyncio.Event().wait())
        bot = SimpleNamespace(space_owner='admin', user_id='12345', user=None,
            worker_tasks=[worker], active=True, paused=False, is_ready=True,
            run_bot=AsyncMock(), close=AsyncMock(), session=SimpleNamespace(close=AsyncMock()),
            interactions=SimpleNamespace(close=AsyncMock()))
        state.bot_instances.append(bot)
        methods = extract('core/supervisor.py', {'_run', '_release_captcha_hold'}, {'state': state})
        await methods['_run'](bot)
        self.assertTrue(worker.cancelled())
        self.assertFalse(bot.active)
        self.assertNotIn(bot, state.bot_instances)
        bot.close.assert_awaited_once(); bot.session.close.assert_awaited_once(); bot.interactions.close.assert_awaited_once()

    def test_queued_captcha_is_not_removed_just_because_it_is_old(self):
        bot = SimpleNamespace(paused=True, throttle_until=float('inf'))
        fn = extract('dashboard/app.py', {'_captcha_still_real'}, {'_live_bot_for_account': lambda _: bot,
                     'CAPTCHA_LIVENESS_GRACE_S': 30})['_captcha_still_real']
        self.assertTrue(fn('12345', {'created_at': time.time() - 3600})[0])
        bot.paused = False; bot.throttle_until = 0
        self.assertFalse(fn('12345', {'created_at': time.time() - 3600})[0])


class CaptchaJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_jobs_get_retried_with_bounded_concurrency(self):
        jobs = CaptchaJobs(concurrency=2, attempts=3, retry_delay=0)
        active = 0; maximum = 0
        bots = [SimpleNamespace(space_owner='admin', user_id=str(i), active=True, stats={}) for i in range(12)]
        calls = [0] * len(bots)
        async def solve(i):
            nonlocal active, maximum
            active += 1; maximum = max(maximum, active)
            calls[i] += 1
            await asyncio.sleep(.001)
            active -= 1
            return calls[i] == 2
        result = await asyncio.gather(*(jobs.run(bot, lambda i=i: solve(i)) for i, bot in enumerate(bots)))
        self.assertTrue(all(result)); self.assertEqual(calls, [2] * 12); self.assertLessEqual(maximum, 2)
        self.assertTrue(all(b.stats['captcha_job']['status'] == 'solved' for b in bots))

    async def test_duplicate_requests_share_one_job(self):
        jobs = CaptchaJobs(retry_delay=0)
        bot = SimpleNamespace(space_owner='admin', user_id='12345', active=True, stats={})
        gate = asyncio.Event()
        solve = AsyncMock(side_effect=lambda: None)
        async def work():
            await solve(); await gate.wait(); return True
        tasks = [asyncio.create_task(jobs.run(bot, work)) for _ in range(8)]
        await asyncio.sleep(.01); gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(solve.call_count, 1)

    async def test_exhausted_job_stays_visible_as_manual_required(self):
        jobs = CaptchaJobs(attempts=2, retry_delay=0)
        bot = SimpleNamespace(space_owner='admin', user_id='12345', active=True, stats={})
        self.assertFalse(await jobs.run(bot, AsyncMock(return_value=False)))
        self.assertEqual(bot.stats['captcha_job']['status'], 'manual_required')
        self.assertEqual(bot.stats['captcha_job']['attempt'], 2)



class V2CaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_dm_with_nested_link_is_queued_without_cached_channel(self):
        import re
        import sys
        dummy_discord = SimpleNamespace(DMChannel=type('DMChannel', (), {}))
        cls = extract('cogs/security.py', {'on_owo_gateway_message'},
                      {'re': re, 'discord': dummy_discord}, 'Security')
        cog = cls(); cog.enabled = True; cog.monitor_id = '408785106942164992'
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=12345), user_id='12345', username='test',
            get_channel=lambda _: None, channels=[], config={}, paused=False,
            identity=SimpleNamespace(text_is_mine=lambda _: False))
        cog._run_autosolve = AsyncMock(return_value=False)
        register = unittest.mock.Mock()
        with patch.dict(sys.modules, {'dashboard.app': SimpleNamespace(register_captcha_challenge=register)}):
            await cog.on_owo_gateway_message({'t': 'MESSAGE_CREATE', 'd': {
                'id': '90000', 'channel_id': '88888', 'author': {'id': cog.monitor_id},
                'components': [{'type': 17, 'components': [{'type': 1, 'components': [
                    {'type': 2, 'style': 5, 'url': 'https://owobot.com/captcha/test', 'label': 'Verify'}]}]}]}})
        register.assert_called_once()
        cog._run_autosolve.assert_awaited_once()
        self.assertTrue(cog.bot.paused)
        self.assertFalse(cog.bot._solving_captcha)
