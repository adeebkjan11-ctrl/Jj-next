# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LazyFarmers is a multi-account Discord **selfbot** that automates the OwO bot game, plus a multi-tenant
Flask web dashboard for monitoring and configuring it. Python 3.10+, `discord.py-self` (pinned commit),
no build step, no test suite.

## Commands

```bash
# Run everything (bot instances + dashboard on http://localhost:8000, or $PORT)
python neura.py

# Same, skipping the interactive rich menu (auto-starts enabled accounts)
LAZYFARMERS_HEADLESS=1 python neura.py

# Installer + terminal account/proxy manager (also reachable as option 2 in neura.py)
python neura_setup.py

# Dependencies (discord.py-self must be the pinned commit; setup_engine verifies the
# version suffix "+g20ae80b3" and force-reinstalls if wrong)
pip install -r requirements.txt
```

There are no tests and no linter config. The de facto verification step is a syntax/import check:

```bash
python -m compileall -q core cogs modules utils lazy_engines component_v2_neura dashboard neura.py neura_setup.py
```

Beyond that, changes are validated by running the app and watching the rich console log / dashboard.

Environment variables: `LAZYFARMERS_DATA_ROOT` (persistent volume), `RAILWAY_VOLUME_MOUNT_PATH` (same,
auto-set by Railway), `LAZYFARMERS_HEADLESS`, `LAZYFARMERS_DASHBOARD_USER`,
`LAZYFARMERS_DASHBOARD_PASSWORD`, `PORT`. Deployment: `Procfile` + `railway.json` + `nixpacks.toml`
(see `RAILWAY.md`); must stay at 1 replica or every account sends each command twice.

## Architecture

### Process shape

`neura.py` → runs `LazySetupEngine.environment_healthy()` (self-installs deps and re-execs if not) →
starts Flask in a **daemon thread** → optional interactive menu → idles forever. Headless is
auto-detected (`LAZYFARMERS_HEADLESS`, any `RAILWAY_*` env var, or no tty) because a blocked console
prompt would otherwise keep the dashboard from ever serving.

**Nothing starts an account on its own.** The process comes up with an empty farm every time — a
redeploy, a crash-restart or a host migration leaves the farm exactly as stopped as it found it, and
there is no autostart flag. An account runs because someone asked for it on the dashboard.

**Starting many accounts is a queue, never a fan-out.** Start All (`/api/accounts/launch_all` →
`supervisor.start_sequence`) walks the space's enabled accounts *one at a time*: start it, wait for
READY (60 s cap, then move on rather than stall the farm behind a slow proxy), sleep
`LAZYFARMERS_START_GAP_S` (default 8 s), next. Do **not** replace this with `asyncio.gather` — putting
~16 accounts on the gateway inside eight seconds is what tripped Railway's 500-lines/sec log limit and
then killed the process with `std::system_error: Resource temporarily unavailable`. `state.login_slot`
(0.35 s) is a Discord rate limit, not a budget for the ~26 cogs each account loads behind it.

One sequence per space, held in `supervisor._sequences`. Arming a second while one is live is refused
(409), not queued — a second pass over a farm that is already coming up does nothing. The route returns
as soon as the queue is armed; the UI polls `GET /api/accounts/launch_all` for progress and can
`POST /api/accounts/launch_all/cancel`. `stop_all` cancels any live sequence first, or the queue would
undo the stop as fast as it worked. Stopping may go wide (`STOP_CONCURRENCY = 16`) — no logins, no cog
loading, none of what made a mass *start* dangerous.

One `NeuraBot` (`core/bot.py`, a `commands.Bot` with `self_bot=True`) per Discord account. All live
instances are in `core.state.bot_instances`. `core/supervisor.py` is the only place bots are created or
destroyed; the dashboard drives it so accounts can be started/stopped without restarting the process.
The first Start in a space is also what opens its history db (`supervisor._open_space_history`, tracked
in `supervisor.started_spaces`, closed by `neura._shutdown`).

**One Discord account, one instance.** `supervisor.running_duplicate(token, user_id)` checks *every*
space before creating a bot, and `NeuraBot.on_ready` repeats the check on `user_id` once login reveals
it (two config rows can hold different tokens for the same account) — the later instance flags itself
`duplicate` and closes. Two gateways on one account double every command it sends.

**A running account's identity is frozen.** Name, token and channel ids may only change while the
account is stopped; `_frozen_field_changed` in `dashboard/app.py` rejects the edit with 409, deleting a
running row is rejected the same way, and `_verify_accounts` skips its channel write-back for a running
account. The live bot was constructed from those three fields and re-reads `accounts.json` on every
config change, so editing them underneath it rotates channels mid-session or leaves a swapped token
unused until something restarts.

**Threading rule:** Flask handlers run on a different thread than the asyncio loop. Anything awaitable
must be scheduled with `asyncio.run_coroutine_threadsafe(coro, bot.loop)` or the `_bot_loop_call` /
`_bot_loop_fire` helpers in `dashboard/app.py` (they use the loop registered by
`supervisor.bind_loop()`). Setting plain flags like `bot.paused` from a route is fine.

### Per-user spaces (multi-tenancy)

The dashboard has two roles — the admin (`config/auth.json`) and activation-key users
(`config/dashboard_users.json`) — and **each gets its own data space**. A space is a directory:

```
data/users/admin/            accounts.json  proxies.json  settings.json  settings_<discord_id>.json  history.db
data/users/u_<hex>/          same, one per dashboard user
```

`core/spaces.py` is the only module that turns an owner string into a path, and `normalise_owner()` is
the only function that does the turning — that is what closes path traversal on this axis. Owner ids
must match `^(?:admin|u_[0-9a-f]{6,32})$`; Discord ids must match `^\d{5,25}$`. Use
`spaces.accounts_path(owner)` / `proxies_path(owner)` / `settings_path(owner, discord_id)` /
`history_path(owner)`; never join `DATA_DIR` by hand. `migrate_legacy()` runs once on `core.state`
import and moves a pre-existing single-tenant `config/{accounts,proxies,settings_*}.json` into the
admin space.

Everything downstream carries the owner explicitly:

- `utils/proxy_manager.py` — owner is the **first argument** of every stateful function.
- `core/supervisor.py` — `find_bot(owner, name)`, `start_account(account, owner)`,
  `stop_account(owner, name)`, `stop_all(owner=None)`,
  `running_names(owner=None)`. Keying on `(owner, name)` is what stops two tenants who both named an
  account `acc1` from stopping each other's bot.
- `NeuraBot` — `self.space_owner`, set from the supervisor; `bot.log`/`flag_account`/`_load_config` all
  route through it. It is deliberately **not** called `owner_id`: `commands.Bot.__init__` assigns
  `self.owner_id = options.get('owner_id')`, so a space id parked there is overwritten with `None` the
  moment `super().__init__()` runs, which detaches every bot from its space (no profile cards, and
  stop reporting "no accounts are running" while the accounts keep farming).
- `core/state.py` — `account_owners` (Discord id string → owner), `owner_of(id)`, `bots_for(owner)`,
  `owns_bot(owner, id)`, `visible_logs(owner)`, and `log_command(..., owner=None)`.

**Isolation in the dashboard is two chokepoints, not per-route checks.** `@space_required` resolves
`flask.g.owner` once, and `get_bot(account_id, owner)` only ever searches `state.bots_for(owner)` — so a
foreign Discord id is indistinguishable from a nonexistent one. `owns_account(account_id, owner)` is the
same check for routes that do not need the bot object. When adding a route:

- reads/writes inside one space → `@space_required`, then `g.owner`
- user management (`/api/users*`) and `/api/debug*` → `@admin_required`
- writing the true global `config/settings.json` (`?global=true`) → `@admin_required`
- called from the bot loop, not a request → there is no session, so take the owner from the account:
  `state.owner_of(account_id)`

Every non-GET `/api/*` request needs an `X-CSRF-Token` header matching the per-session token; the
`window.fetch` wrapper in `dashboard/static/js/core.js` attaches it to same-origin calls, so individual
call sites do not. Per-space caps live in `MAX_ACCOUNTS_PER_SPACE` / `MAX_PROXIES_PER_SPACE`
(`dashboard/app.py`, overridable via `LAZYFARMERS_MAX_ACCOUNTS` / `LAZYFARMERS_MAX_PROXIES`).

The Flask signing secret is `config/secret.key` (generated with `secrets.token_hex(32)` at mode 0600 on
first boot). There is **no constant fallback** — do not add one; a known secret means anyone can sign a
cookie carrying `is_admin: True`.

### Command pipeline (the core abstraction)

Cogs almost never send messages directly. Instead:

1. `register_actions()` on a cog calls `bot.neura_register_command(cmd_id, content, priority, delay,
   initial_offset)`, which writes an entry into `bot.cmd_states`.
2. `neura_scheduler_worker` (1 s tick) sees `now - last_ran >= delay` and pushes onto
   `bot.neura_queue` (an `asyncio.PriorityQueue`).
3. `neura_queue_worker` pops, re-checks the per-`cmd_id` cooldown, then calls `_send_safe`.
4. `_send_safe` enforces warmup, `paused`, `throttle_until`, `min_command_interval`, resolves the
   channel, and sends — through `NeuraHuman.neura_send` (`modules/neura_human.py`) when stealth typing
   is on, which also owns the periodic "human break".

Details that matter when adding a command module:

- `content` may be a **callable** (sync or async); returning `None` skips that tick.
- `content == ""` means "timer only" — the worker calls a cog hook instead of sending (e.g.
  `channelswitch` → `ChannelSwitch.trigger_switch()`).
- After a send, the queue worker calls back into the owning cog (`trigger_action`, `trigger_coinflip`,
  `trigger_slots`, `trigger_blackjack`) so it can recompute the next `content`/`delay`. That mapping is
  a hardcoded `class_map` in `neura_queue_worker`.
- Priorities come from `config/cmd_priorities.json` via `bot.get_cmd_priority` (lower = more urgent;
  `owo` is 1, quest-engine work is 4–5).
- `_fix_command` normalizes the prefix and swaps in short forms from `config/shortform.json` when
  `commands.<name>.use_shortform` is set.
- Ad-hoc, out-of-schedule commands go through `bot.neura_enqueue(...)`; the dashboard's manual command
  box and captcha answers use `bot.send_message(..., priority=True)`.

### Config layering and live reload

Three layers, deep-merged in this order by `NeuraBot._load_config`:

1. `config/settings.json` — the shipped global default (admin-only to write)
2. `data/users/<owner>/settings.json` — one space's default for every account in it
3. `data/users/<owner>/settings_<discord_id>.json` — the per-account override, written on first ready

Paths come from `core.spaces.settings_path(owner, discord_id=None)`; never build one by hand.

`POST /api/settings` writes the file(s) then calls `bot.sync_settings(new_config)`, which diffs dotted
paths and refreshes *only* the affected cogs. Three maps in `core/bot.py` drive that:

- `_collect_changed_paths` — dotted diff of old vs new config
- `_cogs_for_config_changes` — command name / top-level section → cog class name
- `_prune_disabled_scheduler_cmds` — drops `cmd_states` entries whose config was just disabled

**When you add a new config section or command, update those maps**, or the setting will only take
effect on restart.

### Persistence

`core/paths.py` resolves `DATA_ROOT` (env volume or repo root), derives `CONFIG_DIR` / `DATA_DIR`, and
copies bundled `config/*` defaults into the volume on first boot. Re-exported through `core.state`, so
use `state.CONFIG_DIR` / `state.DATA_DIR` — hardcoding `config/` writes to the ephemeral repo copy
instead of the volume.

`core/state.py` holds the shared mutable state: `bot_instances`, `account_stats` (keyed by Discord user
id **string**), the `command_logs` deque the dashboard renders, the `account_owners` map (Discord id →
space) and the `checking_gems` / `missing_gems_cache` coordination dicts. `utils/history_tracker.py`
persists sessions and cash to a **per-space** SQLite db at `spaces.history_path(owner)`; every public
function takes `owner=None` (None means the admin space, which keeps the legacy
`DATA_DIR/neura_history.db` file so no history is lost).

**`state.log_command` is load-bearing.** It is both the log sink and the stats counter: it parses log
*message text* (`"Sent: owo hunt"`, `"captcha solved"`, `"ban detected"`, …) to increment counters and
write history rows. Rewording a `bot.log(...)` string can silently break dashboard stats.

**The only log that survives the process is `data/neura.log`.** `modules/log_file.py` mirrors every
`state.log_command` line onto the volume from a writer thread (batched, never fsynced, never blocking
the loop — a full queue drops lines and reports the count rather than stalling the farm). Start of run
rolls the old file to `neura.log.1`, so a crash-restart cannot bury the evidence of what caused it.
`neura.main()` calls `log_file.start()`, which returns `(clean, previous)` — `True` if the last run set
the `data/clean_shutdown` marker, `False` if it vanished without one, `None` on a first boot with
nothing to judge. `_shutdown(clean=…)` writes that marker, a SIGTERM/SIGINT handler turns the host's
redeploy signal into a `SystemExit` so a deploy is not filed as a crash, and faulthandler dumps native
aborts into its own `neura_crash.log` (its own file because an open handle makes rotation fail on
Windows). Read it with `GET /api/debug/logfile?previous=1&lines=N` (admin only). When something stops
and "the log shows nothing", `neura.log.1` is the thing to read — the dashboard log view is a deque and
comes back empty with the farm.

### Message handling

Every cog filters `on_message` the same way: author id == `core.monitor_bot_id` (the OwO bot,
`408785106942164992`), channel in `bot.channels`, then `bot.is_message_for_me(message)`. That last check
lives in `modules/identity.py` and matches username / display name / guild nick / mention, with
`role="header" | "source" | "target"` variants for "X's zoo!" headers and "A prays for B" directionality.
OwO frequently replies without a mention, so identity matching is the only thing keeping one account
from reacting to another player's messages.

Pause/throttle model: `bot.paused` (indefinite — Security cog or dashboard), `bot.throttle_until`
(timestamp; `float('inf')` means "until a captcha is solved"), `bot.warmup_until`. `_send_safe` blocks
on all three. `cogs/cooldown_manager.py` parses OwO's "slow down" replies (relative `<t:...:r>`
timestamps or "N seconds") to back-date `cmd_states[...]['last_ran']` and set `throttle_until`.

### Captcha handling (three layers)

1. **Local ONNX** — `modules/captcha_solver.py` runs `models/best.onnx` over "letterword" image
   captchas and replies with the predicted letters.
2. **Paid hCaptcha services** — `modules/web_solver.py` performs the Discord OAuth → `owobot.com/captcha`
   → `/api/captcha/verify` flow, delegating the solve to `modules/services/{yescaptcha,nopecha,
   anticaptcha,captchaly}.py` (each exposes `get_balance` / `solve_hcaptcha`).
3. **Manual** — `WebSolver.enqueue_manual_solve` puts the account on a class-level queue serialized by
   `_manual_lock` (one browser/one solve at a time across all accounts) and waits on a future;
   `mark_verification_done(bot_id)` resolves it. `dashboard/app.py` mirrors this in `_pending_captchas`
   via `register_captcha_challenge` / `clear_captcha_challenge` so the UI can render a solve panel.

`cogs/security.py` is the detector (ban keywords, `(n/m)` warning counters, image captchas, captcha
links) and decides which layer to use; it also fires desktop/Termux notifications, a beep, and a Discord
webhook. Note `dashboard/app.py` monkeypatches `socket.getaddrinfo` to pin `owobot.com` to a hardcoded
IP.

### Components V2

OwO now sends "components v2" messages that `discord.py-self` does not model (`message.content` is
empty). `cogs/quest.py` and `cogs/others.py` therefore listen on `on_socket_raw_receive` and walk the raw
payload with `component_v2_neura/parser.py` (`parse_v2_message` → flat `V2Component` list). Clicking those
buttons goes through `component_v2_neura/interactions.py`, which hand-builds a
`POST /api/v9/interactions` with spoofed `X-Super-Properties` (it scrapes Discord's current build number).
Quest and zoo/team/level parsing each have both a V2 path and a legacy embed path.

`buttons(components)` **excludes disabled buttons**, and OwO only enables a quest claim button while the
reward is actually waiting — so quest claiming is driven off "is there an enabled claim button" rather
than re-deriving completion from the `N/M` progress text (`Quest._claim_targets`). Claims are deduped per
`(message_id, custom_id)` because OwO edits the card after each claim and MESSAGE_UPDATE re-enters the
parser; a rejected click un-deduped so the next card retries. Gate: `commands.quest.auto_claim`.

### Accounts and proxies

`data/users/<owner>/accounts.json` is `{"accounts": [{name, token, channels, enabled, proxy_id, status,
...}]}`. `utils/proxy_manager.py` is the single reader/writer for it *and* the proxy pool
(`data/users/<owner>/proxies.json`, parse/bulk-import/test/auto-assign) — **every public function takes
the owner as its first argument**: `load_accounts(owner)`, `save_proxies(owner, proxies)`,
`resolve_account_proxy(owner, account)`, `bulk_import(owner, text, limit=None)`, and so on. The pure
helpers (`parse_proxy_line`, `build_proxy_url`, `get_proxy_auth`, `test_proxy`, `mask_token`) take no
owner. `bot.flag_account(status, reason)` writes back
`invalid_token` / `needs_verification` / `cannot_send` so the dashboard can group broken accounts and
`/api/accounts/export?only=problem` can dump them. SOCKS proxies get an `aiohttp_socks.ProxyConnector`.

### Owner commands

`cogs/owner.py` lets the operator drive every farm account from their own Discord account. Config
section `owner` (`enabled`, `user_id`, `trigger` — default `farmers`). `farmers pay` / `send` /
`showbal` are special-cased; anything else is forwarded verbatim as an OwO command. An account name or
user id token after the trigger (`farmers acc2 bal`) narrows it to one account.

### Cross-account coop

Every `NeuraBot` shares **one** asyncio loop (`neura.py` binds it, `supervisor.start_account` creates each
runner as a task on it), so `await peer.neura_enqueue(...)` across instances is safe — no
`run_coroutine_threadsafe`. `lazy_engines/coop.py` is the single place that decides whether a sibling may
be leaned on: `peers(bot)` returns live accounts that are ready, unpaused, past warmup, not sitting on an
unsolved captcha (`throttle_until == inf`) **and** sharing a channel with the asker (`shared_channel` —
OwO only credits a social interaction it can see both sides of). `is_initiator(bot, peer)` compares user
ids so exactly one side of a two-sided action starts it, and `may_ask`/`note_ask` hold a
process-wide `(giver, receiver, action)` cooldown table.

`cogs/coop.py` schedules the periodic friendly battle (`coop_offer`); `lazy_engines/quest_engine.py`
routes social quests (pray/curse/cookie/emote/battle-with-a-friend) through `coop.ask_peer`.
`cogs/response_handler.py` already auto-accepts any duel it is mentioned in and stamps
`bot.last_duel_accept`, which `Coop.arm_accept_fallback` checks before sending a backup `owo ab`.
Config section `coop` (`enabled`, `quests.enabled`, `battle.{enabled,interval_min,min_gap_s,arbitrate}`,
`fallback_targets`). `fallback_targets` is a *list* of user ids — it is in `isListField` in
`dashboard/static/js/config.js` so the UI writes an array, not a comma string.

### Zoo team watcher

`cogs/others.py` owns both the OwO level sync and the battle team. `parse_zoo` reads the zoo card (v2 or
legacy), ranks every owned animal, and `_apply_team_upgrade` swaps in anything rarer — with hysteresis so
a same-tier tie does not churn the team every scan. `_watch_hunt` is the watcher: a hunt result is the
moment the zoo changes, so a catch rarer than the weakest team slot triggers `request_team_check`
immediately instead of waiting for the `team_scan` timer. Config: `commands.team.{enabled,slots,watch_zoo,
min_action_gap_s}`.

When OwO answers `owo level` with a rendered image card, `_note_level_unreadable()` sets
`stats['level_source'] = 'image'` and leaves level/xp blank; the dashboard renders "image card ·
unreadable" rather than a stale number. Do not reintroduce a guess here.

### Dashboard frontend

`dashboard/templates/index.html` is a single page of `.view` divs toggled by `window.nav()`. Plain
global-function JS, **load order matters**: `core.js` declares the shared globals (`currentConfig`,
`currentAccountId`, chart handles), feature files follow, `init.js` is last and wires
`DOMContentLoaded` plus the polling intervals (1 s stats, 2 s captcha, 5 s accounts). CSS is split
`base/layout/components/responsive` + `pages/*.css`. Every css *and* js tag carries
`?v={{ asset_v }}`, injected by the `inject_asset_version` context processor in `dashboard/app.py` from
the newest mtime under `static/` — so an edited file busts the cache on the next reload. No bundler —
edit and reload.

The config view is generated from the config object itself (`buildConfigCategories` in `config.js`), so a
new settings key appears without frontend work; only its hint text (`CONFIG_CATEGORY_HINTS` /
`CONFIG_CMD_HINTS` in `core.js`) and any list/select special-casing need adding.

## Conventions

- Every source file starts with the 10-line GPL header block (including its
  `Copyright (c) 2025-Present Routo` licence line); copy that when adding files. Do **not** add author,
  Discord, YouTube or upstream-repo links — those were stripped deliberately.
- New cogs are auto-discovered: `NeuraBot._load_cogs` loads every `cogs/*.py`. Provide
  `async def setup(bot)`; add `register_actions()` if the cog schedules commands (it is re-run on every
  ready and on relevant config changes, so it must be idempotent).
- Log through `bot.log(TYPE, message)`, not `print`. Types in use: `SYS`, `CMD`, `INFO`, `SUCCESS`,
  `COOLDOWN`, `STEALTH`, `GAMBLING`, `SECURITY`, `ALARM`, `WARN`, `ERROR`, `DEBUG`; colors come from
  `config/logmisc.json`. **On a host, `bot.log` writes nothing to stdout at all** — `_console_levels`
  in `modules/neura_logs.py` returns an empty set when any `RAILWAY_*` env var is set, so the
  provider's log viewer is left for the web server's own output (the `console.print` calls in
  `neura.py`, waitress errors, tracebacks) and per-account output goes only to the dashboard via
  `_record` → `state.log_command`. `LAZYFARMERS_CONSOLE_LEVELS=ERROR,SECURITY` (or `all`) puts some
  back; `LAZYFARMERS_PLAIN_LOGS` forces host/colour mode either way. A human on a real terminal still
  sees every line. So: **do not use `print` for a bot-side diagnostic expecting to see it on Railway,
  and do not "fix" the quiet console by widening the default set.**
- `CURRENT_VERSION` in `core/bot.py` is a display string only. `check_version` used to fetch a remote
  `version.json` and `sys.exit(0)` on mismatch — a kill-switch pointed at someone else's repo. It is
  gone; do not reintroduce a remote version gate.
- Secrets live in `config/auth.json`, `config/secret.key`, `config/dashboard_users.json` and every
  `data/users/<owner>/{accounts.json,settings_*.json}`. The first three are gitignored; keep it that
  way and never commit a populated copy.
