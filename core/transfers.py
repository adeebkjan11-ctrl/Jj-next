"""Transfer matching and recipient serialization, shared by owner and monitor controls."""
import asyncio
import re

_locks = {}


def recipient_lock(recipient):
    return _locks.setdefault(str(recipient), asyncio.Lock())


def clean_text(text):
    return re.sub(r'[*`_]', '', text or '').lower()


def amount_in(text, amount):
    text = re.sub(r'(?<=\d)[,\s](?=\d)', '', clean_text(text))
    return bool(re.search(rf'(?<!\d){int(amount)}(?!\d)', text))


def matches_prompt(text, sender, recipient, amount):
    text = clean_text(text)
    return (bool(re.search(rf'<@!?{re.escape(str(sender))}>\s+will\s+give\s+<@!?{re.escape(str(recipient))}>', text))
            and amount_in(text, amount))


def confirmed_receipt(text, amount):
    text = clean_text(text).replace(',', '')
    return bool(re.search(rf'\bsent\s+{int(amount)}\s+cowoncy\s+to\b', text))


def payload_text(data):
    from component_v2_neura.parser import parse_v2_message, collect_text
    parts = [data.get('content') or '', collect_text(parse_v2_message(data))]
    for embed in data.get('embeds') or []:
        parts += [embed.get('title') or '', embed.get('description') or '',
                  (embed.get('author') or {}).get('name') or '']
        parts += [str(f.get('name', '')) + ' ' + str(f.get('value', '')) for f in embed.get('fields', [])]
    return '\n'.join(parts)
