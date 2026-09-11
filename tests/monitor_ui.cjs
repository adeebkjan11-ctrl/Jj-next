// Run with Playwright installed: node tests/monitor_ui.cjs
// All network requests are intercepted; no Discord login or live writes.
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
(async () => {
    const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
    try {
        const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
        const errors = [], calls = [];
        let config = {token_set: true, enabled: true, runtime: 'connected', withdraw_percent: 75,
            guild_id: '22222', guild_name: 'Farm server', channel_id: '33333', bot_id: '99999', operation: {}};
        let accounts = [], rejectToken = false, expireSession = false;
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(() => {
            window.checkDirty = () => {};
            window.populateAccountProxyDropdown = () => {};
        });
        await page.route('**/*', async route => {
            const req = route.request(), url = new URL(req.url());
            if (url.origin !== 'http://monitor.test') return route.abort();
            if (url.pathname.startsWith('/api/')) {
                const body = req.postDataJSON();
                calls.push({path: url.pathname, method: req.method(), body, headers: req.headers()});
                let data = {}, status = 200;
                if (expireSession) { status = 401; data = {success: false, error: 'Session expired'}; }
                else if (url.pathname === '/api/monitor') {
                    if (req.method() === 'POST') {
                        if (rejectToken) { status = 400; data = {success: false, error: 'Discord rejected the monitor bot token (HTTP 401)'}; }
                        else { config = {...config, enabled: body.enabled, withdraw_percent: body.withdraw_percent}; data = {success: true, ...config}; }
                    } else data = config;
                } else if (url.pathname === '/api/monitor/servers') data = {success: true, guilds: [{id: '22222', name: 'Farm server'}], invite_url: 'https://discord.com/oauth2/authorize?client_id=99999&scope=bot'};
                else if (url.pathname === '/api/monitor/summary') data = {success: true, summary: {configured: 7, connected: 6, ready: 6, total_owo: 2800000, withdrawable: 384000, unknown_limits: 1, best: {name: 'acc3', net_24h: 82400}}};
                else if (url.pathname === '/api/accounts/config') {
                    if (req.method() === 'POST') { accounts = body.accounts; data = {status: 'success'}; }
                    else data = {accounts};
                } else if (url.pathname === '/api/accounts/list') data = [];
                else if (url.pathname === '/api/accounts/bulk') data = {success: true, message: 'Imported 2 accounts'};
                else data = {success: true};
                return route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
            }
            if (url.pathname === '/login') return route.fulfill({contentType: 'text/html', body: 'Sign in'});
            if (url.pathname === '/') {
                let html = fs.readFileSync(path.join(root, 'dashboard/templates/index.html'), 'utf8')
                    .replaceAll('{{ csrf_token }}', 'synthetic-csrf').replaceAll('{{ asset_v }}', 'test');
                html = html.replace(/<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g, (tag, src) =>
                    /\/static\/js\/(core|navigation|accounts|monitor)\.js/.test(src) ? tag : '');
                return route.fulfill({contentType: 'text/html', body: html});
            }
            if (url.pathname.startsWith('/static/')) {
                const file = path.join(root, 'dashboard', url.pathname);
                if (fs.existsSync(file)) return route.fulfill({path: file});
            }
            return route.fulfill({status: 404, body: ''});
        });
        await page.goto('http://monitor.test/');
        await page.waitForFunction(() => document.querySelectorAll('.monitor-metric').length === 4);
        await page.locator('#monitor-nav').click();
        await page.waitForFunction(() => document.querySelector('#monitor').classList.contains('active-view'));
        assert.equal(await page.locator('#monitor-enabled').isChecked(), true);
        assert.equal(await page.locator('#monitor-percent').inputValue(), '75');
        assert.equal(await page.locator('#monitor-server').inputValue(), '22222');
        await page.locator('#monitor-percent').fill('60');
        await page.evaluate(() => pollMonitor());
        assert.equal(await page.locator('#monitor-percent').inputValue(), '60', 'Polling must preserve edits');
        await page.locator('#monitor-token').fill('Bot synthetic-new-token');
        const before = calls.length;
        await page.getByRole('button', {name: 'Load servers', exact: true}).click();
        await page.waitForFunction(() => !document.querySelector('#monitor-token').disabled);
        const relevant = calls.slice(before).filter(c => c.method === 'POST' || c.path.endsWith('/servers'));
        assert.equal(relevant[0].path, '/api/monitor');
        assert.equal(relevant[0].headers['x-csrf-token'], 'synthetic-csrf');
        assert.equal(relevant[1].path, '/api/monitor/servers');
        assert.equal(await page.locator('#monitor-token').inputValue(), '');
        if (process.env.MONITOR_SCREENSHOTS) {
            fs.mkdirSync(process.env.MONITOR_SCREENSHOTS, {recursive: true});
            await page.screenshot({path: path.join(process.env.MONITOR_SCREENSHOTS, 'monitor-desktop.png'), fullPage: true});
        }
        await page.setViewportSize({width: 390, height: 844});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'Mobile page overflows');
        if (process.env.MONITOR_SCREENSHOTS) await page.screenshot({path: path.join(process.env.MONITOR_SCREENSHOTS, 'monitor-mobile.png'), fullPage: true});
        rejectToken = true;
        await page.locator('#monitor-token').fill('invalid-synthetic');
        await page.getByRole('button', {name: 'Save & validate', exact: true}).click();
        await page.waitForFunction(() => document.querySelector('#monitor-progress').textContent.includes('HTTP 401'));
        assert.equal(new URL(page.url()).pathname, '/', 'Discord error must not log the dashboard out');
        assert.equal(await page.locator('#monitor-token').inputValue(), 'invalid-synthetic');
        assert.equal(await page.locator('#monitor-token').isEnabled(), true);
        await page.evaluate(() => nav('accounts', document.querySelector('.nav-item')));
        await page.evaluate(() => showAccountForm());
        assert.equal(await page.locator('#acct-form-channels').isVisible(), false);
        await page.locator('#acct-form-name').fill('new-account');
        await page.locator('#acct-form-token').fill('synthetic-account');
        await page.locator('#account-form-modal').getByRole('button', {name: 'Save', exact: true}).click();
        await page.waitForFunction(() => !document.querySelector('#account-form-modal').classList.contains('visible'));
        assert.deepEqual(calls.find(c => c.path === '/api/accounts/config' && c.method === 'POST').body.accounts[0].channels, []);
        await page.evaluate(() => showBulkImport());
        assert.equal(await page.locator('#bulk-channels').isVisible(), false);
        await page.locator('#bulk-tokens').fill('synthetic-one\nsynthetic-two');
        await page.locator('#account-bulk-modal').getByRole('button', {name: 'Import', exact: true}).click();
        await page.waitForFunction(() => !document.querySelector('#account-bulk-modal').classList.contains('visible'));
        assert.equal(calls.find(c => c.path === '/api/accounts/bulk').body.channels, '');
        assert.deepEqual(errors, []);
        expireSession = true;
        await page.evaluate(() => pollMonitor());
        await page.waitForURL('**/login?expired=1');
        console.log('PASS: monitor navigation, saved settings, dirty edits, token-save ordering, CSRF, Discord 401, session expiry, token-only add/import, desktop/mobile layout');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
