// Initialize Lucide icons
document.addEventListener('DOMContentLoaded', function() {
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }
    initCalendarInteractions();
    initDatePickers();
    updateThemeIcon();
});

// Month and weekday names for the date picker, in the page's language.
function datePickerLocale() {
    var days = [gettext('Sun'), gettext('Mon'), gettext('Tue'), gettext('Wed'), gettext('Thu'), gettext('Fri'), gettext('Sat')];
    var longDays = [gettext('Sunday'), gettext('Monday'), gettext('Tuesday'), gettext('Wednesday'),
        gettext('Thursday'), gettext('Friday'), gettext('Saturday')];
    var months = [gettext('Jan'), gettext('Feb'), gettext('Mar'), gettext('Apr'), gettext('May'), gettext('Jun'),
        gettext('Jul'), gettext('Aug'), gettext('Sep'), gettext('Oct'), gettext('Nov'), gettext('Dec')];
    var longMonths = [gettext('January'), gettext('February'), gettext('March'), gettext('April'), gettext('May'),
        gettext('June'), gettext('July'), gettext('August'), gettext('September'), gettext('October'),
        gettext('November'), gettext('December')];
    return {
        firstDayOfWeek: 1,
        weekdays: { shorthand: days, longhand: longDays },
        months: { shorthand: months, longhand: longMonths }
    };
}

// Flatpickr datepicker for all date inputs
function initDatePickers(root) {
    if (typeof window.flatpickr !== 'function') return;
    var container = root || document;
    var inputs = container.querySelectorAll('input[type="date"]:not(.flatpickr-input)');
    inputs.forEach(function(el) {
        flatpickr(el, {
            dateFormat: 'Y-m-d',
            defaultDate: el.value || undefined,
            allowInput: true,
            disableMobile: true,
            locale: datePickerLocale(),
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
    initCalendarInteractions(e.detail.target);
    initDatePickers(e.detail.target);
    scanNotifications();
});

// Shared calendar summaries plus optional click-through dialogs.
function initCalendarInteractions(root) {
    var container = root || document;
    var tooltip = document.getElementById('calendar-tooltip');
    if (!tooltip) {
        tooltip = document.createElement('div');
        tooltip.id = 'calendar-tooltip';
        tooltip.className = 'year-cal-tooltip';
        tooltip.hidden = true;
        document.body.appendChild(tooltip);
    }

    function hideTip() {
        tooltip.hidden = true;
    }

    function showTip(el) {
        var tip = el.dataset.calendarTip;
        if (!tip) return;
        tooltip.textContent = tip;
        tooltip.hidden = false;
        var rect = el.getBoundingClientRect();
        var centered = rect.left + window.scrollX + rect.width / 2 - tooltip.offsetWidth / 2;
        tooltip.style.left = Math.max(8, Math.min(centered, window.scrollX + window.innerWidth - tooltip.offsetWidth - 8)) + 'px';
        var above = rect.top + window.scrollY - tooltip.offsetHeight - 6;
        tooltip.style.top = (above > window.scrollY ? above : rect.bottom + window.scrollY + 6) + 'px';
    }

    container.querySelectorAll('[data-calendar-tip]').forEach(function(dot) {
        if (dot.dataset.calendarBound === 'true') return;
        dot.dataset.calendarBound = 'true';
        dot.addEventListener('mouseenter', function() { showTip(dot); });
        dot.addEventListener('mouseleave', hideTip);
        dot.addEventListener('focus', function() { showTip(dot); });
        dot.addEventListener('blur', hideTip);
        dot.addEventListener('click', function(e) {
            e.stopPropagation();
            var dialogId = dot.dataset.calendarDialog;
            var dialog = dialogId && document.getElementById(dialogId);
            if (dialog && typeof dialog.showModal === 'function') {
                e.preventDefault();
                hideTip();
                dialog.openedBy = dot;
                if (!dialog.open) dialog.showModal();
                return;
            }
            showTip(dot);
            window.setTimeout(hideTip, 2500);
        });
    });

    container.querySelectorAll('dialog.training-dialog').forEach(function(dialog) {
        if (dialog.dataset.calendarBound === 'true') return;
        dialog.dataset.calendarBound = 'true';
        dialog.addEventListener('click', function(e) {
            if (e.target === dialog) dialog.close();
        });
        // Back to the day that opened it, or a keyboard user starts over at the top.
        // A dialog the server rendered open came from that day's calendar link.
        dialog.addEventListener('close', function() {
            var opener = dialog.openedBy ||
                document.querySelector('[data-calendar-dialog="' + dialog.id + '"]');
            if (opener && opener.isConnected) opener.focus();
        });
        if (dialog.hasAttribute('open') && typeof dialog.showModal === 'function') {
            dialog.removeAttribute('open');
            try {
                dialog.showModal();
            } catch (error) {
                dialog.setAttribute('open', '');
            }
        }
    });

    if (document.body.dataset.calendarDismissBound !== 'true') {
        document.body.dataset.calendarDismissBound = 'true';
        document.addEventListener('click', hideTip);
    }
}

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
var refreshedAndyJobs = new Set();

function dismissNotification(node) {
    var target = (node && node.closest('#job-notifications article')) || node;
    if (!target || target.dataset.dismissing === 'true') return;
    var forgotNamedJob = clearNamedJob(target);
    target.dataset.dismissing = 'true';
    target.classList.add('is-leaving');
    window.setTimeout(function() { target.remove(); }, TOAST_LEAVE_MS);
    if (forgotNamedJob && window.htmx) {
        htmx.ajax('GET', '/api/jobs/active', { target: '#job-notifications', swap: 'innerHTML' });
    }
}

function clearNamedJob(node) {
    var statusNode = node && (node.matches('[data-job-id]') ? node : node.querySelector('[data-job-id]'));
    if (!statusNode) return false;
    var params = new URLSearchParams(window.location.search);
    if (params.get('job_id') !== statusNode.dataset.jobId) return false;
    params.delete('job_id');
    var query = params.toString();
    history.replaceState(null, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
    return true;
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
        clearNamedJob(node);
        if (node.dataset.jobKind === 'andy_generation') refreshAndySuggestions(node);
        armAutoDismiss(node, JOB_TOAST_DISMISS_MS);
    });
}

function refreshAndySuggestions(jobNode) {
    var jobId = jobNode.dataset.jobId;
    if (!jobId || refreshedAndyJobs.has(jobId) || !document.getElementById('andy-card')) return;
    refreshedAndyJobs.add(jobId);
    fetch(window.location.pathname, { headers: { 'X-Requested-With': 'fetch' } })
        .then(function(response) {
            if (!response.ok) throw new Error('A.N.D.Y. refresh failed');
            return response.text();
        })
        .then(function(html) {
            var page = new DOMParser().parseFromString(html, 'text/html');
            ['andy-card', 'andy-generate-form'].forEach(function(id) {
                var current = document.getElementById(id);
                var replacement = page.getElementById(id);
                if (!current || !replacement) return;
                current.replaceWith(replacement);
                if (window.htmx) htmx.process(replacement);
                initDatePickers(replacement);
            });
            if (typeof lucide !== 'undefined') lucide.createIcons();
        })
        .catch(function() {
            refreshedAndyJobs.delete(jobId);
            showFeedback(gettext('Suggestions are ready, but the daily card could not refresh.'), 'error');
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
        dismiss.setAttribute('aria-label', gettext('Dismiss error'));
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

function flashSaved() {
    showFeedback(gettext('Saved'), 'success');
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
            showFeedback(gettext('A draft from a failed network request was restored.'), 'error');
        } catch (_) {
            clearDraft(form.dataset.draftKey);
        }
    });
}

function pendingControl(form, fallback) {
    return (form && form._feedbackSubmitter) || fallback || null;
}

// Disabling the focused submit button drops keyboard focus to the page, and
// htmx can then put nothing back. Its id is kept to refocus it, or the control
// with that id in the swapped-in markup, once the request settles.
var pendingFocusId = null;

function refocusPending() {
    if (!pendingFocusId) return;
    var active = document.activeElement;
    if (!active || active === document.body) {
        var target = document.getElementById(pendingFocusId);
        if (target && !target.disabled) target.focus({ preventScroll: true });
    }
    pendingFocusId = null;
}

function markPending(form, control) {
    if (!form || form.dataset.feedbackPending === 'true') return;
    form.dataset.feedbackPending = 'true';
    form.setAttribute('aria-busy', 'true');
    if (!control) {
        showFeedback(gettext('Saving...'), 'pending');
        return;
    }
    form._feedbackSubmitter = control;
    if (document.activeElement === control && control.id) pendingFocusId = control.id;
    control.dataset.feedbackOriginalHtml = control.innerHTML;
    control.dataset.feedbackWasDisabled = control.disabled ? 'true' : 'false';
    control.disabled = true;
    control.setAttribute('aria-disabled', 'true');
    control.textContent = control.dataset.pendingLabel || gettext('Working...');
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
    // The attribute, not form.action: a field named "action" (the WOD confirm
    // buttons) shadows that property.
    if ((form.getAttribute('action') || '').endsWith('/logout')) {
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

document.addEventListener('htmx:afterSettle', refocusPending);

// The server percent-encodes the message: header values cannot carry Polish letters.
function feedbackHeader(xhr) {
    var message = xhr.getResponseHeader('X-Feedback-Message');
    if (!message) return message;
    try {
        return decodeURIComponent(message);
    } catch (e) {
        return message;
    }
}

document.addEventListener('htmx:afterRequest', function(event) {
    var form = requestForm(event);
    restorePending(form);
    // A request that swapped nothing (an error, hx-swap="none") settles nowhere.
    if (!event.detail.successful || !event.detail.target || !event.detail.target.isConnected) refocusPending();
    if (!event.detail.successful) return;
    var xhr = event.detail.xhr;
    var message = feedbackHeader(xhr);
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
    var message = feedbackHeader(xhr);
    // A failed read lost nothing; the offline banner reports an unreachable Virgil.
    if (event.detail.requestConfig.verb === 'get' && isProxyUnreachable(xhr.status)) return;
    if (message) showFeedback(message, xhr.getResponseHeader('X-Feedback-Kind') || 'error');
    else if (xhr.status === 422) showFeedback(gettext('Check the submitted values and try again.'), 'error');
    else showFeedback(gettext('Server error. Your changes were not confirmed.'), 'error');
});

document.addEventListener('htmx:beforeSwap', function(event) {
    if (event.detail.xhr.getResponseHeader('X-Feedback-Swap') === 'true') event.detail.shouldSwap = true;
});

document.addEventListener('htmx:sendError', function(event) {
    var form = requestForm(event);
    saveNetworkDraft(form);
    restorePending(form);
    clearDraftPending(form);
    // A failed read lost nothing; the offline banner reports the connection.
    if (event.detail.requestConfig && event.detail.requestConfig.verb === 'get') return;
    showFeedback(gettext('Network error. Your changes were not confirmed.'), 'error');
});

document.addEventListener('htmx:timeout', function(event) {
    var form = requestForm(event);
    saveNetworkDraft(form);
    restorePending(form);
    clearDraftPending(form);
    showFeedback(gettext('Request timed out. Your changes were not confirmed.'), 'error');
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
    if (stateLabel) stateLabel.textContent = {pending: gettext('Pending'), done: gettext('Done'), skipped: gettext('Skipped')}[next];
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
// Offline: read-only until the server answers again
// ═══════════════════════════════════════════
// There is no offline write queue, so while Virgil is unreachable every
// control that writes is disabled and a banner says why. Reading stays as it
// was. Only controls disabled here are re-enabled, never one the page disabled.
var OFFLINE_PROBE_MS = 10000;
var OFFLINE_PROBE_TIMEOUT_MS = 5000;
var WRITE_CONTROL = /^(BUTTON|INPUT|SELECT|TEXTAREA|FIELDSET)$/;
var OFFLINE_TEXT = gettext('Offline. Read-only until Virgil is back.');
var SIGNED_OUT_TEXT = gettext('Signed out. Reload the page to sign in.');
var offlineProbe = null;
var probeRunning = false;

function isWriteForm(form) {
    return form.method.toUpperCase() === 'POST' ||
        ['hx-post', 'hx-put', 'hx-patch', 'hx-delete'].some(function(name) { return form.hasAttribute(name); });
}

function writeControls() {
    var controls = [];
    document.querySelectorAll('form').forEach(function(form) {
        if (isWriteForm(form)) controls.push.apply(controls, form.elements);
    });
    document.querySelectorAll('[hx-post],[hx-put],[hx-patch],[hx-delete]').forEach(function(el) {
        if (el.tagName === 'FORM') return;
        if (WRITE_CONTROL.test(el.tagName)) controls.push(el);
        else controls.push.apply(controls, el.querySelectorAll('button,input,select,textarea'));
    });
    return controls.filter(function(el) { return WRITE_CONTROL.test(el.tagName); });
}

function isOffline() {
    return document.body.classList.contains('is-offline');
}

function showOfflineText(text) {
    var banner = document.getElementById('offline-banner');
    var span = banner.querySelector('span');
    if (span && span.textContent === text) return;
    banner.innerHTML = '<i data-lucide="wifi-off" aria-hidden="true"></i><span></span>';
    banner.querySelector('span').textContent = text;
    if (typeof lucide !== 'undefined') lucide.createIcons({ nodes: [banner] });
}

// Also run while already offline: a request that was in flight when the
// connection dropped re-enables its own button when it ends.
function disableWriteControls() {
    writeControls().forEach(function(el) {
        if (el.disabled) return;
        el.disabled = true;
        el.dataset.offlineDisabled = 'true';
    });
}

function setOffline(offline) {
    var banner = document.getElementById('offline-banner');
    if (!banner || offline === isOffline()) return;
    document.body.classList.toggle('is-offline', offline);
    if (offline) {
        showOfflineText(OFFLINE_TEXT);
        disableWriteControls();
        offlineProbe = setInterval(probeConnection, OFFLINE_PROBE_MS);
        return;
    }
    banner.replaceChildren();
    document.querySelectorAll('[data-offline-disabled]').forEach(function(el) {
        el.disabled = false;
        delete el.dataset.offlineDisabled;
    });
    clearInterval(offlineProbe);
    offlineProbe = null;
    showFeedback(gettext('Connection is back.'), 'success');
}

// Only Virgil's own answer means it is back: a proxy in front of it answers
// with a 5xx while Virgil is down. A redirect is a sign-in page in front of
// Virgil (the session expired), which a reload fixes and waiting does not.
function probeConnection() {
    if (probeRunning) return;
    probeRunning = true;
    var options = { cache: 'no-store', redirect: 'manual' };
    if (window.AbortSignal && AbortSignal.timeout) options.signal = AbortSignal.timeout(OFFLINE_PROBE_TIMEOUT_MS);
    fetch('/livez', options)
        .then(function(response) {
            if (response.ok) setOffline(false);
            else if (response.type === 'opaqueredirect') showOfflineText(SIGNED_OUT_TEXT);
        }, function() {})
        .then(function() { probeRunning = false; });
}

// What a proxy answers when it cannot reach Virgil: 502, 504, and Cloudflare's
// 520 to 530 (530 is a tunnel with no connection).
function isProxyUnreachable(status) {
    return status === 502 || status === 504 || (status >= 520 && status <= 530);
}

window.addEventListener('offline', function() { setOffline(true); });
window.addEventListener('online', probeConnection);
// The browser can report online while Virgil is unreachable (a dead tunnel or
// NAS): a request that fails on the network, or that a proxy answers for it,
// shows that too.
document.addEventListener('htmx:sendError', function() { setOffline(true); });
document.addEventListener('htmx:responseError', function(event) {
    if (isProxyUnreachable(event.detail.xhr.status)) setOffline(true);
});
// Any request Virgil answered proves it is reachable again; one that ends
// while still offline must not leave its controls enabled.
document.addEventListener('htmx:afterRequest', function(event) {
    if (!isOffline()) return;
    if (event.detail.successful) setOffline(false);
    else disableWriteControls();
});
// A write a disabled control did not cover never leaves the page.
document.addEventListener('htmx:confirm', function(event) {
    if (!isOffline() || event.detail.verb === 'get') return;
    event.preventDefault();
    showFeedback(gettext('No connection. Nothing was saved.'), 'error');
});
document.addEventListener('submit', function(event) {
    if (!isOffline() || event.target.tagName !== 'FORM' || !isWriteForm(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    showFeedback(gettext('No connection. Nothing was saved.'), 'error');
}, true);
document.addEventListener('DOMContentLoaded', function() {
    if (!navigator.onLine) setOffline(true);
});

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
    if (features.freedom) navMap['f'] = '/freedom';

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

// The overlay is a modal dialog: it takes focus, keeps Tab inside (it holds
// nothing focusable but itself) and gives focus back to where it was opened.
var shortcutOverlayOpener = null;

function toggleShortcutOverlay() {
    // No keyboard to use them with on a touch-only screen.
    if (window.matchMedia && window.matchMedia('(hover: none)').matches) return;
    if (document.querySelector('.shortcut-overlay')) {
        closeShortcutOverlay();
        return;
    }
    shortcutOverlayOpener = document.activeElement;
    var overlay = document.createElement('div');
    overlay.className = 'shortcut-overlay';
    overlay.onclick = function(e) { if (e.target === overlay) closeShortcutOverlay(); };
    overlay.addEventListener('keydown', function(e) {
        if (e.key === 'Tab') e.preventDefault();
    });
    var feat = JSON.parse(document.body.getAttribute('data-features') || '{}');
    var freedomRow = feat.freedom ? '<div class="shortcut-row"><span>' + gettext('Freedom') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>f</kbd></span></div>' : '';
    overlay.innerHTML =
        '<div class="shortcut-card" role="dialog" aria-modal="true" aria-labelledby="shortcut-title" tabindex="-1">' +
        '<h3 id="shortcut-title">' + gettext('Keyboard Shortcuts') + '</h3>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">' + gettext('Navigation (press g then...)') + '</div>' +
        '<div class="shortcut-row"><span>' + gettext('Dashboard') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>d</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Daily') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>l</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Training') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>t</kbd></span></div>' +
        freedomRow +
        '<div class="shortcut-row"><span>' + gettext('Oura') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>o</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Bloodwork') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>b</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Experiments') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>e</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Goals') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>g</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Settings') + '</span><span class="kbd-combo"><kbd>g</kbd><kbd>s</kbd></span></div>' +
        '</div>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">' + gettext('Daily Page') + '</div>' +
        '<div class="shortcut-row"><span>' + gettext('Previous day') + '</span><span class="kbd-combo"><kbd>&larr;</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Next day') + '</span><span class="kbd-combo"><kbd>&rarr;</kbd></span></div>' +
        '</div>' +
        '<div class="shortcut-section">' +
        '<div class="shortcut-section-title">' + gettext('General') + '</div>' +
        '<div class="shortcut-row"><span>' + gettext('Show shortcuts') + '</span><span class="kbd-combo"><kbd>?</kbd></span></div>' +
        '<div class="shortcut-row"><span>' + gettext('Close overlay') + '</span><span class="kbd-combo"><kbd>Esc</kbd></span></div>' +
        '</div>' +
        '</div>';
    document.body.appendChild(overlay);
    overlay.querySelector('.shortcut-card').focus();
}

function closeShortcutOverlay() {
    var overlay = document.querySelector('.shortcut-overlay');
    if (!overlay) return;
    overlay.remove();
    if (shortcutOverlayOpener && shortcutOverlayOpener.isConnected) shortcutOverlayOpener.focus();
    shortcutOverlayOpener = null;
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

// Textareas marked .autosize grow with their text. CSS field-sizing does this where
// supported; this covers the rest (Safari). A hidden one reports no height, so it is
// left alone until it is shown.
(function () {
    function autosize(el) {
        el.style.height = 'auto';
        if (el.scrollHeight > 0) el.style.height = el.scrollHeight + 'px';
    }
    function autosizeAll(root) {
        (root || document).querySelectorAll('textarea.autosize').forEach(autosize);
    }
    document.addEventListener('input', function (e) {
        if (e.target.matches && e.target.matches('textarea.autosize')) autosize(e.target);
    });
    document.addEventListener('DOMContentLoaded', function () { autosizeAll(); });
    document.addEventListener('htmx:afterSettle', function (e) { autosizeAll(e.target); });
    document.addEventListener('toggle', function (e) { autosizeAll(e.target); }, true);
    window.addEventListener('resize', function () { autosizeAll(); });
})();

// ═══════════════════════════════════════════
// Page wiring
// ═══════════════════════════════════════════
// The CSP allows no inline event handlers, so templates mark elements with data
// attributes and these listeners act on them.

// Every mutating form carries the CSRF token, including HTMX forms without
// method="POST". A form that already has one keeps it.
document.addEventListener('submit', function(e) {
    var form = e.target;
    var mutates = form.tagName === 'FORM' && (
        form.method.toUpperCase() === 'POST' ||
        form.hasAttribute('hx-post') ||
        form.hasAttribute('hx-put') ||
        form.hasAttribute('hx-patch') ||
        form.hasAttribute('hx-delete')
    );
    if (mutates && !form.querySelector('input[name="_csrf_token"]')) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = '_csrf_token';
        input.value = document.querySelector('meta[name="csrf-token"]').content;
        form.appendChild(input);
    }
});

// data-confirm on a form or a button asks first. Capture phase, so a refusal
// stops the event before htmx or the form's own submission sees it.
function refuseUnconfirmed(event, element) {
    if (!element || confirm(element.dataset.confirm)) return;
    event.preventDefault();
    event.stopPropagation();
}
document.addEventListener('submit', function(event) {
    var submitter = event.submitter;
    // A confirming button already asked on click.
    if (submitter && submitter.hasAttribute('data-confirm')) return;
    refuseUnconfirmed(event, event.target.closest('form[data-confirm]'));
}, true);
document.addEventListener('click', function(event) {
    refuseUnconfirmed(event, event.target.closest('button[data-confirm], input[type="submit"][data-confirm]'));
}, true);

// data-navigate="/path/{value}" goes to the address with the control's value.
document.addEventListener('change', function(event) {
    var control = event.target.closest('[data-navigate]');
    if (!control) return;
    window.location.href = control.dataset.navigate.replace('{value}', encodeURIComponent(control.value));
});

// A select with data-view-switch shows the [data-view-for=its id] block whose
// data-view is its value, and hides the others.
document.addEventListener('change', function(event) {
    var select = event.target.closest('select[data-view-switch]');
    if (!select) return;
    document.querySelectorAll('[data-view-for="' + select.id + '"]').forEach(function(view) {
        view.hidden = view.dataset.view !== select.value;
    });
});

// A one-line text area: Enter saves (an htmx trigger named enter-save), never a
// newline. A trigger filter would do this inline, but htmx evaluates those.
document.addEventListener('keydown', function(event) {
    var field = event.target.closest('[data-no-newline]');
    if (event.key !== 'Enter' || !field) return;
    event.preventDefault();
    field.dispatchEvent(new CustomEvent('enter-save', { bubbles: true }));
});

document.addEventListener('click', function(event) {
    var toggle = event.target.closest('[data-cycle-status]');
    if (toggle) cycleStatus(toggle);
    if (event.target.closest('#theme-toggle')) toggleTheme();
    if (event.target.closest('#pwa-install-btn')) pwaInstall();
});

// Daily energy: the slider shows its value, and only a moved slider fills the
// field that is saved, so an untouched day is not stored as a 5.
document.addEventListener('input', function(event) {
    var slider = event.target;
    if (slider.id !== 'energy-slider') return;
    slider.style.setProperty('--pct', ((slider.value - 1) / 9 * 100) + '%');
    document.getElementById('energy-val').textContent = slider.value;
    document.getElementById('energy-field').value = slider.value;
});

// ═══════════════════════════════════════════
// Install as an app
// ═══════════════════════════════════════════
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(function() {});
}
var deferredInstall = null;
window.addEventListener('beforeinstallprompt', function(e) {
    e.preventDefault();
    deferredInstall = e;
    var button = document.getElementById('pwa-install-btn');
    if (button) button.style.display = 'inline-flex';
});
window.addEventListener('appinstalled', function() {
    deferredInstall = null;
    var button = document.getElementById('pwa-install-btn');
    if (button) button.style.display = 'none';
});
function pwaInstall() {
    if (!deferredInstall) return;
    deferredInstall.prompt();
    deferredInstall.userChoice.then(function() { deferredInstall = null; });
}

// A tab row that scrolls sideways on a phone: the active tab is brought into
// view (Security sat off-screen, so no visible tab was marked), and an edge
// with more tabs past it fades out to show the row scrolls.
function markTabOverflow(row) {
    var left = row.scrollLeft > 2;
    var right = row.scrollLeft + row.clientWidth < row.scrollWidth - 2;
    row.classList.toggle('has-more-left', left);
    row.classList.toggle('has-more-right', right);
}
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('.tabs').forEach(function(row) {
        if (row.scrollWidth <= row.clientWidth) return;
        var active = row.querySelector('a.active');
        if (active) row.scrollLeft = active.offsetLeft - (row.clientWidth - active.offsetWidth) / 2;
        markTabOverflow(row);
        row.addEventListener('scroll', function() { markTabOverflow(row); }, { passive: true });
    });
});

// data-copy="#selector" copies that element's text, for values to paste
// elsewhere, such as Oura's redirect URI.
document.addEventListener('click', function(event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    var source = document.querySelector(button.dataset.copy);
    if (!source || !navigator.clipboard) return;
    navigator.clipboard.writeText(source.textContent.trim()).then(function() {
        showFeedback(gettext('Copied.'), 'success');
    }, function() {
        showFeedback(gettext('Could not copy; select the text instead.'), 'error');
    });
});

// data-validate: the browser's checks, said on the page. Its own bubble shows
// nothing on a phone but a focus ring, so an invalid form looked like a dead
// button. Each invalid field gets its message below it, the form a summary
// beside its submit button, and the first invalid field the focus.
function clearFieldErrors(form) {
    form.querySelectorAll('.field-error').forEach(function(note) { note.remove(); });
    form.querySelectorAll('[aria-invalid="true"]').forEach(function(field) {
        field.removeAttribute('aria-invalid');
        field.removeAttribute('aria-describedby');
    });
    var summary = form.querySelector('[data-form-errors]');
    if (summary) summary.textContent = '';
}
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('form[data-validate]').forEach(function(form) { form.noValidate = true; });
});
document.addEventListener('submit', function(event) {
    var form = event.target;
    if (!form.matches || !form.matches('form[data-validate]')) return;
    if (event.submitter && event.submitter.formNoValidate) return;
    clearFieldErrors(form);
    var invalid = Array.prototype.filter.call(form.elements, function(field) {
        return field.willValidate && !field.checkValidity();
    });
    if (!invalid.length) return;
    event.preventDefault();
    event.stopPropagation();
    invalid.forEach(function(field, index) {
        var note = document.createElement('small');
        note.className = 'field-error';
        note.id = (field.id || field.name || 'field') + '-error-' + index;
        note.textContent = field.validationMessage;
        field.setAttribute('aria-invalid', 'true');
        field.setAttribute('aria-describedby', note.id);
        field.insertAdjacentElement('afterend', note);
    });
    var summary = form.querySelector('[data-form-errors]');
    if (summary) {
        summary.textContent = invalid.length === 1
            ? gettext('One field needs attention.')
            : ngettext('{num} field needs attention.', '{num} fields need attention.', invalid.length)
                .replace('{num}', invalid.length);
    }
    invalid[0].focus();
}, true);
document.addEventListener('input', function(event) {
    var field = event.target;
    if (field.getAttribute && field.getAttribute('aria-invalid') === 'true' && field.checkValidity()) {
        var note = document.getElementById(field.getAttribute('aria-describedby'));
        if (note) note.remove();
        field.removeAttribute('aria-invalid');
        field.removeAttribute('aria-describedby');
    }
});
