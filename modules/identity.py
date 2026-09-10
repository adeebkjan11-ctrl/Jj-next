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


import re
import unicodedata

class IdentityManager:
    def __init__(self, bot):
        self.bot = bot
        self.generic_patterns = [
            "are you a real human", "complete your captcha",
            "verify that you are human", "please use the link below",
            "beep boop", "i am back with", "i will be back in",
            "please include your password"
        ] # these owo mssgs does not contain our name , and some for security 

    def clean_text(self, text):
        if not text: return ""
        clean = "".join(ch for ch in text if unicodedata.category(ch)[0] != 'C')
        clean = clean.replace('\u200b', '').replace('\u200c', '').replace('\u200d', '').replace('\ufeff', '')
        return clean.lower().strip()

    @staticmethod
    def strip_punct(text):
        """The punctuation-free view of a string: 'julia.angeled' -> 'juliaangeled'."""
        return re.sub(r'[^\w\s]', '', text or '').strip()

    def idents_for(self, message=None):
        """Every spelling of this account's name worth looking for.

        Both forms are kept: the name as OwO prints it *and* the punctuation-free
        version. Only the stripped one used to be kept, which can never match text
        that still has the punctuation in it - so an account whose name contains a
        dot (`julia.angeled`, the modern Discord username style) matched nothing OwO
        said unless it also got a mention, and every zoo/team/balance reply was
        written off as "not recognised as mine". The stripped form still earns its
        place for names wrapped in decoration (`xX-julia-Xx`).
        """
        names = [self.bot.user.name, self.bot.display_name]
        names += list(getattr(self.bot, 'identifiers', None) or [])
        if message is not None and message.guild:
            member = message.guild.get_member(self.bot.user.id)
            if member and member.nick:
                names.append(member.nick)

        idents = set()
        for name in names:
            plain = self.clean_text(name)
            for form in (plain, self.strip_punct(plain)):
                if form and len(form) >= 2:
                    idents.add(form)
        return idents

    def text_views(self, text):
        """The text as it stands plus its punctuation-free view, for ident matching."""
        clean = self.clean_text(text)
        views = [clean]
        stripped = self.strip_punct(clean)
        if stripped and stripped != clean:
            views.append(stripped)
        return views

    def _named_in(self, text, idents, possessive=False):
        # `'s` survives as a bare `s` in the stripped view, so allow the apostrophe
        # to be missing there ("julia.angeled's zoo" -> "juliaangeleds zoo").
        suffix = r"(?:'?s)?" if possessive else ""
        for view in self.text_views(text):
            for ident in idents:
                # `\b` is wrong at a punctuation edge: for the ident "julia." it would
                # demand a letter after the dot. Lookarounds for a word character are
                # what we actually mean - and they are what stops acc1 from answering
                # acc10's messages, which a plain substring test does not.
                if re.search(rf"(?<!\w){re.escape(ident)}{suffix}(?!\w)", view):
                    return True
        return False

    def text_is_mine(self, full_text):
        """Name match against text with no Message object behind it.

        components v2 payloads are invisible to discord.py-self, so the cogs that
        read them off the raw socket have only the flattened text - there is nothing
        to hand `mentioned_in`.
        """
        text = self.clean_text(full_text)
        if not text:
            return False
        if f"<@{self.bot.user.id}>" in text or f"<@!{self.bot.user.id}>" in text:
            return True
        return self._named_in(text, self.idents_for(), possessive=True)

    def is_message_for_me(self, message, role="any", keyword=None):
        if not message:
            return False

        if self.bot.user.mentioned_in(message):
            return True

        clean_idents = self.idents_for(message)

        content = self.clean_text(message.content)
        if role == "header":
            header_texts = [content.split('\n')[0]]
            if message.embeds:
                for em in message.embeds:
                    if em.title:
                        header_texts.append(self.clean_text(em.title))
                    if em.author and em.author.name:
                        header_texts.append(self.clean_text(em.author.name))
                    if em.description:
                        header_texts.append(self.clean_text(em.description.split('\n')[0]))

            for text in header_texts:
                if self._named_in(text, clean_idents, possessive=True):
                    return True
            return False

        if role in ["source", "target"] and keyword:
            keyword = keyword.lower()
            if keyword in content:
                parts = content.split(keyword, 1)
                check_text = parts[0] if role == "source" else parts[1]
                if self._named_in(check_text, clean_idents):
                    return True
            return False

        texts = [content]
        if message.embeds:
            for em in message.embeds:
                fields_text = " ".join([f"{f.name} {f.value}" for f in em.fields])
                raw_embed_text = f"{em.title or ''} {em.author.name if em.author else ''} {em.description or ''} {fields_text}"
                texts.append(self.clean_text(raw_embed_text))

        for text in texts:
            if self._named_in(text, clean_idents, possessive=True):
                return True

        full_visible_text = " ".join(texts)
        if any(pat in full_visible_text for pat in self.generic_patterns):
            if not message.guild or self.bot.user.mentioned_in(message):
                return True
            return self._named_in(full_visible_text, clean_idents, possessive=True)

        return False

