# Jj-next: isolated development copy

Based on `Aditimybbby/Jj` commit `96ec3ce`. The original repository is not modified.
This copy includes offline regression tests and a GitHub Actions workflow. It is
not yet validated against live Discord accounts or a paid NopeCHA session.

## What changed

- Legacy account/proxy migration recognizes empty destination wrappers and repairs
  installs that already wrote the old migration marker. Existing populated
  destinations are preserved; migrated originals are retained as `.migrated`.
- Password resets invalidate existing regular-user sessions. Users with cookies
  from the old version must sign in again.
- Settings reload consistently applies shared defaults, space defaults, then
  account overrides. New account override files are sparse. Use Apply to all to
  replace older full account snapshots explicitly.
- Failed sends do not advance command cooldowns or daily-success hooks.
- Permanent account failures cancel workers and close sessions.
- Captcha jobs are deduplicated per account, limited to two concurrent solver
  jobs, and retried up to three times. Components-v2 links and partial gateway
  updates are handled. A pending challenge is not hidden merely because 15
  minutes passed. Failed jobs remain visible for manual attention. The website
  queues solver work immediately instead of holding an HTTP request for minutes.
- Transfers use one recipient lock, sender/recipient/amount matching, explicit
  confirmation retries, and final OwO receipt verification. An accepted click
  alone does not count as a transfer. Uncertain transfers are not automatically
  resent. Their journal survives restarts and can be reconciled by reading the
  original prompt again. Other owner commands remain supported.
- An official Discord monitor bot posts and edits an operations embed with
  configured/connected/ready accounts, attention flags, total cached cowoncy,
  estimated withdrawable funds, sampled 1-hour/24-hour net changes, and the top
  account. Confirmed withdrawals are added back when computing net changes.
- The website creates public channels in groups of three saved accounts.
  Re-running setup reuses channels by their managed topic. Large farms are split
  across categories. No channel IDs need to be entered manually.

## Monitor and channel setup

1. Install the existing project requirements and start `python neura.py`.
   Use a fresh data directory for testing: set `LAZYFARMERS_DATA_ROOT` to a test
   directory and set dashboard credentials through environment variables.
2. Create a separate official bot in the Discord Developer Portal. Invite it to
   the target server with Manage Channels, View Channels, Send Messages, Embed
   Links, Attach Files, and Read Message History permissions. No privileged
   gateway intents are required. Leave the application's Interactions Endpoint
   URL empty; this monitor receives interactions over the Gateway.
3. Ensure OwO and the account users are already members of that server.
4. Open the Monitor Bot tab. Enter the monitor bot token
   and save. Tokens are stored server-side with owner-only filesystem permissions
   and are never included in configuration GET responses. Alternatively, the
   admin space can use `LAZYFARMERS_MONITOR_TOKEN` as an initial default. A token
   explicitly saved in the dashboard takes precedence. New tokens are validated
   before saving; a rejected replacement leaves the previous settings intact.
5. Add/import your accounts with tokens. Manual channel fields are optional and
   collapsed by default. Stop any running accounts before continuing.
6. Load servers, choose one by name, and press Create & assign channels. Progress
   is shown on the page. Setup preserves all account rows without testing tokens
   or membership. It creates one public channel per three saved accounts, saves assignments, creates
   `operations-overview`, and starts the monitor.
7. Start your accounts from the Accounts page. They still do not auto-start on
   process boot. The configured monitor can reconnect on boot.
8. Set `owner.user_id` in website Configuration. This configured ID is the
   withdrawal recipient and authorizes the Discord Withdraw button. The authenticated website space can also
   start a withdrawal. The percentage is configurable, defaulting to 100% of
   eligible balance, capped by each sender's estimated remaining daily allowance.

Do not install `discord.py` alongside `discord.py-self`. The official monitor
uses aiohttp's REST/WebSocket support so the two conflicting packages are not
needed.

## Withdrawal limits: research and uncertainty

The published OwO source, accessed 2026-09-10, computes:

`send = 50,000 + level × 14,000 + floor(level / 10) × 5,000,000`

`receive = ceil(send × (floor(level / 10) / 2 + 1))`

| Level | Daily send estimate | Daily receive estimate |
| --- | ---: | ---: |
| 0 | 50,000 | 50,000 |
| 1 | 64,000 | 64,000 |
| 2 | 78,000 | 78,000 |
| 10 | 5,190,000 | 7,785,000 |

This differs from the reported 64,000 at level 0. The public source may differ
from the current deployment; these are **estimates, not a promise of allowance**.
The monitor shows unknown limits separately in its estimate and lets OwO check
them when sending. Explicit sender-limit rejections can reduce the transfer amount. Set
`owner.limit_mode` to `fixed` and `owner.daily_send_limit` to an observed limit if
needed. Otherwise `limit_mode=level` uses the account's existing level tracker.
The local ledger rolls over at UTC midnight. Actual server refusals override its
estimate. The recipient also has a receive cap, so the sum of sender allowances
may not all fit into one recipient today. A receive-limit response stops the batch.

Sources:

- [OwO limit calculation](https://github.com/ChristopherBThai/Discord-OwO-Bot/blob/9aec92b274bae64ea078fc04540398713e7f7928/src/commands/commandList/economy/utils/cowoncyUtils.js)
- [OwO confirmation and receipt flow](https://github.com/ChristopherBThai/Discord-OwO-Bot/blob/9aec92b274bae64ea078fc04540398713e7f7928/src/commands/commandList/economy/give.js)
- [Discord Gateway lifecycle](https://docs.discord.com/developers/events/gateway)
- [Discord interactions](https://docs.discord.com/developers/interactions/receiving-and-responding)

## Verification

Local result: 26 offline tests passed, plus Python/JavaScript syntax and diff checks.
The full Flask application and Discord/NopeCHA integrations were not started here;
their external dependencies and credentials were not available in this runtime.

Run `python -m unittest discover -s tests -v` with Python 3.12 and aiohttp installed.
Tests use temporary data and fake network responses; they do not log into Discord,
spend solver credits, create real channels, or transfer cowoncy. Where Discord or
Flask is unavailable, the tests execute original method bodies extracted from the
source, with dependencies supplied by the harness. They are function-level and
protocol-simulation tests, not an end-to-end deployment certification.

Run `python -m compileall -q core cogs dashboard modules tests` and
`node --check dashboard/static/js/monitor.js` for syntax checks. The included CI
workflow repeats the offline checks without secrets.

Before using a live farm, validate in a small test server with three accounts:

- Verify the managed farm channels and overview are visible to server members,
  including channels migrated from an earlier private setup.
- Confirm the monitor edits one message and rejects a non-owner button click.
- Exercise one real captcha with your configured NopeCHA key and browser. Solver
  failures stay visible; automated solving cannot be guaranteed for every challenge.
- Make one small withdrawal, check the final OwO receipt and journal, then test
  the same prompt through Recheck uncertain receipts. It must not book twice.
- Stop/restart the process and verify saved identities, assignments and journals.

Performance figures are sampled balance changes, not audited income. Cash
sampling frequency/retention can produce a shorter history than the displayed
window. Unknown balances are excluded; cached balances and unknown limits are
shown separately from fresh, known-limit funds.
There is no USD valuation. No software can guarantee 100% transfer success when
Discord/OwO rejects a request or the network loses the final response.
