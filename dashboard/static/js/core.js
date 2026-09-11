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

let currentConfig = {};
let originalConfig = null;
let globalAnalyticsData = null;
let lineChart = null, sessChart = null, cashChart = null, pieChart = null, captchaChart = null;
let currentAccountId = null;
let accountsList = [];
let accountConfigList = [];
let activeConfigCategory = null;
let configSearchQuery = '';
let lastLogsHash = '';

// Everything the server hands us - account names, proxy labels, log lines - is
// written by whoever owns that space, and the admin renders it in their own
// session. So nothing goes into innerHTML unescaped. These live here rather than
// in a feature file because core.js loads first and every renderer needs them.
function escHtml(s) {
    return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escAttr(s) {
    return escHtml(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// For values that go inside a quoted JS string inside an onclick= attribute.
// escAttr is not enough there: the HTML parser decodes &#39; back to ' before the
// JS is parsed, so a name containing a quote could still close the string and run
// code. encodeURIComponent leaves ' alone, hence the extra replace - the result is
// only [A-Za-z0-9%()!~*._-], inert in both contexts, and the handlers already
// decodeURIComponent it back.
function jsArg(s) {
    return encodeURIComponent(String(s === null || s === undefined ? '' : s)).replace(/'/g, '%27');
}

// CSRF, once, instead of at ~60 fetch() call sites. The server requires
// X-CSRF-Token on every non-GET /api/ request; this attaches it to same-origin
// calls and leaves cross-origin ones (hcaptcha, discord) untouched.
//
// Session expiry is handled here too, for the same reason: the server answers
// every expired-session /api/ call with a 401 + {success:false, error:'Session
// expired'} body (see _reject_session). Dozens of feature fetches do
// `data.proxies || []` / `data.commands || []` / `data.users || []`, so a 401
// silently turned each panel empty (proxies vanished, custom commands cleared,
// users list blanked, config broke) instead of sending the operator back to
// login - the exact "everything disappears" symptom. Catching the 401 once,
// at the shared fetch seam, fixes all of them without touching every caller.
(function installCsrf() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    let token = meta ? meta.getAttribute('content') : '';
    const nativeFetch = window.fetch.bind(window);
    // A single in-flight redirect guard: many polls fire concurrently (accounts
    // 5s, stats 1s, captcha 2s, config 5s), and on expiry they all return 401 at
    // once. Without this, every one of them would race to set location.href.
    let redirectingToLogin = false;

    function bailToLogin() {
        if (redirectingToLogin) return;
        redirectingToLogin = true;
        // /login is a GET page; appending a return hint lets it bounce back after
        // a fresh sign-in. href (not replace) so a back-button still works.
        window.location.href = '/login?expired=1';
    }

    window.setCsrfToken = function (value) {
        if (value) token = value;
    };

    window.fetch = function (input, init) {
        init = init || {};
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = (init.method || (input && input.method) || 'GET').toUpperCase();
        const sameOrigin = !/^https?:\/\//i.test(url) || url.startsWith(window.location.origin);

        if (token && sameOrigin && method !== 'GET' && method !== 'HEAD') {
            const headers = new Headers(init.headers || (input && input.headers) || {});
            if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
            init = Object.assign({}, init, { headers: headers });
        }
        return nativeFetch(input, init).then(function (res) {
            // Only same-origin /api/ calls can carry our session; a 401 there is
            // always _reject_session. Leave cross-origin (hcaptcha/discord) and
            // non-api responses untouched so legitimate 401s elsewhere still
            // reach their callers.
            if (res.status === 401 && sameOrigin && /\/api\//.test(url)) {
                bailToLogin();
            }
            return res;
        });
    };
})();

const timeFormatter = new Intl.DateTimeFormat('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: true
});

const CONFIG_CATEGORY_HINTS = {
    core: 'Channels, prefix, and main bot switches',
    stealth: 'Human-like delays and timing',
    security: 'Captcha handling and safety pauses',
    reactionBot: 'Auto-reactions and triggers',
    boss: 'Boss fight automation',
    level_grind: 'XP grinding behavior',
    utilities: 'Extra helper utilities',
    owner: 'Drive every farm account from your own Discord',
    coop: 'Accounts helping each other with quests and battles',
    commands: 'Per-command automation modules'
};

const CONFIG_CMD_HINTS = {
    owo: 'OwO command scheduling',
    hunt: 'Automatic hunting',
    battle: 'Battle / PvP commands',
    curse: 'Curse command settings',
    pray: 'Pray command settings',
    cookie: 'Cookie rewards',
    daily: 'Daily claim automation',
    coinflip: 'Coinflip settings',
    slots: 'Slots settings',
    blackjack: 'Blackjack settings',
    sell_sac: 'Sell / sacrifice items',
    gems: 'Gem usage by tier',
    giveaway: 'Giveaway participation',
    huntbot: 'Huntbot integration',
    open: 'Open crates / boxes',
    quest: 'Quest tracking',
    rpp: 'RPP command',
    shop: 'Shop and ring purchases',
    team: 'Auto zoo team - swaps in rarer animals',
    weapon: 'Auto weapon equipping',
    custom: 'Your own commands on a timer'
};


function showToast(message, type = 'success') {
    const toast = document.getElementById('neura-toast');
    const msgEl = document.getElementById('toast-message');
    if (!toast || !msgEl) return;
    msgEl.innerText = message;
    toast.className = `neura-toast show ${type}`;
    setTimeout(() => { toast.classList.remove('show'); }, 3000);
}

function checkDirty() {
    const bar = document.getElementById('floating-save-bar');
    if (!bar) return;
    const configView = document.getElementById('config');
    const isConfigActive = configView && configView.classList.contains('active-view');
    const isDirty = JSON.stringify(currentConfig) !== JSON.stringify(originalConfig);
    if (isDirty && isConfigActive) {
        bar.classList.add('visible');
    } else {
        bar.classList.remove('visible');
    }
}

window.discardChanges = function() {
    if (originalConfig) {
        currentConfig = JSON.parse(JSON.stringify(originalConfig));
        renderSettings(currentConfig);
        checkDirty();
        showToast("Changes Discarded", "info");
    }
};

function setDeep(o, p, v) {
    if (p.length === 1) o[p[0]] = v;
    else {
        if (!o[p[0]]) o[p[0]] = {};
        setDeep(o[p[0]], p.slice(1), v);
    }
}
function getDeep(o, p) {
    if (!o || p.length === 0) return o;
    return getDeep(o[p[0]], p.slice(1));
}

function formatCfgLabel(key) {
    return String(key).replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function cfgSearchBlob(path, label, categoryName, sections) {
    return [label, path, categoryName, ...(sections || [])].join(' ').toLowerCase();
}


function initDynamicTilt() {
    const cards = document.querySelectorAll('.kpi-card');
    cards.forEach(card => {
        const icon = card.querySelector('.kpi-icon');
        if (!icon) return;
        card.addEventListener('mousemove', (e) => {
            const rect = card.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            const centerX = rect.width / 2;
            const centerY = rect.height / 2;
            const rotateX = -(y - centerY) / 5;
            const rotateY = (x - centerX) / 5;
            icon.style.transform = `rotateX(${rotateX}deg) rotateY(${rotateY}deg) translateZ(10px)`;
        });
        card.addEventListener('mouseleave', () => {
            icon.style.transform = `rotateX(0deg) rotateY(0deg) translateZ(0px)`;
        });
    });
}