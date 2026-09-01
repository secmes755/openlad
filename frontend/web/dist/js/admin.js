/*
 * Admin page logic (extracted from admin.html inline <script>).
 * Requires i18n.js → common.js → ui.js loaded before this file.
 */

function checkAuth() {
    const tenantId = localStorage.getItem('tenant_id');
    const apiKey = localStorage.getItem('api_key');
    if (!tenantId || !apiKey) {
        document.getElementById('loginModal').classList.add('show');
    } else {
        document.getElementById('loginModal').classList.remove('show');
        refreshLibrary();
        updateStats();
        loadIndustries();
    }
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
        document.getElementById('loginModal').classList.remove('show');
        refreshLibrary();
        updateStats();
    } catch (e) {
        showToast(__('login.errorNetwork') + e.message, 'error');
    }
}

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    checkAuth();
});
// Re-render dynamic content when language changes
window.addEventListener('langchange', () => {
    if (localStorage.getItem('tenant_id') && localStorage.getItem('api_key')) {
        refreshLibrary();
    }
});

// ===== Tab Switching =====
function switchTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById('tab-' + tab).classList.add('active');
    document.getElementById('content-' + tab).classList.add('active');
    if (tab === 'models') loadModelConfig();
}

// ===== Model Backend Config (runtime, local & cloud unified) =====
function renderKeyHint(elId, hintElId, mask) {
    const hint = document.getElementById(hintElId);
    if (!hint) return;
    hint.textContent = mask && mask.set
        ? '✓ ' + __('admin.models.keySet') + mask.hint
        : __('admin.models.keyEmpty');
    const input = document.getElementById(elId);
    if (input) {
        input.placeholder = mask && mask.set
            ? __('admin.models.keyKeepPlaceholder')
            : __('admin.models.keyPlaceholder');
    }
}

async function loadModelConfig() {
    try {
        const res = await apiFetch('/admin/models/config');
        if (!res.ok) return;
        const cfg = await res.json();
        document.getElementById('mLLMUrl').value = cfg.llm_url || '';
        document.getElementById('mLLMModel').value = cfg.llm_model || '';
        document.getElementById('mEmbUrl').value = cfg.emb_url || '';
        document.getElementById('mEmbModel').value = cfg.emb_model || '';
        renderKeyHint('mLLMKey', 'mLLMKeyHint', cfg.llm_api_key);
        renderKeyHint('mEmbKey', 'mEmbKeyHint', cfg.emb_api_key);
        const st = document.getElementById('modelCfgStatus');
        if (st) st.textContent = 'LLM: ' + (cfg.llm_url || '-') + '  |  Embedding: ' + (cfg.emb_url || '-');
    } catch (e) {
        console.error('loadModelConfig failed:', e);
    }
}

async function saveModelConfig() {
    const body = {};
    const urlLlm = document.getElementById('mLLMUrl').value.trim();
    const modLlm = document.getElementById('mLLMModel').value.trim();
    const urlEmb = document.getElementById('mEmbUrl').value.trim();
    const modEmb = document.getElementById('mEmbModel').value.trim();
    if (urlLlm) body.llm_url = urlLlm;
    if (modLlm) body.llm_model = modLlm;
    if (urlEmb) body.emb_url = urlEmb;
    if (modEmb) body.emb_model = modEmb;
    const kLlm = document.getElementById('mLLMKey').value;   // '' -> keep? user intent differs
    const kEmb = document.getElementById('mEmbKey').value;
    // Empty key field means "keep current / use default": send null (omit).
    // To revert a cloud key back to local-default, tick the clear box below.
    const clearLlmKey = document.getElementById('mLLMKeyClear') && document.getElementById('mLLMKeyClear').checked;
    const clearEmbKey = document.getElementById('mEmbKeyClear') && document.getElementById('mEmbKeyClear').checked;
    if (kLlm) body.llm_api_key = kLlm; else if (clearLlmKey) body.llm_api_key = '';
    if (kEmb) body.emb_api_key = kEmb; else if (clearEmbKey) body.emb_api_key = '';

    const btn = document.getElementById('modelSaveBtn');
    btn.disabled = true;
    try {
        const res = await apiFetch('/admin/models/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!res.ok) { showToast(__('misc.error') + ': ' + (data.detail || 'unknown'), 'error'); return; }
        document.querySelectorAll('#mLLMKey,#mEmbKey').forEach(i => i.value = '');
        loadModelConfig();
        refreshServiceStatusSafe();
        showToast(__('admin.models.saved'), 'success');
    } catch (e) {
        if (e && e.handled) return;  // 401: login modal already shown
        showToast(__('misc.error') + ': ' + e.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

function refreshServiceStatusSafe() { try { refreshLibrary(); } catch (e) {} }

async function testModelEndpoint(target) {
    const out = document.getElementById(target === 'llm' ? 'mLLMTestResult' : 'mEmbTestResult');
    out.style.color = 'var(--text-muted)';
    out.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + __('admin.models.testing');
    const body = { target };
    const urlInput = document.getElementById(target === 'llm' ? 'mLLMUrl' : 'mEmbUrl');
    const keyInput = document.getElementById(target === 'llm' ? 'mLLMKey' : 'mEmbKey');
    if (urlInput.value.trim()) body.url = urlInput.value.trim();     // unsaved value still testable
    if (keyInput.value) body.api_key = keyInput.value;               // never persisted
    try {
        const res = await apiFetch('/admin/models/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!res.ok) { out.style.color = 'var(--danger)'; out.textContent = data.detail || 'test failed'; return; }
        if (data.ok) {
            out.style.color = 'var(--success)';
            const ids = (data.models || []).join(', ');
            out.innerHTML = '✓ ' + __('admin.models.testOk') +
                (ids ? '<br><span style="color:var(--text-secondary);">' + __('admin.models.found') + ': ' +
                    escapeHtml(ids.slice(0, 300)) + '</span>' : '');
            const dl = document.getElementById(target === 'llm' ? 'llmModelList' : 'embModelList');
            if (dl) dl.innerHTML = (data.models || []).map(m => `<option value="${escapeHtml(m)}">`).join('');
        } else {
            out.style.color = 'var(--danger)';
            out.textContent = '✗ ' + (data.error || ('HTTP ' + data.status_code));
        }
    } catch (e) {
        if (e && e.handled) return;  // 401: login modal already shown
        out.style.color = 'var(--danger)';
        out.textContent = '✗ ' + e.message;
    }
}

// ===== Statistics =====
async function updateStats() {
    try {
        const res = await apiFetch('/stats');
        const data = await res.json();
        document.getElementById('docCount').textContent = data.total_documents || 0;
        document.getElementById('pageCount').textContent = data.total_pages || 0;
    } catch (e) {}
}

// ===== Document Library =====
async function refreshLibrary() {
    const list = document.getElementById('docList');
    list.innerHTML = '<div class="loading"><i class="fas fa-spinner fa-spin"></i> ' + __('misc.loading') + '</div>';
    try {
        const res = await apiFetch('/documents');
        const data = await res.json();
        const docs = data.documents || [];
        if (docs.length === 0) {
            list.innerHTML = `<div class="empty"><i class="fas fa-inbox"></i><p>${__('admin.docs.noDocs')}</p><p style="font-size:13px;color:var(--text-muted);">${__('admin.docs.uploadHint')}</p></div>`;
            return;
        }
        list.innerHTML = docs.map(d => {
            const title = d.filename || d.name || d.original_name || d.id;
            const desc = (d.title || d.summary || '').substring(0, 80);
            return `
                <div class="doc-item">
                    <div class="doc-icon ${getDocIconClass(d.doc_type)}">
                        <i class="fas ${getDocIcon(d.doc_type)}"></i>
                    </div>
                    <div class="doc-info">
                        <div class="doc-name">${escapeHtml(title)}</div>
                        <div class="doc-desc">${escapeHtml(desc)}${desc.length >= 80 ? '...' : ''}</div>
                        <div class="doc-meta">
                            <span><i class="fas fa-clock"></i> ${formatDate(d.created_at || d.updated_at)}</span>
                            <span><i class="fas fa-tag"></i> ${escapeHtml(d.doc_type || d.category_level2 || 'General')}</span>
                            <span><i class="fas fa-industry"></i> ${escapeHtml(d.industry_package_id || 'auto')}</span>
                        </div>
                    </div>
                    <div class="doc-status ${getDocStatusClass(d.status)}"${d.status === 'degraded' ? ' title="' + escapeHtml(((d.metadata && d.metadata.ingest_warnings) || []).join('\n')) + '"' : ''}>${getDocStatus(d.status)}</div>
                    <div class="doc-actions">
                        <button class="doc-action-btn delete" data-doc-id="${escapeHtml(d.id)}" data-doc-name="${escapeHtml(title)}">
                            <i class="fas fa-trash-alt"></i>${__('misc.delete')}</button>
                    </div>
                </div>
            `;}).join('');
    } catch (e) {
        if (e && e.handled) { list.innerHTML = ''; return; }  // 401: login modal shown
        list.innerHTML = '<div class="error"><i class="fas fa-exclamation-triangle"></i> ' + __('misc.error') + ': ' + e.message + '</div>';
    }
}

function getDocStatus(status) {
    const map = {
        'verified': '<i class="fas fa-check"></i> ' + __('admin.docs.status.done'),
        'degraded': '<i class="fas fa-exclamation-triangle"></i> ' + __('admin.docs.status.degraded'),
        'processing': '<i class="fas fa-spinner fa-spin"></i> ' + __('admin.docs.status.processing'),
        'pending_meta': '<i class="fas fa-hourglass-half"></i> ' + __('admin.docs.status.pending'),
        'pending_processing': '<i class="fas fa-hourglass-half"></i> ' + __('admin.docs.status.pending'),
        'error': '<i class="fas fa-times"></i> ' + __('admin.docs.status.error'),
        'already_imported': '<i class="fas fa-check"></i> ' + __('admin.scan.status.imported')
    };
    return map[status] || '<i class="fas fa-question"></i> ' + __('misc.error');
}

function getDocStatusClass(status) {
    const map = {
        'verified': 'status-done',
        'degraded': 'status-degraded',
        'processing': 'status-processing',
        'pending_meta': 'status-pending',
        'pending_processing': 'status-pending',
        'error': 'status-error'
    };
    return map[status] || '';
}

function getDocIcon(type) {
    const map = {
        'pdf': 'fa-file-pdf', 'docx': 'fa-file-word', 'doc': 'fa-file-word',
        'xlsx': 'fa-file-excel', 'xls': 'fa-file-excel', 'pptx': 'fa-file-powerpoint',
        'ppt': 'fa-file-powerpoint', 'png': 'fa-file-image', 'jpg': 'fa-file-image',
        'jpeg': 'fa-file-image', 'bmp': 'fa-file-image',
        'txt': 'fa-file-alt', 'md': 'fa-file-alt'
    };
    return map[(type || '').toLowerCase()] || 'fa-file';
}
function getDocIconClass(type) {
    const map = {
        'pdf': 'pdf', 'docx': 'word', 'doc': 'word',
        'xlsx': 'excel', 'xls': 'excel', 'pptx': 'ppt',
        'ppt': 'ppt', 'png': 'image', 'jpg': 'image',
        'jpeg': 'image', 'bmp': 'image',
        'txt': 'txt', 'md': 'txt'
    };
    return map[(type || '').toLowerCase()] || 'txt';
}
function formatDate(ts) {
    if (!ts) return '-';
    try {
        const d = new Date(ts);
        return d.toLocaleString((typeof CURRENT_LANG !== 'undefined' && CURRENT_LANG === 'en') ? 'en-US' : 'zh-CN');
    } catch { return ts; }
}

// Event delegation for doc-list delete buttons: parameters travel via
// data-* attributes instead of inline onclick string interpolation (XSS-safe)
document.addEventListener('click', (e) => {
    const btn = e.target.closest('#docList button[data-doc-id]');
    if (!btn) return;
    deleteDoc(btn.dataset.docId, btn.dataset.docName);
});

async function deleteDoc(docId, docName) {
    if (!await uiConfirm(__('admin.docs.delete') + ': ' + (docName || docId), { danger: true })) return;
    try {
        const res = await apiFetch(`/documents/${docId}`, { method: 'DELETE' });
        if (res.ok) {
            showToast(__('admin.docs.delete') + ': ' + (docName || docId), 'success');
            refreshLibrary();
            updateStats();
        } else {
            const err = await res.text();
            showToast(__('misc.error') + ': ' + err, 'error');
        }
    } catch (e) {
        if (e && e.handled) return;  // 401: login modal already shown
        showToast(__('misc.error') + ': ' + e.message, 'error');
    }
}

// ===== Scan Directory (not supported in V6)=====
async function scanDirectory() {
    showToast(__('admin.scan.noFiles'), 'info');
}
function toggleSelectAll() {}
async function importSelected() {
    showToast(__('admin.scan.noFiles'), 'info');
}
function startImportMonitor() {}

// ===== Upload =====
function handleDragOver(e) { e.preventDefault(); e.currentTarget.classList.add('dragover'); }
function handleDragLeave(e) { e.currentTarget.classList.remove('dragover'); }
function handleDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('dragover');
    uploadFiles(e.dataTransfer.files);
}
function handleFileSelect(e) { uploadFiles(e.target.files); }

async function loadIndustries() {
    try {
        const res = await apiFetch('/industries');
        const data = await res.json();
        const select = document.getElementById('industrySelect');
        select.innerHTML = '<option value="">' + __('industry.auto') + '</option>';
        if (data.industries) {
            data.industries.forEach(ind => {
                const opt = document.createElement('option');
                opt.value = ind.id;
                opt.textContent = ind.name || ind.id;
                select.appendChild(opt);
            });
        }
    } catch (e) {
        console.error('Failed to load industry packs', e);
    }
}

function toggleIndustrySelect() {
    const mode = document.querySelector('input[name="classifyMode"]:checked').value;
    const select = document.getElementById('industrySelect');
    select.style.display = mode === 'manual' ? 'inline-block' : 'none';
}

async function uploadFiles(files) {
    const progress = document.getElementById('uploadProgress');
    const mode = document.querySelector('input[name="classifyMode"]:checked').value;
    const industry = document.getElementById('industrySelect').value;

    for (const file of files) {
        const div = document.createElement('div');
        div.className = 'upload-item';
        div.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);';
        div.innerHTML = `<span>${file.name}</span> <span class="status" style="min-width:120px;text-align:right;">Waiting to upload...</span>`;
        progress.appendChild(div);

        const form = new FormData();
        form.append('file', file);
        if (mode === 'manual' && industry) {
            form.append('industry', industry);
            form.append('auto_detect', 'false');
        } else {
            form.append('auto_detect', 'true');
        }

        // Use XMLHttpRequest to show upload progress
        await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const statusEl = div.querySelector('.status');

            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const pct = Math.round((e.loaded / e.total) * 100);
                    statusEl.innerHTML = `<div style="width:100px;height:6px;background:var(--bg-subtle);border-radius:3px;display:inline-block;vertical-align:middle;margin-right:6px;"><div style="width:${pct}%;height:100%;background:var(--primary);border-radius:3px;transition:width 0.2s;"></div></div><span style="font-size:12px;color:var(--text-secondary);">${pct}%</span>`;
                }
            });

            xhr.addEventListener('load', () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    const data = JSON.parse(xhr.responseText);
                    if (data.task_id) {
                        statusEl.innerHTML = `<span style="font-size:12px;color:var(--primary);"><i class="fas fa-cog fa-spin"></i> Background Processing 0%</span>`;
                        pollUploadProgress(data.task_id, statusEl, () => {
                            resolve();
                        });
                    } else {
                        statusEl.innerHTML = '<i class="fas fa-check" style="color:var(--success);"></i> Done';
                        resolve();
                    }
                } else {
                    statusEl.innerHTML = '<i class="fas fa-times" style="color:var(--danger);"></i> Upload Failed';
                    reject(new Error('Upload failed'));
                }
            });

            xhr.addEventListener('error', () => {
                statusEl.innerHTML = '<i class="fas fa-times" style="color:var(--danger);"></i> Network Error';
                reject(new Error('Network error'));
            });

            xhr.open('POST', '/api/v1/documents/upload');
            const auth = getAuthHeaders();
            Object.keys(auth).forEach(k => xhr.setRequestHeader(k, auth[k]));
            xhr.send(form);
        });
    }
    updateStats();
    refreshLibrary();
}

// Poll background processing progress. While the tab is hidden the request
// is deferred until it becomes visible again (saves API calls in background
// tabs); the visible-tab cadence is unchanged.
function pollUploadProgress(taskId, statusEl, onDone) {
    let stopped = false;
    const poll = async () => {
        if (stopped) return;
        try {
            const res = await apiFetch(`/documents/upload-progress/${taskId}`);
            const data = await res.json();
            if (!res.ok) {
                statusEl.innerHTML = '<i class="fas fa-times" style="color:var(--danger);"></i> Query Failed';
                stopped = true;
                onDone();
                return;
            }
            if (data.status === 'completed') {
                statusEl.innerHTML = '<i class="fas fa-check" style="color:var(--success);"></i> Done';
                stopped = true;
                onDone();
                return;
            }
            if (data.status === 'failed') {
                statusEl.innerHTML = `<i class="fas fa-times" style="color:var(--danger);"></i> Processing Failed`;
                stopped = true;
                onDone();
                return;
            }
            // Still processing
            const pct = data.progress || 0;
            const msg = data.message || 'Processing';
            statusEl.innerHTML = `<div style="width:100px;height:6px;background:var(--bg-subtle);border-radius:3px;display:inline-block;vertical-align:middle;margin-right:6px;"><div style="width:${pct}%;height:100%;background:var(--warning);border-radius:3px;transition:width 0.3s;"></div></div><span style="font-size:12px;color:var(--text-secondary);">${pct}% ${msg}</span>`;
            setTimeout(() => whenVisible(poll), 1500);
        } catch (e) {
            statusEl.innerHTML = '<i class="fas fa-times" style="color:var(--danger);"></i> Poll Error';
            stopped = true;
            onDone();
        }
    };
    poll();
}

// ===== Reset Database =====
function confirmReset() {
    document.getElementById('resetModal').classList.add('show');
}
function closeModal() {
    document.getElementById('resetModal').classList.remove('show');
}
async function doReset() {
    closeModal();
    try {
        const res = await apiFetch('/reset-database', { method: 'POST' });
        if (res.ok) {
            showToast(__('admin.resetDone'), 'success');
            refreshLibrary();
            updateStats();
        } else {
            showToast(__('admin.resetFailed'), 'error');
        }
    } catch (e) {
        if (e && e.handled) return;  // 401: login modal already shown
        showToast(__('admin.resetFailed') + ': ' + e.message, 'error');
    }
}

// ===== Utilities =====
function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
    return (bytes/(1024*1024)).toFixed(1) + ' MB';
}
