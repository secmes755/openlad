/**
 * OpenLAD Frontend i18n — Chinese/English dynamic switching
 * Usage: __("key") returns translated string for current language
 *        data-i18n="key" on HTML elements auto-translates
 *        Language toggle: <div id="langToggle"></div>
 */
const I18N_DICT = {
    // === App Header & Branding ===
    "app.title":                 { zh: "OpenLAD", en: "OpenLAD" },
    "app.subtitle":              { zh: "智能文档分析助手 — 基于您的知识库回答问题", en: "Intelligent document analysis assistant — Answer questions based on your knowledge base" },
    "app.newChat":               { zh: "新建对话", en: "New Chat" },
    "app.logout":                { zh: "退出登录", en: "Logout" },
    "app.sidebar.close":         { zh: "关闭侧边栏", en: "Close Sidebar" },
    "app.sidebar.open":          { zh: "打开侧边栏", en: "Open Sidebar" },

    // === Input Area ===
    "input.placeholder":         { zh: "输入您的问题...", en: "Enter your question..." },
    "input.hint":               { zh: "Enter 发送 | Shift+Enter 换行", en: "Enter to send | Shift+Enter for new line" },
    "input.send":                { zh: "发送", en: "Send" },

    // === Industry Selector ===
    "industry.label":            { zh: "行业模式", en: "Industry Mode" },
    "industry.auto":             { zh: "自动检测", en: "Auto Detect" },
    "industry.autoHint":         { zh: "根据文档自动选择行业", en: "Auto-select industry based on documents" },
    "industry.fixed":            { zh: "固定行业模式", en: "Fixed Industry Mode" },
    "industry.fixedHint":        { zh: "已选择：", en: "Fixed: " },

    // === Sidebar Navigation ===
    "nav.database":              { zh: "数据库管理", en: "Database Management" },
    "nav.userMgmt":              { zh: "用户管理", en: "User Management" },
    "nav.logout":                { zh: "登出", en: "Logout" },
    "nav.newChat":               { zh: "新对话", en: "New Chat" },
    "nav.noSessions":            { zh: "暂无对话", en: "No conversations yet" },

    // === Login Modal ===
    "login.title":               { zh: "用户登录", en: "User Login" },
    "login.subtitle":            { zh: "请输入用户名和密码", en: "Please enter username and password" },
    "login.username":            { zh: "用户名", en: "Username" },
    "login.password":            { zh: "密码", en: "Password" },
    "login.usernamePlaceholder": { zh: "例如: admin", en: "e.g. admin" },
    "login.passwordPlaceholder": { zh: "请输入密码", en: "Enter your password" },
    "login.btnLogin":            { zh: "登录", en: "Login" },
    "login.errorEmpty":          { zh: "请输入用户名和密码", en: "Please enter username and password" },
    "login.errorFailed":         { zh: "登录失败：", en: "Login failed: " },
    "login.errorNetwork":        { zh: "登录出错：", en: "Login error: " },
    "login.v1.title":            { zh: "OpenLAD 登录", en: "OpenLAD Login" },
    "login.v1.subtitle":         { zh: "请输入租户ID和API密钥", en: "Please enter Tenant ID and API Key" },
    "login.v1.authFailed":       { zh: "认证失败，请重新登录", en: "Authentication failed, please log in again" },
    "login.v1.required":       { zh: "需要登录", en: "Login required" },

    // === User Management Modal ===
    "usermgmt.title":            { zh: "用户管理", en: "User Management" },
    "usermgmt.newUser":          { zh: "用户名", en: "Username" },
    "usermgmt.newPassword":      { zh: "密码（留空随机生成）", en: "Password (leave blank for random)" },
    "usermgmt.role.user":        { zh: "普通用户", en: "Regular User" },
    "usermgmt.role.admin":       { zh: "管理员", en: "Admin" },
    "usermgmt.add":              { zh: "添加", en: "Add" },
    "usermgmt.close":            { zh: "关闭", en: "Close" },
    "usermgmt.column.username":  { zh: "用户名", en: "Username" },
    "usermgmt.column.role":      { zh: "角色", en: "Role" },
    "usermgmt.column.tenant":    { zh: "租户", en: "Tenant" },
    "usermgmt.column.apiKey":    { zh: "API密钥", en: "API Key" },
    "usermgmt.column.actions":   { zh: "操作", en: "Actions" },
    "usermgmt.delete":           { zh: "删除", en: "Delete" },
    "usermgmt.confirmDelete":    { zh: "确定删除用户 ", en: "Are you sure to delete " },
    "usermgmt.confirmDeleteSuffix": { zh: " ？", en: "?" },

    // === Service Status Bar ===
    "status.label":              { zh: "服务", en: "Service" },
    "status.logs":               { zh: "日志", en: "Logs" },
    "status.logsTitle":          { zh: "服务日志", en: "Service Logs" },

    // === Chat / Query ===
    "chat.error":                { zh: "请求失败，请重试", en: "Request failed, please retry" },
    "chat.thinking":             { zh: "思考中...", en: "Thinking..." },
    "chat.empty":                { zh: "暂无回复内容", en: "No response content" },

    // === Admin Page — Header ===
    "admin.title":               { zh: "数据库管理", en: "Database Admin" },
    "admin.back":                { zh: "← 返回聊天", en: "← Back to Chat" },
    "admin.stats.docCount":      { zh: "文档数", en: "Documents" },
    "admin.stats.chunkCount":    { zh: "块数", en: "Chunks" },
    "admin.rebuildAll":          { zh: "重建索引", en: "Rebuild Index" },
    "admin.rebuildBtn":          { zh: "开始重建", en: "Start Rebuild" },
    "admin.rebuildTitle":        { zh: "重建索引", en: "Rebuild Index" },
    "admin.rebuildConfirm":      { zh: "这将删除所有现有索引并重建。确定继续吗？", en: "This will delete all existing indexes and rebuild. Continue?" },
    "admin.rebuildStarted":      { zh: "重建已开始，请稍候...", en: "Rebuild started, please wait..." },
    "admin.rebuildDone":         { zh: "重建完成", en: "Rebuild completed" },
    "admin.rebuildFailed":       { zh: "重建失败：", en: "Rebuild failed: " },

    // === Admin Page — Tabs ===
    "admin.tab.documents":       { zh: "文档", en: "Documents" },
    "admin.tab.upload":          { zh: "上传", en: "Upload" },
    "admin.tab.scan":            { zh: "目录扫描", en: "Directory Scan" },
    "admin.tab.skills":          { zh: "技能", en: "Skills" },
    "admin.tab.diagnostic":      { zh: "诊断", en: "Diagnostic" },

    // === Admin — Documents Tab ===
    "admin.docs.search":         { zh: "搜索文件...", en: "Search documents..." },
    "admin.docs.deleteSelected": { zh: "删除选中", en: "Delete Selected" },
    "admin.docs.noDocs":         { zh: "暂无文档，请上传或扫描导入", en: "No documents. Upload or scan to import." },
    "admin.docs.confirmDelete":  { zh: "确定删除选中的 ", en: "Are you sure to delete the selected " },
    "admin.docs.confirmDeleteSuffix": { zh: " 个文档？", en: " document(s)?" },
    "admin.docs.pages":          { zh: "页", en: "pages" },
    "admin.docs.chunks":         { zh: "块", en: "chunks" },
    "admin.docs.status.done":    { zh: "已完成", en: "Done" },
    "admin.docs.status.processing": { zh: "处理中", en: "Processing" },
    "admin.docs.status.pending": { zh: "等待中", en: "Pending" },
    "admin.docs.status.error":   { zh: "错误", en: "Error" },
    "admin.docs.status.external": { zh: "外部服务", en: "External" },
    "admin.docs.delete":         { zh: "删除", en: "Delete" },
    "admin.docs.importedTitle":  { zh: "已导入文档", en: "Imported Documents" },
    "admin.docs.uploadHint":     { zh: "请在\"上传\"标签页中上传文件", en: "Please upload files in the Upload tab" },
    "admin.resetDb":             { zh: "重置数据库", en: "Reset Database" },

    // === Admin — Upload Tab ===
    "admin.upload.dropzone":     { zh: "点击或拖拽文件到此处上传", en: "Click or drag files here to upload" },
    "admin.upload.dropzoneIcon": { zh: "支持 PDF、Word、Excel、PPT、图片、TXT", en: "Supports PDF, Word, Excel, PPT, Images, TXT" },
    "admin.upload.btn":          { zh: "选择文件并上传", en: "Select Files and Upload" },
    "admin.upload.uploading":    { zh: "上传中...", en: "Uploading..." },
    "admin.upload.success":      { zh: "上传成功", en: "Upload successful" },
    "admin.upload.failed":       { zh: "上传失败：", en: "Upload failed: " },

    // === Admin — Scan Tab ===
    "admin.scan.directory":      { zh: "扫描目录路径", en: "Scan Directory Path" },
    "admin.scan.dirPlaceholder": { zh: "例如: ./docs", en: "e.g. ./docs" },
    "admin.scan.btn":            { zh: "扫描", en: "Scan" },
    "admin.scan.scanning":       { zh: "扫描中...", en: "Scanning..." },
    "admin.scan.importSelected": { zh: "导入选中", en: "Import Selected" },
    "admin.scan.noFiles":        { zh: "目录下未发现支持的文件", en: "No supported files found in directory" },
    "admin.scan.selected":       { zh: "已选择", en: "Selected" },
    "admin.scan.importing":      { zh: "导入中...", en: "Importing..." },
    "admin.scan.importDone":     { zh: "导入完成", en: "Import completed" },
    "admin.scan.importFailed":   { zh: "导入失败：", en: "Import failed: " },
    "admin.scan.column.file":    { zh: "文件", en: "File" },
    "admin.scan.column.size":    { zh: "大小", en: "Size" },
    "admin.scan.column.type":    { zh: "类型", en: "Type" },
    "admin.scan.column.status":  { zh: "状态", en: "Status" },
    "admin.scan.status.imported":  { zh: "已导入", en: "Imported" },
    "admin.scan.status.new":     { zh: "新文件", en: "New" },

    // === Admin — Skills Tab ===
    "admin.skills.title":        { zh: "技能管理", en: "Skill Management" },
    "admin.skills.create":       { zh: "新建技能", en: "Create Skill" },
    "admin.skills.noSkills":     { zh: "暂无技能", en: "No skills yet" },
    "admin.skills.name":         { zh: "名称", en: "Name" },
    "admin.skills.description":  { zh: "描述", en: "Description" },
    "admin.skills.edit":         { zh: "编辑", en: "Edit" },
    "admin.skills.delete":       { zh: "删除", en: "Delete" },
    "admin.skills.confirmDelete":{ zh: "确定删除技能 ", en: "Are you sure to delete skill " },

    // === Admin — Diagnostic Tab ===
    "admin.diag.title":          { zh: "系统诊断", en: "System Diagnostic" },
    "admin.diag.run":            { zh: "运行诊断", en: "Run Diagnostic" },
    "admin.diag.running":        { zh: "正在诊断...", en: "Running diagnostic..." },
    "admin.diag.noResults":      { zh: "暂无诊断结果，请点击\"运行诊断\"", en: "No results. Click \"Run Diagnostic\" to start." },
    "admin.diag.status.ok":      { zh: "正常", en: "OK" },
    "admin.diag.status.warn":    { zh: "警告", en: "Warning" },
    "admin.diag.status.error":   { zh: "错误", en: "Error" },

    // === Misc ===
    "misc.loading":              { zh: "加载中...", en: "Loading..." },
    "misc.error":                { zh: "加载失败", en: "Load failed" },
    "misc.empty":                { zh: "暂无数据", en: "No data" },
    "misc.cancel":               { zh: "取消", en: "Cancel" },
    "misc.confirm":              { zh: "确定", en: "Confirm" },
    "misc.save":                 { zh: "保存", en: "Save" },
    "misc.close":                { zh: "关闭", en: "Close" },
    "misc.refresh":              { zh: "刷新", en: "Refresh" },
    "misc.delete":               { zh: "删除", en: "Delete" },

    // === Language Toggle ===
    "lang.label":                { zh: "中文", en: "EN" },
};

// Current language — persisted in localStorage
let CURRENT_LANG = localStorage.getItem('openlad_lang') || 'zh';

function setLang(lang) {
    CURRENT_LANG = lang;
    localStorage.setItem('openlad_lang', lang);
    applyI18n();
}

function toggleLang() {
    setLang(CURRENT_LANG === 'zh' ? 'en' : 'zh');
}

// Get translated string
function __(key, fallback) {
    const entry = I18N_DICT[key];
    if (entry) {
        return entry[CURRENT_LANG] || entry['en'] || entry['zh'] || (fallback || key);
    }
    return fallback || key;
}

// Apply translations to all data-i18n elements
function applyI18n() {
    // 1. Translate elements with data-i18n attribute
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            el.placeholder = __(key, el.placeholder);
        } else {
            el.textContent = __(key, el.textContent);
        }
    });

    // 2. Translate elements with data-i18n-placeholder
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = __(el.getAttribute('data-i18n-placeholder'));
    });

    // 3. Update language toggle label
    const toggleLabel = document.getElementById('langToggleLabel');
    if (toggleLabel) {
        toggleLabel.textContent = __("lang.label");
    }

    // 4. Fire custom event so app.js can re-render dynamic content
    window.dispatchEvent(new CustomEvent('langchange', { detail: { lang: CURRENT_LANG } }));
}

// Inject the language toggle component
function injectLangToggle() {
    // Don't inject twice
    if (document.getElementById('langToggle')) return;

    const container = document.createElement('div');
    container.id = 'langToggle';
    container.className = 'lang-toggle';
    container.innerHTML = `
        <span class="lang-toggle-label" id="langToggleLabel">${__("lang.label")}</span>
        <label class="lang-switch">
            <input type="checkbox" id="langCheckbox" ${CURRENT_LANG === 'en' ? 'checked' : ''}>
            <span class="lang-slider"></span>
        </label>
    `;
    document.body.appendChild(container);

    document.getElementById('langCheckbox').addEventListener('change', function() {
        setLang(this.checked ? 'en' : 'zh');
    });
}

// CSS for the toggle (inlined so it always loads)
(function injectCSS() {
    if (document.getElementById('i18n-css')) return;
    const style = document.createElement('style');
    style.id = 'i18n-css';
    style.textContent = `
        .lang-toggle {
            position: fixed;
            top: 44px;
            right: 12px;
            z-index: 9999;
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(8px);
            padding: 6px 12px;
            border-radius: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.12);
            font-size: 13px;
        }
        .lang-toggle-label {
            font-size: 12px;
            font-weight: 600;
            color: #374151;
            min-width: 28px;
            text-align: center;
        }
        .lang-switch {
            position: relative;
            display: inline-block;
            width: 40px;
            height: 22px;
            cursor: pointer;
        }
        .lang-switch input {
            opacity: 0; width: 0; height: 0;
        }
        .lang-slider {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: #2563eb;
            border-radius: 22px;
            transition: 0.3s;
        }
        .lang-slider::before {
            content: "";
            position: absolute;
            height: 18px;
            width: 18px;
            left: 2px;
            bottom: 2px;
            background: white;
            border-radius: 50%;
            transition: 0.3s;
        }
        input:checked + .lang-slider {
            background: #059669;
        }
        input:checked + .lang-slider::before {
            transform: translateX(18px);
        }
        /* Dark mode support */
        @media (prefers-color-scheme: dark) {
            .lang-toggle {
                background: rgba(30,30,30,0.95);
                color: #e5e7eb;
            }
            .lang-toggle-label { color: #e5e7eb; }
        }
    `;
    document.head.appendChild(style);
})();

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    injectLangToggle();
    applyI18n();
});
