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

/* Sessions carry an account id and a live flag now (see utils/history_tracker.py),
   so the dropdown can say which account a session belongs to and the table below
   the charts can break the totals down per account. */

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.innerText = value;
}

function sessionLabel(s) {
    const when = s.start_time ? new Date(s.start_time * 1000).toLocaleString() : `session ${s.id}`;
    const who = s.account && s.account !== 'unassigned' ? s.account : null;
    const live = s.active ? ' • LIVE' : '';
    return who ? `${who} — ${when}${live}` : `Session ${s.id} — ${when}${live}`;
}

window.loadHistory = async function() {
    try {
        const startEl = document.getElementById('historyStartDate');
        const endEl = document.getElementById('historyEndDate');
        const start = startEl ? startEl.value : null;
        const end = endEl ? endEl.value : null;
        let url = '/api/history/analytics';
        const params = new URLSearchParams();
        if (start) params.append('start_date', start);
        if (end) params.append('end_date', end);
        if (params.toString()) {
            url += '?' + params.toString();
        }
        const res = await fetch(url);
        if (!res.ok) throw new Error(`analytics returned ${res.status}`);
        globalAnalyticsData = await res.json();

        const totals = globalAnalyticsData.totals || {};
        setText('total-sessions', totals.total_sessions || 0);
        setText('total-hunts', (totals.all_time_hunts || 0).toLocaleString());
        setText('total-battles', (totals.all_time_battles || 0).toLocaleString());
        setText('total-cmds', (totals.all_time_commands || 0).toLocaleString());
        setText('totalCaptchasSolved', (totals.all_time_captchas || 0).toLocaleString());

        window.populateSessionDropdown();
        window.renderPerAccount();
        renderCharts();
    } catch (e) {
        console.error('History Error:', e);
        const body = document.getElementById('perAccountBody');
        if (body) {
            body.innerHTML = '<tr><td colspan="7" class="per-account-empty">'
                + '— could not load history —</td></tr>';
        }
    }
};

window.populateSessionDropdown = function() {
    const dropdown = document.getElementById('session-select');
    if (!dropdown) return;
    if (!globalAnalyticsData || !globalAnalyticsData.sessions) {
        dropdown.innerHTML = '<option value="all">ALL SESSIONS IN RANGE</option>';
        return;
    }
    const currentVal = dropdown.value;
    const opts = ['<option value="all">ALL SESSIONS IN RANGE</option>'];
    // newest first: the session someone wants to look at is almost always the last one
    [...globalAnalyticsData.sessions].reverse().forEach(s => {
        opts.push(`<option value="${s.id}">${escapeHtml(sessionLabel(s))}</option>`);
    });
    dropdown.innerHTML = opts.join('');
    if (currentVal && dropdown.querySelector(`option[value="${CSS.escape(currentVal)}"]`)) {
        dropdown.value = currentVal;
    }
};

function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[ch]);
}

window.renderPerAccount = function() {
    const body = document.getElementById('perAccountBody');
    if (!body) return;
    const rows = (globalAnalyticsData && globalAnalyticsData.per_account) || [];
    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="7" class="per-account-empty">— no history yet —</td></tr>';
        return;
    }
    const liveIds = new Set((globalAnalyticsData.sessions || [])
        .filter(s => s.active)
        .map(s => String(s.account_id)));
    body.innerHTML = rows.map(a => {
        const live = liveIds.has(String(a.account_id));
        const seen = a.last_seen ? new Date(a.last_seen * 1000).toLocaleString() : '—';
        // "unassigned" is what the pre-per-account rows aggregate into; say so
        // rather than pretending it is an account name
        const name = a.account === 'unassigned'
            ? '<span class="per-account-unassigned">before per-account tracking</span>'
            : escapeHtml(a.account);
        return `<tr>
            <td>${name}${live ? ' <span class="per-account-live">LIVE</span>' : ''}</td>
            <td>${(a.sessions || 0).toLocaleString()}</td>
            <td>${(a.hunts || 0).toLocaleString()}</td>
            <td>${(a.battles || 0).toLocaleString()}</td>
            <td>${(a.commands || 0).toLocaleString()}</td>
            <td>${(a.captchas || 0).toLocaleString()}</td>
            <td>${live ? 'now' : escapeHtml(seen)}</td>
        </tr>`;
    }).join('');
};

function getFilteredSessions() {
    if (!globalAnalyticsData || !globalAnalyticsData.sessions) return [];
    const dropdown = document.getElementById('session-select');
    const selected = dropdown ? dropdown.value : 'all';
    if (selected === 'all') return globalAnalyticsData.sessions;
    return globalAnalyticsData.sessions.filter(s => String(s.id) === String(selected));
}

/* Cash rows carry an account id too, so one account's samples no longer get
   interleaved into another's line. */
function cashSeriesFor(sessions) {
    const all = (globalAnalyticsData && globalAnalyticsData.cash_history) || [];
    const dropdown = document.getElementById('session-select');
    if (!dropdown || dropdown.value === 'all') return all;
    const wanted = new Set(sessions.map(s => String(s.account_id)));
    const narrowed = all.filter(c => wanted.has(String(c.account_id)));
    // an old row with no account id belongs to nobody in particular; showing the
    // unfiltered series beats showing an empty chart
    return narrowed.length ? narrowed : all;
}

function showChartEmpty(canvasId, message) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    const parent = el.parentElement;
    el.style.display = 'none';
    let placeholder = parent.querySelector('.chart-empty-msg');
    if (!placeholder) {
        placeholder = document.createElement('div');
        placeholder.className = 'chart-empty-msg';
        placeholder.style.cssText = 'display:flex;align-items:center;justify-content:center;height:100%;color:#555;font-size:0.9rem;font-family:var(--font-mono);';
        parent.appendChild(placeholder);
    }
    placeholder.textContent = message;
    placeholder.style.display = 'flex';
}

function clearChartEmpty(canvasId) {
    const el = document.getElementById(canvasId);
    if (!el) return;
    el.style.display = '';
    const parent = el.parentElement;
    const placeholder = parent.querySelector('.chart-empty-msg');
    if (placeholder) placeholder.style.display = 'none';
}


window.renderCharts = function renderCharts() {
    if (!globalAnalyticsData) return;
    if (typeof Chart === 'undefined') {
        showChartEmpty('sessionChart', '— chart library did not load —');
        showChartEmpty('pieChart', '— chart library did not load —');
        showChartEmpty('cashHistoryChart', '— chart library did not load —');
        return;
    }

    const sessions = getFilteredSessions();

    const sessEl = document.getElementById('sessionChart');
    if (sessEl) {
        if (!sessions || sessions.length === 0) {
            showChartEmpty('sessionChart', '— No session data in range —');
            if (sessChart) { sessChart.destroy(); sessChart = null; }
        } else {
            clearChartEmpty('sessionChart');
            const sctx = sessEl.getContext('2d');
            if (sessChart) sessChart.destroy();
            const revSessions = [...sessions].reverse();
            try {
                sessChart = new Chart(sctx, {
                    type: 'bar',
                    data: {
                        // labelled by account, since one bar per account per session
                        // is the whole point of the per-account schema
                        labels: revSessions.map(s => {
                            const who = s.account && s.account !== 'unassigned' ? s.account : `S${s.id}`;
                            if (s.start_time) {
                                return `${who} (${new Date(s.start_time * 1000).toLocaleDateString()})`;
                            }
                            return who;
                        }),
                        datasets: [
                            { label: 'Hunts', data: revSessions.map(s => s.stats?.hunts || 0), backgroundColor: '#7c6cff', borderRadius: 4 },
                            { label: 'Battles', data: revSessions.map(s => s.stats?.battles || 0), backgroundColor: '#3b82f6', borderRadius: 4 },
                            { label: 'Captchas', data: revSessions.map(s => s.stats?.captchas || 0), backgroundColor: '#00d16e', borderRadius: 4 }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {
                            x: { grid: { display: false }, ticks: { color: '#888' } },
                            y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#888' } }
                        },
                        plugins: {
                            legend: { labels: { color: '#ccc' } },
                            tooltip: {
                                callbacks: {
                                    afterTitle: items => {
                                        const s = revSessions[items[0].dataIndex];
                                        return s && s.active ? 'still running' : '';
                                    }
                                }
                            }
                        }
                    }
                });
            } catch (e) {
                console.error('Error creating session chart:', e);
            }
        }
    }

    const cashEl = document.getElementById('cashHistoryChart');
    if (cashEl) {
        const cashData = cashSeriesFor(sessions);
        if (!cashData || cashData.length === 0) {
            showChartEmpty('cashHistoryChart', '— No cash history recorded —');
            if (cashChart) { cashChart.destroy(); cashChart = null; }
        } else {
            clearChartEmpty('cashHistoryChart');
            const cctx = cashEl.getContext('2d');
            if (cashChart) cashChart.destroy();
            try {
                cashChart = new Chart(cctx, {
                    type: 'line',
                    data: {
                        labels: cashData.map(c => c.timestamp ? c.timestamp.split(' ')[1] || c.timestamp : ''),
                        datasets: [{
                            label: 'Cash Flow',
                            data: cashData.map(c => c.amount),
                            borderColor: '#ffd700',
                            backgroundColor: 'rgba(255, 215, 0, 0.1)',
                            fill: true,
                            tension: 0.4,
                            pointRadius: cashData.length > 30 ? 0 : 3
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: { legend: { display: false } },
                        scales: {
                            x: { display: cashData.length <= 30, ticks: { color: '#888', maxRotation: 0 } },
                            y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#888' } }
                        }
                    }
                });
            } catch (e) {
                console.error('Error creating cash chart:', e);
            }
        }
    }

    const pieEl = document.getElementById('pieChart');
    if (pieEl) {
        let totalHunts = 0, totalBattles = 0, totalCaptchas = 0, totalOther = 0;
        sessions.forEach(s => {
            totalHunts += s.stats?.hunts || 0;
            totalBattles += s.stats?.battles || 0;
            totalCaptchas += s.stats?.captchas || 0;
            totalOther += Math.max(0, (s.stats?.commands || 0) - (s.stats?.hunts || 0) - (s.stats?.battles || 0));
        });
        const total = totalHunts + totalBattles + totalCaptchas + totalOther;

        if (total === 0) {
            showChartEmpty('pieChart', '— No activity data —');
            if (pieChart) { pieChart.destroy(); pieChart = null; }
        } else {
            clearChartEmpty('pieChart');
            const pctx = pieEl.getContext('2d');
            if (pieChart) pieChart.destroy();
            try {
                pieChart = new Chart(pctx, {
                    type: 'doughnut',
                    data: {
                        // captchas are not commands, so subtracting them from "other"
                        // used to hide one non-hunt/battle command per solve
                        labels: ['Hunts', 'Battles', 'Other commands', 'Captchas solved'],
                        datasets: [{
                            data: [totalHunts, totalBattles, totalOther, totalCaptchas],
                            backgroundColor: ['#7c6cff', '#22d3ee', '#5b6072', '#34d399'],
                            borderWidth: 0
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        cutout: '70%',
                        plugins: { legend: { position: 'right', labels: { color: '#ccc' } } }
                    }
                });
            } catch (e) {
                console.error('Error creating pie chart:', e);
            }
        }
    }
};
