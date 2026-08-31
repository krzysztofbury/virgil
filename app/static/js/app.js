// Initialize Lucide icons
document.addEventListener('DOMContentLoaded', function() {
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
    initDatePickers();
    updateThemeIcon();
});

// Flatpickr datepicker for all date inputs
function initDatePickers(root) {
    var container = root || document;
    var inputs = container.querySelectorAll('input[type="date"]:not(.flatpickr-input)');
    inputs.forEach(function(el) {
        flatpickr(el, {
            dateFormat: 'Y-m-d',
            defaultDate: el.value || undefined,
            allowInput: true,
            disableMobile: true,
            locale: { firstDayOfWeek: 1 },
            prevArrow: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"></polyline></svg>',
            nextArrow: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"></polyline></svg>',
            onReady: function(_, __, fp) {
                fp.calendarContainer.classList.add('virgil-datepicker');
            }
        });
    });
}

// Re-initialize Lucide + datepickers after HTMX swaps
document.addEventListener('htmx:afterSwap', function(e) {
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
    initDatePickers(e.detail.target);
    scanNotifications();
});

// One accessible mutation-feedback surface for native forms and HTMX.
var DRAFT_PREFIX = 'virgil-draft:';
var DRAFT_INDEX_KEY = 'virgil-draft-index';
var DRAFT_CLEAR_PENDING_PREFIX = 'virgil-draft-clear-pending:';
var DRAFT_FIELD_MAX = 2000;
var DRAFT_TOTAL_MAX = 4000;
var DRAFT_COUNT_MAX = 12;
var DRAFT_FIELDS_MAX = 20;
var HTMX_TIMEOUT_MS = 30000;

if (window.htmx) window.htmx.config.timeout = HTMX_TIMEOUT_MS;

// A confirmation has said everything it has to say after a few seconds; an
// error and an unfinished job have not, so only success self-dismisses.
var TOAST_DISMISS_MS = 4000;
var JOB_TOAST_DISMISS_MS = 6000;
var TOAST_LEAVE_MS = 220;

function dismissNotification(node) {
    var target = (node && node.closest('#job-notifications article')) || node;
    if (!target || target.dataset.dismissing === 'true') return;
    target.dataset.dismissing = 'true';
    target.classList.add('is-leaving');
    window.setTimeout(function() { target.remove(); }, TOAST_LEAVE_MS);
}

// Hover and focus hold the timer open, so a notification cannot vanish out from
// under a pointer or a keyboard user reading it.
function armAutoDismiss(node, delay) {
    if (!node || node.dataset.autodismissArmed === 'true') return;
    node.dataset.autodismissArmed = 'true';
    var timer = null;
    var start = function() {
        if (timer !== null || node.dataset.dismissing === 'true') return;
        timer = window.setTimeout(function() { dismissNotification(node); }, delay);
    };
    var stop = function() {
        if (timer === null) return;
        window.clearTimeout(timer);
        timer = null;
    };
    node.addEventListener('mouseenter', stop);
    node.addEventListener('focusin', stop);
    node.addEventListener('mouseleave', start);
    node.addEventListener('focusout', start);
    start();
}

function scanNotifications() {
    document.querySelectorAll('#mutation-feedback [data-feedback-autodismiss]').forEach(function(node) {
        armAutoDismiss(node, TOAST_DISMISS_MS);
    });
    // Polling replaces the body div, not the article, so a job re-arms only
    // once it actually reaches a terminal success.
    document.querySelectorAll('#job-notifications [data-job-status="succeeded"]').forEach(function(node) {
        armAutoDismiss(node, JOB_TOAST_DISMISS_MS);
    });
}

function renderFeedback(region, message, className, dismissible) {
    region.replaceChildren();
    var box = document.createElement('div');
    box.className = 'feedback-message ' + className;
    var text = document.createElement('span');
    text.dataset.feedbackText = '';
    text.textContent = message;
    box.appendChild(text);
    if (dismissible) {
        var dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'feedback-dismiss';
        dismiss.dataset.feedbackDismiss = '';
        dismiss.setAttribute('aria-label', 'Dismiss error');
        dismiss.textContent = '\u00d7';
        box.appendChild(dismiss);
    }
    region.appendChild(box);
    if (className === 'feedback-success') {
        box.dataset.feedbackAutodismiss = '';
        armAutoDismiss(box, TOAST_DISMISS_MS);
    }
}

function showFeedback(message, kind) {
    var status = document.getElementById('feedback-status');
    var error = document.getElementById('feedback-error');
    if (!status || !error) return;
    if (kind === 'error') {
        status.replaceChildren();
        renderFeedback(error, message, 'feedback-error', true);
        return;
    }
    error.replaceChildren();
    renderFeedback(status, message, kind === 'pending' ? 'feedback-pending' : 'feedback-success', false);
}

function showToast(message, isError) {
    showFeedback(message, isError ? 'error' : 'success');
}

function flashSaved() {
    showFeedback('Saved', 'success');
}

function draftStorageKey(form) {
    return form && form.dataset.draftKey ? DRAFT_PREFIX + form.dataset.draftKey : '';
}

function draftFieldNames(form) {
    return (form.dataset.draftFields || '').split(',').map(function(name) { return name.trim(); })
        .filter(Boolean).slice(0, DRAFT_FIELDS_MAX);
}

function draftIndex() {
    try {
        var parsed = JSON.parse(sessionStorage.getItem(DRAFT_INDEX_KEY) || '[]');
        if (!Array.isArray(parsed)) return [];
        return parsed.filter(function(key) { return typeof key === 'string' && key.indexOf(DRAFT_PREFIX) === 0; })
            .slice(-DRAFT_COUNT_MAX);
    } catch (_) {
        return [];
    }
}

function writeDraftIndex(index) {
    sessionStorage.setItem(DRAFT_INDEX_KEY, JSON.stringify(index.slice(-DRAFT_COUNT_MAX)));
}

function draftValues(form) {
    var values = {};
    var total = 0;
    draftFieldNames(form).forEach(function(name) {
        var field = form.elements.namedItem(name);
        if (!field || typeof field.value !== 'string') return;
        var value = field.value.slice(0, DRAFT_FIELD_MAX);
        if (total + value.length > DRAFT_TOTAL_MAX) value = value.slice(0, Math.max(0, DRAFT_TOTAL_MAX - total));
        values[name] = value;
        total += value.length;
    });
    return values;
}

function saveNetworkDraft(form) {
    var key = draftStorageKey(form);
    if (!key) return;
    try {
        var index = draftIndex().filter(function(existing) { return existing !== key; });
        while (index.length >= DRAFT_COUNT_MAX) sessionStorage.removeItem(index.shift());
        sessionStorage.setItem(key, JSON.stringify(draftValues(form)));
        index.push(key);
        writeDraftIndex(index);
        form.dataset.draftRestored = 'true';
    } catch (_) {}
}

function clearDraft(key) {
    if (!key) return;
    try {
        var storageKey = DRAFT_PREFIX + key;
        sessionStorage.removeItem(storageKey);
        sessionStorage.removeItem(DRAFT_CLEAR_PENDING_PREFIX + key);
        writeDraftIndex(draftIndex().filter(function(existing) { return existing !== storageKey; }));
    } catch (_) {}
}

function markDraftClearPending(form) {
    if (!form || !form.dataset.draftKey) return;
    try { sessionStorage.setItem(DRAFT_CLEAR_PENDING_PREFIX + form.dataset.draftKey, '1'); } catch (_) {}
}

function clearDraftPending(form) {
    if (!form || !form.dataset.draftKey) return;
    try { sessionStorage.removeItem(DRAFT_CLEAR_PENDING_PREFIX + form.dataset.draftKey); } catch (_) {}
}

function hasDraftClearPending(key) {
    try { return sessionStorage.getItem(DRAFT_CLEAR_PENDING_PREFIX + key) === '1'; } catch (_) { return false; }
}

function updateRestoredDraft(event) {
    var form = event.target.closest('form[data-draft-key][data-draft-restored="true"]');
    if (form) saveNetworkDraft(form);
}

function restoreNetworkDrafts() {
    document.querySelectorAll('form[data-draft-key][data-draft-fields]').forEach(function(form) {
        var key = draftStorageKey(form);
        var raw = null;
        try { raw = sessionStorage.getItem(key); } catch (_) {}
        if (!raw) return;
        try {
            var values = JSON.parse(raw);
            if (!values || Array.isArray(values) || typeof values !== 'object') throw new Error('invalid draft');
            var total = 0;
            draftFieldNames(form).forEach(function(name) {
                if (!Object.prototype.hasOwnProperty.call(values, name)) return;
                if (typeof values[name] !== 'string') throw new Error('invalid draft value');
                var field = form.elements.namedItem(name);
                var value = values[name].slice(0, Math.min(DRAFT_FIELD_MAX, DRAFT_TOTAL_MAX - total));
                if (field && typeof field.value === 'string') field.value = value;
                total += value.length;
            });
            form.dataset.draftRestored = 'true';
            showFeedback('A draft from a failed network request was restored.', 'error');
        } catch (_) {
            clearDraft(form.dataset.draftKey);
        }
    });
}

function pendingControl(form, fallback) {
    return (form && form._feedbackSubmitter) || fallback || null;
}

function markPending(form, control) {
    if (!form || form.dataset.feedbackPending === 'true') return;
    form.dataset.feedbackPending = 'true';
    form.setAttribute('aria-busy', 'true');
    if (!control) {
        showFeedback('Saving...', 'pending');
        return;
    }
    form._feedbackSubmitter = control;
    control.dataset.feedbackOriginalHtml = control.innerHTML;
    control.dataset.feedbackWasDisabled = control.disabled ? 'true' : 'false';
    control.disabled = true;
    control.setAttribute('aria-disabled', 'true');
    control.textContent = control.dataset.pendingLabel || 'Working...';
    showFeedback(control.textContent, 'pending');
}

function restorePending(form) {
    if (!form) return;
    form.removeAttribute('aria-busy');
    delete form.dataset.feedbackPending;
    var control = form._feedbackSubmitter;
    if (control && control.dataset.feedbackOriginalHtml !== undefined) {
        control.innerHTML = control.dataset.feedbackOriginalHtml;
        control.disabled = control.dataset.feedbackWasDisabled === 'true';
        control.removeAttribute('aria-disabled');
        delete control.dataset.feedbackOriginalHtml;
        delete control.dataset.feedbackWasDisabled;
    }
    form._feedbackSubmitter = null;
}

function requestForm(event) {
    var element = event.detail && event.detail.elt;
    if (!element) return null;
    return element.tagName === 'FORM' ? element : element.closest('form');
}

document.addEventListener('click', function(event) {
    var control = event.target.closest('button[type="submit"], input[type="submit"]');
    if (control && control.form) control.form._feedbackSubmitter = control;
});

document.addEventListener('submit', function(event) {
    var form = event.target;
    if (form.tagName !== 'FORM' || event.defaultPrevented) return;
    form._feedbackSubmitter = event.submitter || form._feedbackSubmitter;
    markDraftClearPending(form);
    if (!form.hasAttribute('hx-post')) {
        window.setTimeout(function() { markPending(form, pendingControl(form)); }, 0);
    }
    if (form.action && form.action.endsWith('/logout')) {
        try {
            Object.keys(sessionStorage).filter(function(key) {
                return key.indexOf(DRAFT_PREFIX) === 0 || key.indexOf(DRAFT_CLEAR_PENDING_PREFIX) === 0;
            })
                .forEach(function(key) { sessionStorage.removeItem(key); });
            sessionStorage.removeItem(DRAFT_INDEX_KEY);
        } catch (_) {}
    }
});

document.addEventListener('htmx:beforeRequest', function(event) {
    var form = requestForm(event);
    var element = event.detail.elt;
    var fallback = element && /^(BUTTON|INPUT)$/.test(element.tagName) ? element : null;
    markDraftClearPending(form);
    markPending(form, pendingControl(form, fallback));
});

document.addEventListener('htmx:afterRequest', function(event) {
    var form = requestForm(event);
    restorePending(form);
    if (!event.detail.successful) return;
    var xhr = event.detail.xhr;
    var message = xhr.getResponseHeader('X-Feedback-Message');
    var kind = xhr.getResponseHeader('X-Feedback-Kind') || 'success';
    var clearKey = xhr.getResponseHeader('X-Draft-Clear');
    if (clearKey) clearDraft(clearKey);
    else clearDraftPending(form);
    if (message) showFeedback(message, kind);
    else if (xhr.responseText === 'saved') flashSaved();
});

document.addEventListener('htmx:responseError', function(event) {
    var xhr = event.detail.xhr;
    var form = requestForm(event);
    restorePending(form);
    clearDraftPending(form);
    var message = xhr.getResponseHeader('X-Feedback-Message');
    if (message) showFeedback(message, xhr.getResponseHeader('X-Feedback-Kind') || 'error');
    else if (xhr.status === 422) showFeedback('Check the submitted values and try again.', 'error');
    else showFeedback('Server error. Your changes were not confirmed.', 'error');
});

document.addEventListener('htmx:beforeSwap', function(event) {
    if (event.detail.xhr.getResponseHeader('X-Feedback-Swap') === 'true') event.detail.shouldSwap = true;
});

document.addEventListener('htmx:sendError', function(event) {
    var form = requestForm(event);
    saveNetworkDraft(form);
    restorePending(form);
    clearDraftPending(form);
    showFeedback('Network error. Your changes were not confirmed.', 'error');
});

document.addEventListener('htmx:timeout', function(event) {
    var form = requestForm(event);
    saveNetworkDraft(form);
    restorePending(form);
    clearDraftPending(form);
    showFeedback('Request timed out. Your changes were not confirmed.', 'error');
});

document.addEventListener('input', updateRestoredDraft);
document.addEventListener('click', function(event) {
    if (event.target.closest('[data-feedback-dismiss]')) document.getElementById('feedback-error').replaceChildren();
    var jobDismiss = event.target.closest('[data-job-dismiss]');
    if (jobDismiss) dismissNotification(jobDismiss.closest('article'));
});

window.addEventListener('pageshow', function() {
    document.querySelectorAll('form[data-feedback-pending="true"]').forEach(restorePending);
});

document.addEventListener('DOMContentLoaded', function() {
    var params = new URLSearchParams(window.location.search);
    var clearKey = params.get('clear_draft');
    if (clearKey && hasDraftClearPending(clearKey)) clearDraft(clearKey);
    if (params.has('msg') || params.has('err') || params.has('clear_draft')) {
        params.delete('msg');
        params.delete('err');
        params.delete('clear_draft');
        var query = params.toString();
        history.replaceState(null, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
    }
    restoreNetworkDrafts();
    scanNotifications();
});

// Three-state toggle cycle: pending -> done -> skipped -> pending
function cycleStatus(btn) {
    var input = btn.closest('.toggle-group').querySelector('input[type="hidden"]');
    var states = ['pending', 'done', 'skipped'];
    var icons = {
        'pending': '',
        'done': '<i data-lucide="check" style="width:16px;height:16px;"></i>',
        'skipped': '<i data-lucide="minus" style="width:16px;height:16px;"></i>'
    };
    var classes = {'pending': 'active-pending', 'done': 'active-done', 'skipped': 'active-skipped'};
    var current = input.value;
    var next = states[(states.indexOf(current) + 1) % 3];
    input.value = next;
    btn.innerHTML = icons[next];
    btn.setAttribute('aria-pressed', next === 'done' ? 'true' : 'false');
    var stateLabel = btn.parentElement.querySelector('.toggle-state');
    if (stateLabel) stateLabel.textContent = {pending: 'Pending', done: 'Done', skipped: 'Skipped'}[next];
    // Keep the stated count in step with the toggles, or it reads as stale until
    // the page is saved. Read el.value (the live property), never the value
    // ATTRIBUTE, which still holds whatever the server rendered.
    var counter = document.getElementById('done-count');
    if (counter) {
        var done = 0;
        document.querySelectorAll('.toggle-group input[type="hidden"]').forEach(function (el) {
            if (el.value === 'done') done += 1;
        });
        counter.textContent = done + counter.textContent.slice(counter.textContent.indexOf('/'));
    }
    btn.className = 'toggle-btn ' + classes[next];
    if (typeof lucide !== 'undefined') {
        lucide.createIcons({ nodes: [btn] });
    }
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

// ═══════════════════════════════════════════
// Theme Toggle
// ═══════════════════════════════════════════
function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function updateThemeIcon() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var isDark = getTheme() === 'dark';
    btn.innerHTML = isDark
        ? '<i data-lucide="sun" style="width:20px;height:20px;"></i>'
        : '<i data-lucide="moon" style="width:20px;height:20px;"></i>';
    if (typeof lucide !== 'undefined') {
        lucide.createIcons({ nodes: [btn] });
    }
}

function toggleTheme() {
    var current = getTheme();
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('virgil-theme', next);

    // Update meta theme-color
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = next === 'dark' ? '#06080d' : '#f5f7fa';

    updateThemeIcon();

    // Rebuild charts with new colors
    if (window.rebuildAllCharts) window.rebuildAllCharts();

    // Fire-and-forget save to server for cross-device sync
    var csrf = document.querySelector('meta[name="csrf-token"]');
    if (csrf) {
        fetch('/api/settings/theme', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrf.content
            },
            body: JSON.stringify({ theme: next })
        }).catch(function() {});
    }
}

// ═══════════════════════════════════════════
// Keyboard Shortcuts
// ═══════════════════════════════════════════
(function() {
    var gPressed = false;
    var gTimeout = null;

    var features = JSON.parse(document.body.getAttribute('data-features') || '{}');
    var navMap = {
        'd': '/',
        'l': '/daily',
        't': '/training',
        'o': '/oura',
        'b': '/bloodwork',
        's': '/settings',
        'e': '/experiments',
        'g': '/goals'
    };
    if (features.no_porn) navMap['f'] = '/feniks';

    document.addEventListener('keydown', function(e) {
        // Skip if focused on input
        var tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
            return;
        }

        var key = e.key;

        // Escape: close shortcut overlay
        if (key === 'Escape') {
            closeShortcutOverlay();
            return;
        }

        // ? : toggle shortcut overlay
        if (key === '?') {
            e.preventDefault();
            toggleShortcutOverlay();
            return;
        }

        // Arrow keys: prev/next on daily page
        if (key === 'ArrowLeft' || key === 'ArrowRight') {
            var nav = document.querySelector('.date-nav');
            if (nav) {
                var links = nav.querySelectorAll('a.btn');
                if (key === 'ArrowLeft' && links[0]) links[0].click();
                if (key === 'ArrowRight' && links[1]) links[1].click();
                e.preventDefault();
            }
            return;
        }

        // "g" prefix navigation
        if (gPressed) {
            gPressed = false;
            clearTimeout(gTimeout);
            var target = navMap[key];
            if (target) {
                e.preventDefault();
                window.location.href = target;
            }
            return;
        }

        if (key === 'g') {
            gPressed = true;
            gTimeout = setTimeout(function() { gPressed = false; }, 800);
            return;
        }
    });
})();

function toggleShortcutOverlay() {
    var existing = document.querySelector('.shortcut-overlay');
    if (existing) {
        existing.remove();
        return;
    }
    var overlay = document.createElement('div');
    overlay.className = 'shortcut-overlay';
    overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
    var feat = JSON.parse(document.body.getAttribute('data-features') || '{}');
    var feniksRow = feat.feniks ? '<div class="shortcut-row"><span>Feniks</span><span class="kbd-combo"><kbd>g</kbd><kbd>f</kbd></span></div>' : '';
    overlay.innerHTML =
        '<div class="shortcut-card">' +
        '<h3>Keyboard Shortcuts</h3>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">Navigation (press g then...)</div>' +
        '<div class="shortcut-row"><span>Dashboard</span><span class="kbd-combo"><kbd>g</kbd><kbd>d</kbd></span></div>' +
        '<div class="shortcut-row"><span>Daily</span><span class="kbd-combo"><kbd>g</kbd><kbd>l</kbd></span></div>' +
        '<div class="shortcut-row"><span>Training</span><span class="kbd-combo"><kbd>g</kbd><kbd>t</kbd></span></div>' +
        feniksRow +
        '<div class="shortcut-row"><span>Oura</span><span class="kbd-combo"><kbd>g</kbd><kbd>o</kbd></span></div>' +
        '<div class="shortcut-row"><span>Bloodwork</span><span class="kbd-combo"><kbd>g</kbd><kbd>b</kbd></span></div>' +
        '<div class="shortcut-row"><span>Experiments</span><span class="kbd-combo"><kbd>g</kbd><kbd>e</kbd></span></div>' +
        '<div class="shortcut-row"><span>Goals</span><span class="kbd-combo"><kbd>g</kbd><kbd>g</kbd></span></div>' +
        '<div class="shortcut-row"><span>Settings</span><span class="kbd-combo"><kbd>g</kbd><kbd>s</kbd></span></div>' +
        '</div>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">Daily Page</div>' +
        '<div class="shortcut-row"><span>Previous day</span><span class="kbd-combo"><kbd>&larr;</kbd></span></div>' +
        '<div class="shortcut-row"><span>Next day</span><span class="kbd-combo"><kbd>&rarr;</kbd></span></div>' +
        '</div>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">General</div>' +
        '<div class="shortcut-row"><span>Show shortcuts</span><span class="kbd-combo"><kbd>?</kbd></span></div>' +
        '<div class="shortcut-row"><span>Close overlay</span><span class="kbd-combo"><kbd>Esc</kbd></span></div>' +
        '</div>' +
        '</div>';
    document.body.appendChild(overlay);
}

function closeShortcutOverlay() {
    var overlay = document.querySelector('.shortcut-overlay');
    if (overlay) overlay.remove();
}

// ═══════════════════════════════════════════
// Swipe Gestures
// ═══════════════════════════════════════════
(function() {
    var startX = 0, startY = 0, startTime = 0;

    document.addEventListener('touchstart', function(e) {
        var touch = e.touches[0];
        startX = touch.clientX;
        startY = touch.clientY;
        startTime = Date.now();
    }, { passive: true });

    document.addEventListener('touchend', function(e) {
        var touch = e.changedTouches[0];
        var dx = touch.clientX - startX;
        var dy = touch.clientY - startY;
        var dt = Date.now() - startTime;

        // Require: min 80px horizontal, max 300ms, horizontal > vertical
        if (Math.abs(dx) < 80 || dt > 300 || Math.abs(dx) < Math.abs(dy)) return;

        var attr = dx < 0 ? 'data-swipe-left' : 'data-swipe-right';
        var el = e.target;
        while (el && el !== document.body) {
            if (el.hasAttribute && el.hasAttribute(attr)) {
                window.location.href = el.getAttribute(attr);
                return;
            }
            el = el.parentElement;
        }
    }, { passive: true });
})();
