# Monitor and account setup fixes

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

- `python -m unittest discover -s tests -q`: 34 Python tests, including 8 new monitor authentication/configuration regressions.
- `node tests/monitor_frontend.cjs`: saved settings, edits during polling, save-before-discovery ordering, CSRF, Discord 401 handling, dashboard session expiry, and add/import with empty channel lists. Uses a lightweight DOM test double, not a browser rendering engine.
- Python compilation and JavaScript syntax checks.

`tests/monitor_ui.cjs` is an optional Playwright browser test with mocked APIs. It was not executed successfully in this environment: no local browser binary was available, its download was blocked, and the remote browser blocked the local preview. Rendered desktop/mobile appearance remains unverified. With Playwright and Chromium installed, run `node tests/monitor_ui.cjs` locally. Set `MONITOR_SCREENSHOTS` to an output directory to save screenshots.

No live Discord login or server operation was performed. Captcha processing and withdrawal logic were not changed.
