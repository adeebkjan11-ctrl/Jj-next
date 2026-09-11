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

let customCommands = [];
let customCommandsEnabled = false;

window.loadCustomCommands = async function() {
    const q = currentAccountId ? `?id=${currentAccountId}` : '';
    try {
        const res = await fetch(`/api/bot/custom_commands${q}`);
        const d = await res.json();
        customCommands = d.commands || [];
        customCommandsEnabled = !!d.enabled;
        const box = document.getElementById('customCmdEnabled');
        if (box) box.checked = customCommandsEnabled;
        renderCustomCommands();
    } catch (e) {
        console.error('Failed to load custom commands', e);
    }
};

function renderCustomCommands() {
    const list = document.getElementById('customCmdList');
    if (!list) return;
    if (!customCommands.length) {
        list.innerHTML = '<div class="no-data">No saved commands yet.</div>';
        return;
    }
    list.innerHTML = customCommands.map((c, i) => {
        const timer = c.interval_s > 0 ? `every ${formatInterval(c.interval_s)}` : 'manual only';
        const off = c.enabled === false ? ' off' : '';
        return `
            <div class="custom-cmd-item${off}">
                <span class="custom-cmd-name mono">${escapeHtml(c.command)}</span>
                <span class="custom-cmd-timer">${timer}</span>
                <button class="btn-proxy-sm" onclick="sendQuickCommand('${escapeAttr(c.command)}')">Send</button>
                <button class="btn-proxy-sm" onclick="sendQuickCommand('${escapeAttr(c.command)}', true)">All</button>
                <button class="btn-proxy-sm" onclick="toggleCustomCommand(${i})">${c.enabled === false ? 'On' : 'Off'}</button>
                <button class="btn-proxy-sm danger" onclick="removeCustomCommand(${i})">Del</button>
            </div>
        `;
    }).join('');
}

function formatInterval(seconds) {
    if (seconds >= 3600) return `${Math.round(seconds / 360) / 10}h`;
    if (seconds >= 60) return `${Math.round(seconds / 6) / 10}m`;
    return `${seconds}s`;
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function escapeAttr(s) {
    return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

window.sendQuickCommand = async function(command, all = false) {
    if (!command) return;
    if (!all && !currentAccountId) {
        showToast('Pick an account first', 'error');
        return;
    }
    try {
        const res = await fetch('/api/bot/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: command, id: currentAccountId, all: all })
        });
        const d = await res.json();
        showToast(d.message || d.error || 'Sent', d.success ? 'success' : 'error');
    } catch (e) {
        showToast('Failed to send command', 'error');
    }
};

window.runCustomCommand = function(all) {
    const input = document.getElementById('customCmdInput');
    if (!input) return;
    const command = input.value.trim();
    if (!command) {
        showToast('Type a command first', 'error');
        return;
    }
    sendQuickCommand(command, all);
};

window.addCustomCommand = function() {
    const input = document.getElementById('customCmdInput');
    const intervalEl = document.getElementById('customCmdInterval');
    if (!input) return;
    const command = input.value.trim();
    if (!command) {
        showToast('Type a command first', 'error');
        return;
    }
    const interval = Math.max(0, parseFloat(intervalEl && intervalEl.value ? intervalEl.value : 0) || 0);
    const existing = customCommands.findIndex(c => c.command === command);
    if (existing >= 0) {
        customCommands[existing].interval_s = interval;
        customCommands[existing].enabled = true;
    } else {
        customCommands.push({ command: command, interval_s: interval, enabled: true });
    }
    input.value = '';
    if (intervalEl) intervalEl.value = '';
    saveCustomCommands();
};

window.removeCustomCommand = function(index) {
    customCommands.splice(index, 1);
    saveCustomCommands();
};

window.toggleCustomCommand = function(index) {
    const entry = customCommands[index];
    if (!entry) return;
    entry.enabled = entry.enabled === false;
    saveCustomCommands();
};

window.saveCustomCommands = async function() {
    const box = document.getElementById('customCmdEnabled');
    customCommandsEnabled = box ? box.checked : customCommandsEnabled;
    const q = currentAccountId ? `?id=${currentAccountId}` : '';
    try {
        const res = await fetch(`/api/bot/custom_commands${q}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: customCommandsEnabled, commands: customCommands })
        });
        const d = await res.json();
        if (d.success) {
            customCommands = d.commands || customCommands;
            renderCustomCommands();
            showToast('Custom commands saved', 'success');
        } else {
            showToast(d.error || 'Save failed', 'error');
        }
    } catch (e) {
        showToast('Save failed', 'error');
    }
};
