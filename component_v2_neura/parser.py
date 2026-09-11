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


import os
import re

COMP_TYPES = {
    1: "action_row",
    2: "button",
    3: "select_menu",
    4: "text_input",
    5: "user_select",
    6: "role_select",
    7: "mentionable_select",
    8: "channel_select",
    9: "section",
    10: "text_display",
    11: "thumbnail",
    12: "media_gallery",
    13: "file",
    14: "separator",
    17: "container",
    18: "label",
}

SELECT_TYPES = ("select_menu", "user_select", "role_select", "mentionable_select", "channel_select")
TEXT_TYPES = ("text_display", "section", "label", "container", "button")

# owo hides the animal/weapon name in the custom emoji it renders
CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')


class V2Component:
    def __init__(self, data, parent=None):
        self.raw = data
        self.type = data.get("type")
        self.name = COMP_TYPES.get(self.type, "unknown")
        self.id = data.get("id")
        self.parent = parent
        self.custom_id = data.get("custom_id")
        self.content = data.get("content") or ""
        self.label = data.get("label") or ""
        self.description = data.get("description") or ""
        self.placeholder = data.get("placeholder") or ""
        self.url = data.get("url")
        self.style = data.get("style")
        self.disabled = bool(data.get("disabled", False))

        self.emoji = data.get("emoji")
        self.emoji_name = (self.emoji or {}).get("name") if isinstance(self.emoji, dict) else None

        # string selects carry their choices inline; the click payload needs the values
        self.options = []
        for option in data.get("options", []) or []:
            self.options.append({
                "label": option.get("label", ""),
                "value": option.get("value", ""),
                "description": option.get("description", ""),
                "default": bool(option.get("default", False)),
                "emoji": option.get("emoji"),
            })

        # media lives under a different key for every media-ish component
        self.media = []
        for item in data.get("items", []) or []:
            self.media.append(self._media_entry(item.get("media", {}), item.get("description")))
        if isinstance(data.get("media"), dict):
            self.media.append(self._media_entry(data["media"], self.description))
        if isinstance(data.get("file"), dict):
            self.media.append(self._media_entry(data["file"], data.get("name")))

    @staticmethod
    def _media_entry(media, description=None):
        return {
            "url": media.get("url"),
            "proxy_url": media.get("proxy_url"),
            "placeholder": media.get("placeholder"),
            "description": description,
        }

    @property
    def text(self):
        """Everything a human would read off this component."""
        parts = [self.content, self.label, self.description, self.placeholder]
        parts.extend(option["label"] for option in self.options)
        return " ".join(p for p in parts if p)

    def __repr__(self):
        return f"<V2Component {self.name} id={self.custom_id or self.id!r}>"


def walker(components_data, parent=None):
    """Flatten a components v2 tree in document order."""
    flat_list = []

    if not components_data:
        return flat_list

    if isinstance(components_data, dict):
        components_data = [components_data]

    for data in components_data:
        if not isinstance(data, dict):
            continue
        comp = V2Component(data, parent=parent)
        flat_list.append(comp)

        # containers/action rows/sections nest a list, labels nest exactly one child
        # under the singular key, and sections hang a button or thumbnail off "accessory"
        flat_list.extend(walker(data.get("components"), parent=comp))
        flat_list.extend(walker(data.get("component"), parent=comp))
        flat_list.extend(walker(data.get("accessory"), parent=comp))

    return flat_list


def parse_v2_message(msg_data):
    """Flat component list for a raw MESSAGE_CREATE / MESSAGE_UPDATE payload."""
    if not msg_data:
        return []
    return walker(msg_data.get("components"))


def collect_text(components, types=TEXT_TYPES):
    """Readable text of a v2 message, newline separated, in document order."""
    lines = []
    for comp in components:
        if types and comp.name not in types:
            continue
        chunk = comp.text
        if chunk:
            lines.append(chunk)
    return "\n".join(lines)


def message_text(msg_data, components=None):
    """content + v2 text, lowercased - the string every cog matches against."""
    if components is None:
        components = parse_v2_message(msg_data)
    content = (msg_data or {}).get("content") or ""
    return f"{content}\n{collect_text(components)}".lower()


def buttons(components, include_disabled=False):
    return [
        comp for comp in components
        if comp.name == "button" and comp.custom_id and (include_disabled or not comp.disabled)
    ]


def find_button(components, custom_id=None, contains=None, label_contains=None):
    """First clickable button matching an exact id, an id substring or a label."""
    for comp in buttons(components):
        cid = comp.custom_id.lower()
        if custom_id and cid == str(custom_id).lower():
            return comp
        if contains and str(contains).lower() in cid:
            return comp
        if label_contains and str(label_contains).lower() in (comp.label or "").lower():
            return comp
    return None


def emoji_names(text):
    """Names of the custom emojis in a string - owo animals and weapons ride in these."""
    return [match.group(1).lower() for match in CUSTOM_EMOJI_RE.finditer(text or "")]


def media_urls(components):
    """Every image url on a v2 card, in document order.

    OwO has started answering whole commands with a *rendered picture* - `owo level`
    is a single media_gallery holding level.png, `owo quest` puts the quest rows in
    quest-rows.png. Such a message carries an empty ``content``, no embeds **and an
    empty ``attachments`` list**: the url exists only inside the component. Code that
    looked in ``attachments`` therefore never found the image and silently gave up,
    which is why the dashboard showed a blank level and no quests.
    """
    urls = []
    for comp in components:
        if comp.name not in ("media_gallery", "thumbnail", "file") or not comp.media:
            continue
        for entry in comp.media:
            url = entry.get("url") or entry.get("proxy_url")
            if url:
                urls.append(url)
    return urls


def media_image_url(components, name_contains=None):
    """First card image url, optionally only when its filename matches.

    ``name_contains`` is the attribution signal for an image-only card: it has no
    text to match a username against, so the filename owo chose ("level.png",
    "quest-rows.png") is the only thing that says which command it answers.
    """
    for url in media_urls(components):
        if name_contains is None:
            return url
        base = os.path.basename(url.split("?")[0]).lower()
        if any(part.lower() in base for part in (
                name_contains if isinstance(name_contains, (list, tuple)) else [name_contains])):
            return url
    return None


def is_image_only(components):
    """True when the card is nothing but picture - no readable text anywhere.

    `owo level` answers with exactly this, and because there is no text there is also
    no username to match, so callers have to attribute it by "I just asked for it".
    """
    if not components:
        return False
    if not media_urls(components):
        return False
    return not collect_text(components).strip()


def get_boss_battle_id(components):
    """A stable id for one boss spawn so two accounts never double-join the same fight."""
    for comp in components:
        if comp.name not in ("media_gallery", "thumbnail", "file") or not comp.media:
            continue
        for entry in comp.media:
            if entry.get("placeholder"):
                return entry["placeholder"]
            url = entry.get("url") or entry.get("proxy_url")
            if url:
                return os.path.basename(url.split("?")[0])
    return None
