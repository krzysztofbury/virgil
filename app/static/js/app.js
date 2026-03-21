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
});

// Toast notification
function showToast(message, isError) {
    isError = isError || false;
    var toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = 'toast' + (isError ? ' error' : '');
    toast.style.display = 'block';
    setTimeout(function() { toast.style.display = 'none'; }, 2500);
}

// Save indicator flash
function flashSaved() {
    var el = document.getElementById('save-indicator');
    if (!el) return;
    el.textContent = '';
    void el.offsetWidth;
    el.textContent = 'saved';
}

// Listen for HTMX events
document.addEventListener('htmx:afterRequest', function(e) {
    if (e.detail.successful) {
        var resp = e.detail.xhr.responseText;
        if (resp === 'saved') {
            flashSaved();
        }
        var msg = e.detail.xhr.getResponseHeader('X-Toast');
        if (msg) showToast(msg);
    }
});
document.addEventListener('htmx:responseError', function() {
    showToast('Error saving data', true);
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
    if (features.feniks) navMap['f'] = '/feniks';

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
