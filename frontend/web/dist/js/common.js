/*
 * Shared helpers for both OpenLAD pages (chat + admin).
 *
 * Requires i18n.js (uses __() at call time); include after it and before
 * ui.js / app.js / admin.js:
 *   <script src="/static/js/i18n.js"></script>
 *   <script src="/static/js/common.js"></script>
 */

const API_BASE = '/api/v1';

// ===== Auth =====
// role is not read server-side (tenant.py derives it from the API key);
// sending it keeps both pages on one identical header shape.
function getAuthHeaders() {
    const tenantId = localStorage.getItem('tenant_id');
    const apiKey = localStorage.getItem('api_key');
    const role = localStorage.getItem('user_role');
    return {
        'X-Tenant-ID': tenantId || '',
        'Authorization': apiKey ? 'Bearer ' + apiKey : '',
        'X-User-Role': role || ''
    };
}

function clearAuthStorage() {
    // Remove only auth-related keys; preserve user preferences (e.g. openlad_lang, openlad_theme)
    ['tenant_id', 'api_key', 'user_role', 'username'].forEach(k => localStorage.removeItem(k));
}

function handleAuthExpired() {
    clearAuthStorage();
    const modal = document.getElementById('loginModal');
    if (modal) modal.classList.add('show');
}

// ===== Fetch =====
// JSON request with auth headers; a 401 means the session key was revoked or
// expired — bounce to the login modal once here instead of per call site.
// Callers still get the raw Response and decide how to render non-401 errors.
async function apiFetch(path, options = {}) {
    const res = await fetch(API_BASE + path, {
        ...options,
        headers: { ...getAuthHeaders(), ...(options.headers || {}) }
    });
    if (res.status === 401) {
        handleAuthExpired();
        const err = new Error('401 Unauthorized');
        err.handled = true;  // signals "already surfaced" to catch blocks
        throw err;
    }
    return res;
}

async function readErrorDetail(res) {
    let detail = 'HTTP ' + res.status;
    try {
        const errData = await res.json();
        detail = errData.detail || detail;
    } catch (e) {}
    return detail;
}

// ===== Text =====
function escapeHtml(text) {
    // Escape all HTML-significant chars including quotes, so the result is
    // safe both as element text and inside single/double-quoted attributes
    return String(text ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ===== Visibility-aware polling =====
// Background tabs should not hit the API. Browsers already throttle timers
// in hidden pages, but the request would still fire eventually — so the tick
// skips while hidden, and a fresh fetch runs immediately when the user comes
// back (stale status is worse than no status).
function createIntervalPoller(fn, intervalMs) {
    let timer = null;
    const onVisible = () => { if (!document.hidden) fn(); };
    document.addEventListener('visibilitychange', onVisible);
    return {
        start() {
            if (timer) return;
            timer = setInterval(() => { if (!document.hidden) fn(); }, intervalMs);
            fn();
        },
        stop() {
            if (timer) { clearInterval(timer); timer = null; }
            document.removeEventListener('visibilitychange', onVisible);
        }
    };
}

// Run cb now if the page is visible, otherwise defer until it becomes visible.
// For setTimeout-style poll chains: schedule the timer, then gate the request.
function whenVisible(cb) {
    if (!document.hidden) { cb(); return; }
    const onVisible = () => {
        if (!document.hidden) {
            document.removeEventListener('visibilitychange', onVisible);
            cb();
        }
    };
    document.addEventListener('visibilitychange', onVisible);
}

// ===== Theme (dark mode) =====
// Resolution order: localStorage 'openlad_theme' > prefers-color-scheme.
// Applied as data-theme on <html>; style.css overrides its variables under
// [data-theme="dark"].
function initTheme() {
    const saved = localStorage.getItem('openlad_theme');
    const theme = saved === 'dark' || saved === 'light'
        ? saved
        : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.dataset.theme = theme;
    return theme;
}

function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('openlad_theme', next);
    updateThemeToggleIcon();
}

function updateThemeToggleIcon() {
    const btn = document.getElementById('themeToggleBtn');
    if (!btn) return;
    const dark = document.documentElement.dataset.theme === 'dark';
    btn.innerHTML = '<i class="fas ' + (dark ? 'fa-sun' : 'fa-moon') + '"></i>';
    const label = dark ? __('theme.toLight') : __('theme.toDark');
    btn.setAttribute('aria-label', label);
    btn.title = label;
}

// Same mount pattern as i18n's language toggle: pages may provide
// #themeToggleSlot to host it in their header; otherwise a fixed pill below
// the language toggle.
function injectThemeToggle() {
    if (document.getElementById('themeToggleBtn')) return;
    const btn = document.createElement('button');
    btn.id = 'themeToggleBtn';
    btn.type = 'button';
    btn.addEventListener('click', toggleTheme);

    const slot = document.getElementById('themeToggleSlot');
    if (slot) {
        btn.className = 'theme-toggle theme-toggle--inline';
        slot.appendChild(btn);
    } else {
        btn.className = 'theme-toggle';
        document.body.appendChild(btn);
    }
    updateThemeToggleIcon();
}

// Apply before first paint when the script is in <head>; harmless when loaded
// at the end of <body> (at worst one light frame).
initTheme();
document.addEventListener('DOMContentLoaded', injectThemeToggle);
// Re-translate the toggle's aria-label when the UI language changes
window.addEventListener('langchange', updateThemeToggleIcon);
