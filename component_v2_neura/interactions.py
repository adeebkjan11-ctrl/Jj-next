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


import aiohttp
import asyncio
import json
import base64
import uuid
import time
import re
import random
from datetime import datetime

DISCORD_EPOCH = 1420070400000
MIN_BUILD_NUMBER = 310000


class InteractionManager:

    def __init__(self, bot):
        self.bot = bot
        self._build_number = MIN_BUILD_NUMBER
        self._last_fetch = 0
        self._installation_id = str(uuid.uuid4()).replace('-', '')[:32]
        self._session = None

        self.chrome_version = f"{random.randint(124, 127)}.0.0.0"
        self.user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{self.chrome_version} Safari/537.36"

    # ── plumbing ─────────────────────────────────────────────────────────────

    async def _get_session(self):
        """One session per account, routed through that account's proxy."""
        if self._session is not None and not self._session.closed:
            return self._session

        proxy_url = getattr(self.bot, 'proxy_url', None)
        if proxy_url and proxy_url.startswith(("socks4://", "socks5://")):
            try:
                from aiohttp_socks import ProxyConnector
                self._session = aiohttp.ClientSession(connector=ProxyConnector.from_url(proxy_url, rdns=True))
                return self._session
            except Exception:
                pass
        self._session = aiohttp.ClientSession()
        return self._session

    def _proxy_kwargs(self):
        proxy_url = getattr(self.bot, 'proxy_url', None)
        if proxy_url and not proxy_url.startswith(("socks4://", "socks5://")):
            return {"proxy": proxy_url, "proxy_auth": getattr(self.bot, 'proxy_auth', None)}
        return {}

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def _nonce():
        """Discord expects a snowflake-shaped nonce on interactions."""
        return str(((int(time.time() * 1000) - DISCORD_EPOCH) << 22) + random.getrandbits(22))

    def _session_id(self):
        ws = getattr(self.bot, 'ws', None)
        return getattr(ws, 'session_id', None)

    async def _fetch_build_number(self):
        now = time.time()
        if now - self._last_fetch < 43200 and self._build_number > MIN_BUILD_NUMBER:
            return self._build_number

        try:
            session = await self._get_session()
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get("https://discord.com/login", timeout=timeout, **self._proxy_kwargs()) as resp:
                text = await resp.text()

            # the build number lives in one of the chunk bundles the login page pulls in
            candidates = re.findall(r'assets/(?:web\.)?([\w\.\-]+?)\.js', text)
            for name in reversed(candidates[-6:]):
                url = f"https://static.discord.com/assets/{name}.js"
                try:
                    async with session.get(url, timeout=timeout, **self._proxy_kwargs()) as resp:
                        if resp.status != 200:
                            continue
                        js = await resp.text()
                except Exception:
                    continue

                match = re.search(r'buildNumber\D{0,20}?(\d{6})', js)
                if match:
                    self._build_number = int(match.group(1))
                    self._last_fetch = now
                    break
        except Exception:
            pass
        return self._build_number

    def _generate_super_properties(self, build_number):
        props = {
            "os": "Windows",
            "browser": "Chrome",
            "device": "",
            "system_locale": "en-US",
            "browser_user_agent": self.user_agent,
            "browser_version": self.chrome_version,
            "os_version": "10",
            "referrer": "",
            "referring_domain": "",
            "referrer_current": "",
            "referring_domain_current": "",
            "release_channel": "stable",
            "client_build_number": build_number,
            "client_event_source": None,
            "has_client_mods": False,
            "client_launch_id": str(uuid.uuid4()),
            "launch_signature": str(uuid.uuid4()),
            "client_app_state": "focused",
            "client_heartbeat_session_id": str(uuid.uuid4())
        }
        return base64.b64encode(json.dumps(props, separators=(',', ':')).encode()).decode()

    async def _get_headers(self, channel_id=None, guild_id=None):
        bn = await self._fetch_build_number()
        sp = self._generate_super_properties(bn)
        tz = datetime.now().astimezone().tzname() or "UTC"

        referer = "https://discord.com/channels/@me"
        if guild_id and channel_id:
            referer = f"https://discord.com/channels/{guild_id}/{channel_id}"
        elif channel_id:
            referer = f"https://discord.com/channels/@me/{channel_id}"

        major_ver = self.chrome_version.split('.')[0]
        return {
            "Authorization": self.bot.token,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Super-Properties": sp,
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": tz,
            "User-Agent": self.user_agent,
            "Origin": "https://discord.com",
            "Referer": referer,
            "Sec-CH-UA": f'"Not/A)Brand";v="8", "Chromium";v="{major_ver}", "Google Chrome";v="{major_ver}"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-CH-UA-Platform-Version": '"15.0.0"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Debug-Options": "bugReporterEnabled",
            "X-Discord-Features": "quests",
            "X-Installation-Id": self._installation_id
        }

    # ── clicking ─────────────────────────────────────────────────────────────

    async def click_button(self, custom_id, message, guild_id=None):
        if not custom_id or not message:
            return False

        return await self.click_button_raw(
            custom_id=custom_id,
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=guild_id or (message.guild.id if message.guild else None),
            author_id=message.author.id,
            flags=message.flags.value
        )

    async def click_component(self, component, message_id, channel_id, author_id, guild_id=None, flags=0, values=None):
        """Click a V2Component - picks the right component_type for buttons and selects."""
        if component is None:
            return False
        if component.name == "button":
            return await self.click_button_raw(
                custom_id=component.custom_id,
                message_id=message_id,
                channel_id=channel_id,
                author_id=author_id,
                guild_id=guild_id,
                flags=flags,
            )
        if values is None and component.options:
            values = [component.options[0]["value"]]
        return await self.click_button_raw(
            custom_id=component.custom_id,
            message_id=message_id,
            channel_id=channel_id,
            author_id=author_id,
            guild_id=guild_id,
            flags=flags,
            component_type=component.type,
            values=values or [],
        )

    async def click_button_raw(self, custom_id, message_id, channel_id, author_id,
                               guild_id=None, flags=0, component_type=2, values=None, retries=3):
        if not custom_id or not message_id or not channel_id:
            return False

        session_id = self._session_id()
        if not session_id:
            self.bot.log("ERROR", "Interaction skipped: no gateway session yet.")
            return False

        data = {"component_type": component_type, "custom_id": custom_id}
        if values is not None:
            data["values"] = list(values)

        payload = {
            "type": 3,
            "nonce": self._nonce(),
            "application_id": str(author_id),
            "guild_id": str(guild_id) if guild_id else None,
            "channel_id": str(channel_id),
            "message_id": str(message_id),
            "session_id": session_id,
            "message_flags": int(flags or 0),
            "data": data,
        }

        headers = await self._get_headers(channel_id=channel_id, guild_id=guild_id)
        session = await self._get_session()

        for attempt in range(1, max(1, retries) + 1):
            try:
                async with session.post(
                    "https://discord.com/api/v9/interactions",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                    **self._proxy_kwargs()
                ) as resp:
                    if resp.status in (200, 202, 204):
                        return True

                    body = await resp.text()
                    if resp.status == 429:
                        try:
                            retry_after = float((await resp.json()).get("retry_after", 1.0))
                        except Exception:
                            retry_after = 1.0
                        self.bot.log("COOLDOWN", f"Interaction rate limited, retrying in {retry_after:.1f}s")
                        await asyncio.sleep(min(retry_after, 30) + 0.25)
                        payload["nonce"] = self._nonce()
                        continue

                    if resp.status in (500, 502, 503, 504) and attempt < retries:
                        await asyncio.sleep(1.5 * attempt)
                        payload["nonce"] = self._nonce()
                        continue

                    self.bot.log("ERROR", f"Interaction failed ({resp.status}): {body[:300]}")
                    return False
            except asyncio.TimeoutError:
                if attempt < retries:
                    continue
                self.bot.log("ERROR", "Interaction timed out")
                return False
            except Exception as e:
                self.bot.log("ERROR", f"Interaction error: {e}")
                return False
        return False


def setup_interactions(bot):
    existing = getattr(bot, 'interactions', None)
    if isinstance(existing, InteractionManager):
        # on_ready fires again on every reconnect; keep the cached build number
        return existing
    return InteractionManager(bot)
