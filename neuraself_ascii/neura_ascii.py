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
import sys
import time
import random
from rich.console import Console
from rich.align import Align
from rich.live import Live

TAGLINE = "Multi-Account OwO Automation"

MAIN_LOGO = rf"""[#ff0000] _      _    ______   __  ___ _   ___ __  __ ___ ___  ___[/#ff0000]
[#ff0000]| |    /_\  |_  /\ \ / / | __/_\ | _ \  \/  | __| _ \/ __|[/#ff0000]
[#ff0000]| |__ / _ \  / /  \ V /  | _/ _ \|   / |\/| | _||   /\__ \ [/#ff0000]
[#ff0000]|____/_/ \_\/___|  |_|   |_/_/ \_\_|_\_|  |_|___|_|_\|___/[/#ff0000]
[#ff0000]┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈[/#ff0000]
[bold cyan]   L A Z Y   F A R M E R S[/bold cyan]  [white]•[/white]  [bold cyan]{TAGLINE}[/bold cyan]
[#ff0000]┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈[/#ff0000]"""

SETUP_LOGO = rf"""[red] _      _    ______   __  ___ _   ___ __  __ ___ ___  ___[/red]
[red]| |    /_\  |_  /\ \ / / | __/_\ | _ \  \/  | __| _ \/ __|[/red]
[red]| |__ / _ \  / /  \ V /  | _/ _ \|   / |\/| | _||   /\__ \ [/red]
[red]|____/_/ \_\/___|  |_|   |_/_/ \_\_|_\_|  |_|___|_|_\|___/[/red]
[bold blue]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold blue]
[bold cyan]      l a z y   f a r m e r s   s e t u p      [/bold cyan]
[bold blue]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold blue]"""

NOISE = "█▓▒░@#$%&/=+*<>"

def decode_frame(logo_lines, progress):
    frame = []
    for line in logo_lines:
        out = ""
        in_tag = False
        for ch in line:
            if ch == "[":
                in_tag = True
                out += ch
                continue
            if in_tag:
                out += ch
                if ch == "]":
                    in_tag = False
                continue
            if ch == " ":
                out += " "
                continue
            if random.random() < progress:
                out += ch
            else:
                out += random.choice(NOISE)
        frame.append(out)
    return "\n".join(frame)

def glitch_frame(logo_lines):
    frame = []
    for line in logo_lines:
        out = ""
        in_tag = False
        for ch in line:
            if ch == "[":
                in_tag = True
                out += ch
                continue
            if in_tag:
                out += ch
                if ch == "]":
                    in_tag = False
                continue
            if ch == " ":
                out += " "
                continue
            if random.random() < 0.15:
                out += random.choice(NOISE)
            else:
                out += ch
        frame.append(out)
    return "\n".join(frame)

def show_banner(banner_type='main', animate=True):
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass

    os.system('cls' if os.name == 'nt' else 'clear')
    
    if banner_type == 'setup':
        raw_logo = SETUP_LOGO
    else:
        raw_logo = MAIN_LOGO
        
    logo_lines = raw_logo.splitlines()

    console = Console()
    
    if not animate:
        console.print(Align.center("\n".join(logo_lines), vertical="middle"))
        print("\n")
        return

    with Live("", console=console, screen=False, refresh_per_second=60) as live:
        # boot noise
        for _ in range(5):
            live.update(Align.center(decode_frame(logo_lines, 0), vertical="middle"))
            time.sleep(0.04)
        
        # decode
        total = 30
        for i in range(total):
            progress = (i / (total - 1)) ** 1.8
            live.update(Align.center(decode_frame(logo_lines, progress), vertical="middle"))
            time.sleep(0.03)
            
        time.sleep(0.08)
        
        # glitch
        for _ in range(4):
            live.update(Align.center(glitch_frame(logo_lines), vertical="middle"))
            time.sleep(0.035)
            live.update(Align.center("\n".join(logo_lines), vertical="middle"))
            time.sleep(0.03)
            
        # last flash
        live.update("")
        time.sleep(0.04)
        live.update(Align.center("\n".join(logo_lines), vertical="middle"))
        time.sleep(0.1)
    print("\n")
