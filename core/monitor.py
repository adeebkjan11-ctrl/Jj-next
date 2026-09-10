"""Official Discord monitor bot, per-space provisioning, and owner-only withdrawals.

Uses Discord's bot REST/Gateway APIs separately from discord.py-self; installing
both discord.py variants in one environment would overwrite the same package.
"""
import asyncio
import copy
import datetime
import json
import os
import random
import re
import threading
import time
from pathlib import Path

import aiohttp
from core import spaces
from core.withdrawal_limits import send_limit

_lock = threading.RLock()
_services = {}
_operations = {}
_tasks = {}
VIEW = 1024
SEND = 2048
READ = 65536
EMBED = 16384
ATTACH = 32768
ACCESS = VIEW | SEND | READ | EMBED | ATTACH


def load_config(owner):
    path = Path(spaces.space_dir(owner)) / 'monitor.json'
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError('Invalid monitor configuration')
    if owner == spaces.ADMIN_SPACE and os.environ.get('LAZYFARMERS_MONITOR_TOKEN'):
        cfg['token'] = os.environ['LAZYFARMERS_MONITOR_TOKEN']
    return cfg


def save_config(owner, values):
    with _lock:
        cfg = load_config(owner)
        cfg.update(values)
        path = Path(spaces.space_dir(owner)) / 'monitor.json'
        temp = path.with_suffix('.tmp')
        with open(temp, 'w', encoding='utf-8') as f:
            os.chmod(temp, 0o600)
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    return public_config(owner)


def public_config(owner):
    cfg = load_config(owner)
    result = {k: v for k, v in cfg.items() if k != 'token'}
    result['token_set'] = bool(cfg.get('token'))
    result['operation'] = copy.deepcopy(_operations.get(owner, {}))
    result['runtime'] = _services[owner].status if owner in _services else 'stopped'
    return result


class DiscordError(RuntimeError):
    def __init__(self, status, message='Discord request failed'):
        super().__init__(f'{message} (HTTP {status})')
        self.status = status


class BotAPI:
    def __init__(self, token):
        if not token:
            raise ValueError('Set a monitor bot token first')
        self.token = token
        self.session = None
        self.gate = asyncio.Lock()

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def request(self, method, path, payload=None):
        # Serial REST calls and respect both route and global 429 responses.
        async with self.gate:
            for _ in range(5):
                async with self.session.request(method, 'https://discord.com/api/v10' + path,
                        headers={'Authorization': 'Bot ' + self.token}, json=payload) as response:
                    if response.status == 204:
                        return {}
                    try:
                        data = await response.json()
                    except Exception:
                        data = {}
                    if response.status == 429:
                        await asyncio.sleep(max(0.1, float(data.get('retry_after', 1))))
                        continue
                    if response.status >= 400:
                        # Never return request URLs carrying interaction tokens or headers.
                        raise DiscordError(response.status, str(data.get('message', 'Discord request failed'))[:160])
                    if response.headers.get('X-RateLimit-Remaining') == '0':
                        await asyncio.sleep(max(0, float(response.headers.get('X-RateLimit-Reset-After', 0))))
                    return data
            raise DiscordError(429, 'Discord rate limit; try again later')


async def discover(owner):
    async with BotAPI(load_config(owner).get('token')) as api:
        me = await api.request('GET', '/users/@me')
        if not me.get('bot'):
            raise ValueError('Use an official Discord bot token')
        guilds = await api.request('GET', '/users/@me/guilds')
        # Only names and IDs used by dropdowns; no credentials leave the server.
        return {'bot_name': me['username'], 'bot_id': me['id'],
                'invite_url': f"https://discord.com/oauth2/authorize?client_id={me['id']}&scope=bot&permissions=268528656",
                'guilds': [{'id': g['id'], 'name': g['name']} for g in guilds]}


async def select_guild(owner, guild_id):
    if not re.fullmatch(r'\d{5,25}', str(guild_id)):
        raise ValueError('Select a server')
    async with BotAPI(load_config(owner).get('token')) as api:
        guild = await api.request('GET', f'/guilds/{guild_id}')
    cfg = load_config(owner)
    values = {'guild_id': str(guild_id), 'guild_name': guild['name'],
              'owner_id': guild['owner_id'], 'recipient_id': guild['owner_id']}
    if str(cfg.get('guild_id')) != str(guild_id):
        values.update(channel_id=None, message_id=None, enabled=False)
    result = save_config(owner, values)
    await restart(owner)
    return result


def channel_groups(accounts):
    """Stable groups of at most three distinct Discord accounts."""
    ordered = sorted(accounts, key=lambda a: (str(a.get('name', '')).casefold(), str(a.get('user_id', ''))))
    return [ordered[i:i + 3] for i in range(0, len(ordered), 3)]


async def _identify_account(owner, account):
    # Use the account's own proxy. Only a read-only identity lookup, no gateway login.
    from utils import proxy_manager
    proxy, auth, _ = proxy_manager.resolve_account_proxy(owner, account)
    connector = None
    kwargs = {}
    if proxy and proxy.startswith(('socks4://', 'socks5://')):
        from aiohttp_socks import ProxyConnector
        connector = ProxyConnector.from_url(proxy, rdns=True)
    elif proxy:
        kwargs = {'proxy': proxy, 'proxy_auth': auth}
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.get('https://discord.com/api/v9/users/@me',
                headers={'Authorization': account['token']}, **kwargs) as response:
            if response.status != 200:
                raise ValueError(f"Could not verify {account['name']} (HTTP {response.status})")
            user = await response.json()
            if user.get('bot'):
                raise ValueError(f"{account['name']} is a bot, not an OwO account")
            return str(user['id'])


async def provision(owner):
    from core import supervisor
    from utils import proxy_manager
    if supervisor.running_names(owner):
        raise ValueError('Stop the accounts before assigning channels')
    cfg = load_config(owner)
    guild_id = cfg.get('guild_id')
    if not guild_id:
        raise ValueError('Select a server first')
    accounts = proxy_manager.load_accounts(owner)
    if not accounts:
        raise ValueError('Add accounts first; channel IDs may be left empty')
    progress = _operations[owner]
    progress.update(stage='verifying accounts', total=len(accounts), done=0)
    async with BotAPI(cfg.get('token')) as api:
        me = await api.request('GET', '/users/@me')
        guild = await api.request('GET', f'/guilds/{guild_id}')
        owner_id = guild['owner_id']
        await api.request('GET', f'/guilds/{guild_id}/members/408785106942164992')
        # Verify all identities/membership before creating any channel.
        seen = set()
        for index, account in enumerate(accounts):
            uid = await _identify_account(owner, account)
            if uid in seen:
                raise ValueError(f"Duplicate Discord account: {account['name']}")
            seen.add(uid)
            await api.request('GET', f'/guilds/{guild_id}/members/{uid}')
            account['user_id'] = uid
            progress['done'] = index + 1
            await asyncio.sleep(0.4)
        channels = await api.request('GET', f'/guilds/{guild_id}/channels')
        prefix = f'LazyFarmers:{owner}:'
        base = [{'id': guild_id, 'type': 0, 'deny': str(VIEW), 'allow': '0'},
                {'id': me['id'], 'type': 1, 'allow': str(ACCESS), 'deny': '0'},
                {'id': owner_id, 'type': 1, 'allow': str(ACCESS), 'deny': '0'}]
        for index, group in enumerate(channel_groups(accounts)):
            # Discord categories hold at most 50 channels; use 49 farm channels per category.
            category_name = f'farm-{owner}-{index // 49 + 1}'
            category = next((c for c in channels if c['type'] == 4 and c['name'] == category_name), None)
            if category is None:
                category = await api.request('POST', f'/guilds/{guild_id}/channels',
                    {'name': category_name, 'type': 4, 'permission_overwrites': base})
                channels.append(category)
            topic = prefix + f'group:{index + 1}'
            channel = next((c for c in channels if c.get('topic') == topic and c['type'] == 0), None)
            permissions = base + [{'id': a['user_id'], 'type': 1, 'allow': str(ACCESS), 'deny': '0'}
                                  for a in group if a['user_id'] not in (owner_id, me['id'])]
            # OwO must also be able to read and reply inside the private channel.
            permissions.append({'id': '408785106942164992', 'type': 1, 'allow': str(ACCESS), 'deny': '0'})
            body = {'name': f'farm-{index + 1:03d}', 'type': 0, 'parent_id': category['id'],
                    'topic': topic, 'permission_overwrites': permissions}
            if channel:
                await api.request('PATCH', f"/channels/{channel['id']}", body)
            else:
                channel = await api.request('POST', f'/guilds/{guild_id}/channels', body)
                channels.append(channel)
            for account in group:
                account['channels'] = [channel['id']]
            progress.update(stage='creating channels', done=index + 1, total=len(channel_groups(accounts)))
        status_channel = next((c for c in channels if c.get('topic') == prefix + 'monitor'), None)
        if status_channel is None:
            status_channel = await api.request('POST', f'/guilds/{guild_id}/channels',
                {'name': 'operations-overview', 'type': 0, 'topic': prefix + 'monitor',
                 'permission_overwrites': base})
        # Account credentials might have been edited while the operation awaited HTTP.
        before = [(a.get('name'), a.get('token')) for a in accounts]
        current = proxy_manager.load_accounts(owner)
        if [(a.get('name'), a.get('token')) for a in current] != before or supervisor.running_names(owner):
            raise ValueError('Accounts changed during setup; retry with all accounts stopped')
        proxy_manager.save_accounts(owner, accounts)
        save_config(owner, {'channel_id': status_channel['id'], 'owner_id': owner_id,
                             'recipient_id': owner_id, 'enabled': True})
    await restart(owner)
    return {'channels': len(channel_groups(accounts)), 'accounts': len(accounts)}


def start_operation(owner, kind):
    task = _tasks.get(owner)
    if task and not task.done():
        return {'success': False, 'error': 'An operation is already in progress'}
    _operations[owner] = {'kind': kind, 'status': 'running', 'started_at': time.time(), 'results': []}
    async def run():
        try:
            if kind == 'provision':
                result = await provision(owner)
            elif kind == 'withdraw':
                result = await withdraw_all(owner)
            elif kind == 'reconcile':
                from core import state
                rows = []
                for bot in state.bots_for(owner):
                    cog = bot.get_cog('Owner')
                    if cog:
                        rows.append(dict(await cog.reconcile(), name=bot.account_name))
                _operations[owner]['results'] = rows
                result = {'checked': len(rows)}
            else:
                raise ValueError('Unknown operation')
            _operations[owner].update(status='complete', result=result)
        except asyncio.CancelledError:
            _operations[owner]['status'] = 'cancelled'
            raise
        except Exception as exc:
            _operations[owner].update(status='failed', error=str(exc)[:200])
    _tasks[owner] = asyncio.create_task(run())
    return {'success': True, 'queued': True}


async def withdraw_all(owner):
    from core import state
    cfg = load_config(owner)
    recipient = str(cfg.get('recipient_id') or '')
    if not recipient.isdigit():
        raise ValueError('Complete server setup first')
    results = _operations[owner]['results']
    for bot in list(state.bots_for(owner)):
        cog = bot.get_cog('Owner')
        if cog is None:
            results.append({'name': bot.account_name, 'status': 'skipped', 'reason': 'Owner module unavailable'})
            continue
        result = await cog.withdraw(recipient, percent=cfg.get('withdraw_percent', 100))
        results.append(dict(result, name=bot.account_name))
        if result.get('status') == 'recipient_limited':
            break
    return {'attempted': len(results), 'confirmed': sum(r.get('confirmed_total', 0) for r in results)}


def snapshot(owner):
    from core import state
    from utils import history_tracker
    # Read the owning space only. Keep unknown/stale values explicit.
    with open(spaces.accounts_path(owner), encoding='utf-8') as f:
        accounts = json.load(f).get('accounts', [])
    live = {b.account_name: b for b in state.bots_for(owner)}
    now = time.time()
    cfg = load_config(owner)
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    result = {'configured': len(accounts), 'connected': 0, 'ready': 0, 'cached': 0,
              'captcha': 0, 'paused': 0, 'failed': 0, 'queued': 0, 'low_cash': 0,
              'total_owo': 0, 'withdrawable': 0, 'unknown_limits': 0, 'accounts': [],
              'hour_net': 0, 'day_net': 0, 'best': None, 'sampled_accounts': 0, 'attention': 0}
    history = {}
    try:
        conn = history_tracker.get_db(owner)
        try:
            for uid, stamp, amount in conn.execute('SELECT account_id, unix, amount FROM cash_history ORDER BY unix'):
                history.setdefault(str(uid), []).append((stamp or 0, amount))
        finally:
            conn.close()
    except Exception:
        pass
    for account in accounts:
        bot = live.get(account.get('name'))
        uid = str(account.get('user_id') or '')
        st = bot.stats if bot and getattr(bot, 'user', None) else state.account_stats.get(uid, {})
        if not bot and st.get('space_owner') != owner:
            st = {}
        ready = bool(bot and bot.is_ready)
        result['connected'] += int(ready)
        result['ready'] += int(ready and not bot.paused)
        result['paused'] += int(bool(bot and bot.paused))
        job = st.get('captcha_job', {})
        result['captcha'] += int(bool(bot and (getattr(bot, '_solving_captcha', False)
                                  or (bot.paused and job.get('status') in ('queued', 'solving', 'retrying', 'manual_required')))))
        result['queued'] += int(bool(bot and bot.paused and job.get('status') in ('queued', 'retrying')))
        result['attention'] += int(not ready or bool(bot and bot.paused))
        result['failed'] += int(account.get('status') not in (None, '', 'ok'))
        cash = st.get('current_cash') if st.get('last_cash_update') else None
        fresh = cash is not None and now - st['last_cash_update'] < 900
        result['cached'] += int(cash is not None)
        result['low_cash'] += int(cash is not None and cash < 100)
        result['total_owo'] += max(0, int(cash or 0))
        account_cfg = bot.config.get('owner', {}) if bot else {}
        limit = send_limit(account_cfg, st)
        rec = st.get('owner_send', {}) if st.get('owner_send', {}).get('day') == today else {}
        remaining = rec.get('server_remaining', None if limit is None else max(0, limit - int(rec.get('sent', 0))))
        result['unknown_limits'] += int(remaining is None)
        eligible = ready and not bot.paused and fresh and uid != cfg.get('recipient_id')
        available = min(int(max(0, cash or 0) * cfg.get('withdraw_percent', 100) / 100), max(0, remaining or 0)) if eligible else 0
        if st.get('withdrawal', {}).get('status') in ('sending', 'awaiting_confirmation', 'verifying', 'unknown'):
            available = 0
        result['withdrawable'] += available
        row = {'name': account['name'], 'balance': cash, 'withdrawable': available, 'level': st.get('level'),
               'withdrawal': st.get('withdrawal', {}), 'captcha_job': job, 'net_24h': None}
        samples = history.get(uid, [])
        if len(samples) >= 2 and (bot or st.get('space_owner') == owner):
            for window, key in ((3600, 'hour_net'), (86400, 'day_net')):
                eligible_samples = [s for s in samples if s[0] <= now]
                if len(eligible_samples) < 2:
                    continue
                before = [s for s in eligible_samples if s[0] <= now - window]
                base = before[-1] if before else eligible_samples[0]
                last = eligible_samples[-1]
                if last[0] <= now - window or base == last:
                    continue
                receipts = sum(r['amount'] for r in st.get('withdrawal_receipts', []) if base[0] < r['at'] <= last[0])
                net = last[1] - base[1] + receipts
                result[key] += net
                if key == 'day_net':
                    row['net_24h'] = net
                    result['sampled_accounts'] += 1
                    if result['best'] is None or net > result['best']['net_24h']:
                        result['best'] = {'name': account['name'], 'net_24h': net}
        result['accounts'].append(row)
    result['missing'] = max(0, result['configured'] - result['connected'])
    return result


def build_message(s):
    best = s.get('best')
    attention = s.get('attention', s['captcha'] + s['missing'])
    return {'embeds': [{'title': '🛰️ Operations Overview', 'color': 0x5865F2,
        'description': f"{'🟡' if attention else '🟢'} **{attention} accounts need attention.**",
        'fields': [
            {'name': '👥 Fleet', 'value': f"`Configured` **{s['configured']}**\n`Connected` **{s['connected']}**\n`Ready` **{s['ready']}**\n`Balances` **{s['cached']} / {s['configured']} cached**\n`Low cash` **{s['low_cash']}**", 'inline': False},
            {'name': '⚠️ Attention', 'value': f"`Captcha` **{s['captcha']}** · `Paused` **{s['paused']}**\n`Missing` **{s['missing']}** · `Failed` **{s['failed']}** · `Queued` **{s['queued']}**", 'inline': False},
            {'name': '💰 Balances', 'value': f"`Total OwO` **{s['total_owo']:,}**\n`Withdrawable now` **{s['withdrawable']:,}** (estimate)\n`Unknown limits` **{s['unknown_limits']}**", 'inline': False},
            {'name': '📈 Performance · sampled net change', 'value': f"`1 hour` **{s['hour_net']:+,} OwO**\n`24 hours` **{s['day_net']:+,} OwO**\n{s['sampled_accounts']} accounts with usable samples", 'inline': False},
            {'name': '🏆 Best account · 24 hours', 'value': (f"**{best['name']}**\n{best['net_24h']:+,} OwO" if best else 'Waiting for balance history'), 'inline': False}],
        'footer': {'text': 'Owner-only • Cached balances • Limits estimated; OwO decides • No USD valuation'},
        'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()}],
        'components': [{'type': 1, 'components': [
            {'type': 2, 'style': 3, 'label': 'Withdraw available OwO', 'custom_id': 'lf:withdraw'},
            {'type': 2, 'style': 2, 'label': 'Refresh', 'custom_id': 'lf:refresh'}]}],
        'allowed_mentions': {'parse': []}}


class Monitor:
    def __init__(self, owner):
        self.owner = owner
        self.status = 'connecting'
        self.seq = None
        self.session_id = None
        self.resume_url = None
        self.ack = True
        self.last_interactions = set()
        self.last_publish = 0

    async def publish(self, api):
        cfg = load_config(self.owner)
        if not cfg.get('channel_id'):
            return
        body = build_message(await asyncio.to_thread(snapshot, self.owner))
        if cfg.get('message_id'):
            try:
                await api.request('PATCH', f"/channels/{cfg['channel_id']}/messages/{cfg['message_id']}", body)
                return
            except DiscordError as exc:
                if exc.status != 404:
                    raise
        message = await api.request('POST', f"/channels/{cfg['channel_id']}/messages", body)
        save_config(self.owner, {'message_id': message['id']})

    async def interact(self, api, data):
        cfg = load_config(self.owner)
        actor = (data.get('member', {}).get('user') or data.get('user') or {}).get('id')
        valid = (str(actor) == str(cfg.get('owner_id')) and str(data.get('guild_id')) == str(cfg.get('guild_id'))
                 and str(data.get('channel_id')) == str(cfg.get('channel_id'))
                 and str(data.get('message', {}).get('id')) == str(cfg.get('message_id')))
        action = (data.get('data') or {}).get('custom_id')
        if data.get('id') in self.last_interactions:
            return
        self.last_interactions.add(data['id'])
        if len(self.last_interactions) > 1000:
            self.last_interactions = {data['id']}
        text = 'Only the configured server owner can use this control.'
        if valid and action == 'lf:withdraw':
            result = start_operation(self.owner, 'withdraw')
            text = 'Withdrawal queued. Progress and per-account results are on the website.' if result['success'] else result['error']
        elif valid and action == 'lf:refresh':
            self.last_publish = 0
            text = 'Overview refresh requested.'
        # Callback uses a separate connection/lock so overview REST rate limits
        # cannot delay the initial interaction acknowledgement beyond 3 seconds.
        async with BotAPI(cfg['token']) as callback:
            await callback.request('POST', f"/interactions/{data['id']}/{data['token']}/callback",
                {'type': 4, 'data': {'content': text, 'flags': 64, 'allowed_mentions': {'parse': []}}})

    async def _heartbeat(self, ws, interval):
        await asyncio.sleep(random.random() * interval)
        while not ws.closed:
            if not self.ack:
                await ws.close(code=4000)
                return
            self.ack = False
            await ws.send_json({'op': 1, 'd': self.seq})
            await asyncio.sleep(interval)

    async def _publisher(self, api):
        while True:
            if time.time() - self.last_publish >= 60:
                try:
                    await self.publish(api)
                    self.last_publish = time.time()
                except Exception as exc:
                    self.status = str(exc)[:160]
            await asyncio.sleep(5)

    async def run(self):
        cfg = load_config(self.owner)
        async with BotAPI(cfg.get('token')) as api:
            gateway = await api.request('GET', '/gateway/bot')
            publisher = asyncio.create_task(self._publisher(api))
            try:
                while True:
                    heart = None
                    try:
                        url = (self.resume_url or gateway['url']) + '?v=10&encoding=json'
                        async with api.session.ws_connect(url, timeout=30) as ws:
                            hello = await ws.receive_json(timeout=30)
                            self.ack = True
                            heart = asyncio.create_task(self._heartbeat(ws, hello['d']['heartbeat_interval'] / 1000))
                            if self.session_id:
                                await ws.send_json({'op': 6, 'd': {'token': cfg['token'], 'session_id': self.session_id, 'seq': self.seq}})
                            else:
                                await ws.send_json({'op': 2, 'd': {'token': cfg['token'], 'intents': 1,
                                    'properties': {'os': 'linux', 'browser': 'lazyfarmers-monitor', 'device': 'lazyfarmers-monitor'}}})
                            async for message in ws:
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    break
                                frame = json.loads(message.data)
                                if frame.get('s') is not None:
                                    self.seq = frame['s']
                                if frame['op'] == 11:
                                    self.ack = True
                                elif frame['op'] == 1:
                                    await ws.send_json({'op': 1, 'd': self.seq})
                                elif frame['op'] == 7:
                                    break
                                elif frame['op'] == 9:
                                    if not frame.get('d'):
                                        self.session_id = self.resume_url = self.seq = None
                                    break
                                elif frame.get('t') == 'READY':
                                    self.session_id = frame['d']['session_id']
                                    self.resume_url = frame['d']['resume_gateway_url']
                                    self.status = 'connected'
                                elif frame.get('t') == 'RESUMED':
                                    self.status = 'connected'
                                elif frame.get('t') == 'INTERACTION_CREATE':
                                    await self.interact(api, frame['d'])
                            if ws.close_code in (4004, 4010, 4011, 4012, 4013, 4014):
                                self.status = f'Gateway rejected configuration ({ws.close_code})'
                                return
                            if ws.close_code in (4007, 4009):
                                self.session_id = self.resume_url = self.seq = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.status = type(exc).__name__
                    finally:
                        if heart:
                            heart.cancel()
                            await asyncio.gather(heart, return_exceptions=True)
                    await asyncio.sleep(6)
            finally:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)


async def restart(owner):
    previous = _services.pop(owner, None)
    if previous:
        previous.task.cancel()
        await asyncio.gather(previous.task, return_exceptions=True)
    cfg = load_config(owner)
    if cfg.get('enabled') and cfg.get('token') and cfg.get('channel_id'):
        monitor = Monitor(owner)
        _services[owner] = monitor
        async def run():
            try:
                await monitor.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                monitor.status = type(exc).__name__
        monitor.task = asyncio.create_task(run())
    return {'success': True}


async def boot():
    for owner in spaces.list_owners():
        await restart(owner)
