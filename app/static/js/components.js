// Alpine components. Alpine here is its CSP build, which evaluates no code from
// attributes: anything past a simple expression lives in a component registered
// below. Loaded before Alpine (which is deferred), so alpine:init has not fired.

// Polish strings for our scripts (#152), rendered into the page by base.html.
// English is the message id, so a missing entry shows the English text.
var i18nCatalog = null;
function gettext(message) {
    if (i18nCatalog === null) {
        var block = document.getElementById('i18n-catalog');
        try {
            i18nCatalog = block ? JSON.parse(block.textContent) : {};
        } catch (e) {
            i18nCatalog = {};
        }
    }
    return i18nCatalog[message] || message;
}

// The phone tab bar's More menu.
function bottomBar() {
    return {
        openMenu: null,
        get moreOpen() {
            return (this.openMenu === 'more').toString();
        },
        closeMenu() {
            if (!this.openMenu) return;
            this.openMenu = null;
            this.$refs.moreButton.focus();
        },
        // Tabbing out of the menu closes it; moving within it does not.
        leaveMenu(event) {
            if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) this.openMenu = null;
        }
    };
}

// Settings: adding an LLM provider fills in a default model per provider.
function llmProviderForm() {
    return {
        provider: 'anthropic',
        models: {
            'anthropic': 'anthropic/claude-sonnet-4-20250514',
            'openai': 'openai/gpt-4o-mini',
            'gemini': 'gemini/gemini-3-flash-preview',
            'mistral': 'mistral/mistral-small-latest',
            'groq': 'groq/llama-3.3-70b-versatile',
            'ollama': 'ollama/llama3.2',
            'other': ''
        },
        model: 'anthropic/claude-sonnet-4-20250514',
        isOther: false,
        setProvider(event) {
            var val = event.target.value;
            this.provider = val;
            this.isOther = val === 'other';
            if (!this.isOther) this.model = this.models[val];
        }
    };
}

function experimentForm() {
    const colors = ['#22c55e', '#ef4444', '#3b82f6', '#eab308', '#a855f7', '#f97316'];
    let ci = 0;
    return {
        metrics: [
            {name: '', color: '#22c55e', kind: 'duration', target: '', period: 'week', source_match: ''}
        ],
        newMetric() {
            ci = (ci + 1) % colors.length;
            return {name: '', color: colors[ci], kind: 'count', target: '', period: 'week', source_match: ''};
        },
        hasDuration() {
            return this.metrics.some(m => m.kind === 'duration');
        },
        // In place of x-model on a metric's field: the CSP build refuses the
        // property assignment x-model would generate for m.kind. Through
        // this.metrics, so the write is reactive and x-show follows it.
        setField(index, key, event) {
            this.metrics[index][key] = event.target.value;
        }
    }
}

// Filters the library rows already rendered on the page. It adds no request and
// owns no state the server needs. A section is opened when it holds a match, or
// a hit inside a collapsed section would look like no hit at all.
function libraryFilter() {
    return {
        query: '',
        section: '',
        apply() {
            const needle = this.query.trim().toLowerCase();
            const section = this.section;
            document.querySelectorAll('[data-library-block]').forEach(function (block) {
                if (section !== '' && block.dataset.libraryBlock !== section) {
                    block.hidden = true;
                    return;
                }
                block.hidden = false;
                let matches = 0;
                block.querySelectorAll('[data-library-name]').forEach(function (row) {
                    const haystack = (row.dataset.libraryName + ' ' + (row.dataset.libraryTags || '')).toLowerCase();
                    const hit = needle === '' || haystack.indexOf(needle) !== -1;
                    row.hidden = !hit;
                    if (hit) matches += 1;
                });
                if (needle !== '') block.open = matches > 0;
            });
        }
    };
}

function wodConfirmForm(baseCount, maxRows) {
    return {
        base: baseCount,
        maxRows: maxRows,
        extraRows: [],
        // confirm_wod rejects the WHOLE submission when entry_count exceeds its
        // bound, so growing past it would cost the user every row they had
        // typed. Stop offering rows at the limit instead.
        get atLimit() {
            return this.base + this.extraRows.length >= this.maxRows;
        },
        // Appends a new row at the END of the table (never inserted after the
        // source row) so existing entry_N_* field names never get renumbered.
        // The new row's index continues from the server-rendered count plus
        // however many extra rows already exist, so it can never collide with
        // an existing name.
        addRow(movement, setNumber, equipment) {
            if (this.atLimit) return;
            this.extraRows.push({
                index: this.base + this.extraRows.length,
                movement: movement,
                setNumber: setNumber,
                equipment: equipment || ''
            });
        },
        // Another set of the movement in the row the button sits in, read from
        // the fields themselves: an added row's state is only where it started.
        addSet(buttonEl) {
            const tr = buttonEl.closest('tr');
            const movementSelect = tr.querySelector('select[name$="_movement"]');
            const setNumberInput = tr.querySelector('input[name$="_set_number"]');
            const equipmentSelect = tr.querySelector('select[name$="_equipment"]');
            const movement = movementSelect ? movementSelect.value : '';
            const currentSetNumber = parseInt(setNumberInput && setNumberInput.value, 10) || 0;
            // Another set keeps the equipment: the dumbbell does not change mid-movement.
            this.addRow(movement, currentSetNumber + 1, equipmentSelect ? equipmentSelect.value : '');
        },
        // A different movement: blank select, set 1. Cloning and incrementing
        // (what addSet does) is wrong here -- it would pre-fill someone else's
        // movement and start the new exercise at set 2.
        addExercise() {
            this.addRow('', 1);
        },
        // The server reads entry_count to know how many rows to expect.
        syncCount() {
            this.$refs.entryCount.value = this.base + this.extraRows.length;
        }
    };
}

// One row's picker. Alpine chains nested x-data scopes, so `addSet($el)` inside
// a row still resolves the form component's own method above. This component
// deliberately does not define one.
function movementRow() {
    return {
        query: '',
        ready: false,
        init() {
            // Alpine arrived, so the search box is safe to show. Rendering it
            // unconditionally would give a user whose scripts failed to load
            // an input that filters nothing.
            this.ready = true;
            this.promoteRecent();
        },
        // Recently logged movements first. Everything else keeps its library
        // order, so a user who knows where a movement lives still finds it
        // there.
        promoteRecent() {
            const select = this.$refs.select;
            if (!select) return;
            const recent = select.querySelectorAll('option[data-recent="1"]');
            if (!recent.length) return;
            const group = document.createElement('optgroup');
            group.label = gettext('Recently used');
            recent.forEach(function (opt) {
                const clone = opt.cloneNode(true);
                // Otherwise one select carries two selected options: the
                // original and this copy of it.
                clone.selected = false;
                group.appendChild(clone);
            });
            select.insertBefore(group, select.firstElementChild.nextSibling);
        },
        // Name or tag, substring match. Older Safari ignores `hidden` on an
        // <option>; there the grouping and the native type-ahead still work, so
        // the screen degrades rather than breaks.
        filter() {
            const select = this.$refs.select;
            if (!select) return;
            const needle = this.query.trim().toLowerCase();
            select.querySelectorAll('option').forEach(function (opt) {
                if (!opt.value) return;
                const haystack = (opt.value + ' ' + (opt.dataset.tags || '')).toLowerCase();
                opt.hidden = needle !== '' && haystack.indexOf(needle) === -1;
            });
        }
    };
}

document.addEventListener('alpine:init', function () {
    Alpine.data('bottomBar', bottomBar);
    Alpine.data('llmProviderForm', llmProviderForm);
    Alpine.data('experimentForm', experimentForm);
    Alpine.data('libraryFilter', libraryFilter);
    Alpine.data('wodConfirmForm', wodConfirmForm);
    Alpine.data('movementRow', movementRow);
});
