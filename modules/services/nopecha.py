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

# NopeCHA's Discord-boost reward keys carry extension credits only: api.nopecha.com
# answers them with error 15/18 however much credit they hold. There is no format that
# tells the two apart, so say it here rather than leaving "wrong plan" as the only clue.
EXTENSION_HINT = ("If this key came from boosting NopeCHA's Discord, it only works in "
                  "their browser extension - move it to security.captcha_solver."
                  "nopecha_booster_key and the browser solver will spend it there.")


class NopeCaptchaService:
    def __init__(self, bot, api_key, site_key):
        self.bot = bot
        self.api_key = api_key
        self.site_key = site_key
        self.base_url = "https://api.nopecha.com"

    # NopeCHA reports its error code under "error", not "code". Reading the wrong field
    # meant every error logged as "error None" and, worse, the terminal-code check in
    # solve_hcaptcha never matched - so a permanently dead key (expired plan, bad key,
    # zero credit) burned three attempts with sleeps on every single captcha.
    @staticmethod
    def _error_code(payload):
        if not isinstance(payload, dict):
            return None
        for field in ("error", "code"):
            value = payload.get(field)
            if isinstance(value, int):
                return value
        return None

    async def get_balance(self):
        """Credits available, or -1 when the answer could not be read at all.

        0 used to mean both "no credit" and "the request failed", which made a network
        hiccup look identical to an empty account.
        """
        result = await self._request("GET", "/v1/status")
        if not isinstance(result, dict):
            return -1
        # the plan and its state are the part that actually explains a refusal - an
        # expired plan still reports a plan name, so log both
        plan, status = result.get("plan"), result.get("status")
        if plan or status:
            note = f"NopeCHA plan: {plan or 'unknown'} ({status or 'unknown state'})"
            if str(status or "").lower() not in ("active", "", "none"):
                self.bot.log("ERROR", f"{note} - an inactive plan cannot solve hCaptcha "
                                      f"no matter what the key is.")
            else:
                self.bot.log("SYS", note)
        if result.get("credit") is not None:
            return result["credit"]
        if self._error_code(result) == 12:
            self.bot.log("ERROR", "NopeCHA free IP is banned.")
        return -1

    def _get_headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Basic {self.api_key}"
        return headers

    async def _request(self, method, endpoint, data=None):
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        timeout = 60 if method.upper() == "POST" else 30
        try:
            async with aiohttp.ClientSession() as session:
                if method.upper() == "GET":
                    async with session.get(url, headers=headers, timeout=timeout) as resp:
                        return await self._handle_response(resp)
                else:
                    async with session.post(url, json=data, headers=headers, timeout=timeout) as resp:
                        return await self._handle_response(resp)
        except asyncio.TimeoutError:
            self.bot.log("ERROR", "NopeCHA request timed out")
            return {"error": -1, "message": "Timeout"}
        except Exception as e:
            self.bot.log("ERROR", f"NopeCHA request failed: {e}")
            return {"error": -1, "message": str(e)}

    async def _handle_response(self, response):
        try:
            if response.status == 200:
                return await response.json()
            else:
                try:
                    err = await response.json()
                    code = self._error_code(err)
                    if code == 10:
                        self.bot.log("ERROR", f"NopeCHA invalid request: {err.get('message')}")
                    elif code == 15:
                        self.bot.log("ERROR", f"NopeCHA invalid API key: {err.get('message')}")
                        self.bot.log("WARN", EXTENSION_HINT)
                    elif code == 16:
                        self.bot.log("ERROR", f"NopeCHA out of credit: {err.get('message')}")
                    elif code == 18:
                        self.bot.log("ERROR", f"NopeCHA feature unavailable for current plan: {err.get('message')}")
                        self.bot.log("WARN", EXTENSION_HINT)
                    elif code == 12:
                        self.bot.log("ERROR", f"NopeCHA free tier ineligible (IP banned): {err.get('message')}")
                    elif code == 14:
                        self.bot.log("WARN", f"NopeCHA job incomplete: {err.get('message')}")
                    elif code == 11:
                        self.bot.log("ERROR", f"NopeCHA rate limited: {err.get('message')}")
                    elif code == 9:
                        self.bot.log("ERROR", f"NopeCHA internal server error: {err.get('message')}")
                    else:
                        self.bot.log("ERROR", f"NopeCHA error {code}: {err.get('message')}")
                    return err
                except:
                    if response.status == 520:
                        self.bot.log("ERROR", "NopeCHA HTTP 520 - Cloudflare error (server issue or invalid payload). Retrying...")
                    elif response.status == 429:
                        self.bot.log("ERROR", "NopeCHA rate limit exceeded.")
                    else:
                        text = await response.text()
                        self.bot.log("ERROR", f"NopeCHA HTTP {response.status}: {text[:100]}")
                    return {"error": response.status, "message": await response.text() or "Unknown"}
        except Exception as e:
            self.bot.log("ERROR", f"Failed to parse NopeCHA response: {e}")
            return {"error": -1, "message": str(e)}

    async def solve_hcaptcha(self, retries=3):
        for attempt in range(retries):
            try:
                self.bot.log("SYS", f"Creating NopeCHA task (Attempt {attempt+1}/{retries})...")
                payload = {
                    "sitekey": self.site_key,
                    "url": "https://owobot.com"
                }
                if self.api_key:
                    payload["key"] = self.api_key

                result = await self._request("POST", "/v1/token/hcaptcha", data=payload)

                if result and result.get("data"):
                    job_id = result["data"]
                    self.bot.log("SYS", f"NopeCHA job created: {job_id}")
                    token = await self._poll_for_result(job_id)
                    if token:
                        self.bot.log("SUCCESS", "NopeCHA solved successfully.")
                        return token

                if result and result.get("error"):
                    code = self._error_code(result)
                    if code in (12, 16, 15, 18):
                        # invalid key / no credit / wrong plan / banned IP: retrying
                        # cannot change any of these, so stop and let the caller fall
                        # through to the free browser solve
                        self.bot.log("ERROR", f"NopeCHA cannot solve this (error {code}) "
                                              f"- not retrying.")
                        break
                    if code == 14:
                        continue
                    if code == 11:
                        await asyncio.sleep(5)
                        continue

                    if result.get("error") in (520, -1):
                        self.bot.log("WARN", f"NopeCHA server error, retrying after delay...")
                        await asyncio.sleep(5)
                        continue

                self.bot.log("ERROR", "NopeCHA response missing job ID.")
            except Exception as e:
                self.bot.log("ERROR", f"NopeCHA task failed: {e}")

            if attempt < retries - 1:
                await asyncio.sleep(3)

        return None

    async def _poll_for_result(self, job_id, timeout=120, interval=2):
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                result = await self._request("GET", f"/v1/token/hcaptcha?id={job_id}")
                if result and result.get("data"):
                    return result["data"]
                if result and result.get("error") == 14:
                    await asyncio.sleep(interval)
                    continue
                if result and result.get("error"):
                    self.bot.log("ERROR", f"NopeCHA polling error: {result.get('error')}")
                    break
            except Exception as e:
                self.bot.log("ERROR", f"NopeCHA polling failed: {e}")
                break
        return None