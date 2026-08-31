// Shared helpers (API_BASE, getAuthHeaders, clearAuthStorage, escapeHtml,
// apiFetch, handleAuthExpired, readErrorDetail, pollers, theme) live in
// common.js — loaded before this file.

let currentSessionId = null;
let isLoading = false;
let currentIndustry = 'auto';
let availableIndustries = [];
let sessionMessages = [];

async function checkAuth() {
    const tenantId = localStorage.getItem('tenant_id');
    const apiKey = localStorage.getItem('api_key');
    if (!tenantId || !apiKey) {
        const modal = document.getElementById('loginModal');
        if (modal) modal.classList.add('show');
        return;
    }

    // If role/username is missing, auto-fill via backend query using existing token
    const missingRole = !localStorage.getItem('user_role');
    const missingUsername = !localStorage.getItem('username');
    if (missingRole || missingUsername) {
        try {
            const res = await fetch('/api/v1/me', { headers: getAuthHeaders() });
            if (res.ok) {
                const data = await res.json();
                if (data.role) localStorage.setItem('user_role', data.role);
                if (data.username) localStorage.setItem('username', data.username);
            } else if (res.status === 401) {
                // Token expired, re-login
                clearAuthStorage();
                const modal = document.getElementById('loginModal');
                if (modal) modal.classList.add('show');
                return;
            }
        } catch (e) {
            console.error('[AUTH] [AUTH] Failed to get user info:', e);
        }
    }

    const modal = document.getElementById('loginModal');
    if (modal) modal.classList.remove('show');
    loadIndustries();
    loadSessions();
    initServiceStatusBar();
}

async function doLogin() {
    const username = document.getElementById('usernameInput').value.trim();
    const password = document.getElementById('passwordInput').value.trim();
    if (!username || !password) {
        showToast(__('login.errorEmpty'), 'error');
        return;
    }
    try {
        const res = await fetch('/api/v1/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(__('login.errorFailed') + (data.detail || data.error || 'Unknown error'), 'error');
            return;
        }
        localStorage.setItem('tenant_id', data.tenant_id);
        localStorage.setItem('api_key', data.api_key);
        localStorage.setItem('user_role', data.role || 'user');
        localStorage.setItem('username', data.username || '');
        const modal = document.getElementById('loginModal');
        if (modal) modal.classList.remove('show');
        loadIndustries();
        loadSessions();
    } catch (e) {
        showToast(__('login.errorNetwork') + e.message, 'error');
    }
}

// ===== Markdown Rendering Config =====
// Configure marked renderer
const renderer = new marked.Renderer();
const originalCode = renderer.code.bind(renderer);
renderer.code = function(code, language, escaped) {
    // All code blocks use standard <pre><code> format for consistency
    return originalCode(code, language, escaped);
};
marked.setOptions({ renderer: renderer });

// ===== LaTeX Math Rendering =====
function renderLatex(html) {
    if (typeof katex === 'undefined') return html;
    // Render $$...$$ block-level formulas
    html = html.replace(/\$\$([\s\S]*?)\$\$/g, (match, formula) => {
        try {
            return katex.renderToString(formula.trim(), { displayMode: true, throwOnError: false });
        } catch (e) {
            return match;
        }
    });
    // Render $...$ inline formulas
    html = html.replace(/\$(.*?)\$/g, (match, formula) => {
        if (!formula || !formula.trim()) return match;
        // Skip plain text/identifier patterns (not actual math formulas)
        if (/^[A-Za-z0-9_/\s]+$/.test(formula.trim()) && !/[\\^_{}]/.test(formula)) {
            return '<code>' + formula.trim().replace(/\\ /g, ' ') + '</code>';
        }
        try {
            return katex.renderToString(formula.trim(), { displayMode: false, throwOnError: false });
        } catch (e) {
            return '<code>' + formula.trim().replace(/\\ /g, ' ') + '</code>';
        }
    });
    return html;
}

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
    // If URL has session param, switch to that session
    const params = new URLSearchParams(window.location.search);
    const sid = params.get('session');
    if (sid) {
        switchSession(sid);
    }
    // Click overlay to close sidebar
    const overlay = document.getElementById('sidebarOverlay');
    if (overlay) {
        overlay.addEventListener('click', closeSidebar);
    }
});

// Re-render dynamic (JS-generated) content when language changes
window.addEventListener('langchange', () => {
    if (localStorage.getItem('tenant_id') && localStorage.getItem('api_key')) {
        loadSessions();
        renderIndustrySelector();
    }
});

// ===== Industry Management =====
async function loadIndustries() {
    console.log('[INDUSTRY] [INDUSTRY] Loading industry list......');
    try {
        const res = await apiFetch('/industries');
        if (!res.ok) {
            console.error('[INDUSTRY] [INDUSTRY] API request failed:', res.status);
            return;
        }
        const data = await res.json();
        console.log('[INDUSTRY] [INDUSTRY] API returned:', data);
        availableIndustries = data.industries || [];
        console.log('[INDUSTRY] [INDUSTRY] Available industries:', availableIndustries.map(i => i.id));
        renderIndustrySelector();
    } catch (e) {
        console.error('[INDUSTRY] [INDUSTRY] Failed to load industry list:', e);
    }
}

function renderIndustrySelector() {
    const select = document.getElementById('industrySelect');
    if (!select) {
        console.error('[INDUSTRY] [INDUSTRY] Cannot find industrySelect element');
        return;
    }

    // Keep auto option (translated)
    let html = `<option value="auto">${escapeHtml(__('industry.auto'))}</option>`;
    let count = 0;
    for (const ind of availableIndustries) {
        if (ind.id === 'generic') continue; // Hide generic option
        html += `<option value="${ind.id}">${ind.name}</option>`;
        count++;
    }
    select.innerHTML = html;
    select.value = currentIndustry;
    console.log(`[INDUSTRY] [INDUSTRY] Rendered ${count} industry options`);
}

function switchIndustry(industry) {
    currentIndustry = industry;
    const hint = document.getElementById('industryHint');
    if (hint) {
        if (industry === 'auto') {
            hint.textContent = __('industry.autoHint');
        } else {
            const ind = availableIndustries.find(i => i.id === industry);
            hint.textContent = ind ? __('industry.fixedHint') + ind.name : __('industry.fixed');
        }
    }
    // If there is a current session, update its industry setting
    if (currentSessionId && industry !== 'auto') {
        // Optional: call API to update session industry
        console.log(`Session ${currentSessionId} industry set to: ${industry}`);
    }
}

// ===== Sidebar Toggle =====
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    sidebar.classList.toggle('open');
    if (overlay) overlay.classList.toggle('active');
}

function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebarOverlay');
    sidebar.classList.remove('open');
    if (overlay) overlay.classList.remove('active');
}

// ===== Session Management =====
async function loadSessions() {
    try {
        const res = await apiFetch('/chat/sessions');
        const data = await res.json();
        _allSessions = data.sessions || [];
        renderSessionList(_allSessions);
    } catch (e) {
        console.error('Load sessions failed:', e);
        renderSessionList([]);
    }
}

// ===== Session list rendering, search & rename =====
let _allSessions = [];

function filterSessions(query) {
    const q = (query || '').trim().toLowerCase();
    if (!q) { renderSessionList(_allSessions); return; }
    renderSessionList(_allSessions.filter(s => (s.title || '').toLowerCase().includes(q)));
}

// Relative time for recent items, absolute date for older ones.
function formatTime(isoStr) {
    if (!isoStr) return '';
    // SQLite CURRENT_TIMESTAMP is UTC without a zone marker
    const d = new Date(/Z|[+-]\d{2}:?\d{2}$/.test(isoStr) ? isoStr : isoStr.replace(' ', 'T') + 'Z');
    if (isNaN(d)) return '';
    const diffMin = Math.floor((Date.now() - d.getTime()) / 60000);
    if (diffMin < 1) return __('time.justNow');
    if (diffMin < 60) return diffMin + __('time.minAgo');
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return diffHr + __('time.hrAgo');
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return diffDay + __('time.dayAgo');
    return d.toLocaleDateString();
}

async function renameSession(sessionId, oldTitle) {
    const title = prompt(__('chat.renamePrompt'), oldTitle || '');
    if (title === null) return; // cancelled
    const trimmed = title.trim();
    if (!trimmed || trimmed === oldTitle) return;
    try {
        const res = await apiFetch(`/chat/sessions/${sessionId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: trimmed.slice(0, 100) })
        });
        if (!res.ok) {
            showToast(__('misc.error') + ': ' + res.status, 'error');
            return;
        }
        await loadSessions();
    } catch (e) {
        showToast(__('misc.error') + ': ' + e.message, 'error');
    }
}

function renderSessionList(sessions) {
    const list = document.getElementById('sessionList');
    if (sessions.length === 0) {
        list.innerHTML = '<div style="padding:20px;text-align:center;color:#9ca3af;font-size:13px;">No conversations yet</div>';
        return;
    }
    list.innerHTML = sessions.map(s => `
        <div class="session-item ${s.id === currentSessionId ? 'active' : ''}" data-id="${s.id}" onclick="switchSession('${s.id}')">
            <i class="fas fa-comment"></i>
            <span class="session-body">
                <span class="session-title">${escapeHtml(s.title)}</span>
                <span class="session-meta">${formatTime(s.updated_at)}</span>
            </span>
            <span class="session-rename" onclick="event.stopPropagation();renameSession('${s.id}', decodeURIComponent('${encodeURIComponent(s.title || '')}'))" title="${__('chat.rename')}">
                <i class="fas fa-pen"></i>
            </span>
            <span class="session-delete" onclick="event.stopPropagation();deleteSession('${s.id}')">
                <i class="fas fa-trash-alt"></i>
            </span>
        </div>
    `).join('');
}

function createNewSession() {
    // Lazy creation: only reset local state here; the session row is created
    // by the auto-create branch in sendMessage() on the first message
    currentSessionId = null;
    showWelcome();
    window.history.replaceState({}, '', '/');
    loadSessions(); // clear active highlight in the list
}

async function switchSession(sessionId) {
    if (sessionId === currentSessionId) return;
    currentSessionId = sessionId;
    window.history.replaceState({}, '', `?session=${sessionId}`);
    await loadSessions();
    await loadMessages(sessionId);
}

async function loadMessages(sessionId) {
    try {
        const res = await apiFetch(`/chat/sessions/${sessionId}/messages`);
        if (!res.ok) {
            showWelcome();
            return;
        }
        const data = await res.json();
        const msgs = data.messages || [];
        sessionMessages = msgs;

        if (msgs.length === 0) {
            showWelcome();
            return;
        }

        // Hide welcome page, show message area
        document.getElementById('welcomeScreen').style.display = 'none';
        const chatContainer = document.getElementById('chatMessages');
        chatContainer.style.display = 'flex';
        chatContainer.innerHTML = '';

        msgs.forEach(msg => {
            let sources = msg.sources;
            if (typeof sources === 'string') {
                try { sources = JSON.parse(sources); } catch { sources = null; }
            }
            appendMessageToDOM(msg.role, msg.content, sources, null, null, msg.created_at);
        });
        
        scrollToBottom();
    } catch (e) {
        console.error('Load messages failed:', e);
        showWelcome();
    }
}

async function deleteSession(sessionId) {
    if (!await uiConfirm(__('chat.confirmDelete'), { danger: true })) return;
    try {
        await apiFetch(`/chat/sessions/${sessionId}`, { method: 'DELETE' });
        if (currentSessionId === sessionId) {
            currentSessionId = null;
            showWelcome();
            window.history.replaceState({}, '', '/');
        }
        await loadSessions();
    } catch (e) {
        console.error('Delete session failed:', e);
        showToast(__('misc.error'), 'error');
    }
}

// ===== Message Sending =====
function handleInputKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

function autoResize(textarea) {
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

function sendExample(text) {
    document.getElementById('messageInput').value = text;
    sendMessage();
}

async function sendMessage() {
    const input = document.getElementById('messageInput');
    const text = input.value.trim();
    if (!text || isLoading) return;

    // Hide welcome page, show message area
    document.getElementById('welcomeScreen').style.display = 'none';
    document.getElementById('chatMessages').style.display = 'flex';

    // Show user message
    appendMessageToDOM('user', text, null, null, null, new Date().toISOString());
    input.value = '';
    input.style.height = 'auto';
    scrollToBottom();

    // Show loading state
    const loadingId = appendLoadingMessage();
    isLoading = true;
    document.getElementById('sendBtn').disabled = true;

    try {
        // If no current session, create one first
        if (!currentSessionId) {
            const sessionRes = await apiFetch('/chat/sessions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: __('app.newChat'), industry: currentIndustry })
            });
            const sessionData = await sessionRes.json();
            if (sessionData.id) {
                currentSessionId = sessionData.id;
                window.history.replaceState({}, '', `?session=${sessionData.id}`);
                await loadSessions();
            }
        }

        const chatHistoryForRequest = sessionMessages.slice(-10).map(m => ({ role: m.role, content: m.content }));
        const payload = {
            query: text,
            session_id: currentSessionId,
            industry: currentIndustry,
            chat_history: chatHistoryForRequest
        };
        const data = await runQuery(payload, loadingId);

        // Remove loading state
        removeLoadingMessage(loadingId);
        if (!data) return;  // error already rendered / login modal shown

        // V6 /query does not return session_id, frontend manages it
        // Show assistant message (V6 has no debug_info / citation_map)
        appendMessageToDOM('assistant', data.answer, data.sources, null, null, new Date().toISOString());

        // Update local message history
        sessionMessages.push({ role: 'user', content: text });
        sessionMessages.push({ role: 'assistant', content: data.answer });

    } catch (e) {
        removeLoadingMessage(loadingId);
        appendMessageToDOM('assistant', `❌ ${__('chat.error')}: ${e.message}`);
    } finally {
        isLoading = false;
        document.getElementById('sendBtn').disabled = false;
        scrollToBottom();
    }
}

// ===== Query transport: SSE stream with legacy fallback =====
// Primary path consumes /query/stream stage events so the user sees the
// pipeline advancing (planning → retrieving → generating). Falls back to
// the plain /query endpoint when the stream is unavailable (old backend,
// proxy buffering), so functionality never depends on streaming working.
async function runQuery(payload, loadingId) {
    try {
        return await runQueryStream(payload, loadingId);
    } catch (e) {
        if (e && e.handled) return null;  // error already rendered in the chat
        console.warn('[query] stream failed, falling back to /query:', e);
        return await runQueryLegacy(payload);
    }
}

async function runQueryStream(payload, loadingId) {
    const res = await fetch(`${API_BASE}/query/stream`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
        body: JSON.stringify(payload)
    });
    if (res.status === 401) {
        handleAuthExpired();
        const err = new Error('401');
        err.handled = true;
        throw err;
    }
    if (!res.ok) {
        if (res.status === 404) throw new Error('stream endpoint missing');  // legacy fallback
        const detail = await readErrorDetail(res);
        appendMessageToDOM('assistant', `❌ ${__('chat.error')}: ${detail}`);
        const err = new Error(detail);
        err.handled = true;
        throw err;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result = null;
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            if (!frame.startsWith('data: ')) continue;  // keep-alive comments
            let ev;
            try { ev = JSON.parse(frame.slice(6)); } catch { continue; }
            if (ev.stage === 'result') {
                result = ev.result;
            } else if (ev.stage === 'error') {
                appendMessageToDOM('assistant', `❌ ${__('chat.error')}: ${ev.detail || ''}`);
                const err = new Error(ev.detail || 'stream error');
                err.handled = true;
                throw err;
            } else if (ev.stage) {
                updateLoadingStage(loadingId, __('stage.' + ev.stage));
            }
        }
    }
    if (!result) throw new Error('stream ended without a result frame');  // fallback
    return result;
}

async function runQueryLegacy(payload) {
    try {
        const res = await apiFetch('/query', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            const detail = await readErrorDetail(res);
            appendMessageToDOM('assistant', `❌ ${__('chat.error')}: ${detail}`);
            return null;
        }
        return await res.json();
    } catch (e) {
        if (e && e.handled) return null;  // 401: login modal already shown
        appendMessageToDOM('assistant', `❌ ${__('chat.error')}: ${e.message}`);
        return null;
    }
}

// ===== Page Image Viewer =====
// /images/* requires Authorization headers, which neither <img src> nor
// window.open can send — fetch as an authenticated blob and hand out an
// object URL instead. Object URLs are cached per source URL.
const _imageBlobCache = new Map();

async function fetchProtectedImage(url) {
    if (_imageBlobCache.has(url)) return _imageBlobCache.get(url);
    try {
        const res = await fetch(url, { headers: getAuthHeaders() });
        if (!res.ok) {
            console.warn('[IMAGE] load failed:', url, res.status);
            return null;
        }
        const blobUrl = URL.createObjectURL(await res.blob());
        _imageBlobCache.set(url, blobUrl);
        return blobUrl;
    } catch (e) {
        console.error('[IMAGE] load error:', url, e);
        return null;
    }
}

// ===== Image Lightbox =====
// In-page viewer for tenant-scoped images — keeps reading context instead of
// opening a bare blob in a new tab. Esc / backdrop click / ✕ closes it.
let _lightboxReturnFocus = null;

function openLightbox(src, caption) {
    let overlay = document.getElementById('imageLightbox');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'imageLightbox';
        overlay.className = 'lightbox-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.innerHTML = `
            <button class="lightbox-close"><i class="fas fa-times"></i></button>
            <div class="lightbox-caption"></div>
            <img alt="">`;
        overlay.querySelector('.lightbox-close').setAttribute('aria-label', __('misc.close'));
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('.lightbox-close')) closeLightbox();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlay.classList.contains('show')) closeLightbox();
        });
        document.body.appendChild(overlay);
    }
    overlay.querySelector('img').src = src;
    overlay.querySelector('.lightbox-caption').textContent = caption || '';
    overlay.classList.add('show');
    // Move focus into the dialog so Esc/screen-reader users land somewhere sane
    _lightboxReturnFocus = document.activeElement;
    overlay.querySelector('.lightbox-close').focus();
}

function closeLightbox() {
    const overlay = document.getElementById('imageLightbox');
    if (overlay) overlay.classList.remove('show');
    if (_lightboxReturnFocus && _lightboxReturnFocus.focus) {
        _lightboxReturnFocus.focus();
        _lightboxReturnFocus = null;
    }
}

async function openProtectedImage(url, caption) {
    const blobUrl = await fetchProtectedImage(url);
    if (blobUrl) openLightbox(blobUrl, caption || url);
}

function openPageImage(docId, pageNum) {
    openProtectedImage(`/images/${docId}_p${pageNum}.png`, `${docId} · Page ${pageNum}`);
}

// Delegated citation clicks: processCitations emits data-* attributes (no
// inline JS), and a single document-level listener routes them to the
// tenant-scoped image viewer.
document.addEventListener('click', function(e) {
    const el = e.target && e.target.closest ? e.target.closest('sup.page-cite[data-doc-id]') : null;
    if (!el) return;
    const page = parseInt(el.getAttribute('data-page'), 10);
    if (!page || page < 1) return;
    openPageImage(el.getAttribute('data-doc-id'), page);
});

// Resolve data-img-url placeholders inside a freshly rendered message
// (chart thumbnails) with limited concurrency
function hydrateProtectedImages(container) {
    const imgs = Array.from(container.querySelectorAll('img[data-img-url]'));
    const CONCURRENCY = 3;
    let idx = 0;
    async function worker() {
        while (idx < imgs.length) {
            const img = imgs[idx++];
            const blobUrl = await fetchProtectedImage(img.dataset.imgUrl);
            if (blobUrl) img.src = blobUrl;
            else img.alt = (img.alt || 'image') + ' (unavailable)';
        }
    }
    for (let i = 0; i < Math.min(CONCURRENCY, imgs.length); i++) worker();
}

// Delegated handlers for protected images: chart thumbnails and page-image
// links open the in-page lightbox (keeps the conversation in context)
document.addEventListener('click', (e) => {
    const thumb = e.target.closest('img.chart-thumb');
    if (thumb && thumb.src) {
        const item = thumb.closest('.chart-item');
        const cap = item && item.querySelector('.chart-caption');
        openLightbox(thumb.src, (cap && cap.textContent) || thumb.alt || '');
        return;
    }
    const link = e.target.closest('a.page-img-link[data-img-url]');
    if (link) {
        e.preventDefault();
        openProtectedImage(link.dataset.imgUrl, link.textContent);
    }
});

function findDocIdForPage(sources, pageNum) {
    if (!sources) return null;
    for (const src of sources) {
        const pages = src.pages || [];
        if (pages.includes(pageNum)) {
            return src.doc_id || null;
        }
    }
    return null;
}

function processCitations(html, citationMap, sources) {
    if (!html) return html;
    
    // Protect HTML tags first to avoid false matches in later processing
    const tags = [];
    html = html.replace(/<[^>]+>/g, (match) => {
        tags.push(match);
        return `__HTML_TAG_${tags.length - 1}__`;
    });
    
    // 1. Format 1: [^第N页^] / [^页N^] / [^第N^] / [^N页^]
    html = html.replace(/\[\^(第)?\s*(\d+)\s*(页)?\^\]|\[\^(页)?\s*(\d+)\s*\^\]/g, (match, hasDi, pageNum1, hasYe, hasYe2, pageNum2) => {
        const pageNum = pageNum1 || pageNum2;
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" data-doc-id="${docId}" data-page="${pageNum}" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 2. Format 2: [^N^] (standard, no "第/页")
    html = html.replace(/\[\^(\d+)\^\]/g, (match, pageNum) => {
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" data-doc-id="${docId}" data-page="${pageNum}" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 3. Format 3: [^N] without trailing ^
    html = html.replace(/\[\^(\d+)\]/g, (match, pageNum) => {
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" data-doc-id="${docId}" data-page="${pageNum}" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 4. Format 4: %%CITE_N%% (pre-protected standard)
    html = html.replace(/%%CITE_(\d+)%%/g, (match, pageNum) => {
        const docIds = citationMap ? citationMap[pageNum] : null;
        const docId = docIds && docIds.length > 0 ? docIds[0] : findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" data-doc-id="${docId}" data-page="${pageNum}" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });

    // 5. Format 5: %%CITE_SHORTID_N%% (pre-protected [^DOC_N^])
    html = html.replace(/%%CITE_(\w+?)_(\d+)%%/g, (match, shortId, pageNum) => {
        return `<sup class="page-cite" title="${shortId} Page ${pageNum}">[${shortId}_${pageNum}]</sup>`;
    });

    // 6. Fallback: handle "文档第 N 页" / "第 N 页表格" etc. in plain text
    html = html.replace(/(文档)?第\s*(\d+)\s*页(表格|图|节)?/g, (match, docPrefix, pageNum, suffix) => {
        const suffixStr = suffix || '';
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `${suffixStr}<sup class="page-cite" data-doc-id="${docId}" data-page="${pageNum}" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return match;
    });
    
    // Restore HTML tags
    html = html.replace(/__HTML_TAG_(\d+)__/g, (match, idx) => tags[parseInt(idx)]);
    return html;
}

// ===== DOM Operations =====
function appendMessageToDOM(role, content, sources, debugInfo, citationMap, createdAt) {
    const container = document.getElementById('chatMessages');
    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;

    const avatarIcon = role === 'user' ? 'fa-user' : 'fa-robot';

    let sourcesHtml = '';
    if (sources && sources.length > 0) {
        const sourceLinks = sources.map(s => {
            const pages = s.pages ? s.pages.join(',') : '';
            return `<a href="#" title="${escapeHtml(s.filename)} Page ${pages}">${escapeHtml(s.title || s.filename)}</a>`;
        }).join('');

        // V4: Collect all charts and page images
        let allCharts = [];
        let allPageImages = [];
        sources.forEach(s => {
            if (s.charts && s.charts.length > 0) {
                allCharts = allCharts.concat(s.charts);
            }
            if (s.page_images && s.page_images.length > 0) {
                allPageImages = allPageImages.concat(s.page_images);
            }
        });

        let chartsHtml = '';
        if (allCharts.length > 0) {
            const chartItems = allCharts.map(c => {
                const imgTag = c.image_url
                    ? `<img data-img-url="${escapeHtml(c.image_url)}" alt="${escapeHtml(c.chart_type)}" class="chart-thumb" loading="lazy">`
                    : '';
                const desc = c.description ? `<div class="chart-desc">${escapeHtml(c.description)}</div>` : '';
                const caption = c.caption ? `<div class="chart-caption">${escapeHtml(c.caption)}</div>` : '';
                return `<div class="chart-item">${imgTag}${caption}${desc}</div>`;
            }).join('');
            chartsHtml = `
                <details class="charts-panel">
                    <summary>📊 Related Charts (${allCharts.length})</summary>
                    <div class="charts-grid">${chartItems}</div>
                </details>
            `;
        }

        let pageImagesHtml = '';
        if (allPageImages.length > 0) {
            const imgLinks = allPageImages.map(p =>
                `<a href="#" class="page-img-link" data-img-url="${escapeHtml(p.url)}">Page ${p.page_num} Image</a>`
            ).join(' ');
            pageImagesHtml = `<div class="page-images-bar"><i class="fas fa-image"></i> ${imgLinks}</div>`;
        }

        sourcesHtml = `<div class="message-sources"><i class="fas fa-bookmark"></i> Source: ${sourceLinks}</div>${pageImagesHtml}${chartsHtml}`;
    }

    // V4: Debug info panel (collapsible)
    let debugHtml = '';
    if (debugInfo && debugInfo.decomposition) {
        const decomp = debugInfo.decomposition;
        const trace = debugInfo.retrieval_trace || [];

        let traceHtml = '';
        for (const t of trace) {
            const sections = t.top_sections ? t.top_sections.map(s => `<span class="debug-section">${escapeHtml(s)}</span>`).join(' ') : '';
            traceHtml += `
                <div class="debug-trace-item">
                    <div class="debug-trace-query">${escapeHtml(t.sub_query)}</div>
                    <div class="debug-trace-meta">
                        Recalled ${t.retrieved_count} pages | context ${t.context_chars} chars | Top: ${sections}
                    </div>
                </div>
            `;
        }

        debugHtml = `
            <details class="debug-panel">
                <summary>🔧 Query Decomposition (${decomp.count} sub-tasks)</summary>
                <div class="debug-content">
                    <div class="debug-decomp">
                        ${decomp.sub_queries.map(sq => `<div class="debug-subq">• ${escapeHtml(sq.query)} <span class="debug-purpose">(${escapeHtml(sq.purpose)})</span></div>`).join('')}
                    </div>
                    <div class="debug-trace">
                        <div class="debug-trace-title">Retrieval Details:</div>
                        ${traceHtml}
                    </div>
                </div>
            </details>
        `;
    }

    // Use marked to render Markdown, protect against undefined/null
    let htmlContent;
    if (role === 'assistant') {
        let safeContent = content || '';
        // Pre-protect all common citation formats to prevent marked treating them as Markdown
        safeContent = safeContent.replace(/\[\^(\w+?_\d+)\^\]/g, '%%CITE_$1%%');
        safeContent = safeContent.replace(/\[\^(\d+)\^\]/g, '%%CITE_$1%%');
        safeContent = safeContent.replace(/\[\^(\d+)\]/g, '%%CITE_$1%%');
        // Bare [N] counts as a citation only when evidence maps it to a real
        // source page; otherwise (e.g. "[2023]", "[1]" in prose) leave it as text
        safeContent = safeContent.replace(/\[(\d+)\](?!\()/g, (m, n) => {
            const mapped = (citationMap && citationMap[n]) || findDocIdForPage(sources, parseInt(n));
            return mapped ? `%%CITE_${n}%%` : m;
        });
        if (window.DOMPurify) {
            htmlContent = DOMPurify.sanitize(marked.parse(safeContent));
        } else {
            // Fail closed: the vendored sanitizer is unavailable (partial
            // deploy / corrupted cache). Degrade to plain text — never to
            // unsanitized HTML injection.
            htmlContent = '<p>' + escapeHtml(content || '').replace(/\n/g, '<br>') + '</p>';
        }
        htmlContent = renderLatex(htmlContent);
        // Process page citation badges
        htmlContent = processCitations(htmlContent, citationMap, sources);
    } else {
        htmlContent = escapeHtml(content || '');
    }

    msgDiv.innerHTML = `
        <div class="message-avatar"><i class="fas ${avatarIcon}"></i></div>
        <div class="message-content">${htmlContent}${sourcesHtml}${debugHtml}
            <div class="message-meta">
                <span class="message-time">${createdAt ? formatTime(createdAt) : ''}</span>
                <button class="message-copy" title="${__('misc.copy')}" aria-label="${__('misc.copy')}"><i class="fas fa-copy"></i></button>
            </div>
        </div>
    `;
    // Keep the raw markdown for the copy action (rendered HTML loses structure)
    if (content) msgDiv.dataset.rawContent = content;
    const copyBtn = msgDiv.querySelector('.message-copy');
    copyBtn.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(msgDiv.dataset.rawContent || msgDiv.querySelector('.message-content').textContent);
            showToast(__('misc.copied'), 'success');
        } catch (e) {
            showToast(__('misc.copyManual'), 'info');
        }
    });
    container.appendChild(msgDiv);
    hydrateProtectedImages(msgDiv);
    scrollToBottom();
}

function appendLoadingMessage() {
    const container = document.getElementById('chatMessages');
    const id = 'loading-' + Date.now();
    const div = document.createElement('div');
    div.id = id;
    div.className = 'message assistant';
    div.innerHTML = `
        <div class="message-avatar"><i class="fas fa-robot"></i></div>
        <div class="message-content">
            <div class="loading-dots"><span></span><span></span><span></span></div>
            <div class="loading-stage"></div>
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return id;
}

function updateLoadingStage(loadingId, stageText) {
    const el = document.querySelector('#' + loadingId + ' .loading-stage');
    if (el && stageText) el.textContent = stageText;
}

function removeLoadingMessage(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

function showWelcome() {
    document.getElementById('welcomeScreen').style.display = 'flex';
    document.getElementById('chatMessages').style.display = 'none';
    document.getElementById('chatMessages').innerHTML = '';
    sessionMessages = [];
}

function scrollToBottom() {
    const container = document.getElementById('chatMessages');
    if (container) {
        container.scrollTop = container.scrollHeight;
    }
}

// ===== Service Status Monitoring =====
const _serviceStatusPoller = createIntervalPoller(refreshServiceStatus, 10000);
let currentServiceStatus = {};

async function refreshServiceStatus() {
    try {
        const res = await apiFetch('/services/status');
        if (!res.ok) return;
        const data = await res.json();
        currentServiceStatus = data.services || {};
        currentServiceStatus._mode = data.mode || '';
        renderServiceStatus();
        document.getElementById('statusLastCheck').textContent = new Date().toLocaleTimeString();
    } catch (e) {
        console.error('[STATUS] [STATUS] Failed to refresh service status:', e);
    }
}

function renderServiceStatus() {
    const container = document.getElementById('statusItems');
    if (!container) return;

    const items = [];
    const order = ['llm', 'embedding'];
    const isExternal = currentServiceStatus._mode === 'external';

    for (const key of order) {
        const s = currentServiceStatus[key];
        if (!s) continue;

        // In external mode, still show actual health status from the API
        const dotClass = s.status === 'ok' ? 'ok' :
                         s.status === 'degraded' ? 'degraded' :
                         s.status === 'stopped' ? 'stopped' :
                         s.status === 'unreachable' ? 'unreachable' : 'error';

        const showFix = !isExternal && s.status !== 'ok' && !s.shared;
        const fixBtn = showFix
            ? `<button class="status-fix-btn" onclick="fixService('${key}')" id="fix-btn-${key}">${__('misc.confirm')}</button>`
            : '';

        const meta = isExternal
            ? (s.status === 'ok' ? s.url : (s.last_error || s.status))
            : (s.pid ? `PID:${s.pid}` : __('admin.docs.status.error'));
        const uptime = s.uptime_seconds
            ? ` ${formatUptime(s.uptime_seconds)}`
            : '';

        items.push(`
            <div class="status-item" title="${s.last_error || ''}">
                <span class="status-dot ${dotClass}"></span>
                <span class="status-name">${s.name}</span>
                <span class="status-meta">${meta}${uptime}</span>
                ${fixBtn}
            </div>
        `);
    }

    container.innerHTML = items.join('');
}

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds/60)}m`;
    if (seconds < 86400) return `${Math.round(seconds/3600)}h`;
    return `${Math.round(seconds/86400)}d`;
}

async function fixService(serviceKey) {
    const btn = document.getElementById(`fix-btn-${serviceKey}`);
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    }

    try {
        const res = await apiFetch(`/services/${serviceKey}/restart`, {
            method: 'POST',
        });
        const data = await res.json();

        if (res.ok && data.success) {
            showToast(`${serviceKey} ${__('status.restartOk')} (PID: ${data.pid})`, 'success');
        } else {
            showToast(`${serviceKey} ${__('status.restartFailed')}: ${data.error || data.detail || 'Unknown error'}`, 'error');
        }
    } catch (e) {
        showToast(`${serviceKey} ${__('status.restartFailed')}: ${e.message}`, 'error');
    } finally {
        // Delayed status refresh
        setTimeout(refreshServiceStatus, 3000);
    }
}

// ===== Service Logs Modal =====
function openServiceLogs() {
    let overlay = document.getElementById('serviceLogsOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'serviceLogsOverlay';
        overlay.className = 'service-logs-overlay';
        overlay.innerHTML = `
            <div class="service-logs-box">
                <div class="service-logs-header">
                    <h3><i class="fas fa-file-alt"></i> ${__('status.logsTitle')}</h3>
                    <button class="service-logs-close" onclick="closeServiceLogs()">&times;</button>
                </div>
                <div class="service-logs-tabs" id="logsTabs"></div>
                <div class="service-logs-body">
                    <pre class="service-logs-pre" id="logsPre">Loading...</pre>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
    }
    overlay.classList.add('show');
    loadServiceLogs('llm');
}

function closeServiceLogs() {
    const overlay = document.getElementById('serviceLogsOverlay');
    if (overlay) overlay.classList.remove('show');
}

let currentLogService = 'llm';

async function loadServiceLogs(serviceKey) {
    currentLogService = serviceKey;
    const tabsEl = document.getElementById('logsTabs');
    const preEl = document.getElementById('logsPre');

    // Render tabs
    const services = ['llm', 'embedding'];
    tabsEl.innerHTML = services.map(s => `
        <button class="service-logs-tab ${s === serviceKey ? 'active' : ''}"
            onclick="loadServiceLogs('${s}')">${s.toUpperCase()}</button>
    `).join('');

    preEl.textContent = __('misc.loading');
    try {
        // /services/events is the real endpoint (admin-gated server-side);
        // it returns {events: [...]} with datetime/event_type/message fields
        const res = await apiFetch(`/services/events?service=${serviceKey}&limit=200`);
        if (!res.ok) {
            preEl.textContent = __('misc.error') + ': HTTP ' + res.status;
            return;
        }
        const data = await res.json();
        const events = data.events || [];
        if (events.length === 0) {
            preEl.textContent = __('misc.empty');
            return;
        }
        const text = events.map(ev =>
            `${ev.datetime || ''}  [${(ev.event_type || 'event').toUpperCase()}]  ${ev.message || ''}`
        ).join('\n');
        preEl.innerHTML = colorizeLog(text);
    } catch (e) {
        preEl.textContent = __('misc.error') + ': ' + e.message;
    }
}

function colorizeLog(text) {
    return escapeHtml(text)
        .replace(/^(INFO|info):?.*$/gim, '<span class="log-info">$&</span>')
        .replace(/^(WARN|WARNING|warn|warning):?.*$/gim, '<span class="log-warn">$&</span>')
        .replace(/^(ERROR|error|ERR|err|FAIL|fail):?.*$/gim, '<span class="log-error">$&</span>')
        .replace(/^(SUCCESS|success|OK|ok):?.*$/gim, '<span class="log-success">$&</span>');
}

function initServiceStatusBar() {
    const role = localStorage.getItem('user_role');
    const bar = document.getElementById('serviceStatusBar');
    const userEl = document.getElementById('statusUser');
    const username = localStorage.getItem('username');

    if (userEl && username) {
        userEl.textContent = '👤 ' + username;
    }

    // Show/hide user management link
    const mgmtLink = document.getElementById('userMgmtLink');
    if (mgmtLink) {
        mgmtLink.style.display = role === 'admin' ? 'flex' : 'none';
    }

    // Logs modal is backed by /services/events which is admin-gated
    // server-side; hide the entry point for non-admin users
    const logsBtn = document.querySelector('.logs-btn');
    if (logsBtn) {
        logsBtn.style.display = role === 'admin' ? '' : 'none';
    }

    // Show service status bar for all authenticated users
    if (bar) {
        bar.style.display = 'flex';
        _serviceStatusPoller.start();
    }
}

// ===== Logout =====
async function doLogout() {
    try {
        await fetch('/api/v1/logout', { method: 'POST', headers: getAuthHeaders() });
    } catch (e) {}
    clearAuthStorage();
    location.reload();
}

// ===== User Management =====
// Event delegation for user-table action buttons: parameters travel via
// data-* attributes instead of inline onclick string interpolation (XSS-safe)
document.addEventListener('click', (e) => {
    const btn = e.target.closest('#userTableBody button[data-action]');
    if (!btn) return;
    const { action, id, name } = btn.dataset;
    if (action === 'regenerate') regenerateKey(id, name);
    else if (action === 'delete') deleteUser(id, name);
});

function openUserMgmt() {
    document.getElementById('userMgmtModal').style.display = 'flex';
    loadUsers();
}

function closeUserMgmt() {
    document.getElementById('userMgmtModal').style.display = 'none';
}

function renderExpiry(expiresAt) {
    if (!expiresAt) return `<span style="color:#059669;font-size:12px;">${__('usermgmt.never')}</span>`;
    const exp = new Date(expiresAt);
    const days = Math.ceil((exp - new Date()) / 86400000);
    const dateStr = exp.toISOString().slice(0, 10);
    if (days < 0) {
        return `<span style="color:#dc2626;font-size:12px;font-weight:600;">${__('usermgmt.expired')}</span><br><span style="font-size:11px;color:#9ca3af;">${dateStr}</span>`;
    }
    const color = days <= 7 ? '#dc2626' : (days <= 30 ? '#d97706' : '#059669');
    return `<span style="color:${color};font-size:12px;font-weight:600;">${days}${__('usermgmt.daysLeft')}</span><br><span style="font-size:11px;color:#9ca3af;">${dateStr}</span>`;
}

async function loadUsers() {
    const tbody = document.getElementById('userTableBody');
    tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;color:#9ca3af;">Loading...</td></tr>';
    try {
        const res = await apiFetch('/admin/users');
        if (!res.ok) {
            tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;color:#dc2626;">Load failed</td></tr>';
            return;
        }
        const data = await res.json();
        const users = data.users || [];
        if (users.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;color:#9ca3af;">No Users</td></tr>';
            return;
        }
        tbody.innerHTML = users.map(u => `
            <tr>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;">${escapeHtml(u.username)}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;">
                    <span class="role-badge ${u.role}">${u.role === 'admin' ? __('usermgmt.role.admin') : __('usermgmt.role.user')}</span>
                </td>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-size:11px;color:#6b7280;">${escapeHtml(u.tenant_id)}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-size:11px;color:#6b7280;font-family:monospace;">${escapeHtml(u.api_key)}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;">${renderExpiry(u.api_key_expires_at)}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;text-align:center;white-space:nowrap;">
                    <button data-action="regenerate" data-id="${escapeHtml(u.id)}" data-name="${escapeHtml(u.username)}" title="Rotate API Key" style="background:#2563eb;color:white;border:none;border-radius:4px;padding:4px 8px;font-size:12px;cursor:pointer;margin-right:4px;"><i class="fas fa-sync-alt"></i> ${__('usermgmt.regenerate')}</button>
                    ${u.role !== 'admin' ? `<button data-action="delete" data-id="${escapeHtml(u.id)}" data-name="${escapeHtml(u.username)}" style="background:#dc2626;color:white;border:none;border-radius:4px;padding:4px 10px;font-size:12px;cursor:pointer;">${__('usermgmt.delete')}</button>` : ''}
                </td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="6" style="padding:20px;text-align:center;color:#dc2626;">Load Error</td></tr>';
    }
}

async function createUser() {
    const username = document.getElementById('newUserName').value.trim();
    const password = document.getElementById('newUserPassword').value.trim();
    const role = document.getElementById('newUserRole').value;
    const ttl = parseInt(document.getElementById('newUserTtl').value, 10);
    if (!username) {
        showToast(__('login.errorEmpty'), 'error');
        return;
    }
    try {
        const res = await apiFetch('/admin/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password: password || undefined, role, api_key_ttl_days: ttl })
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'), 'error');
            return;
        }
        showCredentials(__('usermgmt.creds.title'), [
            { label: __('login.username'), value: data.username },
            { label: __('login.password'), value: data.password },
            { label: 'API Key', value: data.api_key },
            { label: __('usermgmt.column.expires'), value: data.api_key_expires_at || __('usermgmt.never') }
        ], __('usermgmt.creds.note'));
        document.getElementById('newUserName').value = '';
        document.getElementById('newUserPassword').value = '';
        loadUsers();
    } catch (e) {
        showToast(__('misc.error') + ': ' + e.message, 'error');
    }
}

async function regenerateKey(userId, username) {
    if (!await uiConfirm(__('usermgmt.confirmRegenerate'), { danger: true })) return;
    try {
        const res = await apiFetch(`/admin/users/${userId}/regenerate-key`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'), 'error');
            return;
        }
        showCredentials(__('usermgmt.creds.title') + ' — ' + username, [
            { label: 'API Key', value: data.api_key },
            { label: __('usermgmt.column.expires'), value: data.api_key_expires_at || __('usermgmt.never') }
        ], __('usermgmt.creds.note'));
        loadUsers();
    } catch (e) {
        showToast(__('misc.error') + ': ' + e.message, 'error');
    }
}

async function deleteUser(userId, username) {
    if (!await uiConfirm(__('usermgmt.confirmDelete') + username + '?', { danger: true })) return;
    try {
        const res = await apiFetch(`/admin/users/${userId}`, {
            method: 'DELETE'
        });
        const data = await res.json();
        if (!res.ok) {
            showToast(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'), 'error');
            return;
        }
        showToast(__('usermgmt.delete') + ': ' + username, 'success');
        loadUsers();
    } catch (e) {
        showToast(__('misc.error') + ': ' + e.message, 'error');
    }
}