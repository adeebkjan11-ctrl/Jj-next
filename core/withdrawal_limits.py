"""Estimates from OwO's published rules; server replies always take precedence.

Source (accessed 2026-09-10): ChristopherBThai/Discord-OwO-Bot,
9aec92b274bae64ea078fc04540398713e7f7928,
src/commands/commandList/economy/utils/cowoncyUtils.js:getUserLimits.
The public source may differ from the deployed bot. No formula is assumed for
an unknown level, and an operator can explicitly choose a fixed limit.
"""
import math


def daily_limits(level):
    if level is None or isinstance(level, bool):
        return None
    try:
        n = int(level)
        if n < 0 or float(level) != n:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    tens = n // 10
    send = 50_000 + n * 14_000 + tens * 5_000_000
    return {'send': send, 'receive': math.ceil(send * (tens / 2 + 1))}


def send_limit(config, stats):
    if config.get('limit_mode', 'level') == 'fixed':
        try:
            return max(0, int(config.get('daily_send_limit', 0)))
        except (ValueError, TypeError):
            return None
    result = daily_limits(stats.get('level'))
    return result['send'] if result else None
