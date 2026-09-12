# Public channel and withdrawal update

## Discord commands

The connected monitor bot registers these slash commands in the selected server on startup:

- `/panel`: posts a fresh active panel in the current text channel. The previous panel's buttons no longer authorize actions.
- `/setup_channels`: creates or reuses public farm channels and assigns every saved account.
- `/recreate_channels confirm:true`: permanently deletes this space's managed farm and overview channels (including message history), recreates public channels, and saves the new account assignments. Unrelated channels and other spaces are preserved; a category containing unrelated channels is kept.

Only IDs in website Configuration `owner.user_id` can use these commands. Stop accounts before setup or recreation. Account starts are blocked while either operation is running. Operations acknowledge the command before doing work and report progress and failures in the website Monitor Bot tab. A failed Discord acknowledgement cancels the queued command.

Restart after updating to register the commands. The monitor must have Keep monitor connected enabled and a selected server; it can connect before an overview channel exists. Channel creation/deletion requires Manage Channels. Commands use Discord's [guild application command API](https://docs.discord.com/developers/interactions/application-commands).

## Channel assignment and withdrawals

- Create & assign channels preserves every saved account row and proxy setting. It does not log into accounts, check membership, or remove duplicates. Existing managed categories, farm channels, and operations-overview are made public when setup is run again.
- Website Configuration `owner.user_id` controls the Discord button and withdrawal recipient. Selecting a server no longer assigns its owner as recipient. Saved owner changes take effect on the next action.
- Withdrawals process every configured account with a per-account result. Running accounts use the same transfer function as `farmers send`; stopped or paused accounts are reported. One account failure does not abort the rest.
- Unknown local levels no longer stop the flow after cash lookup. The command is `owo send @recipient amount`. Known limits cap amounts; when OwO explicitly rejects an amount and reports a smaller remaining allowance before confirmation, the transfer retries at that allowance. Recipient limits stop further transfers and report remaining accounts as skipped.
- Only final receipts count as sent. Uncertain transfers still require reconciliation and are never automatically resent.

After updating, restart the application, save your owner ID under Configuration, stop accounts, and run Create & assign channels once to update existing channel permissions. Then start accounts and use Withdraw available OwO.

Validated with offline protocol and frontend tests. No live Discord channels or transfers were used for validation.

---

The following notes describe earlier versions and are retained as history; the update above supersedes their private-channel, account-verification, server-owner and unknown-level behavior.

# Monitor and account setup fixes

## Withdrawal display update

The Monitor Bot tab now shows **Available to withdraw (estimate)** using the saved
withdrawal percentage and cached balances of ready, active, unpaused accounts.
Known daily limits cap the estimate. Missing levels no longer silently turn a
known balance into a zero estimate: the card separately identifies the amount
that needs level sync before withdrawal, and the amount with fresh balances and
known limits. The old "59 unknown limits" text counted accounts without usable
limit data; it was not an error code or a currency amount.

The estimate is not a promise that the entire amount can be sent. Accounts with
unknown limits still need a readable level or an explicitly configured fixed
daily limit before the existing withdrawal process can send from them. Cached
balances are refreshed by that process. Server limits remain authoritative.

The recipient's own account, stopped/paused accounts, unresolved transfers, and
spaces without a recipient are excluded. The monitor now uses the live account
ID when saved identity is outdated, and falls back to the calculated limit when
the saved server allowance is null. The Discord overview uses the same wording.

Apply this ZIP to the application source and restart/redeploy. Keep existing
deployment secrets and runtime data. Refresh the dashboard after restarting.

Verification: 71 offline Python regression tests and the dependency-free monitor
frontend suite passed, plus Python compilation and JavaScript syntax checks.
Coverage includes the 59-account missing-limit display, daily reset, server
allowances (including null and zero), fixed limits, stale balances, percentages,
recipient exclusion, and unresolved transfers. No live Discord withdrawal or
browser layout verification was performed.

## Using this update

Replace your application source with this version and restart/redeploy using your existing setup. Keep your existing data directory and deployment secrets. Open **Monitor Bot** in the sidebar.

1. Enter the monitor bot token and choose **Save & validate**. Quotes, surrounding whitespace and a copied `Bot ` prefix are cleaned up. Invalid replacements leave the saved settings intact.
2. Use **Load servers**, invite the bot if necessary, and choose your server. Loading servers now validates and saves a newly pasted token first.
3. Add an account with its name and token, or bulk import one token per line. Manual channel IDs are optional and collapsed by default.
4. With your accounts stopped, choose **Create & assign channels** in Monitor Bot. Once setup completes, start your accounts from Accounts.

The monitor tab uses the existing Aurora Glass theme, including its colors, surfaces, typography and responsive spacing. It shows account counts, total cached OwO, estimated withdrawable balance and the top earner.

## Authentication fixes

- A saved token takes precedence over `LAZYFARMERS_MONITOR_TOKEN`; the environment value remains a fallback for initial setup.
- New tokens are checked using Discord's bot identity endpoint before saving. Bot REST and Gateway requests use the same cleaned token.
- Discord credential errors include a useful message and do not trigger dashboard logout. Expired dashboard sessions still redirect to login.
- Saved percentages, enabled state and server selection are restored on reload. Polling preserves unsaved edits. Action errors stay visible and failed requests re-enable the controls.
- Startup errors retain their message instead of showing only the exception class.

A genuinely invalid or revoked token still needs replacement using the application's Bot page in the Discord Developer Portal. These changes cannot make an invalid token valid.

## Verification

Passed locally:

- `python -m unittest discover -s tests -q`: 53 Python tests, including account identity, monitor authentication/configuration and partial provisioning regressions.
- `node tests/monitor_frontend.cjs`: saved settings, edits during polling, save-before-discovery ordering, CSRF, Discord 401 handling, dashboard session expiry, and add/import with empty channel lists. Uses a lightweight DOM test double, not a browser rendering engine.
- Python compilation and JavaScript syntax checks.

`tests/monitor_ui.cjs` is an optional Playwright browser test with mocked APIs. It was not executed successfully in this environment: no local browser binary was available, its download was blocked, and the remote browser blocked the local preview. Rendered desktop/mobile appearance remains unverified. With Playwright and Chromium installed, run `node tests/monitor_ui.cjs` locally. Set `MONITOR_SCREENSHOTS` to an output directory to save screenshots.

No live Discord login or server operation was performed. Captcha processing and withdrawal logic were not changed.

## Channel setup continuation and duplicates

Channel setup now handles account failures individually. Rejected account tokens, connection/proxy errors and accounts missing from the server are listed under **Needs attention**, while healthy accounts continue. A failed channel group also allows later groups to proceed. Successful groups are saved as they finish, so a later overview error does not lose their assignments.

Setup removes duplicate configuration entries based on matching nonempty tokens or freshly verified Discord user IDs, keeping the first matching entry. It does not delete Discord accounts. Failed unique accounts remain saved with their setup error for correction and retry. A successful retry clears the setup error. Duplicate removal also updates proxy assignments.

The monitor shows separate **Assigned**, **Needs attention** and **Duplicates removed** results and totals. Accounts with a setup failure appear in the Accounts page's Needs attention section. A job with isolated failures finishes with errors instead of silently reporting full success. Shared configuration failures, such as invalid monitor credentials or missing server access, still stop setup. Account edits during setup are protected from being overwritten.

Additional tests cover bad tokens, timeouts, missing members, identical-token and verified-identity duplicates, all-failed batches, isolated channel failures, persistence after overview failure, retry recovery, concurrent edits, cancellation, partial-job status and grouped UI results. Live Discord and visual layout checks remain unverified.

## v3: Account identity check matches dashboard authentication

The channel-setup check previously sent a standalone aiohttp request to `/users/@me` and labeled every HTTP 401 response as proof of an invalid account token. Dashboard startup uses the installed Discord client library instead. These were different authentication paths, so the setup message was too definitive for accounts that still start successfully.

Setup now uses `discord.Client.login` with the same account token, resolved proxy/authentication settings and shared login pacing as dashboard startup. The client library handles its own authentication/session setup. This identity lookup does not connect the Gateway or run account workers. The temporary client is closed after success, failure, timeout or cancellation. It requires a freshly returned identity and does not trust a stale saved account ID.

To apply: replace the application source and restart/redeploy. Stop the accounts, then run **Monitor Bot → Create & assign channels** again. The rerun replaces old per-account setup results; successful assignments clear previous setup errors. Do not rotate tokens solely because of the old setup 401 message when those accounts can still start.

Nine added offline tests cover use of the client library instead of bare HTTP, proxy and token forwarding, fresh identity, error reporting, timeouts, cancellation, bot/missing-token rejection and cleanup. Tests use a simulated Discord client: the deployed client and live account login were not available here, so this change still needs verification against the affected accounts.
