// V6 Authentication Management
const AUTH_KEY = 'ada6_auth';

function getAuth() {
    try {
        return JSON.parse(localStorage.getItem(AUTH_KEY)) || {};
    } catch { return {}; }
}

function setAuth(tenantId, apiKey) {
    localStorage.setItem(AUTH_KEY, JSON.stringify({ tenantId, apiKey }));
}

function clearAuth() {
    localStorage.removeItem(AUTH_KEY);
}

function getAuthHeaders() {
    const auth = getAuth();
    const headers = {};
    if (auth.tenantId) headers['X-Tenant-ID'] = auth.tenantId;
    if (auth.apiKey) headers['Authorization'] = 'Bearer ' + auth.apiKey;
    return headers;
}

function requireAuth() {
    const auth = getAuth();
    return auth.tenantId && auth.apiKey;
}

async function apiFetch(url, options = {}) {
    if (!requireAuth()) {
        showLoginModal();
        throw new Error('Login required');
    }
    options.headers = {
        ...getAuthHeaders(),
        ...(options.headers || {})
    };
    const res = await fetch(url, options);
    if (res.status === 401 || res.status === 403) {
        clearAuth();
        showLoginModal();
        throw new Error('Authentication failed, please log in again');
    }
    return res;
}

function showLoginModal() {
    let modal = document.getElementById('loginModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'loginModal';
        modal.innerHTML = `
            <div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;">
                <div style="background:white;padding:28px;border-radius:12px;width:360px;max-width:90%;">
                    <h3 style="margin-bottom:16px;"><i class="fas fa-key"></i> V6 Login</h3>
                    <p style="font-size:13px;color:#666;margin-bottom:12px;">Please enter Tenant ID and API Key</p>
                    <input type="text" id="loginTenantId" placeholder="Tenant ID" style="width:100%;padding:10px;margin-bottom:10px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box;">
                    <input type="password" id="loginApiKey" placeholder="API Key" style="width:100%;padding:10px;margin-bottom:16px;border:1px solid #ddd;border-radius:6px;box-sizing:border-box;">
                    <button onclick="doLogin()" style="width:100%;padding:10px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;">Login</button>
                    <p id="loginError" style="color:#dc2626;font-size:12px;margin-top:10px;display:none;"></p>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }
    modal.style.display = 'flex';
}

function hideLoginModal() {
    const modal = document.getElementById('loginModal');
    if (modal) modal.style.display = 'none';
}

async function doLogin() {
    const tenantId = document.getElementById('loginTenantId').value.trim();
    const apiKey = document.getElementById('loginApiKey').value.trim();
    const errorEl = document.getElementById('loginError');
    if (!tenantId || !apiKey) {
        errorEl.textContent = 'Please enter Tenant ID and API Key';
        errorEl.style.display = 'block';
        return;
    }
    // Verify authentication is valid
    try {
        const res = await fetch('/api/v1/health', {
            headers: { 'X-Tenant-ID': tenantId, 'Authorization': 'Bearer ' + apiKey }
        });
        if (!res.ok) {
            errorEl.textContent = 'Authentication failed, please check Tenant ID and API Key';
            errorEl.style.display = 'block';
            return;
        }
        setAuth(tenantId, apiKey);
        hideLoginModal();
        window.location.reload();
    } catch (e) {
        errorEl.textContent = 'Network error: ' + e.message;
        errorEl.style.display = 'block';
    }
}

// Check login status on page load
document.addEventListener('DOMContentLoaded', () => {
    if (!requireAuth()) {
        showLoginModal();
    }
});
