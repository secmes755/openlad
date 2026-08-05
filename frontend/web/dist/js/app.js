const API_BASE = '/api/v1';

let currentSessionId = null;
let isLoading = false;
let currentIndustry = 'auto';
let availableIndustries = [];
let sessionMessages = [];

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
                localStorage.clear();
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
        alert('Please enter username and password');
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
            alert('Login failed: ' + (data.detail || data.error || 'Unknown error'));
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
        alert('Login error: ' + e.message);
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

// ===== Industry Management =====
async function loadIndustries() {
    console.log('[INDUSTRY] [INDUSTRY] Loading industry list......');
    try {
        const res = await fetch(`${API_BASE}/industries`, { headers: getAuthHeaders() });
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

    // Keep auto option
    let html = '<option value="auto">Auto Detect</option>';
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
        const res = await fetch(`${API_BASE}/chat/sessions`, { headers: getAuthHeaders() });
        const data = await res.json();
        renderSessionList(data.sessions || []);
    } catch (e) {
        console.error('Load sessions failed:', e);
        renderSessionList([]);
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
            <span class="session-title">${escapeHtml(s.title)}</span>
            <span class="session-delete" onclick="event.stopPropagation();deleteSession('${s.id}')">
                <i class="fas fa-trash-alt"></i>
            </span>
        </div>
    `).join('');
}

async function createNewSession() {
    try {
        const res = await fetch(`${API_BASE}/chat/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({ title: __('app.newChat'), industry: currentIndustry })
        });
        if (!res.ok) {
            const text = await res.text();
            console.error('Create session HTTP error:', res.status, text);
            alert(__('chat.error') + ': HTTP ' + res.status);
            return;
        }
        const data = await res.json();
        if (!data.id) {
            console.error('Create session response missing id:', data);
            alert(__('chat.error') + ': invalid response format');
            return;
        }
        currentSessionId = data.id;
        sessionMessages = [];
        await loadSessions();
        showWelcome();
        window.history.replaceState({}, '', `?session=${data.id}`);
    } catch (e) {
        console.error('Create session failed:', e);
        alert(__('chat.error') + ': ' + e.message);
    }
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
        const res = await fetch(`${API_BASE}/chat/sessions/${sessionId}/messages`, { headers: getAuthHeaders() });
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
            appendMessageToDOM(msg.role, msg.content, sources, null, null);
        });
        
        scrollToBottom();
    } catch (e) {
        console.error('Load messages failed:', e);
        showWelcome();
    }
}

async function deleteSession(sessionId) {
    if (!confirm(__('usermgmt.confirmDelete') + __('usermgmt.confirmDeleteSuffix') + '?')) return;
    try {
        await fetch(`${API_BASE}/chat/sessions/${sessionId}`, { method: 'DELETE', headers: getAuthHeaders() });
        if (currentSessionId === sessionId) {
            currentSessionId = null;
            showWelcome();
            window.history.replaceState({}, '', '/');
        }
        await loadSessions();
    } catch (e) {
        console.error('Delete session failed:', e);
        alert(__('misc.error'));
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
    appendMessageToDOM('user', text);
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
            const sessionRes = await fetch(`${API_BASE}/chat/sessions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
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
        const res = await fetch(`${API_BASE}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({
                query: text,
                session_id: currentSessionId,
                industry: currentIndustry,
                chat_history: chatHistoryForRequest
            })
        });
        const data = await res.json();

        // Remove loading state
        removeLoadingMessage(loadingId);

        // V6 /query does not return session_id, frontend manages it
        // Show assistant message (V6 has no debug_info / citation_map)
        appendMessageToDOM('assistant', data.answer, data.sources, null, null);

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

// ===== Page Image Viewer =====
function openPageImage(docId, pageNum) {
    const url = `/images/${docId}_p${pageNum}.png`;
    window.open(url, '_blank');
}

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
            return `<sup class="page-cite" onclick="openPageImage('${docId}', ${pageNum})" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 2. Format 2: [^N^] (standard, no "第/页")
    html = html.replace(/\[\^(\d+)\^\]/g, (match, pageNum) => {
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" onclick="openPageImage('${docId}', ${pageNum})" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 3. Format 3: [^N] without trailing ^
    html = html.replace(/\[\^(\d+)\]/g, (match, pageNum) => {
        const docId = findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" onclick="openPageImage('${docId}', ${pageNum})" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return `<sup class="page-cite" title="Page ${pageNum}">[${pageNum}]</sup>`;
    });
    
    // 4. Format 4: %%CITE_N%% (pre-protected standard)
    html = html.replace(/%%CITE_(\d+)%%/g, (match, pageNum) => {
        const docIds = citationMap ? citationMap[pageNum] : null;
        const docId = docIds && docIds.length > 0 ? docIds[0] : findDocIdForPage(sources, parseInt(pageNum));
        if (docId) {
            return `<sup class="page-cite" onclick="openPageImage('${docId}', ${pageNum})" title="View page ${pageNum}">[${pageNum}]</sup>`;
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
            return `${suffixStr}<sup class="page-cite" onclick="openPageImage('${docId}', ${pageNum})" title="View page ${pageNum}">[${pageNum}]</sup>`;
        }
        return match;
    });
    
    // Restore HTML tags
    html = html.replace(/__HTML_TAG_(\d+)__/g, (match, idx) => tags[parseInt(idx)]);
    return html;
}

// ===== DOM Operations =====
function appendMessageToDOM(role, content, sources, debugInfo, citationMap) {
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
                    ? `<img src="${c.image_url}" alt="${escapeHtml(c.chart_type)}" class="chart-thumb" loading="lazy" onclick="window.open('${c.image_url}', '_blank')">`
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
                `<a href="${p.url}" target="_blank" class="page-img-link">Page ${p.page_num} Image</a>`
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
        safeContent = safeContent.replace(/\[(\d+)\](?!\()/g, '%%CITE_$1%%');
        htmlContent = marked.parse(safeContent);
        htmlContent = renderLatex(htmlContent);
        // Process page citation badges
        htmlContent = processCitations(htmlContent, citationMap, sources);
    } else {
        htmlContent = escapeHtml(content || '');
    }

    msgDiv.innerHTML = `
        <div class="message-avatar"><i class="fas ${avatarIcon}"></i></div>
        <div class="message-content">${htmlContent}${sourcesHtml}${debugHtml}</div>
    `;
    container.appendChild(msgDiv);
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
        </div>
    `;
    container.appendChild(div);
    scrollToBottom();
    return id;
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

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ===== Service Status Monitoring =====
let serviceStatusTimer = null;
let currentServiceStatus = {};

async function refreshServiceStatus() {
    try {
        const res = await fetch(`${API_BASE}/services/status`, { headers: getAuthHeaders() });
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
        const res = await fetch(`${API_BASE}/services/${serviceKey}/restart`, {
            method: 'POST',
            headers: getAuthHeaders(),
        });
        const data = await res.json();

        if (res.ok && data.success) {
            alert(`✅ ${serviceKey} restarted successfully\nNew PID: ${data.pid}`);
        } else {
            alert(`❌ ${serviceKey} restart failed\n${data.error || data.detail || 'Unknown error'}`);
        }
    } catch (e) {
        alert(`❌ ${serviceKey} restart error: ${e.message}`);
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
        const res = await fetch(`${API_BASE}/services/${serviceKey}/logs?lines=200`, {
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        preEl.innerHTML = colorizeLog(data.tail || __('misc.empty'));
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

    // Show service status bar for all authenticated users
    if (bar) {
        bar.style.display = 'flex';
        refreshServiceStatus();
        serviceStatusTimer = setInterval(refreshServiceStatus, 10000);
    }
}

// ===== Logout =====
async function doLogout() {
    try {
        await fetch('/api/v1/logout', { method: 'POST', headers: getAuthHeaders() });
    } catch (e) {}
    localStorage.clear();
    location.reload();
}

// ===== User Management =====
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
        const res = await fetch(`${API_BASE}/admin/users`, { headers: getAuthHeaders() });
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
                    <button onclick="regenerateKey('${u.id}', '${escapeHtml(u.username)}')" title="Rotate API Key" style="background:#2563eb;color:white;border:none;border-radius:4px;padding:4px 8px;font-size:12px;cursor:pointer;margin-right:4px;"><i class="fas fa-sync-alt"></i> ${__('usermgmt.regenerate')}</button>
                    ${u.role !== 'admin' ? `<button onclick="deleteUser('${u.id}', '${escapeHtml(u.username)}')" style="background:#dc2626;color:white;border:none;border-radius:4px;padding:4px 10px;font-size:12px;cursor:pointer;">${__('usermgmt.delete')}</button>` : ''}
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
        alert(__('login.errorEmpty'));
        return;
    }
    try {
        const res = await fetch(`${API_BASE}/admin/users`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({ username, password: password || undefined, role, api_key_ttl_days: ttl })
        });
        const data = await res.json();
        if (!res.ok) {
            alert(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'));
            return;
        }
        alert('OK\nUsername: ' + data.username + '\nPassword: ' + data.password + '\nAPI Key: ' + data.api_key + '\n' + __('usermgmt.column.expires') + ': ' + (data.api_key_expires_at || __('usermgmt.never')));
        document.getElementById('newUserName').value = '';
        document.getElementById('newUserPassword').value = '';
        loadUsers();
    } catch (e) {
        alert(__('misc.error') + ': ' + e.message);
    }
}

async function regenerateKey(userId, username) {
    if (!confirm(__('usermgmt.confirmRegenerate'))) return;
    try {
        const res = await fetch(`${API_BASE}/admin/users/${userId}/regenerate-key`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...getAuthHeaders() },
            body: JSON.stringify({})
        });
        const data = await res.json();
        if (!res.ok) {
            alert(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'));
            return;
        }
        alert(username + '\nNew API Key: ' + data.api_key + '\n' + __('usermgmt.column.expires') + ': ' + (data.api_key_expires_at || __('usermgmt.never')));
        loadUsers();
    } catch (e) {
        alert(__('misc.error') + ': ' + e.message);
    }
}

async function deleteUser(userId, username) {
    if (!confirm(__('usermgmt.confirmDelete') + username + '?')) return;
    try {
        const res = await fetch(`${API_BASE}/admin/users/${userId}`, {
            method: 'DELETE',
            headers: getAuthHeaders()
        });
        const data = await res.json();
        if (!res.ok) {
            alert(__('misc.error') + ': ' + (data.detail || data.error || 'Unknown error'));
            return;
        }
        alert(__('usermgmt.delete') + ': ' + username);
        loadUsers();
    } catch (e) {
        alert(__('misc.error') + ': ' + e.message);
    }
}