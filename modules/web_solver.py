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


import asyncio
import aiohttp
import time
import json
import re
import webbrowser
import sys
import os
import subprocess
from urllib.parse import quote

import core.state as state

from modules.services.yescaptcha import YesCaptchaService
from modules.services.nopecha import NopeCaptchaService
from modules.services.anticaptcha import AntiCaptchaService
from modules.services.captchaly import CaptchalyService

class WebSolver:
    _manual_lock = asyncio.Lock()
    _solve_queue = asyncio.Queue()
    _processor_task = None
    _verification_futures = {}

    # owobot.com must never see the account's Discord token. It used to be set as a
    # session-wide header, and aiohttp sends session headers to *every* host - so each
    # owobot request carried it. That is both a leak of the credential that owns the
    # Discord account and a plausible reason for owobot to reject its own session check
    # ("Auth session check failed"), after which the captcha is never solved no matter
    # which paid service is configured. The token now goes to discord.com only.
    BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    OAUTH_PAYLOAD = {
        "authorize": True,
        "permissions": "0",
        "integration_type": 0,
        "location_context": {"guild_id": "10000", "channel_id": "10000",
                             "channel_type": 10000},
    }
    VERIFY_URL = "https://owobot.com/api/captcha/verify"
    # matches dashboard.app.CAPTCHA_TTL_S - past this a pending captcha is not something
    # a human is still working on, and holding the queue for it only hurts the rest
    MANUAL_SOLVE_TIMEOUT_S = 15 * 60

    def __init__(self, bot):
        self.bot = bot
        self.site_key = "a6a1d5ce-612d-472d-8e37-7601408fbc09"
        self.auth_url = "https://discord.com/api/v9/oauth2/authorize?client_id=408785106942164992&response_type=code&redirect_uri=https://owobot.com/api/auth/discord/redirect&scope=identify guilds"
        self._reload_service()

    def _reload_service(self):
        cfg = self.bot.config.get('security', {}).get('captcha_solver', {})
        self.api_key = cfg.get('api_key', '')
        self.active_service_name = cfg.get('service', 'yescaptcha').lower()
        self.enabled = cfg.get('enabled', True)
        self.browser_cfg = cfg.get('browser_config', {})

        if self.active_service_name == 'nopecha':
            self.active_key = cfg.get('nopecha_api_key', self.api_key)
            self.service = NopeCaptchaService(self.bot, self.active_key, self.site_key)
        elif self.active_service_name == 'anticaptcha':
            self.active_key = cfg.get('anticaptcha_api_key', self.api_key)
            self.service = AntiCaptchaService(self.bot, self.active_key, self.site_key)
        elif self.active_service_name == 'captchaly':
            self.active_key = cfg.get('captchaly_api_key', self.api_key)
            self.service = CaptchalyService(self.bot, self.active_key, self.site_key)
        else:
            self.active_key = cfg.get('yescaptcha_api_key', self.api_key)
            self.service = YesCaptchaService(self.bot, self.active_key, self.site_key)

    async def get_balance(self):
        self._reload_service()
        return await self.service.get_balance()

    async def solve_hcaptcha(self, retries=3):
        self._reload_service()
        return await self.service.solve_hcaptcha(retries)

    async def _open_owobot_session(self):
        """(session, error) - an aiohttp session holding a logged-in owobot cookie.

        This handshake is what /api/captcha/verify actually authenticates: it checks
        owobot's own session cookie, not the Discord token. Discord mints the redirect,
        GETting that redirect is what sets the cookie on this session, and /api/auth
        confirms owobot accepted it. Caller owns the returned session and must close it.
        """
        session = aiohttp.ClientSession(headers={"User-Agent": self.BROWSER_UA})
        try:
            async with session.post(
                self.auth_url,
                json=self.OAUTH_PAYLOAD,
                headers={"Authorization": self.bot.token,
                         "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    await session.close()
                    # this used to return False with no log at all, so a rejected token
                    # or a rate-limited OAuth looked like "the solver failed" with
                    # nothing to act on
                    return None, (f"Discord refused the owobot OAuth grant "
                                  f"(HTTP {resp.status}): {body}")
                redirect_url = (await resp.json()).get("location")

            if not redirect_url:
                await session.close()
                return None, "Discord returned no owobot redirect for this account."

            async with session.get(redirect_url) as r:
                if r.status >= 400:
                    await session.close()
                    return None, f"owobot rejected the OAuth redirect (HTTP {r.status})."

            async with session.get("https://owobot.com/captcha") as captcha_resp:
                if captcha_resp.status != 200:
                    await session.close()
                    return None, (f"Could not open owobot's captcha page "
                                  f"(HTTP {captcha_resp.status}).")

            async with session.get("https://owobot.com/api/auth") as auth_check:
                if auth_check.status != 200:
                    await session.close()
                    return None, (f"owobot did not accept the session "
                                  f"(HTTP {auth_check.status}) - authorise owobot once "
                                  f"for this account in a browser and retry.")
            return session, ""
        except Exception as e:
            await session.close()
            return None, f"{type(e).__name__}: {e}"

    async def _post_verify(self, session, solution):
        """(ok, detail) - hand one hcaptcha token to owobot on an authenticated session."""
        verify_headers = {
            "Referer": "https://owobot.com/captcha",
            "Origin": "https://owobot.com",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        async with session.post(self.VERIFY_URL, json={"token": solution},
                                headers=verify_headers) as verify_resp:
            if verify_resp.status == 200:
                return True, ""
            body = (await verify_resp.text())[:200]
            return False, (f"owobot rejected the captcha token "
                           f"(HTTP {verify_resp.status}): {body}")

    async def submit_manual_token(self, solution):
        """(ok, detail) - verify an hcaptcha token this account did not solve itself.

        The dashboard's Solve box used to POST straight at /api/captcha/verify carrying
        only the Discord token and no owobot cookie, which owobot answers 401 - so it
        could never succeed, for any account, with any token. Reuse the one handshake
        that is known to work.
        """
        if not solution:
            return False, "No captcha token was supplied."
        session, error = await self._open_owobot_session()
        if session is None:
            self.bot.log("ERROR", f"Dashboard captcha verify could not sign in to "
                                  f"owobot: {error}")
            return False, error
        try:
            ok, detail = await self._post_verify(session, solution)
        except Exception as e:
            self.bot.log("ERROR", f"Dashboard captcha verify failed: {e}")
            return False, f"{type(e).__name__}: {e}"
        finally:
            await session.close()
        if ok:
            # deliberately not SUCCESS: state.log_command counts any SUCCESS line
            # containing "verified"/"resuming" as a solved captcha, and
            # Security._resume_after_solve is the one place that logs it
            self.bot.log("INFO", "owobot accepted the captcha token from the dashboard.")
        else:
            self.bot.log("ERROR", detail)
        return ok, detail

    async def auto_verify(self, tries=3):
        self._reload_service()
        if not self.active_key and self.active_service_name != 'nopecha':
            self.bot.log("ERROR", f"{self.active_service_name.capitalize()} API key missing in settings.")
            return False

        # A service returns -1 when its balance could not be read at all (network error,
        # unparseable reply). Refusing to solve on that is wrong: an unreadable balance is
        # not an empty one, and the solve itself is what tells us whether the key works.
        minimums = {'yescaptcha': 30, 'nopecha': 1, 'anticaptcha': 0.5, 'captchaly': 0.005}
        balance = await self.get_balance()
        floor = minimums.get(self.active_service_name)
        if balance is not None and balance >= 0 and floor is not None and balance < floor:
            self.bot.log("ERROR", f"{self.active_service_name.capitalize()} balance too low: "
                                  f"{balance} (needs at least {floor}) - not spending an "
                                  f"attempt on it.")
            return False
        if balance is None or balance < 0:
            self.bot.log("WARN", f"{self.active_service_name.capitalize()} balance could not "
                                 f"be read - attempting the solve anyway.")

        session, error = await self._open_owobot_session()
        if session is None:
            self.bot.log("ERROR", f"Auto-verification could not sign in to owobot: {error}")
            return False

        try:
            solution = await self.solve_hcaptcha(tries)
            if not solution:
                return False

            ok, detail = await self._post_verify(session, solution)
            if ok:
                # not SUCCESS on purpose - see submit_manual_token
                self.bot.log("INFO", "owobot accepted the captcha token.")
                self.mark_verification_done(str(self.bot.user.id))
                return True
            self.bot.log("ERROR", f"Verification failed: {detail}")
            return False
        except Exception as e:
            self.bot.log("ERROR", f"Auto-verification failed: {e}")
            return False
        finally:
            await session.close()

    @classmethod
    def enqueue_manual_solve(cls, bot_id, captcha_url=None):
        cls._solve_queue.put_nowait((bot_id, captcha_url or "https://owobot.com/captcha"))
        if cls._processor_task is None or cls._processor_task.done():
            cls._processor_task = asyncio.create_task(cls._manual_processor())

    @classmethod
    async def _manual_processor(cls):
        while True:
            try:
                bot_id, captcha_url = await cls._solve_queue.get()

                async with cls._manual_lock:
                    future = asyncio.get_event_loop().create_future()
                    cls._verification_futures[bot_id] = future

                    bot = cls._get_bot_by_user_id(bot_id)
                    if not bot:
                        cls._verification_futures.pop(bot_id, None)
                        cls._solve_queue.task_done()
                        continue

                    username = getattr(bot, 'username', bot_id)
                    bot.log("SECURITY", f"[QUEUE] Manual solve started for {username} - {captcha_url}")

                    try:
                        from dashboard.app import register_captcha_challenge
                        register_captcha_challenge(bot_id, {"account_name": username, "captcha_url": captcha_url})
                    except Exception as e:
                        bot.log("ERROR", f"[QUEUE] Failed to register captcha for dashboard: {e}")

                    async def alert_loop():
                        start = time.time()
                        last_alert = 0
                        while not future.done():
                            elapsed = int(time.time() - start)
                            if elapsed > 0 and elapsed % 60 == 0 and elapsed != last_alert:
                                mins = elapsed // 60
                                secs = elapsed % 60
                                if mins == 0:
                                    bot.log("SECURITY", f"[QUEUE] {username}: {secs}s elapsed – captcha still pending")
                                elif mins < 10:
                                    bot.log("SECURITY", f"[QUEUE] {username}: {mins}m {secs}s elapsed – captcha still pending")
                                else:
                                    bot.log("SECURITY", f"[QUEUE] {username}: {mins}m {secs}s elapsed – OVER 10 MINUTES! Solve now to avoid strike!")
                                last_alert = elapsed
                            await asyncio.sleep(1)

                    alert_task = asyncio.create_task(alert_loop())

                    sec_cfg = bot.config.get("security", {})
                    if not getattr(bot, 'is_mobile', False):
                        auto_open = sec_cfg.get("open_captcha_url_on_pc", False)
                    else:
                        auto_open = sec_cfg.get("open_captcha_url_on_mobile", False)

                    if auto_open:
                        bot.log("SYS", f"[QUEUE] Opening captcha for {username}...")
                        success = await cls.open_in_browser(captcha_url, bot=bot)
                        if not success:
                            bot.log("ERROR", f"[QUEUE] Failed to open browser for {username}")

                    # Bounded on purpose: the future only ever resolves on a real solve,
                    # so an untimed await held _manual_lock forever - one captcha nobody
                    # answered blocked the manual queue for every other account, and the
                    # dashboard kept the challenge in the bell to match.
                    solved = True
                    try:
                        solved = bool(await asyncio.wait_for(
                            future, timeout=cls.MANUAL_SOLVE_TIMEOUT_S))
                    except asyncio.TimeoutError:
                        solved = False

                    if solved:
                        # not SUCCESS: state.log_command counts any SUCCESS line holding
                        # "verified" as a solved captcha, and Security._resume_after_solve
                        # already logs the one that counts
                        bot.log("SECURITY", f"[QUEUE] {username}: Manual captcha VERIFIED!")
                    else:
                        mins = cls.MANUAL_SOLVE_TIMEOUT_S // 60
                        bot.log("WARN", f"[QUEUE] {username}: manual solve given up "
                                        f"(no answer within {mins}m, or the account "
                                        f"stopped) - releasing the slot for other "
                                        f"accounts.")
                        try:
                            from dashboard.app import clear_captcha_challenge
                            clear_captcha_challenge(bot_id)
                        except Exception:
                            pass

                    alert_task.cancel()
                    try:
                        await alert_task
                    except asyncio.CancelledError:
                        pass

                    cls._verification_futures.pop(bot_id, None)
                    cls._solve_queue.task_done()

            except Exception as e:
                import traceback
                print(f"[QUEUE ERROR] Manual processor crashed: {e}")
                traceback.print_exc()

    @classmethod
    def _get_bot_by_user_id(cls, user_id):
        try:
            for bot in getattr(state, 'bot_instances', []):
                if hasattr(bot, 'user') and bot.user and str(bot.user.id) == str(user_id):
                    return bot
            return None
        except Exception:
            return None

    @classmethod
    def mark_verification_done(cls, bot_id):
        future = cls._verification_futures.get(bot_id)
        if future and not future.done():
            future.set_result(True)

        try:
            from dashboard.app import clear_captcha_challenge
            clear_captcha_challenge(bot_id)
        except Exception as e:
            bot = cls._get_bot_by_user_id(bot_id)
            if bot:
                bot.log("ERROR", f"Failed to clear captcha challenge: {e}")

    @classmethod
    def abandon_manual_solve(cls, bot_id):
        """Give up this account's manual solve without claiming it was verified.

        Called when the account stops. Without it the processor sat on _manual_lock
        waiting for a solve that can never arrive, so no other account could take the
        single manual-solve slot until the timeout expired.

        Resolves the future with False rather than cancelling it - a cancelled future
        would raise CancelledError inside _manual_processor and kill the processor task
        for every other account.
        """
        future = cls._verification_futures.get(str(bot_id))
        if future is not None and not future.done():
            future.set_result(False)

    @staticmethod
    async def open_in_browser(captcha_url=None, bot=None):
        if not bot:
            return False

        auth_url = "https://discord.com/api/v9/oauth2/authorize?client_id=408785106942164992&response_type=code&redirect_uri=https://owobot.com/api/auth/discord/redirect&scope=identify guilds"

        headers = {
            "Authorization": bot.token,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                auth_payload = {
                    "authorize": True,
                    "permissions": "0",
                    "integration_type": 0,
                    "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000}
                }

                full_auth_url = auth_url
                if captcha_url:
                    full_auth_url += f"&state={quote(captcha_url)}"

                async with session.post(full_auth_url, json=auth_payload) as resp:
                    if resp.status != 200:
                        bot.log("ERROR", f"Browser Solver: OAuth failed (Status {resp.status})")
                        if captcha_url:
                            bot.log("SYS", "OAuth failed. Opening raw captcha URL as fallback.")
                            _open_url(captcha_url, bot)
                        return False

                    auth_data = await resp.json()
                    redirect_url = auth_data.get("location")

                if redirect_url:
                    bot.log("SYS", f"Opening Auth Login for {bot.username}...")
                    _open_url(redirect_url, bot)
                    return True
                return False
            except Exception as e:
                bot.log("ERROR", f"Browser solver start failed: {e}")
                return False

def _open_url(url, bot):
    if getattr(bot, 'is_mobile', False):
        try:
            subprocess.Popen(["termux-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            bot.log("SYS", "Opened URL using termux-open")
            return
        except FileNotFoundError:
            try:
                subprocess.Popen(["am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                bot.log("SYS", "Opened URL using am start")
                return
            except FileNotFoundError:
                bot.log("WARN", "Failed to open URL on mobile. Install termux-open or use dashboard.")
                return

    opened = False
    try:
        if webbrowser.open_new_tab(url):
            bot.log("SYS", "Opened URL using webbrowser module")
            opened = True
    except Exception:
        pass

    if not opened:
        try:
            if sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                bot.log("SYS", "Opened URL using Windows start command")
                opened = True
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                bot.log("SYS", "Opened URL using macOS open command")
                opened = True
            else:
                try:
                    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    bot.log("SYS", "Opened URL using xdg-open")
                    opened = True
                except FileNotFoundError:
                    try:
                        subprocess.Popen(["sensible-browser", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        bot.log("SYS", "Opened URL using sensible-browser")
                        opened = True
                    except FileNotFoundError:
                        pass
        except Exception as e:
            bot.log("WARN", f"Failed command line opener fallback: {e}")

    if not opened:
        bot.log("WARN", "All browser opening methods failed. Please use dashboard to solve captcha manually.")


def setup_web_solver(bot):
    return WebSolver(bot)