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
from core.paths import CONFIG_DIR
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


def normalize_token(token):
    """Accept a raw token or a copied, quoted Bot authorization value."""
    if not isinstance(token, str):
        raise ValueError('Paste a monitor bot token')
    token = token.strip().strip('"\'`').strip()
    token = re.sub(r'^Bot(?:\s+|$)', '', token, flags=re.IGNORECASE).strip().strip('"\'`').strip()
    if not token:
        raise ValueError('Set a monitor bot token first')
    if any(c.isspace() for c in token):
        raise ValueError('The monitor token contains spaces or line breaks. Copy the bot token again.')
    return token


def load_config(owner):
    path = Path(spaces.space_dir(owner)) / 'monitor.json'
    try:
        cfg = json.loads(path.read_text())
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError('Invalid monitor configuration')
    # The environment bootstraps setup; an explicitly saved token must be editable.
    if not cfg.get('token') and owner == spaces.ADMIN_SPACE and os.environ.get('LAZYFARMERS_MONITOR_TOKEN'):
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
    ids = configured_owner_ids(owner)
    result.update(owner_ids=ids, owner_id=ids[0] if ids else None,
                  recipient_id=ids[0] if ids else None)
    result['token_set'] = bool(cfg.get('token'))
    result['operation'] = copy.deepcopy(_operations.get(owner, {}))
    result['runtime'] = _services[owner].status if owner in _services else 'stopped'
    return result


def configured_owner_ids(owner):
    """Read the website's owner configuration, never the selected guild owner.

    Read on each action so saved owner changes revoke old controls immediately.
    The space settings override the bundled/default settings like the bots do.
    """
    settings = {}
    for path in (Path(CONFIG_DIR) / 'settings.json', Path(spaces.settings_path(owner))):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        values = data.get('owner', {})
        if isinstance(values, dict):
            settings.update(values)
    value = settings.get('user_id', '')
    values = value if isinstance(value, list) else re.split(r'[,\s]+', str(value or ''))
    return list(dict.fromkeys(str(uid).strip() for uid in values
                             if spaces.is_valid_discord_id(str(uid).strip())))


class DiscordError(RuntimeError):
    def __init__(self, status, message='Discord request failed'):
        if status == 401:
            message = ('Discord rejected the monitor bot token. Copy or reset the token on the '
                       'Developer Portal Bot page, then save it here. Your dashboard session is still valid.')
        elif status == 403:
            message = 'The monitor bot lacks access or permissions. Check its server membership and channel permissions.'
        super().__init__(f'{message} (HTTP {status})')
        self.status = status


class BotAPI:
    def __init__(self, token):
        self.token = normalize_token(token)
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


async def configure(owner, values):
    """Validate credentials before replacing a working configuration."""
    values = dict(values)
    cfg = load_config(owner)
    token = values.get('token') or cfg.get('token')
    if values.get('token') or values.get('enabled'):
        token = normalize_token(token)
        async with BotAPI(token) as api:
            me = await api.request('GET', '/users/@me')
        if not me.get('bot'):
            raise ValueError('Use an official Discord bot token for the monitor')
        values.update(token=token, bot_id=me['id'], bot_name=me['username'])
        # A different bot cannot edit the previous bot's overview message.
        if cfg.get('bot_id') != me['id']:
            values['message_id'] = None
    if _operations.get(owner, {}).get('status') == 'running':
        raise ValueError('Wait for the current operation to finish')
    save_config(owner, values)
    await restart(owner)
    return public_config(owner)


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
              'owner_id': None, 'recipient_id': None}
    if str(cfg.get('guild_id')) != str(guild_id):
        values.update(channel_id=None, message_id=None, enabled=False)
    result = save_config(owner, values)
    await restart(owner)
    return result


def channel_groups(accounts):
    """Stable groups of at most three distinct Discord accounts."""
    ordered = sorted(accounts, key=lambda a: (str(a.get('name', '')).casefold(), str(a.get('user_id', ''))))
    return [ordered[i:i + 3] for i in range(0, len(ordered), 3)]


class AccountSetupError(ValueError):
    """An account-specific, credential-free setup failure."""


# An account with no channel cannot be started at all (supervisor.start_account
# refuses one), so a channel is assigned even when setup could not establish the
# account's Discord id. Without the id there is no permission overwrite to write,
# and the account cannot see the channel until grant_channel_access() adds one.
AWAITING_HINT = ('The channel is assigned anyway; start this account once from Accounts and it is '
                 'given access automatically as soon as it reports who it is.')


def setup_error(exc, stage):
    if isinstance(exc, AccountSetupError):
        return str(exc)
    if isinstance(exc, DiscordError):
        if exc.status == 404 and stage == 'membership':
            return 'Account is not a member of the selected server. Join it, then retry setup.'
        return f'{stage.capitalize()} failed (Discord HTTP {exc.status}). Check access and retry.'
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f'{stage.capitalize()} timed out. Check the connection or proxy and retry.'
    # Network/proxy exceptions may contain authenticated URLs. Never expose them.
    return f'{stage.capitalize()} failed ({type(exc).__name__}). Check the account token or proxy and retry.'


async def _identify_account(owner, account):
    """Resolve identity through the same Discord login stack as dashboard Start.

    A bare aiohttp /users/@me probe does not reproduce the application's login
    flow and must not be treated as proof that a working account token is dead.
    Only authenticate: do not connect the gateway or start any account workers.
    """
    import discord
    from core import state
    from utils import proxy_manager
    token = account.get('token')
    if not isinstance(token, str) or not token.strip():
        raise AccountSetupError('This account has no token saved. Add its token, then retry setup.')
    proxy, auth, _ = proxy_manager.resolve_account_proxy(owner, account)
    client = discord.Client(proxy=proxy, proxy_auth=auth)
    try:
        # Dashboard startup uses this same gate and lets the installed client
        # library own authentication, headers, session initialization and proxy use.
        await state.login_slot()
        await asyncio.wait_for(client.login(token.strip()), timeout=30)
        user = client.user
        if user is None or not str(user.id).isdigit():
            raise AccountSetupError('Login returned no account identity. Retry setup.')
        if getattr(user, 'bot', False):
            raise AccountSetupError('This token belongs to a bot, not an OwO account.')
        return str(user.id)
    except discord.LoginFailure:
        raise AccountSetupError('Discord refused the identity check for this account (HTTP 401). That is not '
                                'proof the token is dead - accounts that start from Accounts have been '
                                'refused here.') from None
    except discord.HTTPException as exc:
        raise AccountSetupError(f'Account identity check failed (HTTP {exc.status}). '
                                'Retry setup; this alone does not establish that the token is invalid.') from None
    finally:
        try:
            await asyncio.wait_for(client.close(), timeout=5)
        except Exception:
            # Do not replace an identity/login result with a cleanup error.
            pass


def recorded_user_id(account):
    """The Discord id this config row last logged in as, or ''.

    core.bot writes it on every ready, so it is evidence of a completed gateway
    login rather than of a REST probe - the account really did connect with it.
    """
    uid = str((account or {}).get('user_id') or '').strip()
    return uid if spaces.is_valid_discord_id(uid) else ''


async def resolve_identity(owner, account):
    """(discord_id, source) for one account. A refused probe is not the last word.

    A fresh login is still preferred: a token pasted onto an existing row leaves
    the previous account's id behind, and only a login can notice. But a probe
    Discord refuses does not establish that an account is unusable - the whole
    point of this fallback is that the accounts being refused here are the same
    ones that log in and farm from the Accounts page. So a refusal falls back to
    the id a real login already recorded, and the caller's membership check (run
    with the monitor bot's own credentials, which are known to work) is what
    decides whether that id can be given a channel.
    """
    try:
        return await _identify_account(owner, account), 'login'
    except Exception:
        uid = recorded_user_id(account)
        if not uid:
            raise
        return uid, 'recorded'


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
    expected = copy.deepcopy(accounts)
    progress = _operations[owner]
    rows = progress['results'] = []
    # Assign every saved row as-is. Provisioning never authenticates accounts,
    # checks their membership, deduplicates them, or changes proxy assignments.
    kept = accounts
    rows.extend({'name': account['name'], 'status': 'pending'} for account in accounts)

    def commit():
        nonlocal expected
        with proxy_manager._FILE_LOCK:
            if proxy_manager.load_accounts(owner) != expected or supervisor.running_names(owner):
                raise ValueError('Accounts changed during setup; retry with all accounts stopped')
            proxy_manager.save_accounts(owner, kept)
            expected = copy.deepcopy(kept)

    def mark(account, row, status, reason='', **extra):
        row.update(status=status, reason=reason, **extra)
        account['channel_setup'] = dict(status=status, reason=reason, at=time.time(), **extra)

    async with BotAPI(cfg.get('token')) as api:
        me = await api.request('GET', '/users/@me')
        channels = await api.request('GET', f'/guilds/{guild_id}/channels')
        prefix = f'LazyFarmers:{owner}:'
        base = [{'id': str(guild_id), 'type': 0, 'allow': str(ACCESS), 'deny': '0'},
                {'id': me['id'], 'type': 1, 'allow': str(ACCESS), 'deny': '0'}]
        groups = channel_groups(accounts)
        account_rows = {id(account): row for account, row in zip(accounts, rows)}
        assigned_channels = 0
        updated_categories = set()
        progress.update(stage='creating channels', total=len(groups), done=0)
        for index, group in enumerate(groups):
            try:
                category_name = f'farm-{owner}-{index // 49 + 1}'
                category = next((c for c in channels if c['type'] == 4 and c['name'] == category_name), None)
                if category is None:
                    category = await api.request('POST', f'/guilds/{guild_id}/channels',
                        {'name': category_name, 'type': 4, 'permission_overwrites': base})
                    channels.append(category)
                elif category['id'] not in updated_categories:
                    # Migrate categories made by older private-channel setup.
                    await api.request('PATCH', f"/channels/{category['id']}",
                                      {'permission_overwrites': base})
                updated_categories.add(category['id'])
                topic = prefix + f'group:{index + 1}'
                channel = next((c for c in channels if c.get('topic') == topic and c['type'] == 0), None)
                permissions = base
                body = {'name': f'farm-{index + 1:03d}', 'type': 0, 'parent_id': category['id'],
                        'topic': topic, 'permission_overwrites': permissions}
                if channel:
                    await api.request('PATCH', f"/channels/{channel['id']}", body)
                else:
                    channel = await api.request('POST', f'/guilds/{guild_id}/channels', body)
                    channels.append(channel)
            except Exception as exc:
                if isinstance(exc, DiscordError) and exc.status == 401:
                    raise
                for account in group:
                    mark(account, account_rows[id(account)], 'failed', setup_error(exc, 'channel assignment'))
            else:
                assigned_channels += 1
                for account in group:
                    account['channels'] = [channel['id']]
                    row = account_rows[id(account)]
                    mark(account, row, 'assigned', channel_id=channel['id'])
            commit()
            progress['done'] = index + 1

        # The overview channel and the connection itself belong to the monitor, not
        # to any account, so they are set up whatever the per-account results were.
        # Gating them on "at least one channel was assigned this run" is what left
        # the monitor bot permanently offline after a run that assigned nothing -
        # with no way to bring it up from the tab, because this is the only place
        # that ever sets channel_id and enabled.
        status_channel = next((c for c in channels if c.get('topic') == prefix + 'monitor'), None)
        if status_channel is None:
            status_channel = await api.request('POST', f'/guilds/{guild_id}/channels',
                {'name': 'operations-overview', 'type': 0, 'topic': prefix + 'monitor',
                 'permission_overwrites': base})
        else:
            await api.request('PATCH', f"/channels/{status_channel['id']}",
                              {'permission_overwrites': base})
        values = {'channel_id': status_channel['id'], 'owner_id': None,
                  'recipient_id': None, 'enabled': True}
        if str(cfg.get('channel_id')) != str(status_channel['id']):
            values['message_id'] = None
        save_config(owner, values)
    await restart(owner)
    return {'channels': assigned_channels, 'accounts': sum(r['status'] == 'assigned' for r in rows),
            'failed': sum(r['status'] == 'failed' for r in rows),
            'awaiting': sum(r['status'] == 'awaiting_identity' for r in rows),
            'duplicates_removed': sum(r['status'] == 'duplicate_removed' for r in rows)}


def _record_channel_setup(owner, name, values):
    """Update one account's channel_setup row and nothing else.

    Called while that account is running, so it must not rewrite the file from a
    snapshot taken earlier - the whole point is to touch a single key.
    """
    from utils import proxy_manager
    with proxy_manager._FILE_LOCK:
        accounts = proxy_manager.load_accounts(owner)
        for account in accounts:
            if account.get('name') == name:
                account['channel_setup'] = dict(account.get('channel_setup') or {}, at=time.time(), **values)
                proxy_manager.save_accounts(owner, accounts)
                return True
    return False


async def grant_channel_access(owner, name, user_id):
    """Give a newly identified account access to the channel setup already assigned it.

    Channel setup can run before an account has ever connected, and an account
    that has never connected has no Discord id to write a permission overwrite
    for. It is still given a channel - one is required to start at all - so the
    missing overwrite is added here, the first time the account logs in and says
    who it is. Returns False and does nothing for every other account, including
    every account in a space with no monitor bot configured.
    """
    uid = str(user_id or '').strip()
    if not spaces.is_valid_discord_id(uid):
        return False
    cfg = load_config(owner)
    if not cfg.get('token') or not cfg.get('guild_id'):
        return False
    from utils import proxy_manager
    account = next((a for a in proxy_manager.load_accounts(owner) if a.get('name') == name), None)
    setup = (account or {}).get('channel_setup') or {}
    if setup.get('status') != 'awaiting_identity':
        return False
    channel_id = str(setup.get('channel_id') or next(iter(account.get('channels') or []), '') or '')
    if not channel_id.isdigit():
        return False
    async with BotAPI(cfg['token']) as api:
        channel = await api.request('GET', f'/channels/{channel_id}')
        # PATCH replaces the whole array, so the existing overwrites have to be
        # read back and carried over - and reduced to the four fields Discord
        # accepts on the way in.
        overwrites = [{'id': str(o['id']), 'type': int(o.get('type', 1)),
                       'allow': str(o.get('allow', '0')), 'deny': str(o.get('deny', '0'))}
                      for o in channel.get('permission_overwrites') or [] if str(o.get('id', '')) != uid]
        overwrites.append({'id': uid, 'type': 1, 'allow': str(ACCESS), 'deny': '0'})
        await api.request('PATCH', f'/channels/{channel_id}', {'permission_overwrites': overwrites})
    _record_channel_setup(owner, name, {'status': 'assigned', 'reason': '', 'channel_id': channel_id})
    return True


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
            _operations[owner].update(status='complete_with_errors' if kind == 'provision' and result.get('failed') else 'complete', result=result)
        except asyncio.CancelledError:
            _operations[owner]['status'] = 'cancelled'
            raise
        except Exception as exc:
            _operations[owner].update(status='failed', error=str(exc)[:200])
    _tasks[owner] = asyncio.create_task(run())
    return {'success': True, 'queued': True}


async def withdraw_all(owner):
    from core import state
    from utils import proxy_manager
    cfg = load_config(owner)
    recipient = next(iter(configured_owner_ids(owner)), '')
    if not recipient.isdigit():
        raise ValueError('Set owner.user_id in the website Configuration first')
    progress = _operations[owner]
    results = progress['results']
    live = {bot.account_name: bot for bot in state.bots_for(owner)}
    accounts = proxy_manager.load_accounts(owner)
    progress.update(stage='withdrawing accounts', total=len(accounts), done=0)
    recipient_limited = False
    for account in accounts:
        name = account['name']
        bot = live.get(name)
        if recipient_limited or bot is None:
            results.append({'name': name, 'status': 'skipped', 'reason':
                            'Recipient receive limit reached' if recipient_limited else 'Account is not running'})
            progress['done'] += 1
            continue
        cog = bot.get_cog('Owner')
        if cog is None:
            results.append({'name': name, 'status': 'skipped', 'reason': 'Owner module unavailable'})
            progress['done'] += 1
            continue
        progress['current_account'] = name
        try:
            result = await cog.withdraw(recipient, percent=cfg.get('withdraw_percent', 100))
        except Exception as exc:
            # One account must not abort the rest; exceptions can contain tokens.
            result = {'status': 'failed', 'reason': type(exc).__name__}
        results.append(dict(result, name=name))
        recipient_limited = result.get('status') == 'recipient_limited'
        progress['done'] += 1
    progress.pop('current_account', None)
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
    recipient = next(iter(configured_owner_ids(owner)), '')
    today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    result = {'configured': len(accounts), 'connected': 0, 'ready': 0, 'cached': 0,
              'captcha': 0, 'paused': 0, 'failed': 0, 'queued': 0, 'low_cash': 0,
              'total_owo': 0, 'withdrawable': 0, 'unknown_limits': 0, 'accounts': [],
              'withdrawal_estimate': 0, 'pending_limit_amount': 0,
              'pending_limit_accounts': 0, 'stale_balance_accounts': 0,
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
        uid = str(getattr(getattr(bot, 'user', None), 'id', None) or account.get('user_id') or '')
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
        result['failed'] += int(account.get('status') not in (None, '', 'ok') or account.get('channel_setup', {}).get('status') == 'failed')
        cash = st.get('current_cash') if st.get('last_cash_update') else None
        fresh = cash is not None and now - st['last_cash_update'] < 900
        result['cached'] += int(cash is not None)
        result['low_cash'] += int(cash is not None and cash < 100)
        result['total_owo'] += max(0, int(cash or 0))
        account_cfg = bot.config.get('owner', {}) if bot else {}
        limit = send_limit(account_cfg, st)
        rec = st.get('owner_send', {}) if st.get('owner_send', {}).get('day') == today else {}
        remaining = rec.get('server_remaining')
        if remaining is None and limit is not None:
            remaining = max(0, limit - int(rec.get('sent', 0)))
        if remaining is not None:
            remaining = max(0, int(remaining))
        result['unknown_limits'] += int(remaining is None)
        # A withdrawal refreshes cash before sending. Keep its cached balance
        # estimate separate from the fresh, known-limit amount available now.
        eligible = bool(ready and getattr(bot, 'active', True) and not bot.paused
                        and recipient.isdigit() and uid != recipient)
        unresolved = st.get('withdrawal', {}).get('status') in ('sending', 'awaiting_confirmation', 'verifying', 'unknown')
        estimate = int(max(0, cash or 0) * cfg.get('withdraw_percent', 100) / 100) if eligible and not unresolved else 0
        if remaining is not None:
            estimate = min(estimate, remaining)
        available = estimate if fresh and remaining is not None else 0
        pending_limit = estimate if remaining is None else 0
        result['withdrawal_estimate'] += estimate
        result['pending_limit_amount'] += pending_limit
        result['pending_limit_accounts'] += int(pending_limit > 0)
        result['stale_balance_accounts'] += int(estimate > 0 and not fresh)
        result['withdrawable'] += available
        row = {'name': account['name'], 'balance': cash, 'withdrawable': available, 'level': st.get('level'),
               'withdrawal_estimate': estimate, 'remaining_limit': remaining,
               'pending_limit_amount': pending_limit, 'balance_fresh': fresh,
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
    balance_note = f"`Available to withdraw (estimate)` **{s.get('withdrawal_estimate', s['withdrawable']):,}**\n`Fresh balance, known limit` **{s['withdrawable']:,}**"
    if s.get('pending_limit_accounts'):
        balance_note += f"\n{s['pending_limit_amount']:,} OwO has an unknown daily limit; the server will check it when sending ({s['pending_limit_accounts']} accounts)."
    if s.get('stale_balance_accounts'):
        balance_note += '\nCached balances will be refreshed before sending.'
    return {'embeds': [{'title': '🛰️ Operations Overview', 'color': 0x5865F2,
        'description': f"{'🟡' if attention else '🟢'} **{attention} accounts need attention.**",
        'fields': [
            {'name': '👥 Fleet', 'value': f"`Configured` **{s['configured']}**\n`Connected` **{s['connected']}**\n`Ready` **{s['ready']}**\n`Balances` **{s['cached']} / {s['configured']} cached**\n`Low cash` **{s['low_cash']}**", 'inline': False},
            {'name': '⚠️ Attention', 'value': f"`Captcha` **{s['captcha']}** · `Paused` **{s['paused']}**\n`Missing` **{s['missing']}** · `Failed` **{s['failed']}** · `Queued` **{s['queued']}**", 'inline': False},
            {'name': '💰 Balances', 'value': f"`Total OwO` **{s['total_owo']:,}**\n{balance_note}", 'inline': False},
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
        valid = (str(actor) in configured_owner_ids(self.owner) and str(data.get('guild_id')) == str(cfg.get('guild_id'))
                 and str(data.get('channel_id')) == str(cfg.get('channel_id'))
                 and str(data.get('message', {}).get('id')) == str(cfg.get('message_id')))
        action = (data.get('data') or {}).get('custom_id')
        if data.get('id') in self.last_interactions:
            return
        self.last_interactions.add(data['id'])
        if len(self.last_interactions) > 1000:
            self.last_interactions = {data['id']}
        text = 'Only an owner ID saved in the website Configuration can use this control.'
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
                                await ws.send_json({'op': 6, 'd': {'token': api.token, 'session_id': self.session_id, 'seq': self.seq}})
                            else:
                                await ws.send_json({'op': 2, 'd': {'token': api.token, 'intents': 1,
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
                        self.status = str(exc)[:240] or type(exc).__name__
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
                monitor.status = str(exc)[:240] or type(exc).__name__
        monitor.task = asyncio.create_task(run())
    return {'success': True}


async def boot():
    for owner in spaces.list_owners():
        await restart(owner)
