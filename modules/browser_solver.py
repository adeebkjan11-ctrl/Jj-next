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

"""Solve OwO's hCaptcha with a locally installed browser instead of a paid service.

Why this exists
---------------
OwO fronts its captcha with hCaptcha sitekey ``a6a1d5ce-612d-472d-8e37-7601408fbc09``.
``api.hcaptcha.com/checksiteconfig`` answers that sitekey with ``{"c": {"type": "hsw"}}``
and ``features: {"enc_get_req": true}`` - the challenge body that ``/getcaptcha`` returns
is *encrypted*, and only hCaptcha's own obfuscated WASM holds the key. So there is no
HTTP-only solve: the challenge cannot even be read outside a browser, let alone answered.

Inside a browser hCaptcha decrypts and renders it itself, which is what this module uses.
It carries the authenticated owobot session into a real Chrome/Edge over the DevTools
protocol, so:

* when hCaptcha issues a token from its own risk score - which it does on a clean
  residential IP - the solve is fully automatic and costs nothing, and
* when it serves a visual challenge instead, the operator answers that one challenge in
  a window that is already logged in, and the token is submitted for them.

What it deliberately does *not* do is pretend to answer the visual challenge. hCaptcha
currently serves OwO's sitekey tasks like "drag each character to its matching character"
and "click on the THREE characters that are partly blocked by a line", drawn into a
single ``<canvas>`` with no fetchable tiles, and it rotates between them. Guessing at
those would burn the account's attempts, so an unsupported challenge is reported as
exactly that and handed back to the caller.

What it *can* do instead is let NopeCHA answer it: with a key in
``security.captcha_solver.nopecha_booster_key`` the NopeCHA extension is loaded into this
same browser (see ``modules/nopecha_extension.py``) and solves the challenge in-page.
That is the only way a NopeCHA Discord-boost key can be spent - those keys are
extension-only and ``api.nopecha.com`` rejects them - and it needs no new token
plumbing, because the loop below already reads whatever token the page ends up holding.

No new dependencies: the DevTools protocol is plain JSON over a WebSocket, and aiohttp is
already required.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp

# ── the sitekey and endpoints OwO actually uses ─────────────────────────────
SITE_KEY = "a6a1d5ce-612d-472d-8e37-7601408fbc09"
CAPTCHA_PAGE = "https://owobot.com/captcha"
AUTH_API = "https://owobot.com/api/auth"
VERIFY_API = "https://owobot.com/api/captcha/verify"
OAUTH_URL = ("https://discord.com/api/v9/oauth2/authorize?client_id=408785106942164992"
             "&response_type=code&redirect_uri=https://owobot.com/api/auth/discord/redirect"
             "&scope=identify guilds")

# A headless Chrome advertises "HeadlessChrome/NNN", which is the loudest bot signal
# there is - hCaptcha reads the UA before it scores anything else, so it is rewritten.
_UA_FALLBACK = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_WINDOWS_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
)
_UNIX_BROWSERS = (
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
    "/usr/bin/chromium-browser", "/usr/bin/microsoft-edge", "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser():
    """Absolute path of an installed Chromium-family browser, or None."""
    override = os.environ.get("LAZYFARMERS_BROWSER")
    if override and os.path.isfile(override):
        return override
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (_WINDOWS_BROWSERS if os.name == "nt" else _UNIX_BROWSERS):
        if os.path.isfile(candidate):
            return candidate
    return None


def browser_status():
    """Why the browser solver is unavailable, or None when it can run."""
    if not find_browser():
        where = ("Chrome or Edge" if os.name == "nt"
                 else "google-chrome, chromium or microsoft-edge")
        return (f"no Chromium-family browser found - install {where}, or point "
                "LAZYFARMERS_BROWSER at the executable")
    return None


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class CDPError(Exception):
    pass


class CDP:
    """A minimal Chrome DevTools Protocol client over aiohttp.

    Session-aware, because the hCaptcha widget lives in an out-of-process iframe whose
    network and DOM are on a *different* CDP session than the page.

    The reader task must never await a command reply: the future it would wait on is
    resolved by the reader itself, which deadlocks the whole connection. Events are
    therefore handed to a separate queue-driven handler that is allowed to send.
    """

    def __init__(self):
        self._proc = None
        self._ws = None
        self._http = None
        self._reader = None
        self._handler = None
        self._pending = {}
        self._events = None
        self._n = 0
        self._profile = None
        self.page = None          # sessionId of the top-level page
        self.sessions = {}        # sessionId -> target url
        self._proxy_auth = None
        self.extensions = []      # ids of extensions that are actually running
        self.extension_error = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def launch(self, headless=True, proxy_url=None, proxy_auth=None,
                     window="1280,900", timeout=30, extensions=None):
        exe = find_browser()
        if not exe:
            raise CDPError(browser_status())
        port = _free_port()
        self._profile = tempfile.mkdtemp(prefix="lazyfarmers-cdp-")
        self._proxy_auth = proxy_auth
        args = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._profile}",
            f"--window-size={window}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--disable-dev-shm-usage",
            # this is what keeps navigator.webdriver false
            "--disable-blink-features=AutomationControlled",
        ]
        paths = [p for p in (extensions or []) if p]
        if paths:
            # --disable-extensions wins over --load-extension, so an extension can
            # only be loaded by swapping it for the -except form. Both flags are dead
            # on current Google Chrome (see _load_extensions) but still the whole
            # mechanism on Chromium, Edge and Chrome < 137, so they stay.
            joined = ",".join(paths)
            args += [f"--disable-extensions-except={joined}", f"--load-extension={joined}",
                     # re-enables the two flags above on Chrome 137-141
                     "--disable-features=DisableLoadExtensionCommandLineSwitch",
                     # unlocks the Extensions CDP domain, which is the supported way in
                     "--enable-unsafe-extension-debugging"]
        else:
            args.append("--disable-extensions")
        if headless:
            args.append("--headless=new")
        if proxy_url:
            # strip any credentials: chrome ignores them in the flag and they would only
            # leak the password into the process list. They are answered over CDP below.
            bare = proxy_url
            if "@" in bare:
                scheme, _, rest = bare.partition("://")
                bare = f"{scheme}://{rest.rsplit('@', 1)[-1]}"
            args.append(f"--proxy-server={bare}")
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)

        self._http = aiohttp.ClientSession()
        ws_url = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                async with self._http.get(f"http://127.0.0.1:{port}/json/version") as r:
                    ws_url = (await r.json())["webSocketDebuggerUrl"]
                break
            except Exception:
                if self._proc.poll() is not None:
                    raise CDPError("the browser exited immediately after launch")
                await asyncio.sleep(0.2)
        if not ws_url:
            raise CDPError(f"browser did not open a debugging port within {timeout}s")

        self._ws = await self._http.ws_connect(ws_url, max_msg_size=0, heartbeat=30)
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())
        self._handler = asyncio.create_task(self._event_loop())

        if paths:
            await self._load_extensions(paths)

        async with self._http.get(f"http://127.0.0.1:{port}/json/list") as r:
            targets = await r.json()
        pages = [t for t in targets if t.get("type") == "page"]
        # an extension may open its own tab on install; never drive that one
        target = next((t for t in pages
                       if not str(t.get("url", "")).startswith("chrome-extension://")),
                      pages[0] if pages else None)
        if not target:
            raise CDPError("the browser exposed no page target")
        self.page = (await self.send("Target.attachToTarget", targetId=target["id"],
                                     flatten=True)).get("sessionId")
        if not self.page:
            raise CDPError("could not attach to the browser page")
        self.sessions[self.page] = target.get("url", "")
        for method in ("Page.enable", "Runtime.enable", "Network.enable"):
            await self.send(method, sess=self.page)
        await self.send("Target.setAutoAttach", sess=self.page, autoAttach=True,
                        waitForDebuggerOnStart=False, flatten=True)

        ua = await self.evaluate("navigator.userAgent") or _UA_FALLBACK
        await self.send("Network.setUserAgentOverride", sess=self.page,
                        userAgent=ua.replace("HeadlessChrome", "Chrome"),
                        acceptLanguage="en-US,en;q=0.9", platform="Win32")
        if proxy_auth:
            # chrome cannot take proxy credentials on the command line; the only way in
            # is to answer the 407 challenge over CDP
            await self.send("Fetch.enable", sess=self.page, handleAuthRequests=True)
        return self

    async def _extension_ids(self, wait=0.0):
        """Ids of extensions the browser is actually running.

        An extension that loaded owns targets of its own (an MV3 service worker, a
        background page, maybe a tab), so their ``chrome-extension://<id>/`` urls are
        proof the load took - which no command-line flag ever reports.
        """
        deadline = time.time() + max(0.0, wait)
        while True:
            found = []
            try:
                res = await self.send("Target.getTargets")
            except CDPError:
                return []
            for info in res.get("targetInfos") or []:
                url = str(info.get("url") or "")
                if url.startswith("chrome-extension://"):
                    ident = url.split("/")[2] if len(url.split("/")) > 2 else ""
                    if ident and ident not in found:
                        found.append(ident)
            if found or time.time() >= deadline:
                return found
            await asyncio.sleep(0.4)

    async def _load_extensions(self, paths):
        """Load unpacked extensions over CDP, and record whether it worked.

        The flags in ``launch`` are no longer enough: Google Chrome ignores
        ``--load-extension`` from 137, dropped ``--disable-extensions-except`` at 139
        and stopped honouring the ``DisableLoadExtensionCommandLineSwitch`` opt-out at
        142 - it just prints "--load-extension is not allowed in Google Chrome,
        ignoring" and starts with no extension at all. That is silent: the page loads,
        the captcha appears, and nothing ever answers it. ``Extensions.loadUnpacked``
        is the supported replacement.

        Either route is fine, so this only fails when *neither* produced a running
        extension - and then it says so, instead of leaving the solve to blame the
        wait on NopeCHA credits.
        """
        problems = []
        for path in paths:
            try:
                res = await self.send("Extensions.loadUnpacked", path=path, timeout=60)
            except CDPError as exc:
                problems.append(f"{os.path.basename(path)}: {exc}")
                continue
            ident = res.get("id")
            if ident:
                self.extensions.append(ident)
            else:
                problems.append(f"{os.path.basename(path)}: no extension id returned")
        # the command-line flags may have loaded it already, in which case
        # loadUnpacked is redundant and its complaint means nothing
        running = await self._extension_ids(wait=0.0 if self.extensions else 6.0)
        for ident in running:
            if ident not in self.extensions:
                self.extensions.append(ident)
        if not self.extensions:
            self.extension_error = ("; ".join(problems)
                                    or "no extension target ever appeared")

    async def close(self):
        for task in (self._reader, self._handler):
            if task:
                task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._http:
            try:
                await self._http.close()
            except Exception:
                pass
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                for _ in range(20):
                    if self._proc.poll() is not None:
                        break
                    await asyncio.sleep(0.1)
                else:
                    self._proc.kill()
            except Exception:
                pass
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ── protocol ────────────────────────────────────────────────────────────
    async def send(self, method, sess=None, timeout=30, **params):
        if not self._ws or self._ws.closed:
            raise CDPError("the devtools connection is closed")
        self._n += 1
        mid = self._n
        message = {"id": mid, "method": method, "params": params}
        if sess:
            message["sessionId"] = sess
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send_json(message)
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise CDPError(f"{method} timed out after {timeout}s")
        if isinstance(result, dict) and "__error" in result:
            raise CDPError(f"{method}: {result['__error']}")
        return result

    async def _read_loop(self):
        try:
            async for msg in self._ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if "id" in data:
                    fut = self._pending.pop(data["id"], None)
                    if fut and not fut.done():
                        if "error" in data:
                            fut.set_result({"__error": data["error"]})
                        else:
                            fut.set_result(data.get("result") or {})
                else:
                    self._events.put_nowait(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _event_loop(self):
        while True:
            try:
                data = await self._events.get()
                method = data.get("method")
                params = data.get("params", {})
                sess = data.get("sessionId")
                if method == "Target.attachedToTarget":
                    child = params["sessionId"]
                    self.sessions[child] = params.get("targetInfo", {}).get("url", "")
                    for m in ("Runtime.enable", "Network.enable"):
                        try:
                            await self.send(m, sess=child)
                        except Exception:
                            pass
                    try:
                        await self.send("Target.setAutoAttach", sess=child, autoAttach=True,
                                        waitForDebuggerOnStart=False, flatten=True)
                        await self.send("Runtime.runIfWaitingForDebugger", sess=child)
                    except Exception:
                        pass
                elif method == "Target.detachedFromTarget":
                    self.sessions.pop(params.get("sessionId"), None)
                elif method == "Fetch.authRequired" and self._proxy_auth:
                    user, password = self._proxy_auth
                    try:
                        await self.send("Fetch.continueWithAuth", sess=sess,
                                        requestId=params["requestId"],
                                        authChallengeResponse={"response": "ProvideCredentials",
                                                               "username": user,
                                                               "password": password})
                    except Exception:
                        pass
                elif method == "Fetch.requestPaused":
                    try:
                        await self.send("Fetch.continueRequest", sess=sess,
                                        requestId=params["requestId"])
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    # ── conveniences ────────────────────────────────────────────────────────
    async def evaluate(self, expression, sess=None, await_promise=False, timeout=30):
        result = await self.send("Runtime.evaluate", sess=sess or self.page,
                                 expression=expression, returnByValue=True,
                                 awaitPromise=await_promise, timeout=timeout)
        return (result.get("result") or {}).get("value")

    async def navigate(self, url, wait=1.0):
        await self.send("Page.navigate", sess=self.page, url=url)
        if wait:
            await asyncio.sleep(wait)

    async def set_cookie(self, name, value, domain, path="/", secure=True, http_only=True):
        return await self.send("Network.setCookie", sess=self.page, name=name, value=value,
                               domain=domain, path=path, secure=secure, httpOnly=http_only)

    async def screenshot(self):
        """Base64 PNG of the current viewport, or None."""
        try:
            result = await self.send("Page.captureScreenshot", sess=self.page, format="png")
            return result.get("data")
        except Exception:
            return None


# ── reading the token out of owobot's captcha page ──────────────────────────
# vue-hcaptcha writes the solved token into the standard response field, and the global
# hcaptcha object can also be asked directly. Either is enough.
_TOKEN_JS = """
(() => {
  const el = document.querySelector('textarea[name="h-captcha-response"],'
                                  + 'input[name="h-captcha-response"]');
  if (el && el.value) return el.value;
  try { const t = window.hcaptcha && window.hcaptcha.getResponse();
        if (t) return t; } catch (e) {}
  return null;
})()
"""

# What the page is doing right now. owobot only mounts the hCaptcha widget while the
# account genuinely owes a captcha - with nothing pending it renders "you're not a bot,
# <name>! You're free to go!" and never loads hCaptcha at all, so "no widget" has to be
# distinguished from "widget up but unsolved" or every clean account looks like a timeout.
_STATE_JS = """
(() => {
  const frames = [...document.querySelectorAll('iframe')];
  const hFrames = frames.filter(f => /hcaptcha/i.test(f.src || '')
                                  || /captcha/i.test(f.title || ''));
  const challenge = hFrames.find(f => f.getBoundingClientRect().height > 300);
  const text = (document.body.innerText || '').replace(/\\s+/g, ' ');
  return JSON.stringify({
    widget: hFrames.length > 0 || typeof window.hcaptcha !== 'undefined',
    challengeOpen: !!challenge,
    cleared: /not a bot|free to go|verified/i.test(text),
    text: text.slice(0, 200)
  });
})()
"""


class BrowserSolver:
    """Solve OwO's hCaptcha in a locally installed browser. No service, no API key."""

    def __init__(self, bot):
        self.bot = bot
        self._busy = asyncio.Lock()

    # ── config ──────────────────────────────────────────────────────────────
    def _solver_cfg(self):
        return (self.bot.config.get("security", {}).get("captcha_solver", {}) or {})

    def _cfg(self):
        return (self._solver_cfg().get("browser_solver", {}) or {})

    def _nopecha_cfg(self):
        return (self._cfg().get("nopecha", {}) or {})

    def nopecha_key(self):
        """The key the NopeCHA *extension* should run with, if any.

        The precedence itself lives in ``modules.nopecha_extension`` so the dashboard
        resolves the key exactly the way the browser will.
        """
        from modules.nopecha_extension import resolve_key
        return resolve_key(self._solver_cfg())

    def nopecha_enabled(self):
        return bool(self._nopecha_cfg().get("enabled", True)) and bool(self.nopecha_key())

    async def _prepare_nopecha(self):
        """Unpack/patch the NopeCHA extension. Returns its directory or None."""
        if not self.nopecha_enabled():
            return None
        nope = self._nopecha_cfg()
        try:
            from modules.nopecha_extension import ensure_extension
            path, error = await ensure_extension(
                self.nopecha_key(),
                path_hint=nope.get("extension_path"),
                auto_download=bool(nope.get("auto_download", True)),
                log=self.bot.log,
            )
        except Exception as exc:
            self.bot.log("WARN", f"NopeCHA extension unavailable ({type(exc).__name__}: {exc}); "
                                 f"solving without it.")
            return None
        if error:
            self.bot.log("WARN", f"NopeCHA extension unavailable: {error}")
            return None
        return path

    @property
    def enabled(self):
        """Only runs as the NopeCHA extension's host - never as a key-free solver."""
        return bool(self._cfg().get("enabled", True)) and self.nopecha_enabled()

    def available(self):
        return find_browser() is not None

    # ── owobot session ──────────────────────────────────────────────────────
    async def _owobot_session(self):
        """Authenticate to owobot over HTTP and return (connect.sid, identity).

        The Discord token stays in this process - only the resulting site cookie is
        handed to the browser.
        """
        headers = {
            "Authorization": self.bot.token,
            "Content-Type": "application/json",
            "User-Agent": _UA_FALLBACK,
        }
        jar = aiohttp.CookieJar(unsafe=True)
        kwargs = {"headers": headers, "cookie_jar": jar}
        async with aiohttp.ClientSession(**kwargs) as session:
            payload = {"authorize": True, "permissions": "0", "integration_type": 0,
                       "location_context": {"guild_id": "10000", "channel_id": "10000",
                                            "channel_type": 10000}}
            proxy = getattr(self.bot, "proxy_url", None)
            proxy_auth = getattr(self.bot, "proxy_auth", None)
            req = {}
            if proxy and not proxy.startswith(("socks4://", "socks5://")):
                req = {"proxy": proxy, "proxy_auth": proxy_auth}
            async with session.post(OAUTH_URL, json=payload, **req) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    raise CDPError(f"Discord refused the owobot OAuth grant "
                                   f"(HTTP {resp.status}): {body}")
                location = (await resp.json()).get("location")
            if location:
                async with session.get(location, **req):
                    pass
            sid = next((c.value for c in jar if c.key == "connect.sid"), None)
            if not sid:
                raise CDPError("owobot did not issue a session cookie after OAuth")
            identity = {}
            try:
                async with session.get(AUTH_API, **req) as resp:
                    if resp.status == 200:
                        identity = await resp.json()
            except Exception:
                pass
            return sid, identity

    async def check_pending(self):
        """Ask owobot whether this account is actually under a captcha right now.

        ``/api/auth`` carries ``captcha: {active: bool}``, which is ground truth - far
        better than inferring it from message text. Returns True/False, or None if the
        question could not be answered.
        """
        try:
            _, identity = await self._owobot_session()
        except Exception as exc:
            self.bot.log("DEBUG", f"Browser solver: could not read captcha state: {exc}")
            return None
        captcha = identity.get("captcha") or {}
        return bool(captcha.get("active"))

    # ── the solve ───────────────────────────────────────────────────────────
    async def solve(self, timeout=None, headless=None):
        """Let NopeCHA's extension answer this account's hCaptcha.

        Returns a dict describing exactly what happened::

            {"ok": bool, "how": "passive"|"nopecha-extension"|"cleared"|"not-required"|None,
             "reason": str|None, "challenge": str|None}

        ``how="passive"`` means hCaptcha issued the token from its own risk score and
        nothing had to be answered. ``how="nopecha-extension"`` means NopeCHA's
        extension answered the visual challenge. There is no human-in-the-browser
        path: a challenge the extension cannot answer is reported as a failure so the
        caller can fall back to the dashboard's manual solve.
        """
        cfg = self._cfg()
        if timeout is None:
            timeout = float(cfg.get("timeout_s", 180))
        if not self.nopecha_enabled():
            return {"ok": False, "how": None, "challenge": None,
                    "reason": "no NopeCHA extension key set "
                              "(security.captcha_solver.nopecha_booster_key)"}
        if headless is None:
            # nothing for a human to look at when the extension answers the
            # challenge, and a hidden browser is what works on a server
            headless = bool(self._nopecha_cfg().get("headless", True))
        passive_window = float(cfg.get("passive_window_s", 20))
        widget_wait = float(cfg.get("widget_wait_s", 25))
        nope_wait = float(self._nopecha_cfg().get("solve_wait_s", 120))

        problem = browser_status()
        if problem:
            return {"ok": False, "how": None, "reason": problem, "challenge": None}

        if self._busy.locked():
            return {"ok": False, "how": None, "challenge": None,
                    "reason": "a browser solve is already running for this account"}

        async with self._busy:
            try:
                sid, identity = await self._owobot_session()
            except Exception as exc:
                return {"ok": False, "how": None, "challenge": None, "reason": str(exc)}

            if not (identity.get("captcha") or {}).get("active", True):
                self.bot.log("INFO", "Browser solver: owobot reports no active captcha "
                                     "for this account.")
                return {"ok": True, "how": "not-required", "reason": None, "challenge": None}

            proxy_url = getattr(self.bot, "proxy_url", None)
            proxy_auth = getattr(self.bot, "proxy_auth", None)
            creds = None
            if proxy_auth is not None:
                creds = (getattr(proxy_auth, "login", None), getattr(proxy_auth, "password", None))
                if not creds[0]:
                    creds = None
            if proxy_url and proxy_url.startswith(("socks4://", "socks5://")):
                # chrome takes socks5://host:port but cannot authenticate to one
                if creds:
                    self.bot.log("WARN", "Browser solver: Chrome cannot authenticate to a "
                                         "SOCKS proxy; the solve will use a direct "
                                         "connection instead.")
                    proxy_url, creds = None, None

            extension = await self._prepare_nopecha()
            if not extension:
                return {"ok": False, "how": None, "challenge": None,
                        "reason": "NopeCHA extension could not be prepared"}

            browser = CDP()
            try:
                started = time.time()
                await browser.launch(headless=headless, proxy_url=proxy_url,
                                     proxy_auth=creds,
                                     extensions=[extension] if extension else None)
                if browser.extension_error:
                    # blaming the wait on NopeCHA credits when the extension never
                    # loaded sent operators off buying credits they already had
                    await browser.close()
                    return {"ok": False, "how": None, "challenge": None,
                             "reason": f"this browser would not load the NopeCHA extension "
                                       f"({browser.extension_error}) - Google Chrome 137+ "
                                       f"blocks extension loading for automation, so install "
                                       f"Chromium or Edge"}
                await browser.set_cookie("connect.sid", sid, "owobot.com")
                await browser.navigate(CAPTCHA_PAGE, wait=3.0)

                self.bot.log("SECURITY",
                             f"Browser solver: opened OwO's captcha page in "
                             f"{'a hidden' if headless else 'a visible'} browser "
                             f"({'proxied' if proxy_url else 'direct'})"
                             f" with the NopeCHA extension loaded.")

                deadline = started + timeout
                described = False
                challenge_text = None
                saw_widget = False
                nope_deadline = None
                nope_expired = False
                while time.time() < deadline:
                    token = await browser.evaluate(_TOKEN_JS)
                    if token:
                        if time.time() < started + passive_window:
                            how = "passive"
                        elif not nope_expired and nope_deadline:
                            how = "nopecha-extension"
                        else:
                            how = "interactive"
                        ok, detail = await self._verify(token, sid)
                        if ok:
                            self.bot.log("SUCCESS", {
                                "passive": "Browser solver: captcha solved automatically - no "
                                           "paid service used.",
                                "nopecha-extension": "Browser solver: NopeCHA's extension "
                                                     "answered the challenge and the token was "
                                                     "verified.",
                            }.get(how, "Browser solver: captcha answered and verified."))
                            return {"ok": True, "how": how, "reason": None,
                                    "challenge": challenge_text}
                        return {"ok": False, "how": how, "challenge": challenge_text,
                                "reason": f"owobot rejected the token: {detail}"}

                    state_info = {}
                    raw = await browser.evaluate(_STATE_JS)
                    if raw:
                        try:
                            state_info = json.loads(raw)
                        except Exception:
                            state_info = {}

                    if state_info.get("widget"):
                        saw_widget = True
                    elif state_info.get("cleared"):
                        # owobot renders "you're free to go" and mounts no widget - either
                        # nothing was pending or the page verified us on load
                        self.bot.log("SUCCESS", "Browser solver: owobot says this account is "
                                                "verified; nothing left to solve.")
                        return {"ok": True, "how": "cleared", "reason": None,
                                "challenge": None}

                    if state_info.get("challengeOpen"):
                        if not described:
                            described = True
                            challenge_text = await self._describe_challenge(browser)

                        if not nope_expired:
                            if nope_deadline is None:
                                nope_deadline = time.time() + nope_wait
                                # do not let the outer timeout cut the extension off
                                # halfway through a challenge it is already working on
                                deadline = max(deadline, nope_deadline + 10)
                                self.bot.log("SECURITY",
                                             f"NopeCHA extension: hCaptcha wants a visual answer "
                                             f"- \"{challenge_text or 'unknown challenge'}\". "
                                             f"The extension is answering it "
                                             f"(waiting up to {int(nope_wait)}s).")
                            elif time.time() > nope_deadline:
                                nope_expired = True

                        if nope_expired:
                            return {"ok": False, "how": None, "challenge": challenge_text,
                                    "reason": f"NopeCHA's extension did not answer "
                                              f"\"{challenge_text or 'the challenge'}\" within "
                                              f"{int(nope_wait)}s - out of credits, or it "
                                              f"cannot do this challenge type"}

                    if not saw_widget and time.time() > started + widget_wait:
                        return {"ok": False, "how": None, "challenge": None,
                                "reason": f"owobot's captcha page never loaded an hCaptcha "
                                          f"widget within {int(widget_wait)}s (page says: "
                                          f"{state_info.get('text', '')[:120]!r})"}
                    await asyncio.sleep(1.5)

                return {"ok": False, "how": None, "challenge": challenge_text,
                        "reason": f"no token within {int(timeout)}s"
                                  + (f" - challenge shown: {challenge_text}"
                                     if challenge_text else
                                     " (the widget never issued one)")}
            except Exception as exc:
                return {"ok": False, "how": None, "challenge": None,
                        "reason": f"{type(exc).__name__}: {exc}"}
            finally:
                await browser.close()

    async def _describe_challenge(self, browser):
        """The prompt hCaptcha is showing, read from the challenge iframe."""
        js = ("(()=>{const p=document.querySelector('.prompt-text,.challenge-prompt');"
              "return p?p.innerText.trim().replace(/\\s+/g,' '):null})()")
        for sess in list(browser.sessions):
            try:
                text = await browser.evaluate(js, sess=sess, timeout=8)
            except Exception:
                continue
            if text:
                return text[:120]
        return None

    async def _verify(self, token, sid):
        """Hand the token to owobot. Returns (ok, detail)."""
        headers = {
            "Authorization": self.bot.token,
            "Content-Type": "application/json",
            "User-Agent": _UA_FALLBACK,
            "Referer": CAPTCHA_PAGE,
            "Origin": "https://owobot.com",
            "Accept": "application/json, text/plain, */*",
            "Cookie": f"connect.sid={sid}",
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(VERIFY_API, json={"token": token}) as resp:
                    body = (await resp.text())[:200]
                    return resp.status == 200, f"HTTP {resp.status} {body}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"


def setup_browser_solver(bot):
    return BrowserSolver(bot)
