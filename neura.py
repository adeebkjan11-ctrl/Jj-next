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
import os

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
import subprocess
import asyncio
import random
import json
import signal
import socket
import threading
import time
import traceback
from rich.console import Console
from rich.align import Align

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from lazy_engines.setup_engine import LazySetupEngine
from core.bot import NeuraBot
from core import spaces, supervisor
from dashboard.app import app as flask_app
import core.state as state
from modules import log_file
from utils import proxy_manager

console = Console()
engine = LazySetupEngine()

# no interactive menu on hosts without a usable console. Railway attaches a tty that
# nobody can type into, so the menu would block forever and the dashboard would never start.
HEADLESS = (
    os.environ.get("LAZYFARMERS_HEADLESS", "").lower() in ("1", "true", "yes")
    or any(key.startswith("RAILWAY_") for key in os.environ)
    or not sys.stdin.isatty()
)

if not engine.environment_healthy():
    # os.execv replaces this process, so a setup that "succeeds" without actually
    # fixing the imports used to re-exec forever. On a host with a console you see
    # the banner scroll past again and again; on Railway you see a service that
    # stays "running" and never serves a single request, because the dashboard is
    # never reached. Two tries, then say what is missing and fail properly.
    _attempts = 0
    try:
        _attempts = int(os.environ.get("LAZYFARMERS_SETUP_ATTEMPTS", "0") or 0)
    except ValueError:
        _attempts = 0
    if _attempts >= 2:
        console.print("[red]Dependencies are still missing after two setup attempts.[/red]")
        console.print("[red]Run 'python neura_setup.py' and read data/setup.log - re-running "
                      "the installer again would only loop.[/red]")
        sys.exit(1)
    console.print("[yellow]Environment not healthy – running setup...[/yellow]")
    if not engine.run_full_setup(force_bootstrap=True):
        console.print("[red]Setup failed. Please run 'python neura_setup.py' manually.[/red]")
        sys.exit(1)
    console.print("[green]Setup complete. Restarting...[/green]")
    os.environ["LAZYFARMERS_SETUP_ATTEMPTS"] = str(_attempts + 1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def show_banner():
    from neuraself_ascii import neura_ascii
    neura_ascii.show_banner('main', animate=not HEADLESS)

def detect_platform():
    if "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", ""):
        platform = "Mobile (Termux)"
        is_termux = True
    elif sys.platform.startswith("linux"):
        platform = "Linux (Server/Desktop)"
        is_termux = False
    elif sys.platform == "darwin":
        platform = "MacOS"
        is_termux = False
    elif os.name == "nt":
        platform = "PC (Windows)"
        is_termux = False
    else:
        platform = f"Unknown ({sys.platform})"
        is_termux = False
    console.print(f"[bold green]Detected Platform: {platform}[/bold green]")
    return is_termux

DEFAULT_PORT = 8000


def _dashboard_port():
    """(port, came_from_env). Railway injects PORT; everything else defaults."""
    raw = (os.environ.get('PORT') or '').strip()
    if not raw:
        return DEFAULT_PORT, False
    try:
        return int(raw), True
    except ValueError:
        console.print(f"[bold red]PORT={raw!r} is not a number - using {DEFAULT_PORT}.[/bold red]")
        return DEFAULT_PORT, False


def _bind_dashboard(port):
    """Claim the listening socket up front, in the main thread.

    A platform edge (Railway's, a reverse proxy, anything) decides a container is
    up by connecting to a port. Binding here rather than inside the server thread
    means "nothing is listening" is a startup failure with a reason in the log,
    not an exception swallowed by a daemon thread.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == 'posix':
        # POSIX only, deliberately. On Windows SO_REUSEADDR lets a second process
        # bind a port that is already being listened on, so the "nothing else has
        # this port" guarantee above would quietly stop holding.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('0.0.0.0', port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _env_int(name, default, low=None, high=None):
    """Read an int from the environment, clamped, falling back on anything odd."""
    try:
        value = int(str(os.environ.get(name, '')).strip())
    except (TypeError, ValueError):
        return default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _serve_dashboard(sock, port):
    """Serve Flask on an already-bound socket, on the best server available."""
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        waitress_serve = None

    if waitress_serve is not None:
        # threads: the dashboard polls stats every second and several routes block
        # on the bot loop for up to a minute, so waitress' default of 4 would let
        # one slow Start button freeze every other tab. 32 leaves headroom for a
        # 200-account farm with a few tabs open plus the static files those tabs
        # ask for - a request queue that never drains is why the site could stop
        # loading entirely while the bots kept farming.
        threads = _env_int('LAZYFARMERS_WEB_THREADS', 32, low=4, high=128)
        console.print(f"[green]Serving the dashboard with waitress ({threads} threads).[/green]")
        waitress_serve(flask_app, sockets=[sock], threads=threads, channel_timeout=180)
        return

    from werkzeug.serving import make_server
    console.print("[yellow]waitress is not installed - falling back to werkzeug's "
                  "development server (fine locally, slow under load).[/yellow]")
    make_server('0.0.0.0', port, flask_app, threaded=True, fd=sock.fileno()).serve_forever()


def run_dashboard(sock, port):
    try:
        _serve_dashboard(sock, port)
        reason = 'the web server returned on its own'
    except BaseException as e:
        reason = f'{type(e).__name__}: {e}'
        console.print_exception(show_locals=False)
    _dashboard_died(reason)


def _dashboard_died(reason):
    """The web server is gone, so take the process with it.

    This is what made "it says running but the site does not load" possible: the
    dashboard runs in a daemon thread, main() then parks in `await
    asyncio.sleep(60)` forever, and nothing anywhere checked whether the server
    was still alive. A crash inside flask_app.run() printed nothing (the
    "listening" line had already been printed), left the process happily idling
    with no listener, and because the exit code stayed 0 the platform's
    restart-on-failure policy never fired.
    """
    console.print(f"[bold red]The dashboard web server stopped: {reason}[/bold red]")
    console.print("[bold red]Nothing is listening any more - exiting so the host restarts us.[/bold red]")
    _shutdown()
    # a daemon thread cannot end the process by raising, and the asyncio loop is
    # asleep in main(); this is the only exit that actually happens.
    os._exit(1)


_dashboard_thread = None


def start_dashboard():
    global _dashboard_thread
    if _dashboard_thread is not None:
        return
    port, from_env = _dashboard_port()
    try:
        sock = _bind_dashboard(port)
    except OSError as e:
        console.print(f"[bold red]Cannot listen on 0.0.0.0:{port}: {e}[/bold red]")
        if from_env:
            console.print("[bold red]$PORT asked for that port, so nothing could ever reach "
                          "the dashboard on it.[/bold red]")
        else:
            console.print(f"[bold red]Stop whatever is already using {port}, or set PORT "
                          f"to a free port.[/bold red]")
        sys.exit(1)
    console.print(f"[bold green]Dashboard listening on http://0.0.0.0:{port}[/bold green]")
    if not from_env:
        console.print(f"[yellow]No PORT was set, so the dashboard is on {port}. If this is a hosted "
                      f"deploy, the public domain's target port must be {port} - a mismatch is "
                      f"answered with 502 while the service still reads as running.[/yellow]")
    _dashboard_thread = threading.Thread(target=run_dashboard, args=(sock, port),
                                        name='dashboard', daemon=True)
    _dashboard_thread.start()

_shutdown_done = threading.Event()


def _shutdown(clean=True):
    """Close history sessions and flush stats. Safe to call from any thread, once."""
    if _shutdown_done.is_set():
        return
    _shutdown_done.set()
    try:
        import utils.history_tracker as ht
        # only spaces that actually started something this run have an open db
        for owner in list(supervisor.started_spaces):
            ht.end_session(owner=owner)
        # force: the debounced write would otherwise be scheduled on a timer that
        # never gets to run, losing everything since the last flush
        state.save_account_stats(force=True)
        console.print("\n[bold yellow][!] Systems shut down. History saved.[/bold yellow]")
    except Exception as e:
        console.print(f"[red]Shutdown bookkeeping failed: {e}[/red]")

    # Last, and outside the try: reaching here at all means this exit was ours.
    # Anything that skips it - a kill, an abort - leaves the marker absent and the
    # next boot reports the previous run as having died.
    try:
        if clean:
            log_file.write("SYS", "LazyFarmers", "Shutting down cleanly")
            log_file.mark_clean_shutdown()
        else:
            log_file.write("ERROR", "LazyFarmers", "Shutting down after a crash")
        log_file.flush()
    except Exception:
        pass

def configured_account_count():
    """How many accounts exist across every space."""
    return sum(len(proxy_manager.load_accounts(owner)) for owner in spaces.list_owners())


def _menu_choice():
    """The blocking console prompt, to be run off the event loop.

    Both this and the account manager below used to run directly on the asyncio
    loop. The dashboard is already serving by then, so a prompt sitting on the
    loop parked every request that needs it - start, stop, verify, manual command
    all queue a coroutine onto `supervisor`'s loop and wait for a result that can
    never arrive. Option 2 held the loop for the whole terminal session.
    """
    from rich.prompt import Prompt
    try:
        return Prompt.ask("\nSelect option", choices=["1", "2", "3"], default="1")
    except EOFError:
        console.print("\n[yellow]No console input - handing over to the dashboard.[/yellow]")
        return "1"


def _run_account_manager():
    """neura_setup.account_manager() on its own loop, in a worker thread.

    It is declared `async def`, but every line inside it is a blocking Prompt or
    input(); the one await is a self-contained token verification, so giving it a
    private loop costs nothing and leaves the main one free to keep serving bots.
    """
    import neura_setup
    asyncio.run(neura_setup.account_manager())


async def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    supervisor.bind_loop(asyncio.get_running_loop())

    # First, before anything can fail: the on-disk log is the only record that
    # outlives this process, and a crash during startup is exactly the case where
    # there is otherwise nothing to read afterwards.
    clean, previous = log_file.start()
    if clean is False:
        console.print("[bold red]The previous run did not shut down cleanly - it was "
                      "killed or it crashed.[/bold red]")
        if previous:
            console.print(f"[bold red]Its last lines are in {previous}[/bold red]")
            for line in log_file.tail(25, previous=True):
                console.print(f"[dim]  {line.rstrip()}[/dim]")

    # before the banner and the menu, so a blocked console can never hide the dashboard
    start_dashboard()
    from core import monitor
    await monitor.boot()
    while True:
        show_banner()
        is_termux = detect_platform()
        state.load_account_stats()
        console.print(f"[cyan]Config Directory:[/cyan] {state.CONFIG_DIR}")
        console.print(f"[cyan]Accounts File:[/cyan] {spaces.accounts_path(spaces.ADMIN_SPACE)}\n")
        if not HEADLESS:
            console.print("\n[bold cyan]1.[/bold cyan] Hand over to the dashboard")
            console.print("[bold cyan]2.[/bold cyan] Manage Accounts")
            console.print("[bold cyan]3.[/bold cyan] Exit")
            choice = await asyncio.to_thread(_menu_choice)
            if choice == "2":
                await asyncio.to_thread(_run_account_manager)
                continue
            elif choice == "3":
                console.print("\n[yellow]Shutting down. See you next time![/yellow]")
                sys.exit(0)
        # Nothing is started here, deliberately. Bringing the farm up on boot meant
        # a redeploy, a crash-restart or a host moving the container silently put
        # every account back on Discord with nobody watching - and all at once,
        # which is what tripped the host's log rate limit and then killed the
        # process on thread exhaustion. An account runs because someone pressed
        # Start; a restart leaves the farm exactly as stopped as it found it.
        configured = configured_account_count()
        if configured:
            console.print(f"[bold yellow]{configured} account(s) configured, none started - "
                          f"start the ones you want on the dashboard Accounts page.[/bold yellow]")
        else:
            console.print("[bold yellow]No accounts yet - add them on the dashboard Accounts page.[/bold yellow]")
        console.print("[bold green]Dashboard is in control. Start, stop and verify accounts from the Accounts page.[/bold green]")

        # Leave a marker in the dashboard's log view. That view is an in-memory
        # deque, so a restart empties it - and with nothing starting on boot the
        # farm is empty too. "Every account stopped and the log is blank" is
        # therefore what a crash-restart looks like from the dashboard, and it is
        # indistinguishable from someone having stopped them by hand unless the
        # process says so on the way up.
        state.log_command("SYS", f"Process started - {configured} account(s) configured, "
                                 f"none running yet", "info")

        while True:
            await asyncio.sleep(60)

def _handle_signal(signum, _frame):
    """Turn the host's shutdown signal into an ordinary exit.

    Railway sends SIGTERM to redeploy. Python's default action for it is to die on
    the spot - no finally blocks, no _shutdown - so history was left unclosed and,
    now that we record how the process ended, every redeploy would have been filed
    as a crash. Raising SystemExit sends it down the same path as Ctrl-C.
    """
    console.print(f"\n[yellow]Received signal {signum} - shutting down.[/yellow]")
    raise SystemExit(0)


if __name__ == "__main__":
    for _sig in ('SIGTERM', 'SIGINT'):
        try:
            signal.signal(getattr(signal, _sig), _handle_signal)
        except (AttributeError, ValueError, OSError):
            # not every platform has both, and signal() only works on the main thread
            pass

    exit_code = 0
    crashed = False
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        # the code was being swallowed here, so a fatal startup problem still exited
        # 0 and a restart-on-failure policy had nothing to react to
        code = e.code
        exit_code = code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:
        # Record it before unwinding: this is the shape of death that used to leave
        # nothing behind, and the traceback belongs in the file that survives.
        crashed = True
        exit_code = 1
        try:
            log_file.write("ERROR", "LazyFarmers",
                           "Unhandled exception - " + traceback.format_exc())
        except Exception:
            pass
        console.print_exception(show_locals=False)
    finally:
        # A crash is not a clean shutdown, so the next boot must still say so.
        _shutdown(clean=not crashed)
    sys.exit(exit_code)