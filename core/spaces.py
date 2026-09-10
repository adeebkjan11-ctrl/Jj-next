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
Per-user spaces.

Every dashboard login owns a directory under DATA_DIR/users/<owner_id> holding
that user's accounts, proxies, per-account settings overrides and history db.
The admin's own farm lives in the reserved space "admin".

    data/users/admin/accounts.json
    data/users/admin/proxies.json
    data/users/admin/settings_408785106942164992.json
    data/users/u_9f2c1a3b4d5e/accounts.json
    ...

normalise_owner() is the ONLY place an owner string is turned into a path, so it
is also the only place path traversal has to be stopped. Never build a space path
by hand - always go through the helpers here.

Shared, admin-owned config (settings.json defaults, cmd_priorities.json,
shortform.json, logmisc.json, auth.json, dashboard_users.json) stays in
CONFIG_DIR and is not part of any space.
"""


import json
import os
import re
import shutil
import threading

from core.paths import CONFIG_DIR, USERS_DIR

ADMIN_SPACE = 'admin'

# "admin" or the u_<hex> id minted by dashboard/users.py redeem_key()
_OWNER_RE = re.compile(r'^(?:admin|u_[0-9a-f]{6,32})$')
_DISCORD_ID_RE = re.compile(r'^\d{5,25}$')

_MIGRATION_MARKER = os.path.join(USERS_DIR, '.legacy_migrated_v2')
_lock = threading.Lock()

# discord user id (str) -> owner id. Filled in by core.supervisor when a bot is
# started and read back by the dashboard to decide who may see a running account.
account_owners = {}


class InvalidOwner(ValueError):
    """Raised when an owner id could not possibly name a space."""


def is_valid_owner(owner_id):
    return bool(_OWNER_RE.match(str(owner_id or '')))


def normalise_owner(owner_id):
    """Validate an owner id before it becomes a filesystem path."""
    owner = str(owner_id or '').strip()
    if not _OWNER_RE.match(owner):
        raise InvalidOwner(f"invalid space id: {owner_id!r}")
    return owner


def is_valid_discord_id(discord_id):
    return bool(_DISCORD_ID_RE.match(str(discord_id or '')))


def normalise_discord_id(discord_id):
    account_id = str(discord_id or '').strip()
    if not _DISCORD_ID_RE.match(account_id):
        raise InvalidOwner(f"invalid account id: {discord_id!r}")
    return account_id


def space_dir(owner_id, create=True):
    path = os.path.join(USERS_DIR, normalise_owner(owner_id))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def accounts_path(owner_id):
    return os.path.join(space_dir(owner_id), 'accounts.json')


def proxies_path(owner_id):
    return os.path.join(space_dir(owner_id), 'proxies.json')


def settings_path(owner_id, discord_id=None):
    if discord_id is None:
        return os.path.join(space_dir(owner_id), 'settings.json')
    return os.path.join(space_dir(owner_id), f'settings_{normalise_discord_id(discord_id)}.json')


def settings_files(owner_id):
    """Every settings file in a space - the space default first, then per-account."""
    owner = normalise_owner(owner_id)
    root = space_dir(owner)
    out = [settings_path(owner)]
    try:
        for name in sorted(os.listdir(root)):
            if name.startswith('settings_') and name.endswith('.json'):
                out.append(os.path.join(root, name))
    except OSError:
        pass
    return out


def history_path(owner_id):
    owner = normalise_owner(owner_id)
    # the admin space keeps the pre-spaces database file so no history is lost
    if owner == ADMIN_SPACE:
        legacy = os.path.join(os.path.dirname(USERS_DIR), 'neura_history.db')
        if os.path.exists(legacy):
            return legacy
    return os.path.join(space_dir(owner), 'history.db')


def list_owners():
    """Every space that exists on disk, admin first."""
    try:
        names = sorted(n for n in os.listdir(USERS_DIR) if is_valid_owner(n))
    except OSError:
        names = []
    if ADMIN_SPACE not in names:
        names.insert(0, ADMIN_SPACE)
    else:
        names.remove(ADMIN_SPACE)
        names.insert(0, ADMIN_SPACE)
    return names


def ensure_space(owner_id):
    """Create an empty space for a freshly redeemed dashboard user."""
    owner = normalise_owner(owner_id)
    space_dir(owner)
    for path, empty in ((accounts_path(owner), {'accounts': []}), (proxies_path(owner), {'proxies': []})):
        if not os.path.exists(path):
            _write_json(path, empty)
    return owner


def delete_space(owner_id):
    """Remove a space entirely - used when the admin deletes a dashboard user."""
    owner = normalise_owner(owner_id)
    if owner == ADMIN_SPACE:
        return False
    path = os.path.join(USERS_DIR, owner)
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=4)
    os.replace(tmp, path)


def _read_accounts(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f).get('accounts', [])
    except (OSError, ValueError):
        return []


def owner_for_account(discord_id):
    """Which space owns this discord account? None when nothing claims it."""
    account_id = str(discord_id or '')
    if not account_id:
        return None
    known = account_owners.get(account_id)
    if known:
        return known
    for owner in list_owners():
        for account in _read_accounts(accounts_path(owner)):
            if str(account.get('user_id') or '') == account_id:
                return owner
    return None


def owner_for_account_name(name):
    """Which space holds an account with this name? None when nothing does."""
    wanted = str(name or '')
    if not wanted:
        return None
    for owner in list_owners():
        for account in _read_accounts(accounts_path(owner)):
            if str(account.get('name') or '') == wanted:
                return owner
    return None


def migrate_legacy():
    """
    One-time move of the pre-spaces single-tenant files into the admin space.

    config/accounts.json, config/proxies.json and every config/settings_<id>.json
    become data/users/admin/*, and the originals are renamed to *.migrated so it
    is obvious they are no longer read.
    """
    with _lock:
        if os.path.exists(_MIGRATION_MARKER):
            return False
        os.makedirs(USERS_DIR, exist_ok=True)
        moved = []
        try:
            ensure_space(ADMIN_SPACE)
            pairs = [
                (os.path.join(CONFIG_DIR, 'accounts.json'), accounts_path(ADMIN_SPACE)),
                (os.path.join(CONFIG_DIR, 'proxies.json'), proxies_path(ADMIN_SPACE)),
            ]
            try:
                for name in os.listdir(CONFIG_DIR):
                    if name.startswith('settings_') and name.endswith('.json'):
                        pairs.append((os.path.join(CONFIG_DIR, name),
                                      os.path.join(space_dir(ADMIN_SPACE), name)))
            except OSError:
                pass

            for src, dst in pairs:
                if not os.path.isfile(src):
                    continue
                # never clobber a space file that already has content
                if os.path.isfile(dst):
                    # An empty wrapper is not migrated user data.
                    try:
                        with open(dst, encoding='utf-8') as existing:
                            payload = json.load(existing)
                        empty = payload in ({}, {'accounts': []}, {'proxies': []})
                    except (OSError, ValueError):
                        empty = False
                    if not empty:
                        continue
                shutil.copy2(src, dst)
                try:
                    os.replace(src, src + '.migrated')
                except OSError:
                    pass
                moved.append(os.path.basename(src))

            with open(_MIGRATION_MARKER, 'w', encoding='utf-8') as f:
                f.write('\n'.join(moved))
        except Exception as exc:  # never let a migration hiccup stop the boot
            print(f"[!] space migration failed: {exc}", flush=True)
            return False
        if moved:
            print(f"[+] moved {', '.join(moved)} into the admin space "
                  f"({space_dir(ADMIN_SPACE)})", flush=True)
        return bool(moved)
