// Monitor setup uses server dropdowns; account channel IDs can stay empty.
let monitorConfig = {};
async function monitorRequest(path, method = 'GET', body) {
    const res = await fetch(path, {method, headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body)});
    const data = await res.json();
    if (!res.ok || data.success === false) throw new Error(data.error || data.message || 'Request failed');
    return data;
}
async function monitorAction(fn) {
    try { await fn(); } catch (e) { showToast(e.message, 'error'); }
}
window.saveMonitor = () => monitorAction(async () => {
    monitorConfig = await monitorRequest('/api/monitor', 'POST', {
        token: document.getElementById('monitor-token').value,
        enabled: document.getElementById('monitor-enabled').checked,
        withdraw_percent: Number(document.getElementById('monitor-percent').value)
    });
    document.getElementById('monitor-token').value = '';
    showToast('Monitor settings saved', 'success');
    await pollMonitor();
});
window.loadMonitorServers = () => monitorAction(async () => {
    const data = await monitorRequest('/api/monitor/servers');
    const select = document.getElementById('monitor-server');
    select.replaceChildren(new Option('Choose a server', ''));
    for (const server of data.guilds) select.add(new Option(server.name, server.id));
    if (monitorConfig.guild_id) select.value = monitorConfig.guild_id;
    const invite = document.getElementById('monitor-invite');
    invite.href = data.invite_url;
    invite.hidden = false;
    showToast(data.guilds.length ? 'Servers loaded' : 'Invite the monitor bot, then load servers again', 'info');
});
window.setupMonitorChannels = () => monitorAction(async () => {
    const guild = document.getElementById('monitor-server').value;
    if (!guild) throw new Error('Choose a server first');
    await monitorRequest('/api/monitor/server', 'POST', {guild_id: guild});
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
async function pollMonitor() {
    const panel = document.getElementById('monitor-progress');
    if (!panel) return;
    try {
        monitorConfig = await monitorRequest('/api/monitor');
        const op = monitorConfig.operation || {};
        panel.textContent = `${monitorConfig.runtime || 'stopped'} · ${monitorConfig.guild_name || 'No server selected'}${op.kind ? ` · ${op.kind}: ${op.status}` : ''}${op.stage ? ` · ${op.stage} ${op.done || 0}/${op.total || 0}` : ''}${op.error ? ` · ${op.error}` : ''}`;
        document.getElementById('monitor-token').placeholder = monitorConfig.token_set ? 'Token saved; leave empty to keep it' : 'Paste the official monitor bot token';
        const results = document.getElementById('monitor-results');
        results.replaceChildren();
        for (const row of op.results || []) {
            const line = document.createElement('p');
            line.textContent = `${row.name}: ${row.status} · ${(row.confirmed_total || 0).toLocaleString()} confirmed${row.reason ? ` · ${row.reason}` : ''}`;
            results.append(line);
        }
        const {summary: s} = await monitorRequest('/api/monitor/summary');
        document.getElementById('monitor-summary').textContent = `${s.configured} accounts · ${s.ready} ready · ${s.total_owo.toLocaleString()} OwO cached · ${s.withdrawable.toLocaleString()} estimated withdrawable · ${s.unknown_limits} unknown limits`;
    } catch (e) { panel.textContent = e.message; }
}
document.addEventListener('DOMContentLoaded', async () => {
    await pollMonitor();
    document.getElementById('monitor-percent').value = monitorConfig.withdraw_percent ?? 100;
    document.getElementById('monitor-enabled').checked = !!monitorConfig.enabled;
    async function next() {
        if (!document.hidden) await pollMonitor();
        setTimeout(next, 10000);
    }
    setTimeout(next, 10000);
});

window.reconcileWithdrawals = () => monitorAction(async () => {
    await monitorRequest('/api/monitor/reconcile', 'POST', {});
    showToast('Checking original receipts; no transfers will be resent', 'info');
    await pollMonitor();
});
