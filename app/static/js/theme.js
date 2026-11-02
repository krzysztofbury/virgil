// Loaded in <head>, before the stylesheets: the saved theme applies before the
// first paint, so a light-theme reader never sees a dark flash.
(function () {
    var theme = 'dark';
    try { theme = localStorage.getItem('virgil-theme') || 'dark'; } catch (e) {}
    document.documentElement.setAttribute('data-theme', theme);
    // The status bar matches the header at the top of the page, not one fixed colour.
    window.applyThemeColor = function (name) {
        var meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.content = name === 'light' ? '#ffffff' : '#06080d';
    };
    document.addEventListener('DOMContentLoaded', function () { window.applyThemeColor(theme); });
})();
