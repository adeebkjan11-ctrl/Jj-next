// Monitor configuration is separate from account credentials and dashboard login.
let monitorConfig = {};
let monitorBusy = false;
let monitorPoll = null;
let monitorActionError = '';
const monitorDirty = new Set();

async function monitorRequest(path, method = 'GET', body) {
    const res = await fetch(path, {method, credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body)});
    if (res.status === 401) throw new Error('Dashboard session expired. Sign in again.');
    let data;
    try { data = await res.json(); }
    catch (_) { throw new Error(`Monitor request failed (HTTP ${res.status}). Check the server and try again.`); }
    if (!res.ok || data.success === false) throw new Error(data.error || data.message || `Request failed (HTTP ${res.status})`);
    return data;
}

function renderMonitorConfig() {
    const cfg = monitorConfig;
    const op = cfg.operation || {};
    const panel = document.getElementById('monitor-progress');
    const runtime = cfg.runtime || 'stopped';
    const runtimeError = !['stopped', 'connecting', 'connected'].includes(runtime);
    const error = monitorActionError || op.error || (runtimeError ? runtime : '');
    panel.dataset.state = error ? 'error' : 'normal';
    panel.textContent = error || `${runtime} · ${cfg.guild_name || 'No server selected'}${op.kind ? ` · ${op.kind}: ${op.status}` : ''}${op.stage ? ` · ${op.stage} ${op.done || 0}/${op.total || 0}` : ''}`;
    const badge = document.getElementById('monitor-status');
    badge.dataset.state = error ? 'error' : 'normal';
    badge.textContent = error ? 'Needs attention' : runtime === 'connected' ? 'Connected' :
        !cfg.token_set ? 'Not configured' : !cfg.channel_id ? 'Setup needed' : runtime === 'connecting' ? 'Connecting' : 'Stopped';
    document.getElementById('monitor-token').placeholder = cfg.token_set ? 'Token saved; leave empty to keep it' : 'Paste your monitor bot token';
    if (!monitorDirty.has('monitor-percent')) document.getElementById('monitor-percent').value = cfg.withdraw_percent ?? 100;
    if (!monitorDirty.has('monitor-enabled')) document.getElementById('monitor-enabled').checked = !!cfg.enabled;
    const select = document.getElementById('monitor-server');
    // Restore the selected server after a reload without requiring another lookup.
    if (cfg.guild_id && !Array.from(select.options).some(option => option.value === cfg.guild_id)) {
        select.add(new Option(cfg.guild_name || 'Saved server', cfg.guild_id));
        if (!select.value) select.value = cfg.guild_id;
    }
    const invite = document.getElementById('monitor-invite');
    if (cfg.bot_id) {
        invite.href = `https://discord.com/oauth2/authorize?client_id=${encodeURIComponent(cfg.bot_id)}&scope=bot&permissions=268528656`;
        invite.hidden = false;
    }
    const results = document.getElementById('monitor-results');
    results.replaceChildren();
    for (const row of op.results || []) {
        const line = document.createElement('p');
        line.textContent = `${row.name}: ${row.status} · ${(row.confirmed_total || 0).toLocaleString()} confirmed${row.reason ? ` · ${row.reason}` : ''}`;
        results.append(line);
    }
}

function renderMonitorSummary(s) {
    const metrics = [
        ['Accounts', s.configured, `${s.ready} ready · ${s.connected} connected`],
        ['Total OwO', s.total_owo, 'Cached account balances'],
        ['Withdrawable', s.withdrawable, `Estimate · ${s.unknown_limits} unknown limits`],
        ['Top earner · 24h', s.best ? s.best.name : 'Waiting for data', s.best ? `${s.best.net_24h.toLocaleString()} OwO net change` : 'Needs balance history']
    ];
    const summary = document.getElementById('monitor-summary');
    summary.replaceChildren();
    for (const [label, value, note] of metrics) {
        const card = document.createElement('div');
        card.className = 'monitor-metric';
        for (const [kind, text] of [['label', label], ['value', value ?? '—'], ['note', note]]) {
            const span = document.createElement('span');
            span.className = `monitor-metric-${kind}`;
            span.textContent = typeof text === 'number' ? text.toLocaleString() : text;
            card.append(span);
        }
        summary.append(card);
    }
}

async function monitorAction(fn) {
    if (monitorBusy) return;
    monitorBusy = true;
    monitorActionError = '';
    const controls = document.querySelectorAll('#monitor button, #monitor input, #monitor select');
    const disabled = Array.from(controls, control => control.disabled);
    controls.forEach(control => { control.disabled = true; });
    try {
        // Let older reads finish before changing settings.
        if (monitorPoll) await monitorPoll;
        await fn();
    } catch (e) {
        monitorActionError = e.message;
        renderMonitorConfig();
        showToast(e.message, 'error');
    } finally {
        controls.forEach((control, index) => { control.disabled = disabled[index]; });
        monitorBusy = false;
    }
}

async function saveMonitorSettings() {
    const percent = Number(document.getElementById('monitor-percent').value);
    if (!Number.isFinite(percent) || percent < 1 || percent > 100) throw new Error('Withdrawal percentage must be 1–100');
    monitorConfig = await monitorRequest('/api/monitor', 'POST', {
        token: document.getElementById('monitor-token').value,
        enabled: document.getElementById('monitor-enabled').checked,
        withdraw_percent: percent
    });
    document.getElementById('monitor-token').value = '';
    monitorDirty.clear();
    renderMonitorConfig();
}

window.saveMonitor = () => monitorAction(async () => {
    await saveMonitorSettings();
    showToast('Monitor settings saved', 'success');
    await pollMonitor();
});
window.loadMonitorServers = () => monitorAction(async () => {
    // A newly pasted token must be saved and validated before discovery.
    if (document.getElementById('monitor-token').value.trim()) await saveMonitorSettings();
    const data = await monitorRequest('/api/monitor/servers');
    const select = document.getElementById('monitor-server');
    const selected = select.value || monitorConfig.guild_id;
    select.replaceChildren(new Option(data.guilds.length ? 'Choose a server' : 'Invite your bot, then load again', ''));
    for (const server of data.guilds) select.add(new Option(server.name, server.id));
    if (selected) select.value = selected;
    const invite = document.getElementById('monitor-invite');
    invite.href = data.invite_url;
    invite.hidden = false;
    showToast(data.guilds.length ? 'Servers loaded' : 'Invite the monitor bot, then load servers again', 'info');
    renderMonitorConfig();
});
window.setupMonitorChannels = () => monitorAction(async () => {
    const guild = document.getElementById('monitor-server').value;
    if (!guild) throw new Error('Choose a server first');
    if (document.getElementById('monitor-token').value.trim()) await saveMonitorSettings();
    monitorConfig = await monitorRequest('/api/monitor/server', 'POST', {guild_id: guild});
    await monitorRequest('/api/monitor/provision', 'POST', {});
    showToast('Channel setup queued; progress appears below', 'info');
    await pollMonitor();
});
window.withdrawMonitor = () => monitorAction(async () => {
    await monitorRequest('/api/monitor/withdraw', 'POST', {});
    showToast('Withdrawal queued. Only confirmed receipts count as success.', 'info');
    await pollMonitor();
});
window.retryAllCaptchas = () => monitorAction(async () => {
    const data = await monitorRequest('/api/captcha/retry_all', 'POST', {});
    showToast(`${data.queued} pending accounts queued for a solver attempt`, 'info');
});
window.reconcileWithdrawals = () => monitorAction(async () => {
    await monitorRequest('/api/monitor/reconcile', 'POST', {});
    showToast('Checking original receipts; no transfers will be resent', 'info');
    await pollMonitor();
});

function pollMonitor() {
    if (monitorPoll) return monitorPoll;
    if (!document.getElementById('monitor-progress')) return Promise.resolve();
    monitorPoll = (async () => {
        try {
            monitorConfig = await monitorRequest('/api/monitor');
            renderMonitorConfig();
            const {summary} = await monitorRequest('/api/monitor/summary');
            renderMonitorSummary(summary);
        } catch (e) {
            const panel = document.getElementById('monitor-progress');
            panel.textContent = e.message;
            panel.dataset.state = 'error';
        }
    })().finally(() => { monitorPoll = null; });
    return monitorPoll;
}
document.addEventListener('DOMContentLoaded', async () => {
    for (const id of ['monitor-percent', 'monitor-enabled']) {
        document.getElementById(id).addEventListener('input', () => monitorDirty.add(id));
    }
    await pollMonitor();
    async function next() {
        if (!document.hidden && !monitorBusy && document.getElementById('monitor').classList.contains('active-view')) await pollMonitor();
        setTimeout(next, 10000);
    }
    setTimeout(next, 10000);
});
