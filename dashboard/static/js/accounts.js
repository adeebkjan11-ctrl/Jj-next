/* 

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



*/

window.fetchAccounts = async function() {
    console.log("Fetching accounts...");
    try {
        const res = await fetch('/api/accounts/list');
        // 401 = session expired. The server returns an error object, not a
        // list, so without this guard data.find() throws "find is not a
        // function" and accountsList becomes a non-array, which blanks the
        // panel ("everything disappears"). Send the user back to login.
        if (res.status === 401) {
            console.warn("Session expired - reloading to login");
            window.location.href = '/login';
            return;
        }
        const data = await res.json();
        const list = Array.isArray(data) ? data : [];
        console.log(`Fetched ${list.length} accounts`);
        accountsList = list;
        if (list.length > 0) {
            if (!currentAccountId || !list.find(a => a.id === currentAccountId)) {
                currentAccountId = list[0].id;
            }
        }
        renderAccountGrid();
        updateGlobalAccountName(); 
    } catch (e) {
        console.error("Failed to fetch accounts", e);
        const grid = document.getElementById('accounts-grid');
        // keep the last good list instead of clobbering it with an error that
        // makes the cards vanish - a transient network blip should not wipe the
        // panel
        if (grid && (!accountsList || !accountsList.length)) {
            grid.innerHTML = `<div class="no-data error">Error fetching accounts: ${e.message}</div>`;
        }
    }
};

function updateGlobalAccountName() {
    const nameEl = document.getElementById('currentAccountName');
    if (!nameEl) return;
    if (currentAccountId === '__combined__') {
        nameEl.innerText = `ALL ACCOUNTS (${accountsList.length})`;
        return;
    }
    if (currentAccountId && Array.isArray(accountsList)) {
        const acc = accountsList.find(a => a.id === currentAccountId);
        if (acc) {
            nameEl.innerText = `ACCOUNT: ${acc.username}`;
            return;
        }
    }
    nameEl.innerText = 'Loading account...';
}

window.selectAccount = function(id) {
    console.log(`Selecting account: ${id}`);
    currentAccountId = id;
    renderAccountGrid();
    if (id === '__combined__') {
        showToast('Switched to combined view (All Accounts)', 'success');
        updateGlobalAccountName();
    } else {
        const acc = Array.isArray(accountsList) ? accountsList.find(a => a.id === id) : null;
        if (acc) {
            showToast(`Switched to account: ${acc.username}`, 'success');
            updateGlobalAccountName(); 
        }
    }
    if (lineChart) lineChart.data.datasets[0].data = Array(30).fill(0);
    const configView = document.getElementById('config');
    if (configView && configView.classList.contains('active-view')) loadConfig();
    if (typeof loadCustomCommands === 'function') loadCustomCommands();
    update();
    const dashNav = document.querySelector('.nav-item[onclick*="dash"]');
    if (dashNav) nav('dash', dashNav);
};

function renderAccountGrid() {
    const grid = document.getElementById('accounts-grid');
    if (!grid) return;
    if (!Array.isArray(accountsList) || !accountsList.length) {
        grid.innerHTML = '<div class="no-data">No accounts online. Start the bot to see connected accounts here.</div>';
        return;
    }
    // prepend an ALL COMBINED card when there are multiple accounts
    let combinedCard = '';
    if (accountsList.length > 1) {
        const isCombined = currentAccountId === '__combined__';
        const totalCash = accountsList.reduce((s, a) => s + (a.cash || 0), 0);
        const totalCmds = accountsList.reduce((s, a) => s + (a.session_total || 0), 0);
        const totalGems = accountsList.reduce((s, a) => s + (a.gems_used || 0), 0);
        const onlineCount = accountsList.filter(a => !a.paused && !a.connecting).length;
        combinedCard = `
            <div class="account-picker-card ${isCombined ? 'selected' : ''}" onclick="selectAccount('__combined__')" role="button" tabindex="0" style="border-color: var(--primary);">
                <div class="account-card-top">
                    <span class="icon-svg account-avatar-lg account-avatar-fallback" style="--icon: url('/static/assets/neura_icons/layers.svg');"></span>
                    <div class="account-card-meta">
                        <div class="account-card-name">ALL COMBINED</div>
                        <div class="account-card-id">${accountsList.length} accounts</div>
                        <div class="account-card-status running">${onlineCount} online</div>
                    </div>
                    ${isCombined ? '<span class="account-selected-badge">Selected</span>' : ''}
                </div>
                <div class="account-card-stats">
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/coins.svg');"></span>
                        <div class="account-stat-val">${totalCash.toLocaleString()}</div>
                        <div class="account-stat-lbl">Total Balance</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/chart-column.svg');"></span>
                        <div class="account-stat-val">${accountsList.length}</div>
                        <div class="account-stat-lbl">Accounts</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/terminal.svg');"></span>
                        <div class="account-stat-val">${totalCmds}</div>
                        <div class="account-stat-lbl">Session Cmds</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/bolt.svg');"></span>
                        <div class="account-stat-val">${totalGems}</div>
                        <div class="account-stat-lbl">Gems Used</div>
                    </div>
                </div>
            </div>`;
    }
    grid.innerHTML = combinedCard + accountsList.map(acc => {
        const isSelected = acc.id === currentAccountId;
        const statusClass = acc.connecting ? 'connecting' : (acc.paused ? 'paused' : 'running');
        const statusLabel = acc.connecting ? 'Connecting' : (acc.paused ? 'Paused' : 'Running');
        const avatar = acc.avatar
            ? `<img src="${escAttr(acc.avatar)}" class="account-avatar-lg" alt="">`
            : `<span class="icon-svg account-avatar-lg account-avatar-fallback" style="--icon: url('/static/assets/neura_icons/discord.svg');"></span>`;
        return `
            <div class="account-picker-card ${isSelected ? 'selected' : ''}" onclick="selectAccount('${jsArg(acc.id)}')" role="button" tabindex="0">
                <div class="account-card-top">
                    ${avatar}
                    <div class="account-card-meta">
                        <div class="account-card-name">${escHtml(acc.username)}</div>
                        <div class="account-card-id">User ID · ${escHtml(acc.id)}</div>
                        <div class="account-card-status ${statusClass}">${statusLabel}</div>
                    </div>
                    ${isSelected ? '<span class="account-selected-badge">Selected</span>' : ''}
                </div>
                <div class="account-card-stats">
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/coins.svg');"></span>
                        <div class="account-stat-val">${(acc.cash || 0).toLocaleString()}</div>
                        <div class="account-stat-lbl">Balance</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/chart-column.svg');"></span>
                        <div class="account-stat-val">${acc.level ?? '—'}</div>
                        <div class="account-stat-lbl">OwO Level</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/terminal.svg');"></span>
                        <div class="account-stat-val">${acc.session_total || 0}</div>
                        <div class="account-stat-lbl">Session Cmds</div>
                    </div>
                    <div class="account-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/bolt.svg');"></span>
                        <div class="account-stat-val">${acc.gems_used || 0}</div>
                        <div class="account-stat-lbl">Gems Used</div>
                    </div>
                </div>
                ${renderXpBar(acc.xp, acc.xp_needed)}
            </div>
        `;
    }).join('');
}

// owo replies with level AND xp in one message ("is level 42 [1,234/5,000 xp]")
function renderXpBar(xp, needed) {
    if (xp === null || xp === undefined) return '';
    const total = needed || 0;
    const pct = total > 0 ? Math.min(100, Math.round((xp / total) * 100)) : 0;
    const label = total > 0
        ? `${xp.toLocaleString()} / ${total.toLocaleString()} XP · ${pct}%`
        : `${xp.toLocaleString()} XP`;
    return `
        <div class="account-xp">
            <div class="account-xp-track"><div class="account-xp-fill" style="width:${pct}%"></div></div>
            <div class="account-xp-label">${label}</div>
        </div>
    `;
}

window.fetchAccountConfig = async function() {
    try {
        const res = await fetch('/api/accounts/config');
        if (res.status === 401) {
            // session expired - the body is an error object, not {accounts:[]}.
            // Bail to login instead of setting accountConfigList to an object,
            // which would break renderAccountConfigList's .filter/.map calls.
            window.location.href = '/login';
            return;
        }
        const data = await res.json();
        const list = (data && Array.isArray(data.accounts)) ? data.accounts : [];
        accountConfigList = list;
        renderAccountConfigList();
    } catch (e) {
        console.error('Failed to fetch account config', e);
        // keep the last known config on a transient error so the cards do not
        // blank out
    }
};

const ACCOUNT_STATUS_LABELS = {
    invalid_token: 'TOKEN DEAD',
    needs_verification: 'NEEDS VERIFICATION',
    cannot_send: "CAN'T SEND",
    duplicate: 'DUPLICATE',
};

function accountConfigCard(acc) {
    const i = accountConfigList.indexOf(acc);
    const token = acc.token_masked || '••••••';
    const proxy = acc.proxy_id ? `Proxy: ${acc.proxy_id}` : 'Direct';
    const status = acc.enabled !== false ? 'Enabled' : 'Disabled';
    const channels = (acc.channels || []).join(' ') || 'no channel id';
    const name = acc.name || 'Unnamed';
    const health = acc.status || 'ok';
    const runState = acc.running
        ? (acc.ready
            ? '<span class="acct-state running">RUNNING</span>'
            : '<span class="acct-state connecting">CONNECTING</span>')
        : '<span class="acct-state stopped">STOPPED</span>';
    const healthState = health === 'ok' ? '' :
        `<span class="acct-state problem">${escHtml(ACCOUNT_STATUS_LABELS[health] || String(health).toUpperCase())}</span>`;
    const reason = health === 'ok' || !acc.status_reason ? '' :
        `<span class="dim">${escHtml(acc.status_reason)}</span>`;
    const runBtn = acc.running
        ? `<button class="btn-proxy-sm danger" onclick="stopAccount('${jsArg(name)}')">Stop</button>`
        : `<button class="btn-proxy-sm" onclick="launchAccount('${jsArg(name)}')">Start</button>`;
    // Name, token and channels are what the live bot was built from, so the
    // server refuses to change them under a running account (409). Grey the
    // buttons out rather than letting the form collect an edit it will reject.
    const lockedTitle = 'Stop this account first — its name, token and channel IDs are locked while it runs';
    const editBtns = acc.running
        ? `<button class="btn-proxy-sm" disabled title="${lockedTitle}">Edit</button>
           <button class="btn-proxy-sm danger" disabled title="${lockedTitle}">Del</button>`
        : `<button class="btn-proxy-sm" onclick="editAccountConfig(${i})">Edit</button>
           <button class="btn-proxy-sm danger" onclick="deleteAccountConfig(${i})">Del</button>`;
    return `
        <div class="account-config-card">
            <div class="account-config-info">
                <strong>${escHtml(name)} ${runState}${healthState}</strong>
                <span class="mono">${escHtml(token)}</span>
                <span class="dim">${escHtml(proxy)} · ${status} · Channels: ${escHtml(channels)}</span>
                ${reason}
            </div>
            <div class="account-config-actions">
                ${runBtn}
                <button class="btn-proxy-sm" onclick="verifyAccounts(['${jsArg(name)}'])">Verify</button>
                ${editBtns}
            </div>
        </div>
    `;
}

function renderAccountConfigList() {
    const el = document.getElementById('account-config-list');
    if (!el) return;
    if (!Array.isArray(accountConfigList) || !accountConfigList.length) {
        el.innerHTML = '<div class="no-data">No accounts configured. Click Add Account.</div>';
        return;
    }
    const problem = accountConfigList.filter(a => (a.status || 'ok') !== 'ok');
    const healthy = accountConfigList.filter(a => (a.status || 'ok') === 'ok');

    let html = healthy.map(accountConfigCard).join('');
    if (problem.length) {
        html += `
            <div class="account-problem-header">
                <h3 class="section-subtitle">Needs attention (${problem.length})</h3>
                <button class="btn-proxy-sm" onclick="downloadAccounts('problem')">Download these tokens</button>
            </div>
            ${problem.map(accountConfigCard).join('')}
        `;
    }
    el.innerHTML = html;
}

window.downloadAccounts = function(only) {
    window.location = only === 'problem' ? '/api/accounts/export?only=problem' : '/api/accounts/export';
};

async function accountAction(url, body, successMsg) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        if (res.status === 401) {
            window.location.href = '/login';
            return { success: false };
        }
        const data = await res.json();
        showToast(data.message || data.error || successMsg, data.success ? 'success' : 'error');
        // Give the backend a beat to settle (start/stop touches the asyncio
        // loop), then refresh both views so the cards reflect the real state
        // instead of the stale snapshot from the instant reply.
        setTimeout(() => { fetchAccountConfig(); window.fetchAccounts(); }, 600);
        return data;
    } catch (e) {
        showToast('Request failed', 'error');
        return { success: false };
    }
}

window.launchAccount = function(name) {
    return accountAction('/api/accounts/launch', { name: decodeURIComponent(name) }, 'Starting account');
};

window.stopAccount = function(name) {
    return accountAction('/api/accounts/stop', { name: decodeURIComponent(name) }, 'Stopping account');
};

// START ALL queues the farm; it does not fan out. The server brings accounts up
// one at a time with a gap between them, so this call returns immediately and the
// progress line below is how you watch it. Pressing it again while a queue is
// live is refused by the server (409), not queued behind the first.
window.startAllPolling = null;

window.launchAllAccounts = async function() {
    const btn = document.getElementById('btn-start-all');
    if (btn) btn.disabled = true;
    try {
        const res = await fetch('/api/accounts/launch_all', { method: 'POST' });
        const data = await res.json();
        showToast(data.message || data.error || 'Start-all failed',
                  data.success ? 'info' : 'error');
    } catch (e) {
        showToast('Request failed', 'error');
    }
    pollStartAll();
};

window.cancelStartAll = async function() {
    try {
        await fetch('/api/accounts/launch_all/cancel', { method: 'POST' });
        showToast('Queue cancelled — accounts already started keep running', 'info');
    } catch (e) {
        showToast('Request failed', 'error');
    }
    pollStartAll();
};

window.stopAllAccounts = async function() {
    if (!confirm('Stop every running account in this space?')) return;
    const btn = document.getElementById('btn-stop-all');
    if (btn) btn.disabled = true;
    await accountAction('/api/accounts/stop_all', {}, 'Stopping all accounts');
    if (btn) btn.disabled = false;
    pollStartAll();
};

function renderStartAll(p) {
    const box = document.getElementById('start-all-progress');
    const startBtn = document.getElementById('btn-start-all');
    if (!box) return false;

    if (!p || !p.active) {
        box.style.display = 'none';
        box.innerHTML = '';
        if (startBtn) startBtn.disabled = false;
        return false;
    }

    if (startBtn) startBtn.disabled = true;
    const pct = p.total ? Math.round((p.done / p.total) * 100) : 0;
    const now = p.current ? `starting <strong>${escHtml(p.current)}</strong>` : 'waiting';
    box.style.display = '';
    box.innerHTML = `
        <div class="start-all-row">
            <span>Starting accounts one at a time — ${p.done}/${p.total} done, ${now}
                  (one every ${Math.round(p.gap)}s)</span>
            <button class="btn-proxy-sm danger" onclick="cancelStartAll()">Cancel queue</button>
        </div>
        <div class="start-all-bar"><span style="width:${pct}%"></span></div>`;
    return true;
}

// Only polls while a queue is live, and stops itself the moment it is not - the
// accounts page already refreshes on its own timer and does not need a second one
// running forever.
window.pollStartAll = async function() {
    if (window.startAllPolling) {
        clearTimeout(window.startAllPolling);
        window.startAllPolling = null;
    }
    let active = false;
    try {
        const res = await fetch('/api/accounts/launch_all');
        const data = await res.json();
        active = renderStartAll(data.progress);
    } catch (e) {
        renderStartAll(null);
    }
    if (active) {
        window.startAllPolling = setTimeout(window.pollStartAll, 2000);
    }
};

window.verifyAccounts = async function(names) {
    const payload = names ? { names: names.map(decodeURIComponent) } : {};
    showToast(names ? 'Verifying account…' : 'Verifying all enabled accounts…', 'info');
    const data = await accountAction('/api/accounts/verify', payload, 'Verification finished');
    if (!data.results) return;
    const lines = data.results.map(r => r.valid ? `✓ ${r.name}: ${r.user}` : `✗ ${r.name}: ${r.user}`);
    showToast(lines.join(' | '), data.results.every(r => r.valid) ? 'success' : 'error');
};

window.showBulkImport = function() {
    const modal = document.getElementById('account-bulk-modal');
    if (!modal) return;
    document.getElementById('bulk-tokens').value = '';
    document.getElementById('bulk-channels').value = '';
    document.getElementById('bulk-prefix').value = 'acc';
    modal.classList.add('visible');
};

window.hideBulkImport = function() {
    const modal = document.getElementById('account-bulk-modal');
    if (modal) modal.classList.remove('visible');
};

window.submitBulkImport = async function() {
    const tokens = document.getElementById('bulk-tokens').value;
    const channels = document.getElementById('bulk-channels').value;
    const prefix = document.getElementById('bulk-prefix').value;
    if (!tokens.trim()) {
        showToast('Paste at least one token', 'error');
        return;
    }
    const data = await accountAction('/api/accounts/bulk', { tokens, channels, prefix }, 'Imported accounts');
    if (data.success) hideBulkImport();
};

window.showAccountForm = function(index = -1) {
    const modal = document.getElementById('account-form-modal');
    const title = document.getElementById('acct-form-title');
    document.getElementById('acct-form-index').value = index;
    if (index >= 0 && accountConfigList[index]) {
        const acc = accountConfigList[index];
        title.textContent = 'Edit Account';
        document.getElementById('acct-form-name').value = acc.name || '';
        document.getElementById('acct-form-token').value = '';
        document.getElementById('acct-form-token').placeholder = acc.token_masked ? 'Leave blank to keep current token' : 'Discord user token';
        document.getElementById('acct-form-channels').value = (acc.channels || []).join(' ');
        document.getElementById('acct-form-enabled').checked = acc.enabled !== false;
        if (typeof populateAccountProxyDropdown === 'function') populateAccountProxyDropdown();
        document.getElementById('acct-form-proxy').value = acc.proxy_id || '';
    } else {
        title.textContent = 'Add Account';
        document.getElementById('acct-form-name').value = '';
        document.getElementById('acct-form-token').value = '';
        document.getElementById('acct-form-token').placeholder = 'Discord user token';
        document.getElementById('acct-form-channels').value = '';
        document.getElementById('acct-form-enabled').checked = true;
        if (typeof populateAccountProxyDropdown === 'function') populateAccountProxyDropdown();
        document.getElementById('acct-form-proxy').value = '';
    }
    if (modal) modal.classList.add('visible');
};

window.hideAccountForm = function() {
    const modal = document.getElementById('account-form-modal');
    if (modal) modal.classList.remove('visible');
};

window.editAccountConfig = function(index) {
    const acc = accountConfigList[index];
    if (acc && acc.running) {
        showToast('Stop this account before editing it', 'error');
        return;
    }
    showAccountForm(index);
};

window.deleteAccountConfig = async function(index) {
    const acc = accountConfigList[index];
    if (acc && acc.running) {
        showToast('Stop this account before removing it', 'error');
        return;
    }
    if (!confirm('Remove this account from config?')) return;
    accountConfigList.splice(index, 1);
    await saveAccountConfigList();
};

window.saveAccountForm = async function() {
    const index = parseInt(document.getElementById('acct-form-index').value, 10);
    const name = document.getElementById('acct-form-name').value.trim();
    const token = document.getElementById('acct-form-token').value.trim();
    const channels = document.getElementById('acct-form-channels').value.trim().split(/\s+/).filter(Boolean);
    const proxy_id = document.getElementById('acct-form-proxy').value || null;
    const enabled = document.getElementById('acct-form-enabled').checked;
    if (!name) {
        showToast('Account name is required', 'error');
        return;
    }
    const duplicate = accountConfigList.some((acc, i) => i !== index && (acc.name || '') === name);
    if (duplicate) {
        showToast('Another account already uses that name', 'error');
        return;
    }
    if (index >= 0 && accountConfigList[index]) {
        // Keep every field the server tracks (health status, user_id) and only
        // overwrite what this form owns. The real token never reaches the browser,
        // so a blank token box means "keep the stored one"; orig_name lets the
        // server still find that row after a rename.
        const prev = accountConfigList[index];
        const entry = Object.assign({}, prev, { name, channels, enabled, proxy_id });
        entry.orig_name = prev.name || name;
        if (token) entry.token = token; else delete entry.token;
        accountConfigList[index] = entry;
    } else {
        if (!token) {
            showToast('Token is required', 'error');
            return;
        }
        accountConfigList.push({ name, channels, enabled, proxy_id, token });
    }
    await saveAccountConfigList();
    hideAccountForm();
};

async function saveAccountConfigList() {
    // running/ready/token_masked are rendering aids the GET adds, not settings -
    // don't write them back into accounts.json
    const payload = accountConfigList.map(acc => {
        const clean = Object.assign({}, acc);
        delete clean.token_masked;
        delete clean.running;
        delete clean.ready;
        return clean;
    });
    try {
        const res = await fetch('/api/accounts/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ accounts: payload }),
        });
        const data = await res.json();
        if (data.status === 'success') {
            showToast('Accounts saved', 'success');
            await fetchAccountConfig();
        } else {
            showToast(data.message || 'Save failed', 'error');
            // The list was already edited in place (a delete splices before it
            // saves), so a rejected save - "that account is running" - has to put
            // the server's version back or the card stays gone on screen while
            // the account is still farming.
            await fetchAccountConfig();
        }
    } catch (e) {
        showToast('Save failed', 'error');
        await fetchAccountConfig();
    }
}