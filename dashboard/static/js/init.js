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


function initConfigSearch() {
    const input = document.getElementById('config-search');
    if (!input) return;
    input.addEventListener('input', () => filterConfigSearch(input.value));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') clearConfigSearch();
    });
}

function updateMobileControls() {
    const mobileControls = document.getElementById('mobileControls');
    if (!mobileControls) return;
    const isDashboard = document.getElementById('dash').classList.contains('active-view');
    if (isDashboard && window.innerWidth <= 768) {
        mobileControls.style.display = 'flex';
    } else {
        mobileControls.style.display = 'none';
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    console.log("DOM Content Loaded - Initializing...");
    // resolve the session first so the CSRF token is in place and the Users tab is
    // hidden before anything renders. Accounts and proxies are per-space now, so
    // every role polls them - a plain user just sees their own.
    if (typeof applySessionRole === 'function') {
        await applySessionRole();
    }
    initDashCharts();
    window.fetchAccounts();
    if (typeof fetchProxies === 'function') fetchProxies();
    fetchAccountConfig();
    loadConfig();
    if (typeof loadCustomCommands === 'function') loadCustomCommands();
    initDynamicTilt();
    initConfigSearch();
    if (typeof startEarningPoll === 'function') startEarningPoll();

    // Polling intervals. The bots themselves run in the server's asyncio loop,
    // so they keep farming even when this tab is closed - these polls only
    // refresh what the dashboard *shows*. When the tab is hidden we slow them
    // down (no point fetching a live chart nobody is looking at), and on focus
    // we refresh immediately so a returning user sees the real, current state
    // instead of a stale snapshot that made accounts look "stopped".
    // Farm size scales the two whole-farm polls. Each one walks every account, so
    // at 500 accounts a fixed 5s cadence is a permanent load on the box for data
    // nobody is reading that fast. The server also serves both from a 2s per-space
    // snapshot cache, so extra tabs are free.
    function farmSize() {
        try {
            return Math.max(
                Array.isArray(accountsList) ? accountsList.length : 0,
                Array.isArray(accountConfigList) ? accountConfigList.length : 0
            );
        } catch (e) {
            return 0;
        }
    }

    function farmPollDelay(base) {
        const n = farmSize();
        if (document.hidden) return base * 6;
        if (n > 200) return base * 6;
        if (n > 100) return base * 4;
        if (n > 40) return base * 2;
        return base;
    }

    // setTimeout, not setInterval: the delay is re-read after every pass, so it
    // adapts as accounts are added instead of being fixed at page load
    function scheduleFarmPoll(fn, base) {
        setTimeout(async function run() {
            try {
                await fn();
            } finally {
                setTimeout(run, farmPollDelay(base));
            }
        }, farmPollDelay(base));
    }

    scheduleFarmPoll(fetchAccountConfig, 5000);
    scheduleFarmPoll(window.fetchAccounts, 5000);

    // A start queue outlives the page that armed it - it runs in the server
    // process, so a reload or a second tab must pick the progress line back up
    // rather than showing a farm that appears to be starting itself. This polls
    // once and only keeps going if a queue is actually live.
    if (window.pollStartAll) window.pollStartAll();

    // /api/stats is the heaviest payload the dashboard reads, and it used to run on
    // a fixed 1s setInterval that neither waited for the previous response nor cared
    // how big the farm was. Reuse the same self-rescheduling shape as the farm polls
    // so the next tick is only queued once this one has finished - a slow server
    // stretches the cadence instead of being buried under overlapping requests.
    function statsPollDelay() {
        const n = farmSize();
        if (document.hidden) return 10000;
        if (n > 200) return 3000;
        if (n > 100) return 2000;
        return 1000;
    }

    (function pollStats() {
        setTimeout(async function run() {
            try {
                await update();
            } finally {
                setTimeout(run, statsPollDelay());
            }
        }, statsPollDelay());
    })();

    let captchaTimer = setInterval(window.pollForCaptchas, 2000);

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) {
            // back to active: refresh now, and the next statsPollDelay() read picks
            // the fast cadence back up on its own
            window.fetchAccounts();
            fetchAccountConfig();
            update();
        }
    });

    // If the browser discarded the page (bfcache) and restored it, re-sync.
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) {
            window.fetchAccounts();
            fetchAccountConfig();
            update();
        }
    });

    updateMobileControls();
    window.addEventListener('resize', updateMobileControls);
});