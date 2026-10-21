// Loaded in <head>, before the stylesheets: the saved theme applies before the
// first paint, so a light-theme reader never sees a dark flash.
(function () {
    var theme = 'dark';
    try { theme = localStorage.getItem('virgil-theme') || 'dark'; } catch (e) {}
    document.documentElement.setAttribute('data-theme', theme);
})();
