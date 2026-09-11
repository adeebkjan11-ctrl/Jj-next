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

"""Prepare NopeCHA's browser extension so a Discord-boost key can solve captchas.

Why this exists
---------------
NopeCHA hands out reward keys for boosting their Discord server. Those keys carry
credits for the **extension only** - ``api.nopecha.com`` refuses them, so the API
path in ``modules/services/nopecha.py`` can never spend one no matter how many
credits it holds. The extension answers the challenge inside the page instead,
which fits the browser solver perfectly: it already scrapes the token straight
out of owobot's captcha page, so an extension solving in-page needs no new token
plumbing at all - only loading, and a little more patience.

The build to use is the *automation* build: no popup, no onboarding, and every
setting read from the ``nopecha`` block at the bottom of ``manifest.json``. It is
published as ``chromium_automation.zip`` on the extension's GitHub releases and is
not on the Chrome Web Store, so it is fetched, unpacked and patched here.

The extension is closed source and both its asset names and its setting keys have
moved between releases, so nothing here hardcodes a download URL or clobbers keys
it does not recognise: the asset is discovered through the releases API, and the
manifest patch sets only what it must while leaving the rest of the block as the
build shipped it.
"""

import asyncio
import hashlib
import io
import json
import os
import shutil
import threading
import zipfile

import aiohttp

from core.paths import DATA_DIR

RELEASES_API = "https://api.github.com/repos/NopeCHALLC/nopecha-extension/releases"
# in priority order - the automation build is the only one that can be configured
# without a human clicking through a popup
ASSET_HINTS = ("chromium_automation", "chrome_automation", "automation")
PLACEHOLDER = "YOUR KEY HERE"
# both halves are needed: _auto_open makes the extension open the challenge and
# _auto_solve makes it answer one that is already open
SOLVE_FLAGS = ("hcaptcha_auto_open", "hcaptcha_auto_solve")
_UA = "LazyFarmers"
# every unpack/copy/patch runs inside this, because two accounts in two spaces can
# start a solve at the same moment and a half-written build loads as a broken one
_FS_LOCK = threading.Lock()


def resolve_key(solver_cfg):
    """The key the extension should run with, from a ``captcha_solver`` config block.

    One definition of the precedence, shared by the solver and the dashboard, so the
    UI can never report "no key" about a key the browser would happily have used. A
    paid API key comes last rather than demanding the same string be pasted twice:
    it works in the extension as well, it is only booster keys that work *nowhere
    else*.
    """
    solver = solver_cfg or {}
    nope = ((solver.get("browser_solver") or {}).get("nopecha") or {})
    for value in (nope.get("key"), solver.get("nopecha_booster_key"),
                  os.environ.get("LAZYFARMERS_NOPECHA_KEY"),
                  solver.get("nopecha_api_key")):
        value = str(value or "").strip()
        if value:
            return value
    return ""


def cache_root():
    """Where unpacked builds live: one pristine copy plus one per key."""
    return os.path.join(DATA_DIR, "nopecha_extension")


def source_dir():
    """The build exactly as it shipped - never has a key written into it."""
    return os.path.join(cache_root(), "_source")


def build_dir(fingerprint):
    """The patched copy belonging to one key.

    Keys get a directory each rather than sharing one: two dashboard tenants can
    hold different booster keys, and a single shared copy would both thrash (each
    solve rewriting the manifest) and let one tenant's browser launch with the
    other tenant's key in it.
    """
    return os.path.join(cache_root(), f"key_{fingerprint}")


def _stamp_file(root):
    return os.path.join(root, ".lazyfarmers.json")


def _read_stamp(root):
    try:
        with open(_stamp_file(root), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_stamp(root, data):
    """Record what is in a cache directory. False if it could not be written.

    The stamp *is* the cache: without it the next solve cannot tell a good build from
    an empty directory and downloads the whole thing again, so a silent failure here
    turns into a download on every captcha.
    """
    try:
        with open(_stamp_file(root), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False


def _is_extension(path):
    return bool(path) and os.path.isfile(os.path.join(path, "manifest.json"))


def find_manifest_root(root):
    """The directory holding manifest.json, however deeply the zip nested it."""
    if not root or not os.path.isdir(root):
        return None
    if _is_extension(root):
        return root
    for dirpath, dirnames, filenames in os.walk(root):
        if "manifest.json" in filenames:
            return dirpath
        # a build is at most a couple of levels deep; do not crawl a whole disk
        if dirpath.count(os.sep) - root.count(os.sep) > 3:
            dirnames[:] = []
    return None


async def _discover_asset():
    """Newest automation build on GitHub as ``(asset, error)``.

    ``asset`` is ``{"url", "name", "tag"}``. The releases are only published as
    nightly GitHub assets, never on the Web Store, and the file names have changed
    between versions - hence discovery rather than a hardcoded URL.
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _UA}
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(f"{RELEASES_API}?per_page=15") as resp:
                if resp.status != 200:
                    return None, (f"GitHub's release list answered HTTP {resp.status} - "
                                  f"set security.captcha_solver.browser_solver.nopecha."
                                  f"extension_path to a build you unpacked yourself")
                releases = await resp.json()
    except Exception as exc:
        return None, f"could not reach GitHub for the extension: {type(exc).__name__}: {exc}"

    for release in releases or []:
        assets = [a for a in (release.get("assets") or [])
                  if str(a.get("name", "")).lower().endswith(".zip")
                  and a.get("browser_download_url")]
        for hint in ASSET_HINTS:
            for asset in assets:
                if hint in str(asset.get("name", "")).lower():
                    return {"url": asset["browser_download_url"],
                            "name": asset.get("name"),
                            "tag": release.get("tag_name")}, None
    return None, ("no automation build (chromium_automation*.zip) in the latest NopeCHA "
                  "releases - download one by hand and point extension_path at it")


async def _download(url):
    timeout = aiohttp.ClientTimeout(total=180)
    async with aiohttp.ClientSession(headers={"User-Agent": _UA}, timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"download answered HTTP {resp.status}")
            return await resp.read()


def _swap(staging, dest):
    """Move ``staging`` into place as ``dest``, replacing whatever was there."""
    parent = os.path.dirname(os.path.abspath(dest))
    os.makedirs(parent, exist_ok=True)
    old = f"{dest}.old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(dest):
        os.replace(dest, old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def _extract(blob, dest):
    """Unpack a build into ``dest``, refusing any path that escapes it.

    Extraction lands in a staging directory first: a browser that launches while a
    zip is still unpacking loads a half-written extension and simply does nothing.
    """
    staging = f"{dest}.new"
    with _FS_LOCK:
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)
        base = os.path.abspath(staging)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(base, member))
                if target != base and not target.startswith(base + os.sep):
                    raise RuntimeError(f"archive tried to write outside the cache: {member!r}")
            zf.extractall(base)
        _swap(staging, dest)


def _copy_tree(src, dest):
    """Mirror one build onto another path.

    Used both to take a hand-unpacked build under our own control - the operator's
    directory is never modified behind their back - and to stamp a per-key copy out
    of the pristine source.
    """
    staging = f"{dest}.new"
    with _FS_LOCK:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging)
        _swap(staging, dest)


def patch_manifest(root, key):
    """Write the key and the auto-solve flags into the build's ``nopecha`` block.

    Returns ``(version, fields_written)``. Unknown keys are preserved: the block
    carries a dozen settings this project has no opinion about, and a release can
    add more, so only the placeholder, the key and the hCaptcha flags are touched.
    """
    manifest_file = os.path.join(root, "manifest.json")
    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    section = manifest.get("nopecha")
    if not isinstance(section, dict):
        section = {}

    written = []
    # the build ships the literal "YOUR KEY HERE" in whichever field the current
    # version reads the key from, so replace every one of them instead of guessing
    for name, value in list(section.items()):
        if isinstance(value, str) and value.strip().upper() == PLACEHOLDER:
            section[name] = key
            written.append(name)
    if "key" not in written:
        section["key"] = key
        written.append("key")
    for flag in SOLVE_FLAGS:
        section[flag] = True
    section["enabled"] = True

    manifest["nopecha"] = section
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return str(manifest.get("version") or "?"), written


async def _ensure_source(path_hint, auto_download, note):
    """Make sure a pristine build is cached. Returns ``(root, tag, error)``."""
    dest = source_dir()
    source = os.path.abspath(os.path.expanduser(path_hint)) if path_hint else "github"
    stamp = _read_stamp(dest)
    root = find_manifest_root(dest)
    if root and stamp.get("source") == source:
        return root, stamp.get("tag") or "cached", None

    if path_hint:
        src = find_manifest_root(source)
        if not src:
            return None, None, (f"nopecha extension_path {path_hint!r} has no manifest.json "
                                f"under it - point it at the folder you unpacked the "
                                f"build into")
        try:
            await asyncio.to_thread(_copy_tree, src, dest)
        except Exception as exc:
            return None, None, f"could not copy the extension into the data dir: {exc}"
        tag = "local"
    else:
        if not auto_download:
            return None, None, ("no NopeCHA extension cached and auto_download is off - set "
                                "security.captcha_solver.browser_solver.nopecha.extension_path")
        asset, error = await _discover_asset()
        if not error:
            note("SYS", f"NopeCHA: downloading {asset['name']} ({asset['tag'] or 'latest'})...")
            try:
                blob = await _download(asset["url"])
                await asyncio.to_thread(_extract, blob, dest)
            except Exception as exc:
                error = (f"could not install the NopeCHA extension: "
                         f"{type(exc).__name__}: {exc}")
        if error:
            # a build already on disk beats no solve at all: GitHub rate-limits
            # unauthenticated callers to 60 requests an hour, so on a farm with several
            # accounts discovery is the first thing to fail - and it used to take the
            # cached extension down with it even though it was sitting right there
            if root:
                note("WARN", f"NopeCHA: {error} - using the cached build instead.")
                return root, stamp.get("tag") or "cached", None
            return None, None, error
        tag = asset.get("tag") or "latest"

    root = find_manifest_root(dest)
    if not root:
        return None, None, "the NopeCHA build contained no manifest.json"
    if not _write_stamp(dest, {"source": source, "tag": tag}):
        note("WARN", f"NopeCHA: could not write {_stamp_file(dest)} - the build will be "
                     f"downloaded again on every solve until that path is writable.")
    return root, tag, None


async def ensure_extension(key, path_hint=None, auto_download=True, log=None):
    """Return ``(extension_dir, error)`` - a patched, ready-to-load build.

    Never raises: the browser solver treats a missing extension as "solve without
    it", so a GitHub hiccup must not take the whole captcha path down with it.
    """
    def note(kind, message):
        if callable(log):
            try:
                log(kind, message)
            except Exception:
                pass

    key = str(key or "").strip()
    if not key:
        return None, ("no NopeCHA key set - put your booster key in "
                      "security.captcha_solver.nopecha_booster_key")

    path_hint = str(path_hint or os.environ.get("LAZYFARMERS_NOPECHA_EXTENSION") or "").strip()
    source = os.path.abspath(os.path.expanduser(path_hint)) if path_hint else "github"
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    dest = build_dir(fingerprint)

    stamp = _read_stamp(dest)
    existing = find_manifest_root(dest)
    if existing and stamp.get("key") == fingerprint and stamp.get("source") == source:
        # said out loud because a re-download is otherwise indistinguishable from a
        # cache hit in the log - both used to print nothing at all on the way through
        note("DEBUG", f"NopeCHA extension: reusing the cached build "
                      f"(v{stamp.get('version') or '?'}, {stamp.get('tag') or 'cached'}).")
        return existing, None

    src_root, tag, error = await _ensure_source(path_hint, auto_download, note)
    if error:
        return None, error

    try:
        # patched copies are always cut from the pristine source, so a key is never
        # inherited from whichever key was patched in before it
        await asyncio.to_thread(_copy_tree, src_root, dest)
    except Exception as exc:
        return None, f"could not stage the NopeCHA extension: {exc}"

    root = find_manifest_root(dest)
    if not root:
        return None, "the staged NopeCHA build contained no manifest.json"

    try:
        version, written = await asyncio.to_thread(patch_manifest, root, key)
    except Exception as exc:
        return None, f"could not write the key into the extension manifest: {exc}"

    if not _write_stamp(dest, {"key": fingerprint, "source": source, "tag": tag,
                               "version": version, "fields": written}):
        note("WARN", f"NopeCHA: could not write {_stamp_file(dest)} - the extension will "
                     f"be rebuilt on every solve until that path is writable.")
    note("SYS", f"NopeCHA extension ready (v{version}, {tag}) - key written to "
                f"{', '.join(written)}, hCaptcha auto-solve on. Cached in {cache_root()}.")
    return root, None


def cached_info(key=None):
    """What is currently installed, for the dashboard. Never raises."""
    src_stamp = _read_stamp(source_dir())
    info = {
        "installed": bool(find_manifest_root(source_dir())),
        "release": src_stamp.get("tag"),
        "source": src_stamp.get("source"),
        "keyed": False,
        "version": None,
    }
    key = str(key or "").strip()
    if key:
        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        stamp = _read_stamp(build_dir(fingerprint))
        info["keyed"] = bool(find_manifest_root(build_dir(fingerprint))
                             and stamp.get("key") == fingerprint)
        info["version"] = stamp.get("version")
    return info
