// Dependency-free behavior tests against the shipped scripts and template.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
class Element {
    constructor() { this.value = ''; this.checked = false; this.disabled = false; this.children = []; this.dataset = {}; this.options = []; this.listeners = {}; this.style = {}; this.textContent = ''; this.hidden = false; this.classes = new Set(); this.classList = {add: x => this.classes.add(x), remove: x => this.classes.delete(x), contains: x => this.classes.has(x)}; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    append(child) { this.children.push(child); }
    add(option) { this.options.push(option); }
    replaceChildren(...children) { this.children = children; this.options = children; }
}
(async () => {
    const elements = new Map();
    const html = fs.readFileSync(path.join(root, 'dashboard/templates/index.html'), 'utf8');
    for (const match of html.matchAll(/<[a-z][^>]*\bid="([^"]+)"[^>]*>/gi)) {
        assert(!elements.has(match[1]), `Duplicate element id: ${match[1]}`);
        const el = new Element();
        el.value = /\bvalue="([^"]*)"/.exec(match[0])?.[1] || '';
        elements.set(match[1], el);
    }
    const el = id => { assert(elements.has(id), `Missing template element: ${id}`); return elements.get(id); };
    const events = {}, calls = [], toasts = [];
    let config = {token_set: true, enabled: true, runtime: 'connected', withdraw_percent: 75, guild_id: '22222', guild_name: 'Farm server', channel_id: '33333', bot_id: '99999', operation: {}};
    let reject = false, expire = false;
    const context = vm.createContext({console, Headers, Intl, URL, setTimeout: () => 0, clearTimeout: () => {},
        Option: function(text, value) { this.text = text; this.value = value; },
        document: {hidden: false, getElementById: el, createElement: () => new Element(),
            querySelector: selector => selector.includes('csrf-token') ? {getAttribute: () => 'synthetic-csrf'} : null,
            querySelectorAll: selector => selector.startsWith('#monitor ') ? ['monitor-token', 'monitor-percent', 'monitor-enabled', 'monitor-server'].map(el) : [],
            addEventListener: (name, fn) => { events[name] = fn; }},
        location: {origin: 'http://monitor.test', href: '/'},
        fetch: async (url, init = {}) => {
            const body = init.body ? JSON.parse(init.body) : undefined;
            calls.push({url, method: init.method || 'GET', body, headers: init.headers});
            let status = 200, data;
            if (expire) { status = 401; data = {success: false, error: 'Session expired'}; }
            else if (url === '/api/monitor') {
                if (init.method === 'POST') {
                    if (reject) { status = 400; data = {success: false, error: 'Discord rejected the monitor bot token (HTTP 401)'}; }
                    else { config = {...config, enabled: body.enabled, withdraw_percent: body.withdraw_percent}; data = {...config, success: true}; }
                } else data = {...config};
            } else if (url === '/api/monitor/servers') data = {success: true, guilds: [{id: '22222', name: 'Farm server'}], invite_url: 'https://discord.com/oauth2/authorize?client_id=99999'};
            else if (url === '/api/monitor/summary') data = {summary: {configured: 7, ready: 6, connected: 6, total_owo: 123456, withdrawable: 64000, unknown_limits: 1, best: null}};
            else if (url === '/api/accounts/config') data = init.method === 'POST' ? {status: 'success'} : {accounts: []};
            else data = {success: true};
            return {status, ok: status < 400, json: async () => data};
        }});
    context.window = context;
    for (const name of ['core', 'accounts', 'monitor']) vm.runInContext(fs.readFileSync(path.join(root, `dashboard/static/js/${name}.js`), 'utf8'), context);
    const renderAccounts = vm.runInContext('renderAccountConfigList', context);
    vm.runInContext('showToast = (message, type) => window.testToast(message, type); renderAccountConfigList = () => {};', context);
    context.testToast = (message, type) => toasts.push({message, type});
    await events.DOMContentLoaded();
    assert.equal(el('monitor-percent').value, 75);
    assert.equal(el('monitor-enabled').checked, true);
    assert.equal(el('monitor-server').value, '22222');
    assert.equal(el('monitor-summary').children.length, 4);
    el('monitor-percent').value = '60'; el('monitor-percent').listeners.input();
    await context.pollMonitor();
    assert.equal(el('monitor-percent').value, '60');
    el('monitor-token').value = 'Bot synthetic-new';
    const before = calls.length;
    await context.loadMonitorServers();
    assert.equal(calls[before].url, '/api/monitor');
    assert.equal(calls[before].method, 'POST');
    assert.equal(calls[before].headers.get('X-CSRF-Token'), 'synthetic-csrf');
    assert.equal(calls[before+1].url, '/api/monitor/servers');
    assert.equal(el('monitor-token').value, '');
    config.operation = {kind: 'provision', status: 'complete_with_errors', result: {accounts: 3, failed: 1, awaiting: 1, duplicates_removed: 1, channels: 1}, results: [
        {name: 'healthy', status: 'assigned', channel_id: '12345'},
        {name: 'broken', status: 'failed', reason: 'Account token rejected'},
        {name: 'never-started', status: 'awaiting_identity', reason: 'Start it once to get access', channel_id: '12345'},
        {name: 'copy', status: 'duplicate_removed', reason: 'Same Discord account; kept healthy.'}
    ]};
    await context.pollMonitor();
    assert.equal(el('monitor-status').textContent, 'Setup finished with errors');
    assert.match(el('monitor-progress').textContent, /3 assigned · 1 waiting for a first connection · 1 failed · 1 duplicates removed/);
    const resultText = el('monitor-results').children.map(child => child.textContent).join(' ');
    assert.match(resultText, /Needs attention \(1\)/);
    assert.match(resultText, /start the account once to get access \(1\)/);
    assert.match(resultText, /Duplicates removed \(1\)/);
    assert.match(resultText, /Assigned \(1\)/);
    assert(!resultText.includes('confirmed'), 'Setup must not use withdrawal result labels');
    reject = true;
    el('monitor-token').value = 'invalid-synthetic';
    await context.saveMonitor();
    assert.equal(context.location.href, '/');
    assert.match(el('monitor-progress').textContent, /HTTP 401/);
    assert.equal(el('monitor-token').value, 'invalid-synthetic');
    assert.equal(el('monitor-token').disabled, false);
    await context.pollMonitor();
    assert.match(el('monitor-progress').textContent, /HTTP 401/, 'polling should keep action errors visible');
    context.showAccountForm();
    assert.equal(el('acct-channel-options').open, false);
    el('acct-form-name').value = 'new-account'; el('acct-form-token').value = 'synthetic-account';
    await context.saveAccountForm();
    const added = calls.find(call => call.url === '/api/accounts/config' && call.method === 'POST');
    assert.deepEqual(added.body.accounts[0].channels, []);
    context.showBulkImport();
    assert.equal(el('bulk-channel-options').open, false);
    el('bulk-tokens').value = 'synthetic-one\nsynthetic-two';
    await context.submitBulkImport();
    assert.equal(calls.find(call => call.url === '/api/accounts/bulk').body.channels, '');
    vm.runInContext('accountConfigList = [{name: "broken", channel_setup: {status: "failed", reason: "Invalid <token>"}}]', context);
    renderAccounts();
    assert.match(el('account-config-list').innerHTML, /Needs attention \(1\)/);
    assert.match(el('account-config-list').innerHTML, /CHANNEL SETUP FAILED/);
    assert.match(el('account-config-list').innerHTML, /Invalid &lt;token&gt;/);
    expire = true;
    await context.pollMonitor();
    assert.equal(context.location.href, '/login?expired=1');
    console.log('PASS: saved settings, dirty edits, token-save ordering, CSRF, Discord 401 recovery, session expiry, token-only add/import, template IDs, partial setup summaries, grouped account errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
