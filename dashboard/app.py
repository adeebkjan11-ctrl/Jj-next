# This file is part of LazyFarmers.
# Copyright (c) 2025-Present Routo
#
# LazyFarmers is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# You should have received a copy of the GNU General Public License
# along with LazyFarmers. If not, see <https://www.gnu.org/licenses/>.


"""
The Flask dashboard.

Two roles share it. The operator (config/auth.json) manages activation keys and
dashboard users. Everyone else redeems a key, which mints a space of their own
(core/spaces.py) holding their accounts, proxies, settings and history - full
control inside it, no visibility outside it.

Isolation lives in two places rather than being repeated per route:

  * @space_required resolves the caller's space once into flask.g.owner
  * get_bot() only ever searches that space, so a foreign discord id looks
    exactly like one that does not exist

Only /api/users*, /api/debug* and writing the shipped config/settings.json
defaults stay admin-only.
"""


from flask import Flask, render_template, jsonify, request, session, redirect, url_for, g
from functools import wraps
from concurrent.futures import TimeoutError as FuturesTimeout
import threading
import time
import json
import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
import core.state as state
import utils.utils as utils
import asyncio
from datetime import datetime, timedelta
from urllib.parse import quote

from core import spaces
from dashboard import users as dash_users


import socket

_original_getaddrinfo = socket.getaddrinfo

# Some hosts cannot resolve owobot.com (captive resolvers, Termux without a DNS
# server, a few container images), and the captcha flow in modules/web_solver.py
# needs it. This used to answer owobot.com from a hardcoded address
# unconditionally - so the day that Cloudflare address stops serving the site,
# every captcha solve fails and no amount of correct DNS can help. Real DNS wins;
# the pin is only the fallback.
_OWOBOT_FALLBACK_IP = '104.21.35.189'


def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if host == 'owobot.com':
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (_OWOBOT_FALLBACK_IP, port))]
        raise

socket.getaddrinfo = patched_getaddrinfo

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
try:
    app.json.sort_keys = False
except AttributeError:
    pass

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

AUTH_FILE = os.path.join(state.CONFIG_DIR, 'auth.json')
SECRET_FILE = os.path.join(state.CONFIG_DIR, 'secret.key')
LOGIN_ATTEMPTS = {}
BLOCK_DURATION = 300
ATTEMPT_WINDOW = 900
MAX_ATTEMPTS = 5
SESSION_LIFETIME = timedelta(days=7)

# a space may not grow without bound - one tenant should not be able to fill the box.
# 500 is the supported ceiling: the account list and config polls are served from a
# per-space snapshot cache, and each running account still costs a gateway connection
# plus ~25-40MB, so the box's RAM and vCPU are the real limit long before this is.
MAX_ACCOUNTS_PER_SPACE = int(os.environ.get('LAZYFARMERS_MAX_ACCOUNTS', '500'))
MAX_PROXIES_PER_SPACE = int(os.environ.get('LAZYFARMERS_MAX_PROXIES', '500'))

# non-GET /api/ calls need the session's csrf token. key_status is the exception:
# the activate page hits it before there is a session to protect, and it is rate
# limited instead.
CSRF_EXEMPT = {'user_key_status'}


def load_secret_key():
    """The cookie signing key, from config/secret.key.

    This used to fall back to a constant baked into the source, which meant
    anyone reading the repo could sign themselves a cookie carrying
    is_admin: True. There is no fallback now - the key is generated on first
    boot and kept out of git.
    """
    try:
        with open(SECRET_FILE, 'r', encoding='utf-8') as f:
            existing = f.read().strip()
        if len(existing) >= 32:
            return existing
    except OSError:
        pass

    fresh = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(SECRET_FILE) or '.', exist_ok=True)
        with open(SECRET_FILE, 'w', encoding='utf-8') as f:
            f.write(fresh)
        os.chmod(SECRET_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        # an unwritable config dir means sessions die on restart, which is
        # inconvenient but still better than a predictable key
        print(f"[!] could not persist {SECRET_FILE}: {exc}", flush=True)
    return fresh


def seed_auth_config():
    """Write config/auth.json with a random password on a fresh install.

    auth.json is gitignored now, so a fresh clone has none. Shipping a known
    default password would hand over the admin panel to anyone who read the
    repo, so generate one instead and print it once - the operator reads it out
    of the boot log and changes it from the dashboard or neura_setup.py.
    """
    password = secrets.token_urlsafe(12)
    cfg = {'username': 'admin', 'password': password}
    try:
        os.makedirs(os.path.dirname(AUTH_FILE) or '.', exist_ok=True)
        with open(AUTH_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=4)
        os.chmod(AUTH_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        print(f"[!] could not write {AUTH_FILE}: {exc}", flush=True)
        return cfg
    print("\n" + "=" * 62, flush=True)
    print("  First boot - dashboard admin credentials generated:", flush=True)
    print(f"    username: admin", flush=True)
    print(f"    password: {password}", flush=True)
    print(f"  Stored in {AUTH_FILE}. Change it once you are in.", flush=True)
    print("=" * 62 + "\n", flush=True)
    return cfg


def load_auth_config():
    cfg = None
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = None
    elif not (os.environ.get('LAZYFARMERS_DASHBOARD_USER')
              or os.environ.get('LAZYFARMERS_DASHBOARD_PASSWORD')):
        cfg = seed_auth_config()

    env_user = os.environ.get('LAZYFARMERS_DASHBOARD_USER')
    env_pass = os.environ.get('LAZYFARMERS_DASHBOARD_PASSWORD')
    if env_user or env_pass:
        cfg = dict(cfg) if cfg else {}
        if env_user:
            cfg['username'] = env_user
        if env_pass:
            cfg['password'] = env_pass

    return cfg


app.secret_key = load_secret_key()
# eagerly, so a fresh install prints its generated password in the boot log
# rather than on whichever login attempt happens to come first
load_auth_config()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # a plain-http deployment would lose its cookie entirely with Secure on, so
    # only ask for it when the operator says the front door is https
    SESSION_COOKIE_SECURE=os.environ.get('LAZYFARMERS_HTTPS', '').lower() in ('1', 'true', 'yes'),
    PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
)


def same_secret(a, b):
    """Constant-time compare of two credentials, safe on non-ascii input."""
    return hmac.compare_digest(str(a or '').encode('utf-8'), str(b or '').encode('utf-8'))


def admin_fingerprint():
    """Ties an admin session to the credentials it was issued against.

    Changing the dashboard password now logs out sessions minted with the old
    one instead of leaving them valid forever.
    """
    cfg = load_auth_config() or {}
    raw = f"{cfg.get('username', '')}:{cfg.get('password', '')}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]


def _current_user():
    """The activated (non-admin) user behind this session, or None for the admin."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    return dash_users.get_user(user_id)


def _session_valid():
    if 'logged_in' not in session:
        return False
    # a cookie is never both, so one claiming both was hand-made
    if session.get('is_admin') and session.get('user_id'):
        return False
    if session.get('is_admin'):
        return same_secret(session.get('auth_fp'), admin_fingerprint())
    # a user's access dies the moment the key duration runs out or the admin
    # revokes it, so re-check on every request instead of trusting the cookie
    user = _current_user()
    return (dash_users.is_active(user)
            and session.get('user_version') == user.get('session_version', 0))


def _reject_session():
    session.clear()
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'Session expired'}), 401
    return redirect(url_for('login'))


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _session_valid():
            return _reject_session()
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _session_valid():
            return _reject_session()
        if not session.get('is_admin'):
            return jsonify({'success': False, 'error': 'Admin only'}), 403
        return f(*args, **kwargs)
    return decorated_function


def _payload():
    """The JSON body as a dict, without raising on a non-JSON request."""
    if request.is_json:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
    return {}


def resolve_owner():
    """Which space this request acts on, or None when the session cannot name one.

    The admin acts on their own space unless they explicitly ask for another with
    ?space=<owner> - that is the only cross-space access in the app, and it is
    never implicit.
    """
    if session.get('is_admin'):
        wanted = request.args.get('space') or _payload().get('space')
        if wanted:
            try:
                return spaces.normalise_owner(wanted)
            except spaces.InvalidOwner:
                return None
        return spaces.ADMIN_SPACE
    try:
        return spaces.normalise_owner(session.get('user_id'))
    except spaces.InvalidOwner:
        return None


def space_required(f):
    """login_required plus a resolved g.owner for every space-scoped route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not _session_valid():
            return _reject_session()
        owner = resolve_owner()
        if not owner:
            return jsonify({'success': False, 'error': 'Unknown space'}), 403
        g.owner = owner
        return f(*args, **kwargs)
    return decorated_function


def current_owner():
    return getattr(g, 'owner', None) or resolve_owner() or spaces.ADMIN_SPACE


def acting_label():
    """Who to name in the audit log for a mutating action."""
    if session.get('is_admin'):
        return 'operator'
    user = _current_user() or {}
    return user.get('email') or session.get('user_id') or 'user'


def client_ip():
    """The address to rate limit on.

    X-Forwarded-For is only trusted when the operator says something in front of
    us sets it - otherwise a header would let one client empty everyone's bucket
    (or dodge their own).
    """
    if os.environ.get('LAZYFARMERS_TRUSTED_PROXY', '').lower() in ('1', 'true', 'yes'):
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def check_rate_limit(ip):
    """(allowed, seconds_to_wait) for one client.

    The old version reset the counter on every call - `now - block_time >
    BLOCK_DURATION` is always true while block_time is 0 - so no number of bad
    logins ever locked anyone out.
    """
    now = time.time()
    record = LOGIN_ATTEMPTS.get(ip)
    if not record:
        return True, 0

    _count, window_start, blocked_until = record
    if blocked_until > now:
        return False, int(blocked_until - now) + 1
    if now - window_start > ATTEMPT_WINDOW:
        LOGIN_ATTEMPTS.pop(ip, None)
    return True, 0


def fail_login(ip):
    now = time.time()
    count, window_start, blocked_until = LOGIN_ATTEMPTS.get(ip, (0, now, 0.0))
    if now - window_start > ATTEMPT_WINDOW:
        count, window_start, blocked_until = 0, now, 0.0
    count += 1
    if count >= MAX_ATTEMPTS:
        blocked_until = now + BLOCK_DURATION
    LOGIN_ATTEMPTS[ip] = (count, window_start, blocked_until)

    # keep the table from growing forever on a public host
    if len(LOGIN_ATTEMPTS) > 4096:
        for key, (_c, started, blocked) in list(LOGIN_ATTEMPTS.items()):
            if blocked <= now and now - started > ATTEMPT_WINDOW:
                LOGIN_ATTEMPTS.pop(key, None)


def clear_rate_limit(ip):
    LOGIN_ATTEMPTS.pop(ip, None)


def csrf_token():
    token = session.get('csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf'] = token
    return token


@app.before_request
def protect_provisioning():
    # The setup job snapshots credentials and owns assignment until it completes.
    if request.method in ('GET', 'HEAD', 'OPTIONS') or not request.path.startswith('/api/accounts'):
        return None
    if not _session_valid():
        return None
    from core import monitor
    job = monitor._operations.get(resolve_owner(), {})
    if job.get('kind') == 'provision' and job.get('status') == 'running':
        return jsonify({'success': False, 'status': 'error',
                        'error': 'Channel setup is running; wait before editing or starting accounts',
                        'message': 'Channel setup is running; wait before editing or starting accounts'}), 409


@app.before_request
def enforce_csrf():
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    if not request.path.startswith('/api/'):
        return None
    if request.endpoint in CSRF_EXEMPT:
        return None
    sent = request.headers.get('X-CSRF-Token') or request.headers.get('X-CSRFToken') or ''
    expected = session.get('csrf') or ''
    if not expected or not same_secret(sent, expected):
        return jsonify({'success': False, 'error': 'Bad or missing CSRF token'}), 403
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    # 'unsafe-inline' is unavoidable while the templates use inline onclick=,
    # but the origin allow-list still blocks exfiltration to an attacker host
    response.headers.setdefault('Content-Security-Policy', "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://js.hcaptcha.com https://newassets.hcaptcha.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
        "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net",
        "img-src 'self' data: https:",
        "connect-src 'self' https://hcaptcha.com https://*.hcaptcha.com",
        "frame-src https://newassets.hcaptcha.com https://hcaptcha.com",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]))
    return response


def protect_large_ints(obj):
    if isinstance(obj, dict):
        return {k: protect_large_ints(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [protect_large_ints(v) for v in obj]
    elif isinstance(obj, int) and (obj > 9007199254740991 or obj < -9007199254740991):
        return str(obj)
    return obj

_asset_version_cache = (0.0, "0")


def _asset_version():
    """Cache-busting token for the css/js tags.

    Derived from the newest mtime under static/ so an edited file is picked up on
    the next reload. The js tags previously carried no ?v= at all while the css did,
    so after a deploy a browser ran the old javascript against the new markup - the
    dashboard looked broken until someone hard-refreshed by hand.

    Cached for a few seconds: this walks the whole static tree, and on a deploy
    with a network-backed volume that is a few hundred stat() calls in the middle
    of serving the page.
    """
    global _asset_version_cache
    checked_at, value = _asset_version_cache
    now = time.time()
    if now - checked_at < 5.0:
        return value

    newest = 0.0
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    for root, _dirs, files in os.walk(static_dir):
        for name in files:
            if not name.endswith(('.js', '.css')):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, name)))
            except OSError:
                continue
    value = str(int(newest)) or "0"
    _asset_version_cache = (now, value)
    return value


@app.context_processor
def inject_asset_version():
    return {'asset_v': _asset_version(), 'csrf_token': csrf_token()}


@app.route('/healthz')
def healthz():
    """Liveness probe for the host's health check.

    No session, no template, no database, no auth - it answers if and only if the
    WSGI server is accepting requests, which is exactly what a health check should
    measure. /login was being used for this, and it renders a template and touches
    the signing key, so an unwritable config dir or a template error read as "the
    whole container is down" and the deploy was never promoted.
    """
    return jsonify({
        'ok': True,
        'accounts_running': len(state.bot_instances),
        'uptime': int(time.time() - state.stats.get('uptime_start', time.time())),
    })


@app.route('/')
@login_required
def home():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = client_ip()
        allowed, wait_time = check_rate_limit(ip)

        if not allowed:
             return jsonify({'success': False, 'error': f'Too many failed attempts. Try again in {wait_time}s'})

        data = _payload()
        identifier = data.get('username') or data.get('email') or ''
        password = data.get('password') or ''
        cfg = load_auth_config()

        # compare_digest so a wrong password cannot be found a character at a time
        if cfg and same_secret(identifier, cfg.get('username')) and same_secret(password, cfg.get('password')):
            session.clear()
            session['logged_in'] = True
            session['is_admin'] = True
            # so changing the dashboard password invalidates old admin cookies
            session['auth_fp'] = admin_fingerprint()
            session.permanent = True
            csrf_token()
            clear_rate_limit(ip)
            return jsonify({'success': True, 'is_admin': True})

        # not the admin - try an activated user account
        user, error = dash_users.authenticate(identifier, password)
        if user:
            session.clear()
            session['logged_in'] = True
            session['is_admin'] = False
            session['user_id'] = user['id']
            session['user_version'] = user.get('session_version', 0)
            session.permanent = True
            csrf_token()
            clear_rate_limit(ip)
            # a login is the first thing that needs the space to exist
            try:
                spaces.ensure_space(user['id'])
            except spaces.InvalidOwner:
                pass
            return jsonify({'success': True, 'is_admin': False, 'days_left': user.get('days_left')})

        fail_login(ip)
        if not cfg and not dash_users.list_users():
            return jsonify({'success': False, 'error': 'Auth config missing'})
        return jsonify({'success': False, 'error': error or 'Invalid Credentials'})

    return render_template('login.html')


@app.route('/activate', methods=['GET', 'POST'])
def activate():
    """Redeem a one-time activation key and pick an email + password."""
    if request.method == 'GET':
        return render_template('activate.html')

    data = _payload()
    ip = client_ip()
    allowed, wait_time = check_rate_limit(ip)
    if not allowed:
        return jsonify({'success': False, 'error': f'Too many attempts. Try again in {wait_time}s'})

    key = data.get('key')
    email = data.get('email')
    password = data.get('password')
    confirm = data.get('confirm')

    if confirm is not None and confirm != password:
        return jsonify({'success': False, 'error': 'Passwords do not match'})

    user, error = dash_users.redeem_key(key, email, password)
    if error:
        fail_login(ip)
        return jsonify({'success': False, 'error': error})

    clear_rate_limit(ip)
    session.clear()
    session['logged_in'] = True
    session['is_admin'] = False
    session['user_id'] = user['id']
    session['user_version'] = user.get('session_version', 0)
    session.permanent = True
    csrf_token()
    state.log_command("SYS", f"Activation key redeemed by {user['email']} ({user['days']} days)", "success",
                      owner=spaces.ADMIN_SPACE)
    return jsonify({'success': True, 'days_left': user.get('days_left')})


@app.route('/api/session')
@login_required
def session_info():
    user = _current_user()
    owner = resolve_owner() or spaces.ADMIN_SPACE
    return jsonify({
        'is_admin': bool(session.get('is_admin')),
        'email': user.get('email') if user else None,
        'days_left': user.get('days_left') if user else None,
        'expires_at': user.get('expires_at') if user else None,
        # every non-GET /api/ call has to echo this back in X-CSRF-Token
        'csrf_token': csrf_token(),
        'space': owner,
        'space_label': 'operator' if owner == spaces.ADMIN_SPACE else (user.get('email') if user else owner),
        'limits': {
            'max_accounts': MAX_ACCOUNTS_PER_SPACE,
            'max_proxies': MAX_PROXIES_PER_SPACE,
        },
    })


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── dashboard snapshot cache ──────────────────────────────────────────────────
# Every open tab polls the account list and the account config every 5s. Each
# call walks every bot (and reads accounts.json), so a 500-account farm with a
# few tabs open spent most of its web workers rebuilding the same JSON - which
# is what made the dashboard stop answering at ~25 accounts. Build it at most
# once per TTL per space and hand the same payload to everyone.
_SNAPSHOT_TTL = 2.0
# /api/stats is polled once a second per open tab, so it gets a tighter TTL than
# the 5s-polled lists - long enough to collapse a burst of tabs onto one build,
# short enough that the panel still ticks every second.
_STATS_TTL = 1.0
_snapshots = {}
_snapshot_lock = threading.Lock()


def _snapshot(key, build, ttl=_SNAPSHOT_TTL):
    now = time.time()
    with _snapshot_lock:
        cached = _snapshots.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]
    value = build()
    with _snapshot_lock:
        _snapshots[key] = (time.time(), value)
    return value


def _invalidate_snapshots(owner=None):
    """Drop cached payloads so the next poll shows a just-made change."""
    with _snapshot_lock:
        if owner is None:
            _snapshots.clear()
        else:
            for key in [k for k in _snapshots if k[1] == owner]:
                _snapshots.pop(key, None)


@app.route('/api/accounts/list')
@space_required
def account_list():
    return jsonify(_snapshot(('accounts_list', g.owner), lambda: _build_account_list(g.owner)))


def _build_account_list(owner):
    # Always a JSON array. The frontend indexes .find()/.map() on this, so an
    # error dict here used to throw "find is not a function" and blank the whole
    # accounts panel whenever the session lapsed or a bot was mid-reconnect.
    accounts = []
    try:
        for bot in state.bots_for(owner):
            # A bot with no user is still connecting (or reconnecting after a
            # gateway drop). Keep it in the list as a connecting card instead of
            # hiding it - hiding it is what made accounts "disappear" and look
            # like they had stopped on their own.
            if not bot.user:
                accounts.append({
                    'id': getattr(bot, 'account_name', '') or '',
                    'username': getattr(bot, 'account_name', 'Connecting…') or 'Connecting…',
                    'avatar': None,
                    'paused': bot.paused,
                    'connecting': True,
                    'cash': 0,
                    'level': None,
                    'xp': None,
                    'xp_needed': None,
                    'level_source': None,
                    'session_total': 0,
                    'gems_used': 0,
                })
                continue
            if not bot.is_ready:
                # Logged in but cogs not yet armed - show it as connecting.
                accounts.append({
                    'id': str(bot.user.id),
                    'username': bot.username,
                    'avatar': str(bot.user.display_avatar.url) if bot.user.display_avatar else None,
                    'paused': bot.paused,
                    'connecting': True,
                    'cash': state.account_stats.get(str(bot.user.id), {}).get('current_cash', 0),
                    'level': state.account_stats.get(str(bot.user.id), {}).get('level'),
                    'xp': state.account_stats.get(str(bot.user.id), {}).get('xp'),
                    'xp_needed': state.account_stats.get(str(bot.user.id), {}).get('xp_needed'),
                    'level_source': state.account_stats.get(str(bot.user.id), {}).get('level_source'),
                    'session_total': 0,
                    'gems_used': state.account_stats.get(str(bot.user.id), {}).get('gems_used', 0),
                })
                continue
            uid = str(bot.user.id)
            st = state.account_stats.get(uid, {})
            session_total = st.get('session_hunt_count', 0) + st.get('session_battle_count', 0) + st.get('session_owo_count', 0) + st.get('session_other_count', 0)

            accounts.append({
                'id': uid,
                'username': bot.username,
                'avatar': str(bot.user.display_avatar.url) if bot.user.display_avatar else None,
                'paused': bot.paused,
                'connecting': False,
                'cash': st.get('current_cash', 0),
                'level': st.get('level'),
                'xp': st.get('xp'),
                'xp_needed': st.get('xp_needed'),
                'level_source': st.get('level_source'),
                'session_total': session_total,
                'gems_used': st.get('gems_used', 0)
            })
    except Exception as e:
        # Never let a stale bot.user / stats lookup turn this into a 500 dict.
        state.log_command("SYS", f"account_list error: {e}", "error", owner=owner)
    return accounts

def get_bot(account_id, owner=None):
    """Resolve an account id to a live bot inside one space.

    An explicit id that is not running returns None - falling back to the first
    instance would silently send commands to the wrong account. Resolution never
    leaves the caller's space, so a foreign discord id is indistinguishable from
    one that does not exist.
    """
    pool = state.bots_for(owner or current_owner())
    if not account_id:
        return pool[0] if pool else None
    for bot in pool:
        if bot.user and str(bot.user.id) == str(account_id):
            return bot
    return None


def owns_account(account_id, owner=None):
    """True when this space owns the discord account, running or not."""
    if not account_id:
        return True
    space = owner or current_owner()
    if get_bot(account_id, space):
        return True
    return spaces.owner_for_account(account_id) == space


@app.route('/api/stats')
@space_required
def stats():
    account_id = request.args.get('id')
    bot = get_bot(account_id, g.owner)
    # stats are served from account_stats even when the bot is offline, so the
    # ownership check cannot rely on a live instance
    if account_id and not owns_account(account_id, g.owner):
        return jsonify({})
    uid = str(account_id) if account_id else (str(bot.user.id) if bot and bot.user else None)

    if not uid:
        return jsonify({})

    # This is the heaviest read-only payload the dashboard has - the battle team,
    # the whole zoo, the quest card, every scheduled command and 200 log lines -
    # and several open tabs all poll it once a second. Building it at most once per
    # TTL per account is the same trick /api/accounts/list already uses. The owner
    # must stay at index 1 of the key: _invalidate_snapshots matches on k[1].
    return jsonify(_snapshot(('stats', g.owner, uid),
                             lambda: _build_stats(uid, bot), ttl=_STATS_TTL))


def _build_stats(uid, bot):
    st = state.account_stats.get(uid)
    if not st:
        if bot and bot.user:
             st = state.get_empty_stats()
             st['username'] = bot.username
             state.account_stats[uid] = st
        else:
             return {}
    
    uptime_start = st.get('uptime_start', time.time())
    elapsed = time.time() - uptime_start
    session_cmds = (
        st.get('session_hunt_count', 0) + 
        st.get('session_battle_count', 0) + 
        st.get('session_owo_count', 0) + 
        st.get('session_other_count', 0)
    )
    mins = elapsed / 60
    cpm = round(session_cmds / mins, 1) if mins > 0.1 else 0
    
    cph = 0
    history = st.get('cowoncy_history', [])
    if len(history) > 1:
        first = history[0]
        last = history[-1]
        time_diff_hrs = (last[0] - first[0]) / 3600
        cash_diff = last[1] - first[1]
        if time_diff_hrs > 0.01:
            cph = round(cash_diff / time_diff_hrs)

    is_active = bot and str(bot.user.id) == uid if bot and bot.user else False
    current_status = ("PAUSED" if bot.paused else "ONLINE") if is_active else "OFFLINE"

    # the battle team lives on the cog, not in stats, because it is rebuilt from
    # whatever owo last showed us rather than persisted
    team_info = {'slots': [], 'watching': False, 'owned': 0, 'zoo': []}
    if is_active:
        others = bot.get_cog('Others')
        if others:
            team_cfg = bot.config.get('commands', {}).get('team', {})
            team_info = {
                'slots': [
                    {'animal': animal, 'rarity': others.rarity_name(animal)}
                    for animal in (others.current_team or [])
                ],
                'watching': bool(team_cfg.get('enabled', True) and team_cfg.get('watch_zoo', True)),
                'owned': others.owned_count,
                'zoo': others.zoo_data,
            }

    response_data = {
        'uptime': utils.format_seconds(elapsed),
        'cash': st.get('current_cash', 0),
        'level': st.get('level'),
        'xp': st.get('xp'),
        'xp_needed': st.get('xp_needed'),
        'rank': st.get('rank'),
        # "image" means owo answered with a rendered card we cannot read - the UI shows
        # that instead of leaving a stale number looking freshly synced
        'level_source': st.get('level_source'),
        'level_card_url': st.get('level_card_url'),
        'last_level_update': st.get('last_level_update'),
        'team': team_info,
        # per-account ring buffer, not a filter over the shared 1000-entry deque:
        # that scan was O(1000) on every poll of every open account panel, and once
        # ~200 accounts shared those 1000 entries an account's own panel went blank
        # because its lines had already been pushed out by its neighbours'.
        'logs': state.logs_for_bot(uid, 200),
        'status': current_status,
        'security': {
             'captchas': st.get('captchas_solved', 0),
             'bans': st.get('bans_detected', 0),
             'warnings': st.get('warnings_detected', 0),
             'last_message': st.get('last_captcha_msg', '')
        },
        'analytics': {
            'cph': cph,
            'gems_used': st.get('gems_used', 0)
        },
        'bot': {
            'user_id': uid,
            'username': st.get('username', 'Unknown'),
            'channel_id': bot.channel_id if is_active else None,
            'paused': bot.paused if is_active else True,
            'throttled': (time.time() < bot.throttle_until) if is_active else False,
            'cooldown_remaining': 999999 if (is_active and bot.throttle_until == float('inf')) else (max(0, int(bot.throttle_until - time.time())) if is_active else 0),
            'cooldown_command': bot.last_sent_command if is_active else None
        },
        'chart_data': {
            'hunt': st.get('hunt_count', 0),
            'battle': st.get('battle_count', 0),
            'session_hunt': st.get('session_hunt_count', 0),
            'session_battle': st.get('session_battle_count', 0),
            'session_owo': st.get('session_owo_count', 0),
            'other': st.get('other_count', 0),
            'owo': st.get('owo_count', 0),
            'total': st.get('total_cmd_count', 0),
            'perf_bpm': cpm
        },
        'system': {
            'last_cash_update': st.get('last_cash_update', 0),
            'pending_commands': len(st.get('pending_commands', []))
        },
        'quest_data': st.get('quest_data', []),
        # owo now draws the quest rows into quest-rows.png, so the descriptions and the
        # N/M counters do not exist as text any more. quest_source says so honestly and
        # quest_card_url lets the panel show owo's own card instead of an empty list.
        'quest_source': st.get('quest_source'),
        'quest_card_url': st.get('quest_card_url'),
        'quest_seals': st.get('quest_seals'),
        'next_quest_timer': st.get('next_quest_timer'),
        # absolute unix seconds, so the panel counts down live instead of showing a
        # string that was already stale when owo printed it
        'next_quest_at': st.get('next_quest_at'),
        'cmd_states': {k: {**v, 'content': '[Dynamic function]' if callable(v.get('content')) else v.get('content')} for k, v in bot.cmd_states.items()} if bot else {},
        'gambling_stats': st.get('gambling_stats', {})
    }

    return response_data

@app.route('/api/stats/combined')
@space_required
def stats_combined():
    """Aggregate stats across every bot in the caller's space."""
    # Walks every bot three times and filters the shared log deque, on the same 1s
    # tick as /api/stats - so it gets the same per-space TTL rather than being
    # rebuilt once per open tab per second.
    return jsonify(_snapshot(('stats_combined', g.owner),
                             lambda: _build_stats_combined(g.owner), ttl=_STATS_TTL))


def _build_stats_combined(owner):
    bots = state.bots_for(owner)
    if not bots:
        return {}

    total_cash = 0
    total_hunt = 0
    total_battle = 0
    total_owo = 0
    total_other = 0
    total_cmd = 0
    total_captchas = 0
    total_bans = 0
    total_warnings = 0
    total_gems = 0
    session_hunt = 0
    session_battle = 0
    session_owo = 0
    session_other = 0
    earliest_uptime = time.time()
    all_logs = []
    online_count = 0
    paused_count = 0
    accounts_summary = []

    for bot in bots:
        uid = str(bot.user.id) if bot.user else None
        if not uid:
            continue
        st = state.account_stats.get(uid, {})
        total_cash += st.get('current_cash', 0)
        total_hunt += st.get('hunt_count', 0)
        total_battle += st.get('battle_count', 0)
        total_owo += st.get('owo_count', 0)
        total_other += st.get('other_count', 0)
        total_cmd += st.get('total_cmd_count', 0)
        total_captchas += st.get('captchas_solved', 0)
        total_bans += st.get('bans_detected', 0)
        total_warnings += st.get('warnings_detected', 0)
        total_gems += st.get('gems_used', 0)
        session_hunt += st.get('session_hunt_count', 0)
        session_battle += st.get('session_battle_count', 0)
        session_owo += st.get('session_owo_count', 0)
        session_other += st.get('session_other_count', 0)
        up = st.get('uptime_start', time.time())
        if up < earliest_uptime:
            earliest_uptime = up

        bot_status = "PAUSED" if bot.paused else "ONLINE"
        if bot.paused:
            paused_count += 1
        else:
            online_count += 1

        accounts_summary.append({
            'id': uid,
            'username': bot.username,
            'cash': st.get('current_cash', 0),
            'hunt': st.get('hunt_count', 0),
            'battle': st.get('battle_count', 0),
            'status': bot_status,
        })

    elapsed = time.time() - earliest_uptime
    session_cmds = session_hunt + session_battle + session_owo + session_other
    mins = elapsed / 60
    cpm = round(session_cmds / mins, 1) if mins > 0.1 else 0

    combined_status = "ONLINE" if online_count > 0 else ("PAUSED" if paused_count > 0 else "OFFLINE")

    # aggregate cowoncy history across all bots for CPH
    cph = 0
    all_histories = []
    for bot in bots:
        uid = str(bot.user.id) if bot.user else None
        if uid:
            st = state.account_stats.get(uid, {})
            all_histories.extend(st.get('cowoncy_history', []))
    if len(all_histories) > 1:
        all_histories.sort(key=lambda x: x[0])
        first = all_histories[0]
        last = all_histories[-1]
        time_diff_hrs = (last[0] - first[0]) / 3600
        if time_diff_hrs > 0.01:
            # sum cash diffs per account
            cph = round((total_cash - sum(
                state.account_stats.get(str(b.user.id), {}).get('start_cash', 0)
                for b in bots if b.user
            )) / max(time_diff_hrs, 0.01))

    # combined logs from all bots. The inner `any(...)` this replaces re-walked
    # every bot for every one of the 1000 deque entries - O(1000 x accounts) on a
    # route the dashboard polls every second.
    space_uids = {str(b.user.id) for b in bots if b.user}
    combined_logs = [l for l in state.command_logs
                     if str(l.get('bot_id')) in space_uids][:200]

    return {
        'uptime': utils.format_seconds(elapsed),
        'cash': total_cash,
        'status': combined_status,
        'accounts_online': online_count,
        'accounts_paused': paused_count,
        'accounts_total': len(accounts_summary),
        'accounts': accounts_summary,
        'logs': combined_logs,
        'security': {
            'captchas': total_captchas,
            'bans': total_bans,
            'warnings': total_warnings,
            'last_message': ''
        },
        'analytics': {
            'cph': cph,
            'gems_used': total_gems
        },
        'chart_data': {
            'hunt': total_hunt,
            'battle': total_battle,
            'session_hunt': session_hunt,
            'session_battle': session_battle,
            'session_owo': session_owo,
            'other': total_other,
            'owo': total_owo,
            'total': total_cmd,
            'perf_bpm': cpm
        },
        'bot': {
            'user_id': '__combined__',
            'username': f'All Accounts ({len(accounts_summary)})',
            'channel_id': None,
            'paused': online_count == 0,
            'throttled': False,
            'cooldown_remaining': 0,
            'cooldown_command': None
        },
        'level': None,
        'xp': None,
        'xp_needed': None,
        'rank': None,
        'level_source': None,
        'level_card_url': None,
        'last_level_update': None,
        'team': {'slots': [], 'watching': False, 'owned': 0, 'zoo': []},
        'system': {'last_cash_update': 0, 'pending_commands': 0},
        'quest_data': [],
        'quest_source': None,
        'quest_card_url': None,
        'quest_seals': None,
        'next_quest_timer': None,
        'next_quest_at': None,
        'cmd_states': {},
        'gambling_stats': {}
    }

@app.route('/api/debug')
@admin_required
def debug():
    return jsonify({
        'account_stats': state.account_stats,
        'bot_instances': len(state.bot_instances),
        'command_logs_count': len(state.command_logs),
        'full_history_count': len(state.full_session_history)
    })

@app.route('/api/debug/logfile')
@admin_required
def debug_logfile():
    """The on-disk log, which is the only copy that survives the process dying.

    `?previous=1` reads the run before this one - after a crash-restart that is
    the file with the answer in it, since the in-memory log view came back empty
    along with the farm.
    """
    from modules import log_file
    previous = request.args.get('previous') in ('1', 'true', 'yes')
    try:
        lines = int(request.args.get('lines', 300))
    except (TypeError, ValueError):
        lines = 300
    lines = max(1, min(5000, lines))
    return jsonify({'success': True, 'previous': previous,
                    'lines': log_file.tail(lines, previous=previous)})


@app.route('/api/debug_status')
@admin_required
def debug_status():
    res = []
    for bot in state.bot_instances:
        res.append({
            'username': bot.username,
            'id': str(bot.user.id) if bot.user else None,
            'ready': bot.is_ready,
            'cmd_count': len(bot.cmd_states),
            'cmds': list(bot.cmd_states.keys())
        })
    return jsonify(res)

@app.route('/api/history')
@space_required
def get_history():
    return jsonify(list(reversed(state.visible_logs(state.full_session_history, g.owner))))

@app.route('/api/history/analytics')
@space_required
def get_analytics():
    try:
        from utils import history_tracker
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')

        dat = history_tracker.get_analytics_data(start_date=start_date, end_date=end_date, owner=g.owner)
        dat['recent_logs'] = state.visible_logs(state.full_session_history, g.owner)[-500:]
        return jsonify(dat)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
@space_required
def settings():
    account_id = request.args.get('id')
    want_global = False

    # ?id= becomes a filename, so it is validated before it ever touches a path -
    # "../../auth" used to be an arbitrary json read/write
    if account_id:
        if not spaces.is_valid_discord_id(account_id):
            return jsonify({"status": "error", "message": "Invalid account id"}), 400
        if not owns_account(account_id, g.owner):
            return jsonify({"status": "error", "message": "Not your account"}), 403
        config_path = spaces.settings_path(g.owner, account_id)
    else:
        # a space's own default for every account in it. The shipped
        # config/settings.json is only reachable with ?global=true, admin only.
        want_global = request.args.get('global') == 'true'
        if want_global and not session.get('is_admin'):
            return jsonify({"status": "error", "message": "Admin only"}), 403
        config_path = (os.path.join(state.CONFIG_DIR, 'settings.json') if want_global
                       else spaces.settings_path(g.owner))

    if request.method == 'POST':
        new_config = _payload()
        if not new_config:
            # a null/empty body used to be written straight through, leaving
            # settings.json holding "null" and every account unable to boot
            return jsonify({"status": "error", "message": "settings body must be a JSON object"}), 400
        try:
            save_to_all = request.args.get('all_accounts') == 'true' or request.args.get('all') == 'true'

            if save_to_all:
                # "all" now means every account in the caller's own space, so a
                # normal user can finally configure their farm in one go
                targets = spaces.settings_files(g.owner)
                for file_path in targets:
                    with open(file_path, 'w') as f:
                        json.dump(new_config, f, indent=4)

                for bot in state.bots_for(g.owner):
                    asyncio.run_coroutine_threadsafe(bot.sync_settings(new_config), bot.loop)

                state.log_command("SYS", f"Settings updated for ALL accounts by {acting_label()}", "success",
                                  owner=g.owner)
            else:
                os.makedirs(os.path.dirname(config_path) or '.', exist_ok=True)
                with open(config_path, 'w') as f:
                    json.dump(new_config, f, indent=4)

                for bot in state.bots_for(g.owner):
                    if (not account_id) or (bot.user and str(bot.user.id) == str(account_id)):
                        asyncio.run_coroutine_threadsafe(bot.sync_settings(new_config), bot.loop)

                state.log_command("SYS", f"Settings updated for {'Account ' + account_id if account_id else 'Global'}", "success",
                                  owner=g.owner)

            return jsonify({"status": "success"})
        except Exception as e:
            state.log_command("ERROR", f"Failed to save settings: {e}", owner=g.owner)
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        try:
            if not want_global:
                return jsonify(protect_large_ints(_space_config(g.owner, account_id)))
            with open(config_path, encoding='utf-8') as f:
                return jsonify(protect_large_ints(json.load(f)))
        except (OSError, ValueError):
            return jsonify({})


def _frozen_field_changed(prior, account):
    """Which identity field an edit tried to change, or None.

    Token, name and channels are what a live NeuraBot was constructed from, so
    they may only move while the account is stopped. Everything else on the card
    (enabled, proxy, health) is either advisory or picked up on the next start.
    """
    if not prior:
        # no row on disk to compare against - a brand new account is not running
        return None
    if str(prior.get('name') or '') != str(account.get('name') or ''):
        return 'name'
    if str(prior.get('token') or '') != str(account.get('token') or ''):
        return 'token'
    if [str(c) for c in (prior.get('channels') or [])] != \
       [str(c) for c in (account.get('channels') or [])]:
        return 'channel ids'
    return None


@app.route('/api/accounts/config', methods=['GET', 'POST'])
@space_required
def accounts_config_api():
    from utils import proxy_manager
    from core import supervisor
    if request.method == 'POST':
        payload = _payload() or {}
        if isinstance(payload, list):
            accounts = payload
        elif 'accounts' in payload:
            accounts = payload.get('accounts')
        else:
            # A body with no 'accounts' key used to fall through as "save zero
            # accounts", and save_accounts would then wipe every account and
            # every stored token with no undo - so an empty or malformed request
            # destroyed the whole farm. Deleting the last account still works:
            # that sends an explicit {"accounts": []}.
            return jsonify({"status": "error",
                            "message": "Missing 'accounts' in request body"}), 400
        if not isinstance(accounts, list):
            return jsonify({"status": "error", "message": "accounts must be a list"}), 400
        if len(accounts) > MAX_ACCOUNTS_PER_SPACE:
            return jsonify({"status": "error",
                            "message": f"At most {MAX_ACCOUNTS_PER_SPACE} accounts per space"}), 400

        # The browser is never given the real tokens (see the GET below), so the
        # stored ones are the only copy. Anything the client left out is carried
        # over from disk rather than dropped.
        existing = proxy_manager.load_accounts(g.owner)
        by_name = {str(a.get('name')): a for a in existing}
        # A running account's identity is frozen. The live NeuraBot holds the token
        # it logged in with and re-reads accounts.json on every config change, so a
        # token/name/channel edit underneath it produced a bot whose config row no
        # longer describes it: flag_account wrote health to a name that had moved,
        # channels rotated mid-session, and a swapped token was farmed by the old
        # one until something happened to restart it. Stop the account, edit, start.
        running = set(supervisor.running_names(g.owner))

        # an account name reaches the admin's browser inside an account card, and
        # it is also matched against by supervisor - so keep it boring
        seen = set()
        submitted_prior_names = set()
        for account in accounts:
            if not isinstance(account, dict):
                return jsonify({"status": "error", "message": "Malformed account entry"}), 400
            name = str(account.get('name') or '').strip()
            if not proxy_manager.valid_account_name(name):
                return jsonify({"status": "error",
                                "message": f"Invalid account name: {name!r}"}), 400
            if name in seen:
                return jsonify({"status": "error", "message": f"Duplicate account name: {name}"}), 400
            seen.add(name)
            account['name'] = name
            pid = account.get('proxy_id')
            if pid and not proxy_manager.valid_proxy_id(pid):
                return jsonify({"status": "error", "message": f"Invalid proxy id: {pid!r}"}), 400

            # orig_name lets a rename still find its old row, so editing the name
            # of an account does not lose its token or its health history
            orig_name = str(account.pop('orig_name', '') or '')
            prior = by_name.get(orig_name) or by_name.get(name)
            prior_name = str(prior.get('name')) if prior else name
            submitted_prior_names.add(prior_name)
            # display-only fields the GET adds; they are not configuration
            for transient in ('token_masked', 'running', 'ready'):
                account.pop(transient, None)
            if not str(account.get('token') or '').strip():
                if not prior or not prior.get('token'):
                    return jsonify({"status": "error",
                                    "message": f"Token is required for {name!r}"}), 400
                account['token'] = prior['token']

            if prior_name in running:
                frozen = _frozen_field_changed(prior, account)
                if frozen:
                    return jsonify({"status": "error",
                                    "message": f"{prior_name} is running - stop it before "
                                               f"changing its {frozen}"}), 409

            if prior:
                for carried in ('status', 'status_reason', 'status_at', 'user_id'):
                    if carried not in account and carried in prior:
                        account[carried] = prior[carried]

        # Deleting a row out from under a live bot is the same problem, minus the
        # config row: the account would keep farming with nothing on disk to stop
        # it from, or report health into.
        orphaned = sorted(running - submitted_prior_names)
        if orphaned:
            return jsonify({"status": "error",
                            "message": f"{', '.join(orphaned)} is running - stop it before "
                                       f"removing it"}), 409

        try:
            proxy_manager.save_accounts(g.owner, accounts)
            proxy_manager.sync_proxy_assignments(g.owner)
            for bot in state.bots_for(g.owner):
                bot.accounts = accounts
            # An account removed from the list is gone for good, so drop its log ring
            # buffer. Renames keep theirs: user_id is carried over from the prior row
            # above, so it is still in `kept`. Stopping an account deliberately does
            # not come through here - its history stays readable while it is down.
            kept = {str(a.get('user_id')) for a in accounts if a.get('user_id')}
            for prior in existing:
                uid = str(prior.get('user_id') or '')
                if uid and uid not in kept:
                    state.forget_bot_logs(uid)
            state.log_command("SYS", f"Accounts config updated by {acting_label()}.", "success", owner=g.owner)
            _invalidate_snapshots(g.owner)
            return jsonify({"status": "success"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    return jsonify(_snapshot(('accounts_config', g.owner),
                             lambda: _build_accounts_config(g.owner)))


def _build_accounts_config(owner):
    from utils import proxy_manager
    from core import supervisor
    try:
        accounts = proxy_manager.load_accounts(owner)
        running = supervisor.running_states(owner)
        out = []
        for acc in accounts:
            # A Discord user token is full account access. The accounts page only
            # ever renders the masked form, so the real one never leaves the
            # server - not into the DOM, not into a browser cache, not into an
            # extension reading XHR bodies. POST above fills it back in.
            safe = {k: v for k, v in acc.items() if k != 'token'}
            if acc.get('token'):
                safe['token_masked'] = proxy_manager.mask_token(acc['token'])
            safe['running'] = acc.get('name') in running
            # False while the instance exists but has not logged in yet
            safe['ready'] = bool(running.get(acc.get('name')))
            out.append(safe)
        return {'accounts': out}
    except Exception:
        return {'accounts': []}


def _bot_loop_call(coro, timeout=60):
    """Run a coroutine on the bot's event loop and wait for its result."""
    from core import supervisor
    loop = supervisor.get_loop()
    if loop is None:
        coro.close()
        return None, 'bot loop is not running'
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout), None
    except FuturesTimeout:
        # Cancel it. Without this the coroutine kept running after the browser had
        # been told the call failed, so Start reported an error and the account
        # came up anyway a moment later - which read as "it stops accounts, then
        # they start again on their own".
        future.cancel()
        return None, f'timed out after {timeout}s - cancelled'
    except Exception as e:
        return None, str(e)


def _bot_loop_fire(coro):
    """Schedule a coroutine on the bot's event loop without waiting for it."""
    from core import supervisor
    loop = supervisor.get_loop()
    if loop is None:
        coro.close()
        return 'bot loop is not running'
    asyncio.run_coroutine_threadsafe(coro, loop)
    return None


def _find_account(accounts, name):
    return next((a for a in accounts if a.get('name') == name), None)


@app.route('/api/accounts/launch', methods=['POST'])
@space_required
def account_launch():
    from utils import proxy_manager
    from core import supervisor
    name = _payload().get('name')
    # the lookup is inside the caller's space, so one tenant cannot start another
    # tenant's account by guessing its name
    account = _find_account(proxy_manager.load_accounts(g.owner), name)
    if not account:
        return jsonify({'success': False, 'error': f'No account named {name}'}), 404

    result, error = _bot_loop_call(supervisor.start_account(account, g.owner))
    if error:
        return jsonify({'success': False, 'error': error}), 503
    ok, message = result
    state.log_command("SYS", message, "success" if ok else "error", owner=g.owner)
    _invalidate_snapshots(g.owner)
    return jsonify({'success': ok, 'message': message})


@app.route('/api/accounts/stop', methods=['POST'])
@space_required
def account_stop():
    from core import supervisor
    name = _payload().get('name')
    # Nothing to persist: "stay stopped" is now the only behaviour there is. The
    # process never starts an account by itself, so a stop lasts until someone
    # clicks Start again.
    result, error = _bot_loop_call(supervisor.stop_account(g.owner, name))
    if error:
        return jsonify({'success': False, 'error': error}), 503
    ok, message = result
    state.log_command("SYS", message, "success" if ok else "error", owner=g.owner)
    _invalidate_snapshots(g.owner)
    return jsonify({'success': ok, 'message': message})


# Start All is a queue, never a fan-out. The route returns as soon as the queue is
# armed - one account every LAZYFARMERS_START_GAP_S seconds is minutes of work for
# a real farm, and holding a request open that long just means a proxy timeout
# halfway through with no idea what got started.
@app.route('/api/accounts/launch_all', methods=['POST'])
@space_required
def account_launch_all():
    from utils import proxy_manager
    accounts = [a for a in proxy_manager.load_accounts(g.owner) if a.get('enabled', True)]
    if not accounts:
        return jsonify({'success': False, 'error': 'No enabled accounts to start'}), 400

    # start_sequence only arms a task; it does not await the accounts, so it is
    # safe to call it and return. It refuses a second arming while one is live.
    result, error = _bot_loop_call(_arm_sequence(g.owner, accounts))
    if error:
        return jsonify({'success': False, 'error': error}), 503
    ok, message = result
    state.log_command("SYS", message, "success" if ok else "error", owner=g.owner)
    if not ok:
        return jsonify({'success': False, 'error': message}), 409
    return jsonify({'success': True, 'message': message})


async def _arm_sequence(owner, accounts):
    """start_sequence is sync but must run on the loop - it creates a task."""
    from core import supervisor
    return supervisor.start_sequence(owner, accounts)


async def _cancel_sequence(owner):
    from core import supervisor
    return supervisor.cancel_sequence(owner)


@app.route('/api/accounts/launch_all', methods=['GET'])
@space_required
def account_launch_all_status():
    from core import supervisor
    return jsonify({'success': True, 'progress': supervisor.sequence_status(g.owner)})


@app.route('/api/accounts/launch_all/cancel', methods=['POST'])
@space_required
def account_launch_all_cancel():
    # task.cancel() is not thread-safe, and this handler is on a waitress thread
    result, error = _bot_loop_call(_cancel_sequence(g.owner), timeout=15)
    if error:
        return jsonify({'success': False, 'error': error}), 503
    cancelled = result
    if cancelled:
        state.log_command("SYS", "Start-all queue cancelled - already started "
                                 "accounts keep running", "info", owner=g.owner)
    return jsonify({'success': True, 'cancelled': cancelled})


@app.route('/api/accounts/stop_all', methods=['POST'])
@space_required
def account_stop_all():
    # Stopping may go wide: no logins, no cog loading, nothing that made the mass
    # *start* dangerous. It also cancels a start queue that is still feeding.
    from core import supervisor
    result, error = _bot_loop_call(supervisor.stop_all(g.owner), timeout=180)
    if error:
        return jsonify({'success': False, 'error': error}), 503
    stopped = sum(1 for ok, _ in result if ok)
    state.log_command("SYS", f"Stopped {stopped} account(s)", "success", owner=g.owner)
    _invalidate_snapshots(g.owner)
    return jsonify({'success': True, 'message': f'Stopped {stopped} account(s)'})


async def _verify_accounts(owner, accounts, targets):
    from lazy_engines.setup_engine import LazySetupEngine
    from core import supervisor
    from utils import proxy_manager
    engine = LazySetupEngine()
    results = []
    channels_changed = False
    # Verify is read-only for a running account. It normally writes back the
    # channels it found reachable, and doing that under a live bot is the same
    # edit the config route refuses: the bot re-reads accounts.json and rotates
    # itself onto a channel nobody chose, mid-session.
    running = set(supervisor.running_names(owner))

    for account in targets:
        proxy_url, proxy_auth, _label = proxy_manager.resolve_account_proxy(owner, account)
        try:
            valid, user, channels = await engine.verify_token(
                account.get('token'), account.get('channels', []), proxy_url, proxy_auth
            )
        except Exception as e:
            valid, user, channels = False, str(e), []

        locked = account.get('name') in running
        if valid and channels and channels != account.get('channels') and not locked:
            account['channels'] = channels
            channels_changed = True

        results.append({
            'name': account.get('name'),
            'valid': valid,
            'user': user,
            'channels': channels,
        })

    if channels_changed:
        proxy_manager.save_accounts(owner, accounts)
    return results


@app.route('/api/accounts/verify', methods=['POST'])
@space_required
def account_verify():
    from utils import proxy_manager
    payload = _payload()
    names = payload.get('names')
    accounts = proxy_manager.load_accounts(g.owner)

    if names:
        targets = [a for a in accounts if a.get('name') in names]
    else:
        targets = [a for a in accounts if a.get('enabled', True)]
    if not targets:
        return jsonify({'success': False, 'error': 'No accounts to verify'})

    results, error = _bot_loop_call(_verify_accounts(g.owner, accounts, targets),
                                    timeout=40 * len(targets) + 20)
    if error:
        return jsonify({'success': False, 'error': error}), 503

    passed = sum(1 for r in results if r['valid'])
    state.log_command("SYS", f"Verified {passed}/{len(results)} accounts", "success" if passed else "error",
                      owner=g.owner)
    return jsonify({'success': True, 'results': results})


@app.route('/api/accounts/export')
@space_required
def account_export():
    from utils import proxy_manager
    only_problem = request.args.get('only') == 'problem'
    # raw tokens leave the box here, so it is strictly the caller's own space
    accounts = proxy_manager.load_accounts(g.owner)
    if only_problem:
        accounts = [a for a in accounts if a.get('status', 'ok') != 'ok']

    lines = []
    for account in accounts:
        parts = [str(account.get('name', 'unnamed')), str(account.get('token', ''))]
        if account.get('status', 'ok') != 'ok':
            parts.append(f"{account.get('status')} - {account.get('status_reason') or ''}".strip())
        lines.append(':'.join(parts))

    body = '\n'.join(lines) + ('\n' if lines else '')
    name = 'problem-accounts.txt' if only_problem else 'accounts.txt'
    state.log_command("SYS", f"Accounts exported by {acting_label()}", "info", owner=g.owner)
    return app.response_class(
        body,
        mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename={name}'},
    )


@app.route('/api/accounts/bulk', methods=['POST'])
@space_required
def account_bulk_import():
    from utils import proxy_manager
    payload = _payload()
    tokens = [t.strip().strip('"\'') for t in (payload.get('tokens') or '').splitlines() if t.strip()]
    channels = [c for c in (payload.get('channels') or '').split() if c]
    proxy_id = payload.get('proxy_id') or None
    prefix = (payload.get('prefix') or 'acc').strip() or 'acc'

    if not tokens:
        return jsonify({'success': False, 'error': 'Paste at least one token'})
    if proxy_id and not proxy_manager.valid_proxy_id(proxy_id):
        return jsonify({'success': False, 'error': 'Invalid proxy id'}), 400
    # the prefix ends up in an account name, which ends up in a rendered card
    if not proxy_manager.valid_account_name(prefix):
        return jsonify({'success': False, 'error': 'Prefix may only use letters, digits, _ . -'}), 400

    accounts = proxy_manager.load_accounts(g.owner)
    if len(accounts) + len(tokens) > MAX_ACCOUNTS_PER_SPACE:
        return jsonify({'success': False,
                        'error': f'That would exceed the {MAX_ACCOUNTS_PER_SPACE} account limit'}), 400

    used_names = {a.get('name') for a in accounts}
    # A token already in the space is the same Discord account. Importing it again
    # only creates a second row that can never be started (the supervisor refuses
    # a token that is already live) while looking like a real account on the page.
    known_tokens = {str(a.get('token') or '').strip() for a in accounts if a.get('token')}
    counter = 1
    imported = 0
    skipped = 0
    for token in tokens:
        if token in known_tokens:
            skipped += 1
            continue
        known_tokens.add(token)
        while f"{prefix}{counter}" in used_names:
            counter += 1
        name = f"{prefix}{counter}"
        used_names.add(name)
        accounts.append({
            'name': name,
            'token': token,
            'channels': list(channels),
            'enabled': True,
            'proxy_id': proxy_id,
        })
        imported += 1

    if not imported:
        return jsonify({'success': False,
                        'error': f'Every token pasted is already in this space ({skipped} duplicates)'})

    proxy_manager.save_accounts(g.owner, accounts)
    proxy_manager.sync_proxy_assignments(g.owner)
    state.log_command("SYS", f"Imported {imported} accounts ({acting_label()})", "success", owner=g.owner)
    _invalidate_snapshots(g.owner)
    message = f'Imported {imported} accounts'
    if skipped:
        message += f', skipped {skipped} already in this space'
    return jsonify({'success': True, 'message': message})


@app.route('/api/proxies', methods=['GET', 'POST'])
@space_required
def proxies_api():
    from utils import proxy_manager
    if request.method == 'POST':
        payload = _payload()
        proxies = payload.get('proxies', [])
        if not isinstance(proxies, list):
            return jsonify({"status": "error", "message": "proxies must be a list"}), 400
        if len(proxies) > MAX_PROXIES_PER_SPACE:
            return jsonify({"status": "error",
                            "message": f"At most {MAX_PROXIES_PER_SPACE} proxies per space"}), 400
        proxy_manager.save_proxies(g.owner, proxies)
        proxy_manager.sync_proxy_assignments(g.owner)
        state.log_command("SYS", "Proxy pool saved", "success", owner=g.owner)
        return jsonify({"status": "success", "proxies": proxy_manager.load_proxies(g.owner)})
    return jsonify({"proxies": proxy_manager.load_proxies(g.owner)})


@app.route('/api/proxies/bulk', methods=['POST'])
@space_required
def proxies_bulk():
    from utils import proxy_manager
    text = _payload().get('text', '')
    existing = len(proxy_manager.load_proxies(g.owner))
    if existing >= MAX_PROXIES_PER_SPACE:
        return jsonify({"status": "error",
                        "message": f"Proxy pool is already at the {MAX_PROXIES_PER_SPACE} limit"}), 400
    result = proxy_manager.bulk_import(g.owner, text, limit=MAX_PROXIES_PER_SPACE)
    state.log_command("SYS", f"Bulk imported {len(result['added'])} proxies", "success", owner=g.owner)
    return jsonify({
        "status": "success",
        "added": len(result['added']),
        "errors": result['errors'],
        "proxies": proxy_manager.load_proxies(g.owner),
    })


@app.route('/api/proxies/test', methods=['POST'])
@space_required
def proxies_test():
    from utils import proxy_manager
    payload = _payload()
    proxy_id = payload.get('id')
    owner = g.owner
    if proxy_id and not proxy_manager.valid_proxy_id(proxy_id):
        return jsonify({"status": "error", "message": "Invalid proxy id"}), 400

    async def _run():
        if proxy_id:
            proxy = proxy_manager.get_proxy_by_id(owner, proxy_id)
            if not proxy:
                return {"ok": False, "error": "not found"}
            ok = await proxy_manager.test_proxy(proxy)
            proxies = proxy_manager.load_proxies(owner)
            for p in proxies:
                if p.get('id') == proxy_id:
                    p['status'] = proxy['status']
                    p['last_check'] = proxy['last_check']
            proxy_manager.save_proxies(owner, proxies)
            return {"ok": ok, "id": proxy_id, "status": proxy['status']}
        results = await proxy_manager.test_all_proxies(owner)
        return {"results": results, "proxies": proxy_manager.load_proxies(owner)}

    # Must run on the bot's loop, like every other awaitable this file touches.
    # asyncio.run() spins up a *second* loop on the waitress thread, and
    # test_all_proxies builds aiohttp connectors - objects that bind themselves to
    # the loop that created them. On the throwaway loop they are torn down with it,
    # so a proxy that tested fine here could still fail when a bot used it, and a
    # 200-proxy "Test All" pinned one of the sixteen web threads for the whole run.
    timeout = 30 if proxy_id else max(60, 2 * len(proxy_manager.load_proxies(owner)))
    result, error = _bot_loop_call(_run(), timeout=timeout)
    if error:
        return jsonify({"status": "error", "message": error}), 503
    return jsonify({"status": "success", **result})


@app.route('/api/proxies/assign', methods=['POST'])
@space_required
def proxies_assign():
    from utils import proxy_manager
    assigned = proxy_manager.auto_assign(g.owner)
    state.log_command("SYS", f"Auto-assigned {len(assigned)} proxies to accounts", "success", owner=g.owner)
    return jsonify({"status": "success", "assigned": assigned, "proxies": proxy_manager.load_proxies(g.owner)})


@app.route('/api/proxies/<proxy_id>', methods=['DELETE'])
@space_required
def proxies_delete(proxy_id):
    from utils import proxy_manager
    if not proxy_manager.valid_proxy_id(proxy_id):
        return jsonify({"status": "error", "message": "Invalid proxy id"}), 400
    proxy_manager.remove_proxy(g.owner, proxy_id)
    state.log_command("SYS", f"Removed proxy {proxy_id}", "info", owner=g.owner)
    return jsonify({"status": "success", "proxies": proxy_manager.load_proxies(g.owner)})


@app.route('/api/proxies/all', methods=['DELETE'])
@space_required
def proxies_delete_all():
    from utils import proxy_manager
    proxy_manager.remove_all_proxies(g.owner)
    state.log_command("SYS", "Deleted ALL proxies", "info", owner=g.owner)
    return jsonify({"status": "success", "proxies": []})


@app.route('/api/proxies/failed', methods=['DELETE'])
@space_required
def proxies_delete_failed():
    from utils import proxy_manager
    count = proxy_manager.remove_failed_proxies(g.owner)
    state.log_command("SYS", f"Deleted {count} failed proxies", "info", owner=g.owner)
    return jsonify({"status": "success", "count": count, "proxies": proxy_manager.load_proxies(g.owner)})


@app.route('/api/security/summary')
@space_required
def security_summary():
    """One row per account for the Security tab's card grid.

    The tab used to build this client-side by calling /api/stats once per account,
    serially, from the 1s dashboard tick. At 200 accounts that is 200 requests a
    second against 16 web workers, each one serialising the full stats payload
    (team + zoo + quest card + cmd_states + 200 log lines) to read three integers -
    which is what saturated waitress and made the whole site, static files
    included, stop answering. This is the same three integers for every account in
    one cached response.
    """
    return jsonify({
        'success': True,
        'accounts': _snapshot(('security_summary', g.owner),
                              lambda: _build_security_summary(g.owner)),
    })


def _build_security_summary(owner):
    rows = []
    for bot in state.bots_for(owner):
        if not bot.user:
            # still connecting - it has no stats row yet, and the card grid keys
            # off the account list anyway
            continue
        uid = str(bot.user.id)
        st = state.account_stats.get(uid, {})
        rows.append({
            'id': uid,
            'username': bot.username,
            'avatar': str(bot.user.display_avatar.url) if bot.user.display_avatar else None,
            'status': 'PAUSED' if bot.paused else 'ONLINE',
            'captchas': st.get('captchas_solved', 0),
            'bans': st.get('bans_detected', 0),
            'warnings': st.get('warnings_detected', 0),
        })
    return rows


@app.route('/api/security/test', methods=['POST'])
@space_required
def test_security():
    account_id = request.args.get('id')
    bot = get_bot(account_id, g.owner)
    if not bot:
        return jsonify({'status': 'error', 'message': 'Bot not found'}), 404

    sec = bot.get_cog('Security')
    if sec:
        asyncio.run_coroutine_threadsafe(sec.play_beep(), bot.loop)
        sec._show_desktop_notification("Test: Lazy Farmers Security Alert working!")
        sec._send_webhook("SYSTEM TEST", "This is a test of your security notification system. All systems are operational.")
        return jsonify({'status': 'success', 'message': 'Test signals sent'})

    return jsonify({'status': 'error', 'message': 'Security module not loaded'}), 500

@app.route('/api/control', methods=['POST'])
@space_required
def control():
    data = _payload()
    action = (data.get('action') or '').lower()
    account_id = data.get('id')
    bot = get_bot(account_id, g.owner)

    if not bot: return jsonify({'success': False, 'error': 'Bot not found'})

    # the mobile action bar sends pause/resume/checkcash, the desktop one
    # stop/start/cash - both mean the same thing
    if action in ('stop', 'pause'):
        bot.paused = True
        bot.log("SYS", "Bot STOPPED via Dashboard")

    elif action in ('start', 'resume'):
        bot.paused = False
        bot.throttle_until = 0
        bot.log("SYS", "Bot RESUMED via Dashboard")

    elif action in ('cash', 'checkcash'):
        asyncio.run_coroutine_threadsafe(
            bot.send_message(f"{bot.prefix}cash", skip_typing=True, priority=True),
            bot.loop
        )
        state.log_command("CMD", "Manual Cash Check Sent", "info", bot_name=bot.username, owner=g.owner)

    else:
        return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400

    return jsonify({'success': True})

@app.route('/api/security', methods=['POST'])
@space_required
def security():
    data = _payload()
    action = data.get('action')
    account_id = data.get('id')
    bot = get_bot(account_id, g.owner)

    if not bot: return jsonify({'success': False, 'error': 'Bot not found'})

    if action == 'resume':
        bot.paused = False
        bot.throttle_until = 0
        state.log_command("SEC", f"User Resumed {bot.username} from Security Alert", "success", owner=g.owner)

    return jsonify({'success': True})

@app.route('/api/captcha/current')
@space_required
def captcha_current():
    account_id = request.args.get('id')
    bot = get_bot(account_id, g.owner)
    if not bot: return jsonify({'success': False})

    st = bot.stats
    captcha_data = st.get('current_captcha')
    
    if captcha_data and captcha_data.get('image_url'):
        timestamp = captcha_data.get('timestamp', 0)
        if time.time() - timestamp < 600:
            return jsonify({
                'success': True,
                'url': captcha_data['image_url'],
                'cash': captcha_data.get('cash', 16000),
                'command': captcha_data.get('command_template', 'owo autohunt {cash} {password}'),
                'age_seconds': int(time.time() - timestamp)
            })
        else:
            if 'current_captcha' in st:
                del st['current_captcha']
    
    return jsonify({'success': False, 'message': 'No active captcha'})

@app.route('/api/captcha/submit', methods=['POST'])
@space_required
def captcha_submit():
    data = _payload()
    # a JSON null here (rather than a missing key) used to reach .strip() on None
    code = str(data.get('code') or '').strip()
    account_id = data.get('id')
    bot = get_bot(account_id, g.owner)

    if not bot: return jsonify({'success': False, 'error': 'Bot not found'})

    if not code:
        return jsonify({'success': False, 'error': 'No password provided'})

    st = bot.stats
    captcha_data = st.get('current_captcha')
    if not captcha_data:
        return jsonify({'success': False, 'error': 'No active captcha'})

    cash = captcha_data.get('cash', 16000)
    command_template = captcha_data.get('command_template', f"owo autohunt {cash} {{password}}")
    full_command = command_template.replace('{password}', code)

    asyncio.run_coroutine_threadsafe(
        bot.send_message(full_command, skip_typing=True, priority=True),
        bot.loop
    )

    if 'current_captcha' in st:
        del st['current_captcha']

    st['captchas_solved_today'] = st.get('captchas_solved_today', 0) + 1
    st['captcha_success_count'] = st.get('captcha_success_count', 0) + 1
    state.log_command("CMD", f"Captcha solution sent: {full_command}", bot_name=bot.username, owner=g.owner)

    return jsonify({'success': True, 'message': f'Captcha solution sent: {full_command}'})

@app.route('/api/captcha/balance', methods=['GET', 'POST'])
@space_required
def captcha_balance():
    account_id = request.args.get('id')
    bot = get_bot(account_id, g.owner)
    if not bot:
        return jsonify({'balance': None, 'service': 'unknown', 'error': 'Bot not found'})

    cfg = bot.config.get('security', {}).get('captcha_solver', {})
    service = cfg.get('service', 'yescaptcha')
    api_key = ''

    if request.method == 'POST':
        data = _payload()
        if 'service' in data:
            service = data['service']
        if 'api_key' in data:
            api_key = data['api_key']

    if not api_key:
        if service == 'nopecha':
            api_key = cfg.get('nopecha_api_key', cfg.get('api_key', ''))
        elif service == 'anticaptcha':
            api_key = cfg.get('anticaptcha_api_key', cfg.get('api_key', ''))
        elif service == 'captchaly':
            api_key = cfg.get('captchaly_api_key', cfg.get('api_key', ''))
        else:
            api_key = cfg.get('yescaptcha_api_key', cfg.get('api_key', ''))

    # A NopeCHA booster key has no API balance to read - api.nopecha.com refuses the
    # key outright - so asking for one would paint a red "balance unreadable" badge
    # over a setup that is working. Report what the extension cache holds instead.
    if service == 'nopecha' and not str(api_key or '').strip():
        return jsonify(_nopecha_extension_status(cfg))

    temp_solver = None
    if service == 'nopecha':
        from modules.services.nopecha import NopeCaptchaService
        temp_solver = NopeCaptchaService(bot, api_key, "")
    elif service == 'anticaptcha':
        from modules.services.anticaptcha import AntiCaptchaService
        temp_solver = AntiCaptchaService(bot, api_key, "")
    elif service == 'captchaly':
        from modules.services.captchaly import CaptchalyService
        temp_solver = CaptchalyService(bot, api_key, "")
    else:
        from modules.services.yescaptcha import YesCaptchaService
        temp_solver = YesCaptchaService(bot, api_key, "")

    try:
        future = asyncio.run_coroutine_threadsafe(temp_solver.get_balance(), bot.loop)
        balance = future.result(timeout=10)
        # the services return -1 for "could not read it", which is not a balance
        if balance is None or balance < 0:
            return jsonify({'balance': None, 'service': service,
                            'enabled': cfg.get('enabled', False),
                            'error': 'balance unreadable - check the key and the service'})
        return jsonify({'balance': balance, 'service': service, 'enabled': cfg.get('enabled', False)})
    except Exception as e:
        return jsonify({'balance': None, 'service': service, 'error': str(e)})


def _nopecha_extension_status(cfg):
    """What the NopeCHA extension path can report in place of an API balance."""
    from modules import nopecha_extension
    nope = (cfg.get('browser_solver') or {}).get('nopecha') or {}
    key = nopecha_extension.resolve_key(cfg)
    body = {
        'balance': None,
        'service': 'nopecha',
        'mode': 'extension',
        'enabled': cfg.get('enabled', False),
        'extension_enabled': bool(nope.get('enabled', True)),
        'has_key': bool(key),
    }
    if not key:
        body['error'] = ('no NopeCHA key set - paste your booster key into the '
                         'booster key field')
        return body
    try:
        body['extension'] = nopecha_extension.cached_info(key)
    except Exception as exc:
        body['extension'] = {'installed': False}
        body['error'] = f'could not read the extension cache: {exc}'
    return body


@app.route('/api/captcha/nopecha/install', methods=['POST'])
@space_required
def captcha_nopecha_install():
    """Download and patch the NopeCHA extension now, instead of at the first captcha.

    Worth its own button: the first solve after configuring a booster key otherwise
    spends its opening seconds on a GitHub download, and any problem with the key or
    the release shows up only in the log of an account that is already in trouble.
    """
    data = _payload()
    account_id = data.get('id')
    bot = get_bot(account_id, g.owner)
    if bot:
        cfg = bot.config.get('security', {}).get('captcha_solver', {}) or {}
    else:
        # no account picked (or not one of ours): the cache is process-wide, so a
        # config-only install is still meaningful - read the space's settings instead
        cfg = ((_space_config(g.owner).get('security') or {}).get('captcha_solver') or {})

    from core import supervisor
    from modules import nopecha_extension
    key = nopecha_extension.resolve_key(cfg)
    if not key:
        return jsonify({'success': False, 'error': 'no NopeCHA key set - paste your '
                                                   'booster key in first'})
    nope = (cfg.get('browser_solver') or {}).get('nopecha') or {}
    loop = supervisor.get_loop()
    if not loop:
        return jsonify({'success': False, 'error': 'the bot loop is not running yet - '
                                                   'start an account first'})

    log = bot.log if bot else (lambda kind, msg: state.log_command(kind, msg, owner=g.owner))
    try:
        future = asyncio.run_coroutine_threadsafe(
            nopecha_extension.ensure_extension(
                key,
                path_hint=nope.get('extension_path'),
                auto_download=bool(nope.get('auto_download', True)),
                log=log,
            ),
            loop,
        )
        path, error = future.result(timeout=240)
    except Exception as exc:
        return jsonify({'success': False, 'error': f'{type(exc).__name__}: {exc}'})

    if error:
        return jsonify({'success': False, 'error': error,
                        'extension': nopecha_extension.cached_info(key)})
    info = nopecha_extension.cached_info(key)
    return jsonify({'success': True, 'path': path, 'extension': info,
                    'message': f"NopeCHA extension ready (v{info.get('version') or '?'})"})


@app.route('/api/captcha/stats')
@space_required
def captcha_stats():
    account_id = request.args.get('id')
    bot = get_bot(account_id, g.owner)
    st = bot.stats if bot else {}

    solved = st.get('captchas_solved_today', 0)
    success = st.get('captcha_success_count', 0)
    success_rate = 100 if solved == 0 else round((success / max(solved, 1)) * 100)

    return jsonify({
        'solved': solved,
        'success_rate': success_rate
    })

@app.route('/api/bot/command', methods=['POST'])
@space_required
def bot_command():
    data = _payload()
    command = (data.get('command') or '').strip()
    account_id = data.get('id')
    send_to_all = bool(data.get('all'))

    if not command:
        return jsonify({'success': False, 'error': 'No command provided'})

    def _dispatch(bot):
        # go through the cog when it is loaded so the "owo " prefix rule lives
        # in exactly one place; fall back to a raw send if it is not
        cog = bot.get_cog('CustomCommands')
        coro = cog.run_now(command) if cog else bot.send_message(
            command, skip_typing=True, priority=True)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    if send_to_all:
        # "all" is all of the caller's own accounts, never the whole process
        targets = [b for b in state.bots_for(g.owner) if b.user and b.is_ready]
        if not targets:
            return jsonify({'success': False, 'error': 'No accounts are running'})
        for bot in targets:
            _dispatch(bot)
            state.log_command("CMD", f"Manual command sent: {command}", bot_name=bot.username, owner=g.owner)
        return jsonify({
            'success': True,
            'message': f'Sent "{command}" on {len(targets)} accounts',
            'count': len(targets),
        })

    bot = get_bot(account_id, g.owner)
    if not bot: return jsonify({'success': False, 'error': 'Bot not found'})

    _dispatch(bot)
    state.log_command("CMD", f"Manual command sent: {command}", bot_name=bot.username, owner=g.owner)
    return jsonify({'success': True, 'message': f'Command sent: {command}'})


def _settings_path_for(owner, account_id):
    """A settings file inside one space. Validates the id first - it is a filename."""
    if account_id:
        return spaces.settings_path(owner, account_id)
    return spaces.settings_path(owner)


def _read_settings_for(owner, account_id):
    candidates = [_settings_path_for(owner, account_id)]
    if account_id:
        candidates.append(spaces.settings_path(owner))
    candidates.append(os.path.join(state.CONFIG_DIR, 'settings.json'))
    for path in candidates:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _merge_dicts(base, override):
    """Deep-merge ``override`` into ``base`` in place (same rule as NeuraBot)."""
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_dicts(base[key], value)
        else:
            base[key] = value
    return base


def _space_config(owner, account_id=None):
    """The config a bot in this space would see, layered the same way it layers it.

    ``_read_settings_for`` returns the *first* readable file, which is right for
    handing a whole document back to the config editor but wrong for asking one
    question of it: a space file that omits a section would answer "not configured"
    about a section the shipped defaults do define. Routes with no bot in hand (a
    process-wide cache, or nothing started yet) need the merged view instead.
    """
    merged = {}
    paths = [os.path.join(state.CONFIG_DIR, 'settings.json'), spaces.settings_path(owner)]
    if account_id:
        paths.append(spaces.settings_path(owner, account_id))
    for path in paths:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict):
            _merge_dicts(merged, data)
    return merged


EARNING_DEFAULTS = {'enabled': False, 'exclusive': True, 'sell_interval_min': 20,
                    'sell_type': 'all', 'huntbot_cash': 3000, 'hunt_cost': 5,
                    'cash_poll_min': 5}


def _earning_row(bot):
    """One account's ledger, flattened into the numbers the tab renders.

    The derived figures are computed here rather than stored: ``net`` has to equal
    the account's real cowoncy movement, and recomputing it from the buckets on
    every read is what guarantees a bucket that drifted shows up as a wrong bucket
    instead of a wrong profit.
    """
    from core import state as core_state
    uid = str(getattr(bot, 'user_id', '') or '')
    stats = core_state.account_stats.get(uid, {})
    led = stats.get('earning')
    if not isinstance(led, dict):
        led = core_state.empty_earning()

    gained = int(led.get('gained_sell') or 0) + int(led.get('gained_other') or 0)
    spent_autohunt = int(led.get('spent_autohunt') or 0)
    spent_hunt = int(led.get('spent_hunt') or 0)
    spent_other = int(led.get('spent_other') or 0)
    spent = spent_autohunt + spent_hunt + spent_other

    started = led.get('started_at')
    hours = max((time.time() - started) / 3600.0, 0.0) if started else 0.0
    net = gained - spent
    # under a couple of minutes any rate is noise, and dividing by ~0 hours produced
    # "earning 4.2M/h" on the first sample
    per_hour = round(net / hours) if hours >= 0.05 else None

    earning_cfg = (bot.config.get('earning') or {}) if isinstance(bot.config, dict) else {}
    return {
        'id': uid,
        'name': getattr(bot, 'account_name', None) or stats.get('username') or uid,
        'enabled': bool(earning_cfg.get('enabled', False)),
        'running': bool(getattr(bot, 'is_ready', False)),
        'paused': bool(getattr(bot, 'paused', False)),
        'started_at': started,
        'hours': round(hours, 3),
        'start_cash': led.get('start_cash'),
        'current_cash': stats.get('current_cash'),
        'last_cash_at': led.get('last_cash_at'),
        'gained': gained,
        'gained_sell': int(led.get('gained_sell') or 0),
        'gained_other': int(led.get('gained_other') or 0),
        'spent': spent,
        'spent_autohunt': spent_autohunt,
        'spent_hunt': spent_hunt,
        'spent_other': spent_other,
        'net': net,
        'per_hour': per_hour,
        'sold_count': int(led.get('sold_count') or 0),
        'hunts': int(led.get('hunts') or 0),
        'autohunt_runs': int(led.get('autohunt_runs') or 0),
        'battles': int(led.get('battles') or 0),
        'team_changes': int(led.get('team_changes') or 0),
        'last_sell_amount': led.get('last_sell_amount'),
        'last_event': led.get('last_event'),
        'last_event_at': led.get('last_event_at'),
    }


@app.route('/api/earning', methods=['GET'])
@space_required
def earning_api():
    """Every account in this space, its ledger, and the space's earning settings."""
    rows = [_earning_row(bot) for bot in state.bots_for(g.owner)]
    rows.sort(key=lambda r: r['name'].lower())

    settings = dict(EARNING_DEFAULTS)
    saved = (_space_config(g.owner).get('earning') or {})
    for key in EARNING_DEFAULTS:
        if key in saved:
            settings[key] = saved[key]

    totals = {'gained': 0, 'spent': 0, 'spent_autohunt': 0, 'spent_hunt': 0,
              'spent_other': 0, 'net': 0, 'sold_count': 0, 'hunts': 0,
              'autohunt_runs': 0, 'battles': 0, 'team_changes': 0,
              'per_hour': 0, 'accounts': len(rows), 'on': 0}
    for row in rows:
        for key in ('gained', 'spent', 'spent_autohunt', 'spent_hunt', 'spent_other',
                    'net', 'sold_count', 'hunts', 'autohunt_runs', 'battles',
                    'team_changes'):
            totals[key] += row[key] or 0
        totals['per_hour'] += row['per_hour'] or 0
        if row['enabled']:
            totals['on'] += 1
    return jsonify({'success': True, 'settings': settings, 'accounts': rows,
                    'totals': totals})


@app.route('/api/earning/toggle', methods=['POST'])
@space_required
def earning_toggle():
    """Switch earning mode on or off for the whole space, in one write.

    It writes the space default *and* every per-account file, because the per-account
    file is the last layer the bot merges: leaving those alone would let an account
    that once had settings saved to it opt itself out of a mode the operator turned
    on for the farm.
    """
    data = _payload() or {}
    settings = dict(EARNING_DEFAULTS)
    saved = (_space_config(g.owner).get('earning') or {})
    for key in EARNING_DEFAULTS:
        if key in saved:
            settings[key] = saved[key]

    if 'enabled' in data:
        settings['enabled'] = bool(data.get('enabled'))
    if 'exclusive' in data:
        settings['exclusive'] = bool(data.get('exclusive'))
    for key, low, high in (('sell_interval_min', 1, 1440), ('huntbot_cash', 0, 250000),
                           ('hunt_cost', 0, 10000), ('cash_poll_min', 1, 120)):
        if key in data:
            try:
                settings[key] = max(low, min(high, int(data[key])))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': f'{key} must be a number'}), 400
    if 'sell_type' in data:
        sell_type = str(data.get('sell_type') or 'all').strip().lower()
        # this string is pasted straight into an outgoing OwO command
        if not re.fullmatch(r'[a-z]{1,16}', sell_type):
            return jsonify({'success': False, 'error': 'sell_type must be a single word'}), 400
        settings['sell_type'] = sell_type

    targets = [spaces.settings_path(g.owner)] + list(spaces.settings_files(g.owner))
    written = 0
    for path in dict.fromkeys(targets):
        try:
            document = {}
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    document = loaded
            document['earning'] = settings
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(document, f, indent=4)
            written += 1
        except Exception as exc:
            return jsonify({'success': False, 'error': f'could not save {os.path.basename(path)}: {exc}'}), 500

    for bot in state.bots_for(g.owner):
        _bot_loop_fire(bot.sync_settings({'earning': settings}))

    state.log_command("SYS", f"Earning mode {'ON' if settings['enabled'] else 'OFF'} "
                             f"({written} settings file(s)) by {acting_label()}",
                      "success", owner=g.owner)
    return jsonify({'success': True, 'settings': settings})


@app.route('/api/earning/reset', methods=['POST'])
@space_required
def earning_reset():
    """Zero the ledger - for one account with {"id": ...}, otherwise the whole space."""
    data = _payload() or {}
    account_id = data.get('id')
    if account_id:
        bot = get_bot(account_id, g.owner)
        if not bot:
            return jsonify({'success': False, 'error': 'Account not running'}), 404
        bots = [bot]
    else:
        bots = list(state.bots_for(g.owner))

    reset = 0
    for bot in bots:
        cog = bot.get_cog('Earning')
        if cog and cog.reset():
            reset += 1
    state.save_account_stats()
    return jsonify({'success': True, 'reset': reset})


def _normalise_custom_commands(raw):
    """Accept a textarea, a list of strings, or a list of dicts."""
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    cleaned = []
    for item in raw or []:
        if isinstance(item, str):
            item = {'command': item}
        if not isinstance(item, dict):
            continue
        command = str(item.get('command', '')).strip()
        if not command:
            continue
        try:
            interval = float(item.get('interval_s') or 0)
        except (TypeError, ValueError):
            interval = 0.0
        cleaned.append({
            'command': command,
            'interval_s': max(0.0, interval),
            'enabled': item.get('enabled', True) is not False,
        })
    return cleaned


@app.route('/api/bot/custom_commands', methods=['GET', 'POST'])
@space_required
def custom_commands_api():
    """The saved per-bot command list (commands.custom in the settings file)."""
    account_id = request.args.get('id')
    if account_id:
        if not spaces.is_valid_discord_id(account_id):
            return jsonify({'success': False, 'error': 'Invalid account id'}), 400
        if not owns_account(account_id, g.owner):
            return jsonify({'success': False, 'error': 'Not your account'}), 403

    if request.method == 'GET':
        custom = _read_settings_for(g.owner, account_id).get('commands', {}).get('custom', {})
        return jsonify({
            'enabled': custom.get('enabled', False),
            'commands': _normalise_custom_commands(custom.get('commands')),
        })

    payload = _payload()
    commands_list = _normalise_custom_commands(payload.get('commands'))
    block = {'enabled': bool(payload.get('enabled', True)), 'commands': commands_list}
    save_to_all = bool(payload.get('all'))

    try:
        if save_to_all:
            # every settings file in the caller's own space - the space default
            # first, so a newly added account inherits the list
            paths = spaces.settings_files(g.owner)
        else:
            paths = [_settings_path_for(g.owner, account_id)]

        for path in paths:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = _read_settings_for(g.owner, account_id)
            cfg.setdefault('commands', {})['custom'] = block
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, indent=4)

        for bot in state.bots_for(g.owner):
            if save_to_all or (not account_id) or (bot.user and str(bot.user.id) == str(account_id)):
                asyncio.run_coroutine_threadsafe(
                    bot.sync_settings({'commands': {'custom': block}}), bot.loop
                )

        state.log_command("SYS", f"Custom commands saved ({len(commands_list)} entries)", "success")
        return jsonify({'success': True, 'commands': commands_list, 'enabled': block['enabled']})
    except Exception as e:
        state.log_command("ERROR", f"Failed to save custom commands: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


_pending_captchas = {}

# A pending challenge is a claim that one account is sitting on an unsolved captcha
# *right now*. Nothing used to withdraw that claim except an actual solve, so stopping
# an account - or OwO dropping the captcha on its own - left the entry in the
# notification bell forever, and every Solve button on it answered "Bot not found",
# which the UI renders as "Failed to get captcha URL".
CAPTCHA_TTL_S = int(os.environ.get('LAZYFARMERS_CAPTCHA_TTL', str(15 * 60)))
# some detection paths register the challenge a moment before they pause the account,
# so never judge an entry by the bot's run state inside this window
CAPTCHA_LIVENESS_GRACE_S = 30


def _live_bot_for_account(account_id):
    """Find a running bot by discord id, ignoring spaces (used for liveness only)."""
    for inst in list(state.bot_instances):
        if inst.user and str(inst.user.id) == str(account_id):
            return inst
    return None


def _captcha_still_real(account_id, challenge):
    """(keep?, why-not) - is this pending challenge still something a human can solve?"""
    age = time.time() - (challenge.get('created_at') or 0)
    # A blocked running account stays visible even after the old 15-minute TTL.
    bot = _live_bot_for_account(account_id)
    if bot is None:
        # the account was stopped: nothing can solve for it, so it must not keep
        # offering a Solve button that can only fail
        return False, 'account not running'
    if age < CAPTCHA_LIVENESS_GRACE_S:
        return True, ''
    blocked = (bool(bot.paused)
               or (getattr(bot, 'throttle_until', 0) or 0) > time.time()
               or getattr(bot, '_solving_captcha', False))
    if not blocked:
        # the account is farming again, so whatever the captcha was, it is over
        return False, 'account already resumed'
    return True, ''


def _reap_stale_captchas():
    """Drop pending challenges that no longer describe reality. Returns removed ids."""
    removed = []
    for acc_id, challenge in list(_pending_captchas.items()):
        keep, why = _captcha_still_real(acc_id, challenge)
        if keep:
            continue
        _pending_captchas.pop(acc_id, None)
        removed.append(acc_id)
        state.log_command("SEC", f"Captcha challenge dropped for account {acc_id} ({why})",
                          "info", owner=state.owner_of(acc_id))
    return removed


@app.route('/api/captcha_challenge', methods=['GET'])
@space_required
def get_captcha_challenge():
    """Get pending captcha challenges for dashboard display."""
    _reap_stale_captchas()
    account_id = request.args.get('account_id', type=str)
    if account_id:
        # an explicit id must never fall through to somebody else's challenge - the
        # answer would be submitted against the wrong account
        challenge = _pending_captchas.get(account_id)
        if not challenge or not owns_account(account_id, g.owner):
            return jsonify({'success': False, 'message': 'No captcha pending'})
        return jsonify({'success': True, 'challenge': challenge})

    for acc_id, challenge in _pending_captchas.items():
        if not owns_account(acc_id, g.owner):
            continue
        return jsonify({'success': True, 'challenge': challenge, 'account_id': acc_id})
    return jsonify({'success': False, 'message': 'No captcha pending'})

@app.route('/api/captcha_solve', methods=['POST'])
@space_required
def submit_captcha_solution():
    """Submit an hCaptcha token solved in the dashboard.

    owobot's /api/captcha/verify authenticates the *session*, not the Discord token.
    This route used to POST the token with nothing but `Authorization: <discord token>`
    and no owobot cookie at all, which owobot answers 401 - so the Solve box could never
    succeed, for any account, with any token. The verify now runs through
    WebSolver.submit_manual_token on the bot loop, which performs the same Discord OAuth
    handshake modules/web_solver.py already proves works before posting.
    """
    data = _payload()
    account_id = data.get('account_id', '')
    token = data.get('token', '')

    if not account_id or not token:
        return jsonify({'success': False, 'error': 'Missing account_id or token'})

    bot = get_bot(account_id, g.owner)
    if not bot:
        # a Solve button on a challenge whose account is gone: withdraw the challenge
        # instead of leaving it in the bell to fail again
        _reap_stale_captchas()
        return jsonify({'success': False,
                        'error': 'That account is not running any more - the captcha '
                                 'notification has been cleared.'})

    outcome, loop_error = _bot_loop_call(bot.web_solver.submit_manual_token(token), timeout=90)
    if loop_error:
        return jsonify({'success': False, 'error': loop_error}), 503
    ok, detail = outcome

    if ok:
        security_cog = bot.get_cog('Security')
        if security_cog:
            # one chokepoint unpauses, clears the challenge, resolves the manual-solve
            # future and notifies - same as a DM confirmation
            _bot_loop_fire(_resume_account(security_cog,
                                           "Captcha solved from the dashboard."))
        else:
            from modules.web_solver import WebSolver
            WebSolver.mark_verification_done(str(account_id))
            clear_captcha_challenge(str(account_id))
        state.log_command("SEC", f"Captcha verified for account {account_id}", "success",
                          owner=g.owner)
        return jsonify({'success': True, 'message': 'Captcha verified successfully'})

    state.log_command("SEC", f"Captcha verification failed: {detail}", "error", owner=g.owner)
    return jsonify({'success': False, 'error': detail or 'owobot rejected the captcha token'})


@app.route('/api/captcha/oauth_url', methods=['POST'])
@space_required
def captcha_oauth_url():
    """Mint the Discord OAuth redirect that fronts owobot.com's captcha page.

    The URL is signed by the account's own token, so resolving the id inside the
    caller's space is the whole security boundary here.
    """
    data = _payload()
    account_id = data.get('account_id')
    if not account_id:
        return jsonify({'success': False, 'error': 'Missing account_id'})

    bot = get_bot(account_id, g.owner)
    if not bot:
        _reap_stale_captchas()
        return jsonify({'success': False,
                        'error': 'That account is not running any more - the captcha '
                                 'notification has been cleared.'})

    import aiohttp

    auth_url = "https://discord.com/api/v9/oauth2/authorize?client_id=408785106942164992&response_type=code&redirect_uri=https://owobot.com/api/auth/discord/redirect&scope=identify guilds"

    async def get_redirect_url():
        headers = {
            "Authorization": bot.token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            auth_payload = {
                "authorize": True,
                "permissions": "0",
                "integration_type": 0,
                "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000}
            }
            async with session.post(auth_url, json=auth_payload) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
                return payload.get("location")

    # must run on the bot loop - a fresh loop on the Flask thread leaks and
    # cannot see the bot's connector/proxy
    redirect_url, error = _bot_loop_call(get_redirect_url(), timeout=30)
    if error:
        return jsonify({'success': False, 'error': error}), 503

    if not redirect_url:
        return jsonify({'success': False, 'error': 'Failed to get OAuth URL'})

    return jsonify({'success': True, 'url': redirect_url})

@app.route('/api/captcha/browser_solve', methods=['POST'])
@space_required
def captcha_browser_solve():
    """Solve this account's captcha in a locally installed browser - no service key.

    Carries the account's owobot session into Chrome/Edge over the DevTools protocol, so
    hCaptcha runs in a real browser (the only place it will run at all - its challenge
    payload is encrypted and only its own WASM can read it). hCaptcha issues the token
    itself when its risk score allows; otherwise the challenge appears in the window for
    one answer and the token is submitted automatically.
    """
    data = _payload()
    account_id = data.get('account_id')
    if not account_id:
        return jsonify({'success': False, 'error': 'Missing account_id'})

    bot = get_bot(account_id, g.owner)
    if not bot:
        _reap_stale_captchas()
        return jsonify({'success': False,
                        'error': 'That account is not running any more - the captcha '
                                 'notification has been cleared.'})

    solver = getattr(bot, 'browser_solver', None)
    if solver is None:
        return jsonify({'success': False, 'error': 'Extension solver not initialised yet'})
    if not solver.enabled:
        return jsonify({'success': False,
                        'error': 'The extension solver needs a NopeCHA booster key - set '
                                 'security.captcha_solver.nopecha_booster_key, or solve this '
                                 'captcha here on the dashboard.'})

    from modules.browser_solver import browser_status
    unavailable = browser_status()
    if unavailable:
        return jsonify({'success': False, 'error': unavailable})

    security_cog = bot.get_cog('Security')
    if not security_cog:
        return jsonify({'success': False, 'error': 'Security module unavailable'}), 409
    _bot_loop_fire(security_cog._run_autosolve(
        bot.config.get('security', {}).get('captcha_solver', {}), 'dashboard'))
    return jsonify({'success': True, 'queued': True,
                    'message': 'Queued; progress stays visible until verified or manual action is needed.'})


async def _resume_account(security_cog, why):
    """Call the Security cog's resume on the bot loop (it touches bot state)."""
    security_cog._resume_after_solve(why)


@app.route('/api/captcha/pending', methods=['GET'])
@space_required
def pending_captchas():
    # the bell polls this every 2s, which makes it the natural place to notice that a
    # challenge has stopped being real
    _reap_stale_captchas()
    pending = []
    for acc_id, challenge in _pending_captchas.items():
        if not owns_account(acc_id, g.owner):
            continue
        pending.append({
            'account_id': acc_id,
            'account_name': challenge.get('account_name', acc_id),
            'job': state.account_stats.get(str(acc_id), {}).get('captcha_job', {}),
            'created_at': challenge.get('created_at', time.time())
        })
    return jsonify({'pending': pending})

def register_captcha_challenge(account_id, challenge_data):
    _pending_captchas[account_id] = {
        'account_id': account_id,
        'created_at': time.time(),
        **challenge_data
    }
    # called from the bot loop, so the space comes from the account, not the session
    state.log_command("SEC", f"Captcha challenge registered for account {account_id}", "info",
                      owner=state.owner_of(account_id))

def clear_captcha_challenge(account_id):
    if account_id in _pending_captchas:
        _pending_captchas.pop(account_id, None)
        state.log_command("SEC", f"Captcha challenge cleared for account {account_id}", "info",
                          owner=state.owner_of(account_id))


# --------------------------------------------------------------------------
# users - activation keys and the accounts they create (admin only)
# --------------------------------------------------------------------------

@app.route('/api/users/keys', methods=['GET', 'POST'])
@admin_required
def user_keys_api():
    if request.method == 'GET':
        keys = dash_users.list_keys()
        keys.sort(key=lambda k: k.get('created_at') or 0, reverse=True)
        return jsonify({'success': True, 'keys': keys})

    data = _payload()
    created, error = dash_users.generate_keys(
        data.get('days'),
        data.get('count', 1),
        data.get('note', ''),
    )
    if error:
        return jsonify({'success': False, 'error': error}), 400

    base = request.host_url.rstrip('/')
    for entry in created:
        entry['link'] = f"{base}/activate?key={quote(entry['key'])}"
    state.log_command("USERS", f"Generated {len(created)} activation key(s) for {created[0]['days']} days", "success",
                      owner=spaces.ADMIN_SPACE)
    return jsonify({'success': True, 'keys': created})


@app.route('/api/users/keys/<key>', methods=['DELETE'])
@admin_required
def user_key_delete(key):
    if dash_users.delete_key(key):
        state.log_command("USERS", f"Activation key {key} deleted", "info",
                          owner=spaces.ADMIN_SPACE)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'No such key'}), 404


@app.route('/api/users', methods=['GET'])
@admin_required
def users_api():
    users = dash_users.list_users()
    users.sort(key=lambda u: u.get('created_at') or 0, reverse=True)
    return jsonify({'success': True, 'users': users})


@app.route('/api/users/<user_id>', methods=['PATCH', 'DELETE'])
@admin_required
def user_detail_api(user_id):
    if request.method == 'DELETE':
        # stop whatever that space is still running before its accounts.json goes
        # away, otherwise the bots keep farming with no file behind them
        from core import supervisor
        try:
            owner = spaces.normalise_owner(user_id)
        except spaces.InvalidOwner:
            owner = None
        if owner and state.bots_for(owner):
            _bot_loop_fire(supervisor.stop_all(owner))
        if dash_users.delete_user(user_id):
            state.log_command("USERS", f"Dashboard user {user_id} deleted (space wiped)", "info",
                              owner=spaces.ADMIN_SPACE)
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'No such user'}), 404

    data = _payload()
    action = (data.get('action') or '').lower()

    if action == 'revoke':
        user, error = dash_users.set_revoked(user_id, True)
    elif action == 'restore':
        user, error = dash_users.set_revoked(user_id, False)
    elif action == 'extend':
        user, error = dash_users.extend_user(user_id, data.get('days'))
    elif action == 'password':
        user, error = dash_users.set_password(user_id, data.get('password'))
    else:
        return jsonify({'success': False, 'error': f'Unknown action: {action or "(none)"}'}), 400

    if error:
        return jsonify({'success': False, 'error': error}), 400
    state.log_command("USERS", f"Dashboard user {user['email']}: {action}", "info",
                      owner=spaces.ADMIN_SPACE)
    return jsonify({'success': True, 'user': user})


@app.route('/api/users/key_status', methods=['POST'])
def user_key_status():
    """Open on purpose - the activate page checks a key before asking for a password."""
    ip = client_ip()
    allowed, wait_time = check_rate_limit(ip)
    if not allowed:
        return jsonify({'success': False, 'error': f'Too many attempts. Try again in {wait_time}s'})

    data = _payload()
    entry, error = dash_users.key_status(data.get('key'))
    if error:
        # rate limited like a login so the endpoint cannot be used to brute-force keys
        fail_login(ip)
        return jsonify({'success': False, 'error': error})
    return jsonify({'success': True, 'days': entry.get('days')})



@app.route('/api/monitor', methods=['GET', 'POST'])
@space_required
def monitor_config_api():
    from core import monitor
    if request.method == 'GET':
        return jsonify(monitor.public_config(g.owner))
    data = _payload()
    allowed = {}
    if 'token' in data and str(data['token']).strip():
        allowed['token'] = str(data['token']).strip()
    if 'enabled' in data:
        if not isinstance(data['enabled'], bool):
            return jsonify({'success': False, 'error': 'enabled must be true or false'}), 400
        allowed['enabled'] = data['enabled']
    if 'withdraw_percent' in data:
        try:
            pct = float(data['withdraw_percent'])
            if not 1 <= pct <= 100:
                raise ValueError()
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Withdrawal percentage must be 1–100'}), 400
        allowed['withdraw_percent'] = pct
    if monitor._operations.get(g.owner, {}).get('status') == 'running':
        return jsonify({'success': False, 'error': 'Wait for the current operation to finish'}), 409
    monitor.save_config(g.owner, allowed)
    _, error = _bot_loop_call(monitor.restart(g.owner))
    if error:
        return jsonify({'success': False, 'error': error}), 503
    return jsonify({'success': True, **monitor.public_config(g.owner)})


@app.route('/api/monitor/servers', methods=['GET'])
@space_required
def monitor_servers_api():
    from core import monitor
    result, error = _bot_loop_call(monitor.discover(g.owner))
    return (jsonify({'success': False, 'error': error}), 400) if error else jsonify({'success': True, **result})


@app.route('/api/monitor/server', methods=['POST'])
@space_required
def monitor_select_server_api():
    from core import monitor
    if monitor._operations.get(g.owner, {}).get('status') == 'running':
        return jsonify({'success': False, 'error': 'Wait for the current operation to finish'}), 409
    result, error = _bot_loop_call(monitor.select_guild(g.owner, _payload().get('guild_id')))
    return (jsonify({'success': False, 'error': error}), 400) if error else jsonify({'success': True, **result})


async def _monitor_start(owner, kind):
    from core import monitor
    return monitor.start_operation(owner, kind)


@app.route('/api/monitor/provision', methods=['POST'])
@space_required
def monitor_provision_api():
    result, error = _bot_loop_call(_monitor_start(g.owner, 'provision'))
    return (jsonify({'success': False, 'error': error}), 503) if error else jsonify(result)


@app.route('/api/monitor/withdraw', methods=['POST'])
@space_required
def monitor_withdraw_api():
    result, error = _bot_loop_call(_monitor_start(g.owner, 'withdraw'))
    return (jsonify({'success': False, 'error': error}), 503) if error else jsonify(result)


@app.route('/api/monitor/summary')
@space_required
def monitor_summary_api():
    from core import monitor
    return jsonify({'success': True, 'summary': _snapshot(('monitor', g.owner), lambda: monitor.snapshot(g.owner), ttl=5)})


async def _retry_captchas(owner):
    queued = 0
    for bot in state.bots_for(owner):
        if str(bot.user_id) not in _pending_captchas:
            continue
        cog = bot.get_cog('Security')
        if cog:
            task = asyncio.create_task(cog._run_autosolve(
                bot.config.get('security', {}).get('captcha_solver', {}), 'retry all'))
            bot.worker_tasks.append(task)
            task.add_done_callback(lambda done, b=bot: b.worker_tasks.remove(done) if done in b.worker_tasks else None)
            queued += 1
    return {'success': True, 'queued': queued}


@app.route('/api/captcha/retry_all', methods=['POST'])
@space_required
def captcha_retry_all_api():
    result, error = _bot_loop_call(_retry_captchas(g.owner))
    return (jsonify({'success': False, 'error': error}), 503) if error else jsonify(result)


@app.route('/api/monitor/reconcile', methods=['POST'])
@space_required
def monitor_reconcile_api():
    result, error = _bot_loop_call(_monitor_start(g.owner, 'reconcile'))
    return (jsonify({'success': False, 'error': error}), 503) if error else jsonify(result)
