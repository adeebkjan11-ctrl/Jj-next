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

// The Earning tab. One switch that points every account in the space at
// hunt + huntbot + sell, keeps battle and the team watcher on so the team the
// hunt depends on keeps growing, and the ledger those accounts keep while it is on.
//
// Every figure here comes from the server's cash-flow ledger (cogs/earning.py):
// gained minus spent always equals the account's real cowoncy movement, so a
// bucket that guessed wrong shows up as a mislabelled row, never as fake profit.

let earningSettings = null;
let earningTimer = null;

function earnNum(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    return Number(n).toLocaleString('en-US');
}

function earnSigned(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return '—';
    const v = Number(n);
    return (v > 0 ? '+' : '') + v.toLocaleString('en-US');
}

// Values are tinted through the theme's own classes rather than inline colours,
// so a palette change in base.css reaches this tab too.
function earnCls(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return 'earn-dim';
    if (Number(n) > 0) return 'earn-pos';
    if (Number(n) < 0) return 'earn-neg';
    return '';
}

function earnDuration(hours) {
    const h = Number(hours || 0);
    if (h <= 0) return 'just started';
    if (h < 1) return `${Math.round(h * 60)}m`;
    if (h < 48) return `${Math.floor(h)}h ${Math.round((h % 1) * 60)}m`;
    return `${Math.floor(h / 24)}d ${Math.round(h % 24)}h`;
}

window.loadEarning = async function () {
    try {
        const r = await fetch('/api/earning');
        const d = await r.json();
        if (!d || !d.success) throw new Error((d && d.error) || 'request failed');
        earningSettings = d.settings || {};
        renderEarningControls(d.settings || {}, d.totals || {});
        renderEarningKpis(d.totals || {}, d.accounts || []);
        renderEarningAccounts(d.accounts || []);
    } catch (e) {
        const box = document.getElementById('earning-controls');
        if (box) box.innerHTML = `<div class="no-data">Could not load earning data: ${escHtml(String(e.message || e))}</div>`;
    }
};

function renderEarningControls(s, totals) {
    const box = document.getElementById('earning-controls');
    if (!box) return;
    const on = !!s.enabled;
    box.innerHTML = `
        ${earnRow('Earning mode',
            `Points every account in this space at <b>hunt</b>, <b>huntbot</b> and <b>sell</b>,
             and keeps <b>battle</b> and the <b>team watcher</b> on so the team it hunts with keeps
             growing. Overrides each account's own command switches while it is on; turning it off
             restores them.`,
            `<div class="earn-mode-control">
                <span class="earn-pill ${on ? 'on' : 'off'}">${on ? 'running' : 'off'}</span>
                <span class="earn-hint earn-dim">${totals.on || 0}/${totals.accounts || 0} running</span>
                <div class="neura-toggle ${on ? 'is-on' : ''}" role="switch" aria-checked="${on}"
                     onclick="toggleEarning(${on ? 'false' : 'true'}, this)">
                    <div class="neura-toggle-track"><span class="neura-toggle-thumb"></span></div>
                </div>
            </div>`,
            'earn-row-head')}
        ${earnRow('Exclusive',
            `Also switch off what competes for the cowoncy or the time: the gambling three,
             curse/pray, cookie, rpp, giveaway and coop battles. Daily, gems, team, battle,
             weapon, quest and shop stay on &mdash; they feed the hunt.`,
            earnToggle('exclusive', !!s.exclusive))}
        ${earnRow('Huntbot cowoncy',
            'Handed to the huntbot on every dispatch. This is the spend the ledger meters.',
            earnStepper('huntbot_cash', s.huntbot_cash, 500, 'cwy'))}
        ${earnRow('Cowoncy per hunt',
            `What OwO charges for one <code>owo hunt</code> &mdash; 5 today. The ledger books this
             much per hunt, so change it only if OwO changes its price.`,
            earnStepper('hunt_cost', s.hunt_cost, 1, 'cwy'))}
        ${earnRow('Sell every',
            'How often to sell animals. Selling is the only income the ledger counts as a sale.',
            earnStepper('sell_interval_min', s.sell_interval_min, 5, 'min'))}
        ${earnRow('Sell what',
            `Passed straight to <code>owo sell &lt;type&gt;</code>. Use <code>all</code> for every
             animal, or a rarity like <code>common</code>.`,
            `<div class="cfg-input-wrap"><input class="cfg-input" style="max-width:150px"
                value="${escAttr(s.sell_type || 'all')}"
                onchange="saveEarningField('sell_type', this.value, this)"></div>`)}
        ${earnRow('Ledger',
            'Reset zeroes every figure below and re-opens the run at each account\'s current balance.',
            `<button class="btn-control" onclick="resetEarning(null, this)">
                <span class="btn-text">RESET ALL</span></button>`,
            'earn-row-foot')}`;
}

// ── control chrome ──────────────────────────────────────────────────────────
// Reuses the config view's row / stepper / toggle markup verbatim so the two
// tabs are the same surface. Anything earning-specific is a class in
// css/pages/earning.css, never an inline style.

function earnRow(label, hint, control, cls) {
    return `
        <div class="cfg-row ${cls || ''}">
            <div class="cfg-row-label">
                <span class="cfg-label-text">${escHtml(label)}</span>
                <span class="earn-hint">${hint}</span>
            </div>
            <div class="cfg-row-control">${control}</div>
        </div>`;
}

function earnToggle(key, on) {
    return `
        <div class="neura-toggle ${on ? 'is-on' : ''}" role="switch" aria-checked="${on}"
             onclick="saveEarningField('${key}', ${on ? 'false' : 'true'}, this)">
            <div class="neura-toggle-track"><span class="neura-toggle-thumb"></span></div>
        </div>`;
}

function earnStepper(key, value, step, unit) {
    const v = Number(value || 0);
    return `
        <div class="cfg-stepper">
            <button type="button" class="cfg-stepper-btn"
                    onclick="saveEarningField('${key}', ${Math.max(0, v - step)}, this)">
                <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/minus.svg');"></span>
            </button>
            <input type="number" class="cfg-stepper-val" value="${v}"
                   onchange="saveEarningField('${key}', this.value, this)">
            ${unit ? `<span class="cfg-stepper-unit">${unit}</span>` : ''}
            <button type="button" class="cfg-stepper-btn"
                    onclick="saveEarningField('${key}', ${v + step}, this)">
                <span class="icon-svg" style="--icon: url('/static/assets/neura_icons/plus.svg');"></span>
            </button>
        </div>`;
}

function earnKpi(icon, title, value, sub, cls) {
    return `
        <div class="kpi-card analytics-card">
            <div class="kpi-icon"><span class="icon-svg"
                    style="--icon: url('/static/assets/neura_icons/${icon}.svg');"></span></div>
            <div class="kpi-data">
                <h3>${escHtml(title)}</h3>
                <p class="earn-num ${cls || ''}">${value}${sub
            ? `<span class="earn-sub">${sub}</span>` : ''}</p>
            </div>
        </div>`;
}

function renderEarningKpis(t, accounts) {
    const box = document.getElementById('earning-kpis');
    if (!box) return;
    const hours = accounts.reduce((m, a) => Math.max(m, a.hours || 0), 0);
    const perHunt = Number((earningSettings && earningSettings.hunt_cost) ?? 5);
    box.innerHTML = [
        earnKpi('coins', 'Earned', earnNum(t.gained), `${earnNum(t.sold_count)} sales`, 'earn-pos'),
        earnKpi('money', 'Huntbot spend', earnNum(t.spent_autohunt),
                `${earnNum(t.autohunt_runs)} dispatches`, 'earn-neg'),
        earnKpi('gun', 'Hunt spend', earnNum(t.spent_hunt),
                `${earnNum(t.hunts)} hunts × ${perHunt}`, 'earn-neg'),
        earnKpi('layers', 'Unattributed', earnNum(t.spent_other),
                'no command to blame', 'earn-dim'),
        earnKpi('bolt', 'Net', earnSigned(t.net), earnDuration(hours), earnCls(t.net)),
        earnKpi('chart-column', 'Per hour', earnSigned(t.per_hour), 'across the farm', earnCls(t.per_hour)),
    ].join('');
}

function renderEarningAccounts(rows) {
    const box = document.getElementById('earning-accounts');
    if (!box) return;
    if (!rows.length) {
        box.innerHTML = '<div class="no-data">No accounts are running. Start one from the Accounts tab.</div>';
        return;
    }
    box.innerHTML = `
        <div class="proxy-table-wrap earn-table-wrap">
            <table class="proxy-table earn-table">
                <thead><tr>
                    <th>Account</th>
                    <th>Balance</th>
                    <th>Earned</th>
                    <th>Spent</th>
                    <th>Net</th>
                    <th>Per hour</th>
                    <th>Run</th>
                    <th>Last event</th>
                    <th></th>
                </tr></thead>
                <tbody>${rows.map(earningRow).join('')}</tbody>
            </table>
        </div>`;
}

function earningRow(a) {
    const pill = !a.enabled ? '<span class="earn-pill off">off</span>'
        : a.paused ? '<span class="earn-pill paused">paused</span>'
            : '<span class="earn-pill on">earning</span>';
    // autohunt and hunt spend share one column: the split lives in the sub line, so the
    // table reads left to right as balance -> earned -> spent -> net instead of as
    // eleven columns of numbers
    const spent = (a.spent_autohunt || 0) + (a.spent_hunt || 0);
    return `
        <tr>
            <td>
                <span class="earn-acc-name">${escHtml(a.name)}</span>${pill}
                <span class="earn-sub">${earnNum(a.battles)} battles · ${earnNum(a.team_changes)} team swaps</span>
            </td>
            <td class="earn-num">${earnNum(a.current_cash)}</td>
            <td class="earn-num earn-pos">${earnNum(a.gained)}
                <span class="earn-sub">${earnNum(a.sold_count)} sales</span></td>
            <td class="earn-num earn-neg">${earnNum(spent)}
                <span class="earn-sub">${earnNum(a.spent_autohunt)} huntbot · ${earnNum(a.spent_hunt)} hunt</span></td>
            <td class="earn-num earn-net ${earnCls(a.net)}">${earnSigned(a.net)}</td>
            <td class="earn-num ${earnCls(a.per_hour)}">${earnSigned(a.per_hour)}</td>
            <td class="earn-dim">${escHtml(earnDuration(a.hours))}</td>
            <td><div class="earn-event" title="${escAttr(a.last_event || '')}">${escHtml(a.last_event || '—')}</div></td>
            <td><button class="btn-proxy-sm" onclick="resetEarning('${jsArg(a.id)}', this)">Reset</button></td>
        </tr>`;
}

async function earningPost(url, body, btn) {
    const label = btn ? btn.innerHTML : null;
    if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
    try {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const d = await r.json();
        if (!d || !d.success) throw new Error((d && d.error) || 'request failed');
        return d;
    } catch (e) {
        showToast(String(e.message || e), 'error');
        return null;
    } finally {
        if (btn) { btn.disabled = false; btn.style.opacity = ''; if (label) btn.innerHTML = label; }
    }
}

window.toggleEarning = async function (on, btn) {
    const d = await earningPost('/api/earning/toggle', { enabled: !!on }, btn);
    if (!d) return;
    showToast(on ? 'Earning mode ON — accounts redirected to hunt + huntbot + sell'
        : 'Earning mode OFF — your own command settings are back', 'success');
    // the command gates just changed under the config editor, so re-read it too
    if (typeof loadConfig === 'function') loadConfig();
    loadEarning();
};

window.saveEarningField = async function (key, value, btn) {
    const body = {};
    body[key] = value;
    const d = await earningPost('/api/earning/toggle', body, btn);
    if (!d) return;
    showToast('Saved', 'success');
    loadEarning();
};

// id arrives jsArg-encoded from the onclick above (see core.js) - undo that here
window.resetEarning = async function (rawId, btn) {
    const id = rawId ? decodeURIComponent(rawId) : rawId;
    const d = await earningPost('/api/earning/reset', id ? { id: id } : {}, btn);
    if (!d) return;
    showToast(`Ledger reset for ${d.reset} account(s)`, 'success');
    loadEarning();
};

// Polled only while the tab is open: the ledger lives on the server and keeps
// counting regardless, so there is nothing to lose by not fetching it.
window.startEarningPoll = function () {
    if (earningTimer) clearInterval(earningTimer);
    earningTimer = setInterval(() => {
        const view = document.getElementById('earning');
        if (view && view.classList.contains('active-view') && !document.hidden) loadEarning();
    }, 5000);
};
