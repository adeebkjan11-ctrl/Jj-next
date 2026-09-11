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


import sys
import asyncio
import time
import re
import os
import threading
import unicodedata
import requests
import random
import json
import discord
from discord.ext import commands
from plyer import notification
import core.state as state

from modules.browser_solver import browser_status as browser_solver_status

class Security(commands.Cog):
    # the settings field each service actually reads. web_solver._reload_service picks
    # the key for the *selected* service only, so the old "is any key set at all" test
    # was wrong in both directions: a NopeCHA key pasted while the service was still
    # yescaptcha started an OAuth dance that then failed on an empty key, and a user
    # with no key for the selected service got the same.
    CAPTCHA_KEY_FIELDS = {
        "yescaptcha": "yescaptcha_api_key",
        "nopecha": "nopecha_api_key",
        "anticaptcha": "anticaptcha_api_key",
        "captchaly": "captchaly_api_key",
    }

    @classmethod
    def _selected_key_field(cls, sol_cfg):
        service = str(sol_cfg.get("service") or "yescaptcha").lower()
        return service, cls.CAPTCHA_KEY_FIELDS.get(service, "yescaptcha_api_key")

    @staticmethod
    def _nopecha_extension_key(sol_cfg):
        """A NopeCHA key that only the extension can spend, if one is configured.

        Keys handed out for boosting NopeCHA's Discord server carry extension
        credits: ``api.nopecha.com`` refuses them outright, so they live in their own
        field and are used by the browser solver, never by the paid API path.
        """
        nope = ((sol_cfg.get("browser_solver") or {}).get("nopecha") or {})
        if not nope.get("enabled", True):
            return ""
        return str(nope.get("key") or sol_cfg.get("nopecha_booster_key") or "").strip()

    def _should_autosolve(self, sol_cfg):
        """True when the auto-solver is on and the *selected* service is usable."""
        if not sol_cfg.get("enabled", True):
            return False
        service, key_field = self._selected_key_field(sol_cfg)
        if sol_cfg.get(key_field):
            return True
        # The extension is a whole solver on its own and holds no account with any of
        # the paid services, so its key settles the "is this configured" question
        # whatever `service` says: that field only picks *which paid provider* would be
        # used, and insisting on one made an extension-only setup log a warning telling
        # the operator to go buy a key they do not need. Checked before the per-service
        # branch so it holds for the shipped default (yescaptcha) too.
        if self._nopecha_extension_key(sol_cfg):
            self.bot.log(
                "SYS",
                "NopeCHA extension key set: no paid captcha service is needed - the "
                "browser solve will spend it in-page instead."
            )
            return False
        if service == "nopecha":
            # nopecha is the one service with a keyless free tier - web_solver.auto_verify
            # lets it through without a key too, so agree with it here
            return True
        # say why we are about to ask a human, instead of silently queueing a manual solve
        self.bot.log(
            "WARN",
            f"Auto-solve is enabled but the selected service '{service}' has no key set "
            f"(security.captcha_solver.{key_field} is empty) - falling back to a manual solve. "
            f"Switch the service to the one your key belongs to."
        )
        return False

    def _resume_after_solve(self, why):
        """Undo everything a captcha did to this account.

        Used to live only in the "I have verified that you are human" DM branch, so a
        captcha cleared any other way - a paid service, the dashboard, the browser solver,
        or OwO simply dropping it - left the account paused with throttle_until == inf
        forever, which looks exactly like a solver that does not work.
        """
        # OwO's "I have verified that you are human" DM lands *after* whichever path
        # cleared the captcha already resumed us. Logging twice would double-count
        # captchas_solved, since state.log_command counts on the message text.
        already_running = (not self.bot.paused
                           and not self.bot.throttle_until
                           and not getattr(self.bot, '_solving_captcha', False))
        self.bot.web_solver.mark_verification_done(str(self.bot.user.id))
        self.bot._solving_captcha = False
        try:
            from dashboard.app import clear_captcha_challenge
            clear_captcha_challenge(str(self.bot.user.id))
        except Exception as e:
            self.bot.log("ERROR", f"Failed to clear captcha challenge: {e}")
        self.bot.paused = False
        self.bot.throttle_until = 0.0
        self.bot.last_sent_time = 0
        self.bot.warmup_until = 0
        grinding_cog = self.bot.get_cog('Grinding')
        if grinding_cog:
            grinding_cog.cooldowns['hunt'] = 0
            grinding_cog.cooldowns['battle'] = 0
            grinding_cog.cooldowns['owo'] = 0
        if already_running:
            return
        # "captcha solved" is what state.log_command counts, so keep that wording
        self.bot.log("SUCCESS", f"{why} Resuming...")
        self.bot.log("INFO", "All cooldowns reset. Bot will resume in 2 seconds...")
        # A captcha only ever notified on *detection*. With auto-solving on, the operator
        # got an @everyone alarm and then silence, with no way to tell a solved captcha
        # from one still sitting there - which is what "not notifying for solved captchas"
        # is. Every solve path funnels through here, so one notification here covers all
        # of them and cannot double up.
        self._show_desktop_notification(f"Captcha solved for {self.bot.username} - farming again.")
        self._send_webhook("CAPTCHA SOLVED", f"{why}\n{self.bot.username} is farming again.",
                           color=0x3BA55D, mention=False)

    async def _run_autosolve(self, sol_cfg, where=""):
        from core.captcha_jobs import jobs
        return await jobs.run(self.bot, lambda: self._run_autosolve_once(sol_cfg, where))

    async def _run_autosolve_once(self, sol_cfg, where=""):
        """Every way we can clear a captcha without a human, in order. True if cleared.

        Order matters: a paid service costs credits but needs no window, so it goes first
        when one is configured. The browser solver needs no key at all, so it runs whether
        or not a service is set - it is the only free path and it is also the only one that
        can tell us the captcha is already gone.
        """
        suffix = f" ({where})" if where else ""
        if self._should_autosolve(sol_cfg):
            service_name = self.bot.web_solver.active_service_name.capitalize()
            self.bot.log("SYS", f"Attempting {service_name} auto-solve{suffix}...")
            try:
                paid_ok = await self.bot.web_solver.auto_verify()
            except Exception as e:
                paid_ok = False
                self.bot.log("ERROR", f"{service_name} auto-solve crashed{suffix}: {e}")
            if paid_ok:
                # only _resume_after_solve reports the success - it logs the line
                # state.log_command counts and fires the solved notification, so a
                # second one here would double-count and double-notify
                self._resume_after_solve(f"{service_name} cleared the captcha.")
                return True
            # no "solve manually" notification yet - the free browser path is next
            self.bot.log("ERROR", f"{service_name} auto-solve failed{suffix}!")

        browser_solver = getattr(self.bot, "browser_solver", None)
        nope_key = self._nopecha_extension_key(sol_cfg)
        if not browser_solver or not browser_solver.enabled or not nope_key:
            # extension-only: without a NopeCHA key there is nothing free to try,
            # so fall through to the dashboard's manual solve
            return False
        unavailable = browser_solver_status()
        if unavailable:
            self.bot.log("WARN", f"NopeCHA extension solve unavailable: {unavailable}")
            return False

        self.bot.log("SYS", f"Attempting NopeCHA extension solve{suffix}...")
        try:
            result = await browser_solver.solve()
        except Exception as e:
            self.bot.log("ERROR", f"NopeCHA extension solve crashed{suffix}: {e}")
            return False
        if result.get("ok"):
            how = result.get("how")
            if how in ("not-required", "cleared"):
                self._resume_after_solve("OwO says this account is verified.")
            elif how == "nopecha-extension":
                self._resume_after_solve("NopeCHA's extension cleared the captcha.")
            else:
                self._resume_after_solve("The captcha cleared without a challenge.")
            return True
        self.bot.log("ERROR", f"NopeCHA extension solve failed{suffix}: {result.get('reason')}")
        return False

    @commands.Cog.listener('on_owo_gateway_message')
    async def on_owo_gateway_message(self, payload):
        # Components-v2 links/text do not appear in discord.py-self's Message.
        from component_v2_neura.parser import parse_v2_message, collect_text
        data = payload.get('d') or {}
        if not self.enabled or not self.bot.user:
            return
        if str((data.get('author') or {}).get('id')) != self.monitor_id:
            return
        parts = parse_v2_message(data)
        text = (data.get('content') or '') + '\n' + collect_text(parts)
        urls = [p.url for p in parts if p.url and re.match(r'^https://owobot\.com/captcha(?:[/#?]|$)', p.url)]
        urls += re.findall(r'https://owobot\.com/captcha(?:[/#?][^\s<>]*)?', text)
        if not urls:
            return
        # A shared channel may contain three accounts. A link alone cannot
        # establish ownership there; require our identity or a private DM.
        channel = self.bot.get_channel(int(data.get('channel_id', 0)))
        private = (isinstance(channel, discord.DMChannel)
                   or (payload.get('t') == 'MESSAGE_CREATE' and not data.get('guild_id')))
        if not private and (str(data.get('channel_id')) not in map(str, self.bot.channels)
                            or not self.bot.identity.text_is_mine(text)):
            return
        from dashboard.app import register_captcha_challenge
        register_captcha_challenge(str(self.bot.user_id), {
            'account_name': self.bot.username, 'captcha_url': urls[0]})
        self.bot.paused = True
        self.bot.throttle_until = float('inf')
        if getattr(self.bot, '_solving_captcha', False):
            return
        self.bot._solving_captcha = True
        try:
            cfg = self.bot.config.get('security', {}).get('captcha_solver', {})
            await self._run_autosolve(cfg, 'components-v2')
        finally:
            self.bot._solving_captcha = False

    def __init__(self, bot):
        self.bot = bot
        cfg = bot.config.get('security', {})
        self.enabled = cfg.get('enabled', True)
        self.notifications_enabled = cfg.get('notifications', {}).get('enabled', True)
        self.notification_title = cfg.get('notifications', {}).get('desktop', {}).get('title', "Lazy Farmers Security Alert")
        self.monitor_id = str(bot.config.get('core', {}).get('monitor_bot_id', '408785106942164992'))
        self.beep_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "beeps", "security_beep.mp3")
        self.ban_keywords = [
            "youhavebeenbanned",
            "bannedforbotting",
            "bannedformacros"
        ]
        self.captcha_keywords = [
            "areyouarealhuman",
            "verifythatyouarehuman",
            "pleasecompletethiswithin",
            "pleaseusethelinkbelow",
            "completeyourcaptcha",
            "pleasedmmewiththefollowing",
            "pleasedmmewithonly",
            "ifyouhavetroublesolvingthecaptcha",
            "pleasecomplete",
            "tocheckthatyouareahuman",
            "tocheck",
            "human"
        ]
        self.warning_pattern = re.compile(r'\(\s*(\d+)\s*/\s*(\d+)\s*\)')
        self.image_captcha_keywords = [
            "pleasedmme",
            "dmme",
            "beepboop",
            "checkthatyouareahuman",
            "solvingthecaptcha",
            "letterword"
        ]

    async def register_actions(self):
        cfg = self.bot.config.get('security', {})
        self.enabled = cfg.get('enabled', True)
        self.notifications_enabled = cfg.get('notifications', {}).get('enabled', True)
        self.notification_title = cfg.get('notifications', {}).get('desktop', {}).get('title', "Lazy Farmers Security Alert")
        self.monitor_id = str(self.bot.config.get('core', {}).get('monitor_bot_id', '408785106942164992'))
        self.bot.log("SYS", "Security Module settings refreshed (Live Sync).")

    def _normalize(self, text):
        if not text:
            return ""
        text = unicodedata.normalize("NFKD", text)
        return re.sub(r'[^a-zA-Z0-9]', '', text.lower())

    def _show_desktop_notification(self, message):
        if not self.notifications_enabled:
            return
        sec_cfg = self.bot.config.get('security', {})
        notif_cfg = sec_cfg.get('notifications', {})
        if self.bot.is_mobile:
            mobile = notif_cfg.get('mobile', {})
            if mobile.get('enabled', True):
                try:
                    os.system(f'termux-notification --title "{self.notification_title}" --content "{message}"')
                    vib = mobile.get('vibrate', {})
                    if vib.get('enabled', True):
                        duration = int(vib.get('time', 0.5) * 1000)
                        os.system(f'termux-vibrate -d {duration}')
                    toast = mobile.get('toast', {})
                    if toast.get('enabled', True):
                        bg = toast.get('bg_color', 'black')
                        fg = toast.get('text_color', 'white')
                        pos = toast.get('position', 'middle')
                        os.system(f'termux-toast -b {bg} -c {fg} -g {pos} "{message}"')
                    tts = mobile.get('tts', {})
                    if tts.get('enabled', False):
                        os.system(f'termux-tts-speak "{message}"')
                except:
                    pass
            return
        desktop = notif_cfg.get('desktop', {})
        if desktop.get('enabled', True):
            try:
                notification.notify(title=self.notification_title, message=message, timeout=10)
            except:
                pass

    def _send_webhook(self, title, message, color=0xFF3B3B, mention=True):
        cfg = self.bot.config.get('security', {})
        wh_cfg = cfg.get('webhook', {})
        if not wh_cfg.get('enabled', True): return
        url = wh_cfg.get('url')
        if not url: return
        payload = {
            "content": "@everyone @here" if mention else "",
            "embeds": [{
                "title": title,
                "description": message,
                "color": color,
                "author": {
                    "name": f"Lazy Farmers Security - {self.bot.username}",
                    "icon_url": "https://media.discordapp.net/attachments/1357951011456684252/1524069544401047773/neuralogo.png?ex=6a4e67df&is=6a4d165f&hm=21deba052462f712808661dc8aac4204eecb781cfcaa1ff189861b79c7db0c92"
                },
                "footer": {"text": f"Lazy Farmers • Account: {self.bot.username}"},
                "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S')
            }]
        }
        # off the event loop: this runs from on_message, and a webhook host that hangs
        # would stall every account's scheduler for the full timeout
        def _post():
            try:
                requests.post(url, json=payload, timeout=5)
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    async def play_beep(self):
        def _play():
            if not os.path.exists(self.beep_file):
                return
            if self.bot.is_mobile:
                try:
                    os.system(f'termux-media-player play "{self.beep_file}"')
                except:
                    pass
                return
            try:
                from playsound3 import playsound
                playsound(self.beep_file, block=False)
            except:
                pass
        threading.Thread(target=_play, daemon=True).start()

    def _contains_keyword(self, text, keywords):
        cleaned = self._normalize(text)
        return any(k in cleaned for k in keywords)

    def _get_captcha_url(self, message):
        if not message.components:
            return None
        for comp in message.components:
            if not getattr(comp, "children", None): continue
            for child in comp.children:
                url = str(getattr(child, "url", "") or "")
                if "owobot.com/captcha" in url:
                    return url
        return None

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.enabled: return
        if (message.guild is None or isinstance(message.channel, discord.DMChannel)) and message.author.id == int(self.monitor_id):
            if (discord.utils.utcnow() - message.created_at).total_seconds() > 30: return
            if "i have verified that you are human" in message.content.lower():
                self._resume_after_solve("Verified detected in DM. Captcha solved successfully.")
                await asyncio.sleep(2)
                return

            if "letterword" in message.content.lower() and message.attachments:
                self.bot.log("SECURITY", "Detection AI: Letterword captcha identified in DMs.")
                count_match = re.search(r'(\d+)\s*letterword', message.content.lower())
                letter_count = int(count_match.group(1)) if count_match else 5
                image_url = message.attachments[0].url
                self.bot.log("SYS", f"Attempting to solve DM Captcha ({letter_count} letters)...")
                answer = await self.bot.captcha_solver.solve_image(image_url, letter_count)
                if answer:
                    self.bot.log("SUCCESS", f"AI Solver Answer: {answer}. Sending to OwO...")
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    async with message.channel.typing():
                        await asyncio.sleep(len(answer) * 0.1)
                        await message.channel.send(answer)
                else:
                    self.bot.log("ERROR", "AI Solver failed to generate an answer. Pausing bot.")
                    self.bot.paused = True
                    self.bot.throttle_until = float('inf')
                    self._show_desktop_notification("AI Solver failed! Solve manually.")
                    self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), "https://owobot.com/captcha")
                return

            captcha_url = self._get_captcha_url(message)
            if not captcha_url:
                url_match = re.search(r'https?://owobot\.com/captcha/\S+', message.content)
                if url_match: captcha_url = url_match.group(0)

            if captcha_url:
                self.bot.paused = True
                self.bot.throttle_until = float('inf')
                self.bot.log("ALARM", "LINK CAPTCHA DETECTED IN DM!")
                await self.play_beep()
                self._show_desktop_notification("DM Captcha detected!")

                sec_cfg = self.bot.config.get("security", {})
                sol_cfg = sec_cfg.get("captcha_solver", {})

                try:
                    from dashboard.app import register_captcha_challenge
                    register_captcha_challenge(
                        str(self.bot.user.id),
                        {
                            "account_name": self.bot.username,
                            "captcha_url": captcha_url or "https://owobot.com/captcha"
                        }
                    )
                    self.bot.log("SYS", f"Captcha registered for dashboard display (account_id={self.bot.user.id})")
                except Exception as e:
                    self.bot.log("ERROR", f"Failed to register captcha for dashboard: {e}")

                if getattr(self.bot, '_solving_captcha', False):
                    self.bot.log("SECURITY", "[GUARD] Captcha already being solved – skipping duplicate DM captcha task.")
                    return
                # try/finally: the flag means "a solve is in flight right now". It used
                # to be cleared only on the autosolve-succeeded path, so one failed or
                # skipped solve latched it True for the life of the process and every
                # later captcha was silently dropped by the guard above - no retry, no
                # manual solve queued, account parked at throttle_until=inf forever.
                self.bot._solving_captcha = True
                try:
                    autosolved = await self._run_autosolve(sol_cfg, "DM")

                    if not autosolved:
                        self._send_webhook("DM CAPTCHA", f"Solve link in DM: {captcha_url}")
                        if not getattr(self.bot, 'is_mobile', False):
                            auto_open = sec_cfg.get("open_captcha_url_on_pc", False)
                        else:
                            auto_open = sec_cfg.get("open_captcha_url_on_mobile", False)
                        if auto_open:
                            self.bot.log("SYS", "Queuing manual solve for DM captcha...")
                            self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), captcha_url)
                            self._show_desktop_notification(f"Manual solve queued for {self.bot.username}")
                finally:
                    self.bot._solving_captcha = False
                return

        if str(message.author.id) != self.monitor_id: return

        if self.bot.owo_user is None:
            self.bot.owo_user = message.author

        content = message.content or ""
        embed_text = ""
        if message.embeds:
            parts = []
            for e in message.embeds:
                if e.title: parts.append(e.title)
                if e.description: parts.append(e.description)
                if e.footer and e.footer.text: parts.append(e.footer.text)
            embed_text = " ".join(parts)
        text_to_check = f"{content} {embed_text}"
        is_for_me = self.bot.is_message_for_me(message)
        if not is_for_me: return


        is_security_event = (
            self._contains_keyword(text_to_check, self.ban_keywords) or
            self.warning_pattern.search(text_to_check) is not None or
            (len(message.attachments) > 0 and self._contains_keyword(text_to_check, self.image_captcha_keywords)) or
            self._contains_keyword(text_to_check, self.captcha_keywords) or
            self._get_captcha_url(message) is not None or
            re.search(r'https?://owobot\.com/captcha/\S+', text_to_check) is not None
        )

        try:
            allowed_channels = [int(ch) for ch in self.bot.channels]
        except:
            allowed_channels = [self.bot.channel_id]

        if not is_security_event and message.channel.id not in allowed_channels:
            return

        if self._contains_keyword(text_to_check, self.ban_keywords):
            self.bot.paused = True
            self.bot.log("ALARM", "BAN DETECTED!")
            await self.play_beep()
            self._show_desktop_notification("Ban detected!")
            self._send_webhook("BAN DETECTED", f"Message:\n{content}")
            return

        warning_match = self.warning_pattern.search(text_to_check)
        if warning_match:
            current_warning = int(warning_match.group(1))
            max_warnings = int(warning_match.group(2))
            normalized = self._normalize(text_to_check)
            captcha_url = self._get_captcha_url(message)
            if not captcha_url:
                url_match = re.search(r'https?://owobot\.com/captcha/\S+', text_to_check)
                if url_match:
                    captcha_url = url_match.group(0)

            if captcha_url or any(kw in normalized for kw in ["pleasecomplete", "captcha", "verify", "human"]):
                self.bot.paused = True
                self.bot.throttle_until = float('inf')
                self.bot.stats['last_captcha_msg'] = text_to_check[:200]
                self.bot.log("ALARM", f"CAPTCHA WARNING DETECTED ({current_warning}/{max_warnings})!")
                await self.play_beep()
                self._show_desktop_notification(f"Captcha warning {current_warning}/{max_warnings} detected!")
                self._send_webhook("CAPTCHA WARNING", f"Warning {current_warning}/{max_warnings}\nMessage:\n{content}")

                if captcha_url:
                    sec_cfg = self.bot.config.get("security", {})
                    sol_cfg = sec_cfg.get("captcha_solver", {})

                    try:
                        from dashboard.app import register_captcha_challenge
                        register_captcha_challenge(
                            str(self.bot.user.id),
                            {
                                "account_name": self.bot.username,
                                "captcha_url": captcha_url or "https://owobot.com/captcha"
                            }
                        )
                        self.bot.log("SYS", f"Captcha registered for dashboard display (account_id={self.bot.user.id})")
                    except Exception as e:
                        self.bot.log("ERROR", f"Failed to register captcha for dashboard: {e}")

                    if getattr(self.bot, '_solving_captcha', False):
                        self.bot.log("SECURITY", "[GUARD] Captcha already being solved – skipping duplicate warning captcha task.")
                        return
                    # see the DM branch: cleared in finally so a failed solve cannot
                    # latch the guard on and mute every later captcha
                    self.bot._solving_captcha = True
                    try:
                        autosolved = await self._run_autosolve(sol_cfg, "warning")

                        if not autosolved:
                            solve_link = captcha_url or "https://owobot.com/captcha"
                            self._send_webhook("CAPTCHA WARNING", f"Solve: {solve_link}")
                            if not getattr(self.bot, 'is_mobile', False):
                                auto_open = sec_cfg.get("open_captcha_url_on_pc", False)
                            else:
                                auto_open = sec_cfg.get("open_captcha_url_on_mobile", False)
                            if auto_open:
                                self.bot.log("SYS", "Queuing manual solve for captcha...")
                                self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), captcha_url)
                                self._show_desktop_notification(f"Manual solve queued for {self.bot.username}")
                    finally:
                        self.bot._solving_captcha = False
                return

        has_image = len(message.attachments) > 0
        image_captcha_hit = self._contains_keyword(text_to_check, self.image_captcha_keywords)
        if has_image and image_captcha_hit:
            self.bot.paused = True
            self.bot.throttle_until = float('inf')
            self.bot.stats['last_captcha_msg'] = text_to_check[:200]
            self.bot.log("ALARM", "IMAGE CAPTCHA DETECTED! Warning triggered.")
            await self.play_beep()
            self._show_desktop_notification("Image captcha detected! Check DMs.")
            img_urls = "\n".join([att.url for att in message.attachments])
            self._send_webhook("IMAGE CAPTCHA DETECTED", f"Message:\n{content}\n\nImages:\n{img_urls}")

            count_match = re.search(r'(\d+)\s*letterword', text_to_check)
            letter_count = int(count_match.group(1)) if count_match else 5
            image_url = message.attachments[0].url
            self.bot.log("SYS", f"Attempting to solve image captcha ({letter_count} letters)...")
            answer = await self.bot.captcha_solver.solve_image(image_url, letter_count)
            if answer:
                self.bot.log("SUCCESS", f"AI Solver Answer: {answer}. Sending to OwO...")

                # force=True: the account is paused and throttled to inf a few lines up,
                # so a normal send is dropped and the answer never reaches OwO. Reply in
                # the channel the captcha arrived in, not the configured grind channel.
                sent = await self.bot.send_message(
                    answer,
                    skip_typing=True,
                    priority=True,
                    target_channel_id=message.channel.id,
                    force=True,
                )
                if sent:
                    self._show_desktop_notification(f"Image captcha solved: {answer}")
                else:
                    self.bot.log("ERROR", "Could not deliver the captcha answer. Solve manually.")
                    self._show_desktop_notification("Captcha answer failed to send! Solve manually.")
                    self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), "https://owobot.com/captcha")
            else:
                self.bot.log("ERROR", "AI Solver failed to generate an answer for image captcha.")
                self._show_desktop_notification("AI Solver failed! Solve manually.")
                self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), "https://owobot.com/captcha")
            return

        captcha_keywords_hit = self._contains_keyword(text_to_check, self.captcha_keywords)
        captcha_url = self._get_captcha_url(message)
        if not captcha_url:
            url_match = re.search(r'https?://owobot\.com/captcha/\S+', text_to_check)
            if url_match:
                captcha_url = url_match.group(0)

        if captcha_url or captcha_keywords_hit:
            self.bot.paused = True
            self.bot.throttle_until = float('inf')
            self.bot.stats['last_captcha_msg'] = text_to_check[:200]
            self.bot.log("ALARM", "CAPTCHA DETECTED!")
            await self.play_beep()
            self._show_desktop_notification("Captcha detected!")

            sec_cfg = self.bot.config.get("security", {})
            sol_cfg = sec_cfg.get("captcha_solver", {})

            try:
                from dashboard.app import register_captcha_challenge
                register_captcha_challenge(
                    str(self.bot.user.id),
                    {
                        "account_name": self.bot.username,
                        "captcha_url": captcha_url or "https://owobot.com/captcha"
                    }
                )
                self.bot.log("SYS", f"Captcha registered for dashboard display (account_id={self.bot.user.id})")
            except Exception as e:
                self.bot.log("ERROR", f"Failed to register captcha for dashboard: {e}")

            if getattr(self.bot, '_solving_captcha', False):
                self.bot.log("SECURITY", "[GUARD] Captcha already being solved – skipping duplicate channel captcha task.")
                return
            # see the DM branch: cleared in finally so a failed solve cannot latch the
            # guard on and mute every later captcha
            self.bot._solving_captcha = True
            try:
                autosolved = await self._run_autosolve(sol_cfg, "channel")

                if not autosolved:
                    solve_link = captcha_url or "https://owobot.com/captcha"
                    self._send_webhook("CAPTCHA DETECTED", f"Solve: {solve_link}")
                    if not getattr(self.bot, 'is_mobile', False):
                        auto_open = sec_cfg.get("open_captcha_url_on_pc", False)
                    else:
                        auto_open = sec_cfg.get("open_captcha_url_on_mobile", False)
                    if auto_open:
                        self.bot.log("SYS", "Queuing manual solve for captcha...")
                        self.bot.web_solver.enqueue_manual_solve(str(self.bot.user.id), captcha_url)
                        self._show_desktop_notification(f"Manual solve queued for {self.bot.username}")
            finally:
                self.bot._solving_captcha = False
            return

async def setup(bot):
    await bot.add_cog(Security(bot))