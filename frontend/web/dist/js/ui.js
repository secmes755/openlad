/**
 * OpenLAD shared UI primitives — toast notifications, promise-based confirm
 * dialog, and a credential display modal with per-field copy buttons.
 *
 * Requires i18n.js (uses __()); include after it:
 *   <script src="/static/js/i18n.js"></script>
 *   <script src="/static/js/ui.js"></script>
 *
 * API:
 *   showToast(message, type?)            type: info|success|error
 *   uiConfirm(message, opts?)            -> Promise<boolean>; opts: {danger, confirmText}
 *   showCredentials(title, fields, note?)  fields: [{label, value}]
 */
(function () {
    'use strict';

    // ===== Injected styles (self-contained, loads with the module) =====
    const CSS = `
        .ui-toast-stack {
            position: fixed; top: 48px; right: 16px; z-index: 10000;
            display: flex; flex-direction: column; gap: 8px; max-width: 380px;
        }
        .ui-toast {
            padding: 10px 16px; border-radius: 8px; font-size: 13px;
            color: #fff; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: flex; align-items: flex-start; gap: 8px;
            animation: ui-toast-in 0.2s ease; word-break: break-word;
            white-space: pre-wrap;
        }
        .ui-toast.info    { background: #374151; }
        .ui-toast.success { background: #059669; }
        .ui-toast.error   { background: #dc2626; }
        .ui-toast.fade { opacity: 0; transition: opacity 0.3s; }
        @keyframes ui-toast-in { from { transform: translateX(20px); opacity: 0; } to { transform: none; opacity: 1; } }

        .ui-modal-overlay {
            display: none; position: fixed; inset: 0; z-index: 10001;
            background: var(--overlay-bg, rgba(0,0,0,0.5)); align-items: center; justify-content: center;
        }
        .ui-modal-overlay.show { display: flex; }
        .ui-modal {
            background: var(--bg-main, #fff); border-radius: 12px; padding: 24px;
            max-width: 460px; width: 92%; box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        .ui-modal h3 { margin: 0 0 12px; font-size: 16px; display: flex; align-items: center; gap: 8px; color: var(--text-primary, #1f2937); }
        .ui-modal .ui-modal-body { color: var(--text-secondary, #4b5563); font-size: 14px; margin-bottom: 20px; white-space: pre-wrap; word-break: break-word; }
        .ui-modal .ui-modal-actions { display: flex; gap: 10px; justify-content: flex-end; }
        .ui-modal .ui-btn {
            padding: 8px 18px; border-radius: 8px; font-size: 13px; cursor: pointer;
            border: none; display: inline-flex; align-items: center; gap: 6px;
        }
        .ui-modal .ui-btn-primary { background: var(--primary, #2563eb); color: #fff; }
        .ui-modal .ui-btn-primary:hover { background: var(--primary-hover, #1d4ed8); }
        .ui-modal .ui-btn-danger { background: var(--danger, #dc2626); color: #fff; }
        .ui-modal .ui-btn-danger:hover { background: var(--danger-hover, #b91c1c); }
        .ui-modal .ui-btn-secondary { background: var(--bg-subtle, #f3f4f6); color: var(--text-primary, #1f2937); }
        .ui-modal .ui-btn-secondary:hover { background: var(--border, #e5e7eb); }

        .ui-cred-row {
            display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
        }
        .ui-cred-label { min-width: 90px; font-size: 12px; color: var(--text-secondary, #6b7280); font-weight: 600; }
        .ui-cred-value {
            flex: 1; font-family: monospace; font-size: 12px; background: var(--bg-subtle, #f9fafb);
            color: var(--text-primary, #1f2937);
            border: 1px solid var(--border, #e5e7eb); border-radius: 6px; padding: 6px 8px;
            word-break: break-all; user-select: all;
        }
        .ui-cred-copy {
            border: 1px solid var(--border, #e5e7eb); background: var(--bg-main, #fff); border-radius: 6px;
            color: var(--text-secondary, #6b7280);
            padding: 5px 10px; font-size: 12px; cursor: pointer; white-space: nowrap;
        }
        .ui-cred-copy:hover { background: var(--bg-subtle, #f3f4f6); }
    `;

    function injectCSS() {
        if (document.getElementById('ui-primitives-css')) return;
        const style = document.createElement('style');
        style.id = 'ui-primitives-css';
        style.textContent = CSS;
        document.head.appendChild(style);
    }

    // ===== Toast =====
    function toastStack() {
        let stack = document.querySelector('.ui-toast-stack');
        if (!stack) {
            stack = document.createElement('div');
            stack.className = 'ui-toast-stack';
            // Screen readers announce new toasts without moving focus
            stack.setAttribute('role', 'status');
            stack.setAttribute('aria-live', 'polite');
            document.body.appendChild(stack);
        }
        return stack;
    }

    const TOAST_ICON = { info: 'fa-info-circle', success: 'fa-check-circle', error: 'fa-exclamation-circle' };

    window.showToast = function (message, type) {
        type = type || 'info';
        injectCSS();
        const el = document.createElement('div');
        el.className = 'ui-toast ' + type;
        if (type === 'error') el.setAttribute('role', 'alert'); // errors assert immediately
        el.innerHTML = '<i class="fas ' + (TOAST_ICON[type] || TOAST_ICON.info) + '"></i><span></span>';
        el.querySelector('span').textContent = String(message);
        toastStack().appendChild(el);
        const ttl = type === 'error' ? 6000 : 3500;
        setTimeout(() => {
            el.classList.add('fade');
            setTimeout(() => el.remove(), 350);
        }, ttl);
    };

    // ===== Confirm dialog (promise-based) =====
    window.uiConfirm = function (message, opts) {
        opts = opts || {};
        injectCSS();
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'ui-modal-overlay';
            overlay.setAttribute('role', 'dialog');
            overlay.setAttribute('aria-modal', 'true');
            const icon = opts.danger
                ? '<i class="fas fa-exclamation-triangle" style="color:var(--danger,#dc2626);"></i>'
                : '<i class="fas fa-question-circle" style="color:var(--primary,#2563eb);"></i>';
            overlay.innerHTML = `
                <div class="ui-modal">
                    <h3>${icon}<span></span></h3>
                    <div class="ui-modal-body"></div>
                    <div class="ui-modal-actions">
                        <button class="ui-btn ui-btn-secondary" data-act="cancel"></button>
                        <button class="ui-btn ${opts.danger ? 'ui-btn-danger' : 'ui-btn-primary'}" data-act="ok"></button>
                    </div>
                </div>`;
            overlay.querySelector('h3 span').textContent = opts.title || __('misc.confirm');
            overlay.querySelector('.ui-modal-body').textContent = String(message);
            overlay.querySelector('[data-act="cancel"]').textContent = __('misc.cancel');
            overlay.querySelector('[data-act="ok"]').textContent = opts.confirmText || __('misc.confirm');

            function done(val) {
                overlay.remove();
                document.removeEventListener('keydown', onKey);
                resolve(val);
            }
            function onKey(e) {
                if (e.key === 'Escape') done(false);
                if (e.key === 'Enter') done(true);
            }
            overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => done(false));
            overlay.querySelector('[data-act="ok"]').addEventListener('click', () => done(true));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) done(false); });
            document.addEventListener('keydown', onKey);
            document.body.appendChild(overlay);
            overlay.classList.add('show');
            overlay.querySelector('[data-act="ok"]').focus();
        });
    };

    // ===== Credential display modal =====
    window.showCredentials = function (title, fields, note) {
        injectCSS();
        const overlay = document.createElement('div');
        overlay.className = 'ui-modal-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.innerHTML = `
            <div class="ui-modal">
                <h3><i class="fas fa-key" style="color:var(--primary,#2563eb);"></i><span></span></h3>
                <div class="ui-modal-body"><div class="ui-cred-fields"></div><div class="ui-cred-note" style="font-size:12px;color:var(--text-muted,#9ca3af);"></div></div>
                <div class="ui-modal-actions">
                    <button class="ui-btn ui-btn-primary" data-act="close"></button>
                </div>
            </div>`;
        overlay.querySelector('h3 span').textContent = title;
        const box = overlay.querySelector('.ui-cred-fields');
        for (const f of fields) {
            const row = document.createElement('div');
            row.className = 'ui-cred-row';
            const label = document.createElement('span');
            label.className = 'ui-cred-label';
            label.textContent = f.label;
            const value = document.createElement('span');
            value.className = 'ui-cred-value';
            value.textContent = f.value;
            const copy = document.createElement('button');
            copy.className = 'ui-cred-copy';
            copy.innerHTML = '<i class="fas fa-copy"></i> ';
            copy.appendChild(document.createTextNode(__('misc.copy')));
            copy.addEventListener('click', async () => {
                try {
                    await navigator.clipboard.writeText(f.value);
                    showToast(__('misc.copied'), 'success');
                } catch (e) {
                    // clipboard API needs a secure context; fall back to selection
                    const range = document.createRange();
                    range.selectNodeContents(value);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                    showToast(__('misc.copyManual'), 'info');
                }
            });
            row.appendChild(label); row.appendChild(value); row.appendChild(copy);
            box.appendChild(row);
        }
        overlay.querySelector('.ui-cred-note').textContent = note || '';
        const closeBtn = overlay.querySelector('[data-act="close"]');
        closeBtn.textContent = __('misc.close');
        closeBtn.addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
        document.addEventListener('keydown', function onKey(e) {
            if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', onKey); }
        });
        document.body.appendChild(overlay);
        overlay.classList.add('show');
    };

    injectCSS();
})();
