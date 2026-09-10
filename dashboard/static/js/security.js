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

const CAPTCHA_SERVICES = {
    yescaptcha:  { label: 'YesCaptcha',   keyField: 'yescaptcha_api_key',  balanceUnit: 'pts',    color: '#f59e0b', hint: 'Paid service – requires ≥ 30 pts to auto-solve.' },
    nopecha:     { label: 'NopeCHA',      keyField: 'nopecha_api_key',     balanceUnit: 'credits',color: '#a855f7', hint: 'Free 100 credits daily. Discord-boost keys are extension-only – put those in the NopeCHA Extension section.' },
    anticaptcha: { label: 'Anti-Captcha', keyField: 'anticaptcha_api_key', balanceUnit: '$',      color: '#22c55e', hint: 'Paid service – supports hCaptcha Enterprise too.' },
    captchaly:   { label: 'Captchaly',    keyField: 'captchaly_api_key',   balanceUnit: '$',      color: '#3b82f6', hint: 'Paid service – strict 120s solve times.' },
};

let pendingCaptchas = {};
let pendingInterval = null;
let _manualSolvePopup = null;

async function testSecurity(btn) {
    const q = currentAccountId ? `?id=${currentAccountId}` : '';
    const original = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> TESTING...';
    btn.disabled = true;
    try {
        const res = await fetch(`/api/security/test${q}`, { method: 'POST' });
        const d = await res.json();
        if (d.status === 'success') {
            btn.style.borderColor = 'var(--success)';
            btn.innerHTML = '<i class="fa-solid fa-check"></i> SIGNALS SENT';
        } else {
            alert("Test failed: " + d.message);
            btn.innerHTML = original;
        }
    } catch (e) {
        alert("Request failed");
        btn.innerHTML = original;
    } finally {
        setTimeout(() => {
            btn.innerHTML = original;
            btn.disabled = false;
            btn.style.border = '';
        }, 3000);
    }
}

// One request for the whole grid.
//
// This used to loop over accountsList and `await fetch('/api/stats?id=' + acc.id)`
// per account - serially, from the 1s dashboard tick - to read three integers out
// of a payload that also carries the battle team, the whole zoo, the quest card and
// every scheduled command. At 200 accounts that is 200 requests a second against 16
// web workers: the queue never drains, and the browser cannot even fetch a
// stylesheet, which is the "website stops loading" symptom. /api/security/summary
// returns exactly what is drawn below, cached per space.
async function fetchSecuritySummary() {
    const view = document.getElementById('security');
    if (!view || !view.classList.contains('active-view')) return;
    const container = document.getElementById('security-accounts-grid');
    if (!container) return;
    let rows = [];
    try {
        const res = await fetch('/api/security/summary');
        const d = await res.json();
        if (!d || !d.success) return;
        rows = d.accounts || [];
    } catch (e) {
        return;
    }
    const html = rows.map(a => {
        const paused = a.status === 'PAUSED';
        const statusColor = paused ? 'var(--danger)' : 'var(--success)';
        return `
            <div class="sec-account-card ${paused ? 'alert-active' : ''} ${a.id === currentAccountId ? 'selected' : ''}">
                <div class="sec-acc-header">
                    <div class="sec-acc-info">
                        ${a.avatar ? `<img src="${escAttr(a.avatar)}" class="account-avatar-lg" alt="">` : '<span class="icon-svg account-avatar-lg account-avatar-fallback" style="--icon: url(\'/static/assets/neura_icons/discord.svg\');"></span>'}
                        <div class="sec-acc-text">
                            <div class="sec-acc-name">${escHtml(a.username)}</div>
                            <div class="sec-acc-id">User ID · ${escHtml(a.id)}</div>
                            <div class="sec-acc-status" style="color:${statusColor}">${escHtml(a.status)}</div>
                        </div>
                    </div>
                </div>
                <div class="sec-acc-stats">
                    <div class="sec-mini-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/check-to-slot.svg'); background-color: var(--success);"></span>
                        <div class="val">${escHtml(String(a.captchas ?? 0))}</div>
                        <div class="lbl">Solved</div>
                    </div>
                    <div class="sec-mini-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/user-slash.svg'); background-color: var(--danger);"></span>
                        <div class="val">${escHtml(String(a.bans ?? 0))}</div>
                        <div class="lbl">Bans</div>
                    </div>
                    <div class="sec-mini-stat">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/warning.svg'); background-color: var(--warning);"></span>
                        <div class="val">${escHtml(String(a.warnings ?? 0))}</div>
                        <div class="lbl">Warns</div>
                    </div>
                </div>
            </div>
        `;
    }).join('');
    container.innerHTML = html || '<div class="no-data">Initializing system details...</div>';
}

function renderCaptchaSolverWidget(cfg, basePath, parentEnabled) {
    const enabled    = cfg.enabled !== false;
    const service    = (cfg.service || 'yescaptcha').toLowerCase();
    const svcInfo    = CAPTCHA_SERVICES[service] || CAPTCHA_SERVICES.yescaptcha;
    const apiKey     = cfg[svcInfo.keyField] || '';
    // The key box has to follow the same rule as the Service dropdown below.
    // It used to stay editable while the auto-solver was off, and because the
    // dropdown was frozen at that point, a key for another service (say NopeCHA)
    // landed in yescaptcha_api_key - a key that looked saved but was never read,
    // so every captcha still came back to the operator to solve by hand.
    const live       = enabled && parentEnabled;
    const dis        = live ? '' : ' disabled';
    // Mirrors nopecha_extension.resolve_key / Security._nopecha_extension_key: with an
    // extension key configured the paid path is skipped entirely, so the three rows
    // below are dead weight. They stay editable - somebody may hold both - but they
    // stop presenting themselves as something that has to be filled in.
    const nope       = (cfg.browser_solver || {}).nopecha || {};
    const extOnly    = nope.enabled !== false
        && !!String(nope.key || cfg.nopecha_booster_key || '').trim();
    const optional   = extOnly ? ' csw-row-optional' : '';
    const optHint    = extOnly
        ? '<span class="csw-service-hint csw-optional-note">Not needed &mdash; the NopeCHA extension below is solving these.</span>'
        : '';
    const serviceOptions = Object.entries(CAPTCHA_SERVICES).map(([id, s]) => `
        <option value="${id}" ${id === service ? 'selected' : ''}>${s.label}</option>
    `).join('');
    return `
        <div class="cfg-row" data-path="${basePath}.enabled">
            <div class="cfg-row-label"><span class="cfg-label-text">Enable Auto-Solver</span></div>
            <div class="cfg-row-control">${renderNeuraToggle(basePath + '.enabled', enabled, parentEnabled, true)}</div>
        </div>
        <div class="cfg-row csw-service-row${optional}" data-path="${basePath}.service">
            <div class="cfg-row-label">
                <span class="cfg-label-text">Service${extOnly ? ' <span class="csw-optional-tag">optional</span>' : ''}</span>
                <span class="csw-service-hint">${svcInfo.hint}</span>
                ${optHint}
            </div>
            <div class="cfg-row-control">
                <div class="csw-dropdown-wrap">
                    <div class="csw-svc-dot" style="background:${svcInfo.color}"></div>
                    <select id="csw-service-select" class="csw-select" ${live ? '' : 'disabled'}
                        onchange="updateCaptchaService(this.value)">
                        ${serviceOptions}
                    </select>
                </div>
            </div>
        </div>
        <div class="cfg-row csw-key-row${optional}" data-path="${basePath}.${svcInfo.keyField}" id="csw-key-row">
            <div class="cfg-row-label">
                <span class="cfg-label-text">${svcInfo.label} API Key${extOnly ? ' <span class="csw-optional-tag">optional</span>' : ''}</span>
                ${live ? '' : '<span class="csw-service-hint">Turn on Enable Auto-Solver first, then pick your service.</span>'}
                ${live ? optHint : ''}
            </div>
            <div class="cfg-row-control">
                <div class="cfg-input-wrap">
                    <input type="password" id="csw-api-key-input" class="cfg-input" value="${apiKey}"${dis}
                        placeholder="${live ? `Paste your ${svcInfo.label} API key here…` : 'Enable the auto-solver to set a key'}"
                        onchange="updateDeepVal('${basePath}.${svcInfo.keyField}', this.value)">
                </div>
            </div>
        </div>
        <div class="cfg-row csw-balance-row${optional}">
            <div class="cfg-row-label"><span class="cfg-label-text">Live Balance</span></div>
            <div class="cfg-row-control">
                <div class="csw-balance-wrap">
                    <span id="csw-balance-badge" class="csw-balance-badge" onclick="fetchCaptchaBalance()">
                        <span class="csw-balance-dot"></span>
                        <span id="csw-balance-text">Click to check…</span>
                    </span>
                    <button class="cfg-stepper-btn csw-refresh-btn" onclick="fetchCaptchaBalance()" title="Refresh balance">
                        <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/sync.svg');"></span>
                    </button>
                </div>
            </div>
        </div>
        ${renderBrowserSolverRows(cfg.browser_solver || {}, basePath + '.browser_solver', parentEnabled, cfg, basePath)}
    `;
}

// This widget hand-renders security.captcha_solver, and renderCategoryFlat swallows every
// key of that section (see its `path === 'security.captcha_solver'` branch) - so a nested
// object added to the config is invisible here unless it is rendered explicitly. Without
// these rows the extension solver could only be turned off by editing settings.json.
//
// Deliberately gated on parentEnabled, not on `live`: the extension solver needs no service
// and no paid service, so it must stay usable while "Enable Auto-Solver" is off.
function renderBrowserSolverRows(bs, base, parentEnabled, solverCfg, solverBase) {
    const on  = bs.enabled !== false;
    const sub = parentEnabled && on;
    return `
        <div class="cfg-section cfg-section-nested ${on ? '' : 'cfg-section-disabled'}">
            <div class="cfg-section-head">NopeCHA Extension Solver</div>
            <div class="cfg-section-rows">
                <div class="cfg-row" data-search="nopecha extension browser hcaptcha"
                     data-path="${base}.enabled">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Enable Extension Solver</span>
                        <span class="csw-service-hint">Hosts NopeCHA's extension in a local Chromium and lets it answer the challenge. Needs the booster key below - without one this layer is skipped.</span>
                    </div>
                    <div class="cfg-row-control">${renderNeuraToggle(base + '.enabled', on, parentEnabled, true)}</div>
                </div>
                <div class="cfg-row" data-path="${base}.timeout_s">
                    <div class="cfg-row-label"><span class="cfg-label-text">Timeout</span></div>
                    <div class="cfg-row-control">${renderStepperInner(base + '.timeout_s', '', bs.timeout_s ?? 180, 's', sub)}</div>
                </div>
                ${renderNopechaRows(bs.nopecha || {}, base + '.nopecha', sub,
                                    (solverCfg || {}).nopecha_booster_key || '',
                                    (solverBase || 'security.captcha_solver') + '.nopecha_booster_key')}
            </div>
        </div>
    `;
}

// The NopeCHA *extension*, not the API. A key earned by boosting NopeCHA's Discord holds
// extension credits only - api.nopecha.com rejects it - so it gets its own field here
// rather than sharing the "NopeCHA API Key" box above, where the paid API path would keep
// trying to spend something it can never spend.
function renderNopechaRows(nope, base, parentEnabled, boosterKey, boosterPath) {
    const on  = nope.enabled !== false;
    const sub = parentEnabled && on;
    const dis = sub ? '' : ' disabled';
    return `
        <div class="cfg-section cfg-section-nested ${on ? '' : 'cfg-section-disabled'}"
             data-search="nopecha extension booster key boost discord credits">
            <div class="cfg-section-head">NopeCHA Extension (booster keys)</div>
            <div class="cfg-section-rows">
                <div class="cfg-row" data-path="${base}.enabled">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Use NopeCHA Extension</span>
                        <span class="csw-service-hint">Loads NopeCHA into the solver's Chrome so it answers the challenge in the page. This is the only way a Discord-boost key can be spent.</span>
                    </div>
                    <div class="cfg-row-control">${renderNeuraToggle(base + '.enabled', on, parentEnabled, true)}</div>
                </div>
                <div class="cfg-row" data-path="${boosterPath}">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Booster Key</span>
                        <span class="csw-service-hint">The key NopeCHA DMs you for boosting their server. A paid API key works here too.</span>
                    </div>
                    <div class="cfg-row-control">
                        <div class="cfg-input-wrap">
                            <input type="password" class="cfg-input" value="${escAttr(boosterKey)}"${dis}
                                placeholder="${sub ? 'Paste your NopeCHA booster key here…' : 'Turn the extension on first'}"
                                onchange="updateDeepVal('${boosterPath}', this.value)">
                        </div>
                    </div>
                </div>
                <div class="cfg-row" data-path="${base}.headless">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Headless</span>
                        <span class="csw-service-hint">Safe to leave on: the extension answers the challenge itself, so nobody needs to watch the window.</span>
                    </div>
                    <div class="cfg-row-control">${renderNeuraToggle(base + '.headless', nope.headless !== false, sub)}</div>
                </div>
                <div class="cfg-row" data-path="${base}.solve_wait_s">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Solve Wait</span>
                        <span class="csw-service-hint">How long the extension gets on an open challenge before a human is asked. Image grids take a while.</span>
                    </div>
                    <div class="cfg-row-control">${renderStepperInner(base + '.solve_wait_s', '', nope.solve_wait_s ?? 120, 's', sub)}</div>
                </div>
                <div class="cfg-row" data-path="${base}.auto_download">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Auto-Download Build</span>
                        <span class="csw-service-hint">Fetches the automation build from NopeCHA's GitHub releases. Off means you supply it yourself below.</span>
                    </div>
                    <div class="cfg-row-control">${renderNeuraToggle(base + '.auto_download', nope.auto_download !== false, sub)}</div>
                </div>
                <div class="cfg-row" data-path="${base}.extension_path">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Extension Folder</span>
                        <span class="csw-service-hint">Optional. Folder holding an unpacked build's manifest.json - it is copied, never edited in place.</span>
                    </div>
                    <div class="cfg-row-control">
                        <div class="cfg-input-wrap">
                            <input type="text" class="cfg-input" value="${escAttr(nope.extension_path || '')}"${dis}
                                placeholder="${sub ? 'Leave empty to download it automatically' : ''}"
                                onchange="updateDeepVal('${base}.extension_path', this.value)">
                        </div>
                    </div>
                </div>
                <div class="cfg-row">
                    <div class="cfg-row-label">
                        <span class="cfg-label-text">Extension Build</span>
                        <span class="csw-service-hint">Installs it now instead of during your next captcha. Save the key first.</span>
                    </div>
                    <div class="cfg-row-control">
                        <div class="csw-balance-wrap">
                            <span id="csw-nopecha-badge" class="csw-balance-badge" onclick="fetchCaptchaBalance()">
                                <span class="csw-balance-dot"></span>
                                <span id="csw-nopecha-text">Not checked</span>
                            </span>
                            <button class="btn-control" id="csw-nopecha-install"${dis}
                                onclick="installNopechaExtension(this)">Install / Update</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// Fills the "Extension Build" badge from either /api/captcha/balance (extension mode) or
// the install route's own reply, so both paths report the same thing.
function _renderNopechaStatus(info, err) {
    const badge = document.getElementById('csw-nopecha-badge');
    const text  = document.getElementById('csw-nopecha-text');
    if (!badge || !text) return;
    if (err) {
        text.textContent = err.length > 90 ? err.slice(0, 89) + '…' : err;
        badge.className = 'csw-balance-badge error';
        badge.setAttribute('title', err);
        return;
    }
    info = info || {};
    badge.removeAttribute('title');
    if (!info.installed) {
        text.textContent = 'Not installed yet';
        badge.className = 'csw-balance-badge';
        return;
    }
    const bits = [info.version ? `v${info.version}` : null, info.release || null].filter(Boolean);
    text.textContent = (info.keyed ? 'Ready' : 'Downloaded, no key written') +
                       (bits.length ? ` · ${bits.join(' · ')}` : '');
    badge.className = 'csw-balance-badge ' + (info.keyed ? 'ok' : '');
}

window.installNopechaExtension = async function(btn) {
    const original = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Installing…'; }
    _renderNopechaStatus(null, null);
    const text = document.getElementById('csw-nopecha-text');
    if (text) text.textContent = 'Downloading…';
    try {
        const res = await fetch('/api/captcha/nopecha/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: currentAccountId || null })
        });
        const d = await res.json();
        if (d.success) {
            _renderNopechaStatus(d.extension, null);
            showToast(d.message || 'NopeCHA extension ready', 'success');
        } else {
            _renderNopechaStatus(d.extension, d.error || 'install failed');
            showToast(d.error || 'Could not install the NopeCHA extension', 'error');
        }
    } catch (e) {
        _renderNopechaStatus(null, 'request failed');
        showToast('Install request failed', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = original || 'Install / Update'; }
    }
};

window.updateCaptchaService = function(newService) {
    const basePath = 'security.captcha_solver';
    setDeep(currentConfig, `${basePath}.service`.split('.'), newService);
    checkDirty();
    renderSettings(currentConfig);
};

window.fetchCaptchaBalance = async function() {
    const badge   = document.getElementById('csw-balance-badge');
    const balText = document.getElementById('csw-balance-text');
    if (!badge || !balText) return;
    balText.textContent = 'Checking…';
    badge.className = 'csw-balance-badge loading';
    try {
        const q = currentAccountId ? `?id=${currentAccountId}` : '';
        const selectedService = getDeep(currentConfig, 'security.captcha_solver.service'.split('.')) || 'yescaptcha';
        const svcInfo = CAPTCHA_SERVICES[selectedService] || CAPTCHA_SERVICES.yescaptcha;
        const currentKey = getDeep(currentConfig, `security.captcha_solver.${svcInfo.keyField}`.split('.')) || '';
        const res = await fetch(`/api/captcha/balance${q}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ service: selectedService, api_key: currentKey })
        });
        const d = await res.json();
        if (d.mode === 'extension') {
            // A booster key has no API balance to read; reporting "unreadable" over a
            // working extension setup is what made this look broken. Say what it is.
            _renderNopechaStatus(d.extension, d.has_key ? null : d.error);
            balText.textContent = d.has_key
                ? (d.extension_enabled ? 'Extension key – no API balance' : 'Extension key – extension is off')
                : 'No NopeCHA key set';
            badge.className = 'csw-balance-badge ' + (d.has_key && d.extension_enabled ? 'ok' : '');
            return;
        }
        if (d.error || d.balance === null || d.balance === undefined) {
            balText.textContent = d.message || d.error || 'Error – check API key';
            badge.className = 'csw-balance-badge error';
        } else {
            let balance = d.balance;
            let unit = svcInfo.balanceUnit;
            let display;
            if (typeof balance === 'number') {
                if (unit === '$') display = `$${balance.toFixed(2)}`;
                else display = `${Math.round(balance).toLocaleString()} ${unit}`;
            } else {
                display = String(balance);
            }
            balText.textContent = display;
            badge.className = 'csw-balance-badge ok';
        }
    } catch (e) {
        balText.textContent = 'Request failed';
        badge.className = 'csw-balance-badge error';
    }
};

async function updatePendingCaptchas() {
    try {
        const res = await fetch('/api/captcha/pending');
        const data = await res.json();
        const pending = data.pending || [];
        const newPending = {};
        pending.forEach(p => {
            newPending[p.account_id] = {
                accountId: p.account_id,
                accountName: p.account_name || p.account_id,
                createdAt: p.created_at,
                job: p.job || {}
            };
        });
        Object.keys(pendingCaptchas).forEach(id => {
            if (!newPending[id]) delete pendingCaptchas[id];
        });
        Object.keys(newPending).forEach(id => {
            pendingCaptchas[id] = newPending[id];
        });
        updateNotificationUI();
    } catch (e) {
        console.error('Failed to fetch pending captchas:', e);
    }
}

function updateNotificationUI() {
    const count = Object.keys(pendingCaptchas).length;
    const bell = document.getElementById('notification-bell');
    const badge = document.getElementById('notification-badge');
    if (bell) {
        if (count > 0) {
            bell.classList.add('has-alert');
            badge.textContent = count;
            badge.style.display = 'block';
        } else {
            bell.classList.remove('has-alert');
            badge.style.display = 'none';
        }
    }
    renderPendingDropdown();
    renderSecurityCards();
}

function renderPendingDropdown() {
    const dropdown = document.getElementById('notification-dropdown');
    if (!dropdown) return;
    const count = Object.keys(pendingCaptchas).length;
    if (count === 0) {
        dropdown.innerHTML = '<div class="no-data">No pending captchas</div>';
        return;
    }
    let html = '';
    const now = Date.now() / 1000;
    Object.values(pendingCaptchas).forEach(p => {
        const elapsed = now - p.createdAt;
        const remaining = Math.max(0, 600 - elapsed);
        const urgencyClass = getUrgencyClass(remaining);
        const timeStr = formatTime(remaining);
        html += `
            <div class="pending-item ${urgencyClass}">
                <span class="pending-name">${escHtml(p.accountName)}</span>
                <span class="pending-timer">${escHtml(p.job?.status || "pending")} · ${timeStr}</span>
                <button class="btn-proxy-sm solve-btn" onclick="triggerManualSolve('${jsArg(p.accountId)}')">Solve</button>
            </div>
        `;
    });
    dropdown.innerHTML = html;
}

function renderSecurityCards() {
    const container = document.getElementById('captcha-cards-container');
    if (!container) return;
    const count = Object.keys(pendingCaptchas).length;
    if (count === 0) {
        container.innerHTML = '<div class="no-data">No pending captchas</div>';
        return;
    }
    let html = '';
    const now = Date.now() / 1000;
    Object.values(pendingCaptchas).forEach(p => {
        const elapsed = now - p.createdAt;
        const remaining = Math.max(0, 600 - elapsed);
        const urgencyClass = getUrgencyClass(remaining);
        const timeStr = formatTime(remaining);
        html += `
            <div class="captcha-card ${urgencyClass}">
                <div class="captcha-card-header">
                    <span class="captcha-account">${escHtml(p.accountName)}</span>
                    <span class="captcha-timer">${escHtml(p.job?.status || "pending")} · attempt ${Number(p.job?.attempt || 0)} · ${timeStr}</span>
                </div>
                <div class="captcha-card-body">
                    <button class="btn-control gold" onclick="triggerManualSolve('${jsArg(p.accountId)}')">Solve</button>
                    <button class="btn-control" onclick="dismissCaptchaCard('${jsArg(p.accountId)}')">Dismiss</button>
                </div>
            </div>
        `;
    });
    container.innerHTML = html;
}

function dismissCaptchaCard(accountId) {
    if (pendingCaptchas[accountId]) {
        delete pendingCaptchas[accountId];
        updateNotificationUI();
    }
}

function getUrgencyClass(seconds) {
    if (seconds > 300) return 'urgency-green';
    if (seconds > 120) return 'urgency-yellow';
    if (seconds > 60) return 'urgency-orange';
    if (seconds > 30) return 'urgency-red';
    return 'urgency-critical';
}

function formatTime(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}m ${secs}s`;
}

window.triggerManualSolve = async function(accountId) {
    try {
        const res = await fetch('/api/captcha/oauth_url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account_id: accountId })
        });
        const data = await res.json();
        if (!data.success || !data.url) {
            // the server says *why* (stopped account, Discord refused the grant, ...);
            // this used to always read "Failed to get captcha URL", which is what an
            // operator reports as "it says url not found"
            showToast(data.error || 'Could not open the captcha for this account', 'error');
            // a stale notification is withdrawn server-side on that same call, so
            // refresh the bell instead of leaving the dead entry on screen
            updatePendingCaptchas();
            return;
        }
        const popup = window.open(data.url, '_blank', 'width=420,height=600,resizable=yes,scrollbars=yes');
        if (!popup) {
            window.open(data.url, '_blank');
        } else {
            _manualSolvePopup = popup;
        }
        showToast('Captcha page opened in new window', 'info');
    } catch (e) {
        showToast('Error opening captcha', 'error');
    }
};

window.pollForCaptchas = async function() {
    await updatePendingCaptchas();
};

function startPendingTimer() {
    if (pendingInterval) clearInterval(pendingInterval);
    pendingInterval = setInterval(() => {
        if (Object.keys(pendingCaptchas).length > 0) {
            renderPendingDropdown();
            renderSecurityCards();
        }
    }, 1000);
}

window.toggleNotificationDropdown = function() {
    const dropdown = document.getElementById('notification-dropdown');
    if (!dropdown) return;
    if (dropdown.style.display === 'block') {
        dropdown.style.display = 'none';
    } else {
        dropdown.style.display = 'block';
        document.addEventListener('click', function closeDropdown(e) {
            const bell = document.getElementById('notification-bell');
            if (bell && !bell.contains(e.target) && !dropdown.contains(e.target)) {
                dropdown.style.display = 'none';
                document.removeEventListener('click', closeDropdown);
            }
        });
    }
};

window.cancelManualSolve = function() {
    if (_manualSolvePopup) {
        try { _manualSolvePopup.close(); } catch (e) {}
        _manualSolvePopup = null;
    }
};

// ---------------------------------------------------------------------------
// Embedded hCaptcha panel (index.html #captcha-solver-section).
// dashboard.js update() calls openEmbeddedCaptcha() whenever an account is
// paused with a captcha message, so these must exist even if the hCaptcha
// script is blocked.
// ---------------------------------------------------------------------------
const OWO_HCAPTCHA_SITEKEY = 'a6a1d5ce-612d-472d-8e37-7601408fbc09';
let _embeddedCaptcha = { accountId: null, accountName: null, widgetId: null };

function _captchaFallbackHtml(accountId) {
    return `
        <div class="no-data" style="text-align:center;">
            hCaptcha widget unavailable here.<br>
            <button class="btn-control gold" style="margin-top:10px;"
                onclick="triggerManualSolve('${jsArg(accountId)}')">Open solve page</button>
        </div>
    `;
}

// hCaptcha serves 127.0.0.1 but answers the literal host "localhost" with
// 403 "Invalid Data", so the embedded widget can never load on http://localhost:8000 -
// it silently renders an empty box. Same machine, same port, different string.
function _localhostBlocked() {
    return location.hostname === 'localhost';
}

function _localhostHtml() {
    const swapped = location.href.replace('//localhost', '//127.0.0.1');
    return `
        <div class="no-data" style="text-align:left; line-height:1.6;">
            hCaptcha refuses the hostname <code>localhost</code> (403 Invalid Data), so the
            widget cannot load on this address. Two ways round it:
            <div style="margin-top:12px;">
                <a class="btn-control green" href="${swapped}">Reopen on 127.0.0.1</a>
                <button class="btn-control gold" onclick="solveInBrowser()">Solve with NopeCHA</button>
            </div>
            <div style="margin-top:10px; opacity:.75; font-size:.9em;">
                Cookies are per-hostname, so 127.0.0.1 will ask you to log in again.
                The NopeCHA extension solve runs in a local Chromium on the machine hosting the bot.
            </div>
        </div>
    `;
}

function _renderEmbeddedHcaptcha() {
    const container = document.getElementById('hcaptcha-container');
    if (!container) return;
    const accountId = _embeddedCaptcha.accountId;

    if (_localhostBlocked()) {
        container.innerHTML = _localhostHtml();
        return;
    }

    if (typeof hcaptcha === 'undefined' || typeof hcaptcha.render !== 'function') {
        container.innerHTML = _captchaFallbackHtml(accountId);
        return;
    }

    container.innerHTML = '<div id="hcaptcha-widget"></div>';
    try {
        _embeddedCaptcha.widgetId = hcaptcha.render('hcaptcha-widget', {
            sitekey: OWO_HCAPTCHA_SITEKEY,
            theme: 'dark',
            callback: 'submitEmbeddedCaptcha',
            'expired-callback': 'reloadEmbeddedCaptcha',
            'error-callback': 'reloadEmbeddedCaptcha'
        });
    } catch (e) {
        console.error('hCaptcha render failed:', e);
        _embeddedCaptcha.widgetId = null;
        container.innerHTML = _captchaFallbackHtml(accountId);
    }
}

window.openEmbeddedCaptcha = function(accountId, accountName) {
    const section = document.getElementById('captcha-solver-section');
    if (!section) return;
    _embeddedCaptcha.accountId = accountId;
    _embeddedCaptcha.accountName = accountName || accountId;
    section.style.display = 'block';
    const title = section.querySelector('.module-header h3');
    if (title) title.setAttribute('title', `Solving for ${_embeddedCaptcha.accountName}`);
    _renderEmbeddedHcaptcha();
};

window.reloadEmbeddedCaptcha = function() {
    if (!_embeddedCaptcha.accountId) return;
    if (_embeddedCaptcha.widgetId !== null && typeof hcaptcha !== 'undefined') {
        try { hcaptcha.reset(_embeddedCaptcha.widgetId); return; } catch (e) {}
    }
    _renderEmbeddedHcaptcha();
};

window.closeEmbeddedCaptcha = function() {
    const section = document.getElementById('captcha-solver-section');
    if (section) section.style.display = 'none';
    if (_embeddedCaptcha.widgetId !== null && typeof hcaptcha !== 'undefined') {
        try { hcaptcha.reset(_embeddedCaptcha.widgetId); } catch (e) {}
    }
    _embeddedCaptcha = { accountId: null, accountName: null, widgetId: null };
};

window.submitEmbeddedCaptcha = async function(token) {
    const accountId = _embeddedCaptcha.accountId;
    if (!accountId || !token) return;
    try {
        const res = await fetch('/api/captcha_solve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account_id: accountId, token: token })
        });
        const d = await res.json();
        if (d.success) {
            showToast('Captcha verified', 'success');
            dismissCaptchaCard(accountId);
            closeEmbeddedCaptcha();
        } else {
            showToast(d.error || 'Captcha rejected', 'error');
            // the server withdraws a challenge whose account has gone away, so resync
            // rather than leaving a card that can never succeed
            updatePendingCaptchas();
            reloadEmbeddedCaptcha();
        }
    } catch (e) {
        showToast('Failed to submit captcha', 'error');
        reloadEmbeddedCaptcha();
    }
};

window.cancelEmbeddedCaptcha = function() {
    cancelManualSolve();
    closeEmbeddedCaptcha();
};

// Extension solve: the bot's machine opens OwO's captcha page in a local Chromium with
// the account already authenticated and NopeCHA's extension loaded, and the extension
// answers the challenge. Needs a booster key; there is no key-free path any more.
window.solveInBrowser = async function() {
    const accountId = _embeddedCaptcha.accountId;
    if (!accountId) { showToast('No account selected', 'error'); return; }
    const btn = document.getElementById('browser-solve-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'NopeCHA solving...'; }
    showToast('Handing the captcha to the NopeCHA extension...', 'info');
    try {
        const res = await fetch('/api/captcha/browser_solve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account_id: accountId })
        });
        const d = await res.json();
        if (d.success && d.queued) {
            showToast('Captcha queued. It stays visible until verified.', 'info');
        } else if (d.success) {
            const how = d.how === 'passive' ? 'passed without a challenge'
                      : d.how === 'nopecha-extension' ? 'solved by the NopeCHA extension'
                      : 'already clear';
            showToast(`Captcha done (${how})`, 'success');
            dismissCaptchaCard(accountId);
            closeEmbeddedCaptcha();
        } else {
            showToast(d.error || 'Extension solve failed', 'error');
        }
    } catch (e) {
        showToast('Browser solve request failed', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Solve with NopeCHA'; }
    }
};

startPendingTimer();