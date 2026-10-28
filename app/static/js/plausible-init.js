// Plausible on Virgil's public pages (#179). Loaded only by the landing page,
// the privacy policy and the terms. Sends page views of those paths alone,
// without the query string, the fragment or the referrer.
window.plausible = window.plausible || function () {
    (window.plausible.q = window.plausible.q || []).push(arguments);
};
window.plausible.init = window.plausible.init || function (options) {
    window.plausible.o = options || {};
};

var plausiblePagePaths = ["/", "/welcome", "/privacy", "/terms"];

window.plausible.init({
    autoCapturePageviews: true,
    captureOnLocalhost: false,
    fileDownloads: false,
    formSubmissions: false,
    hashBasedRouting: false,
    logging: false,
    outboundLinks: false,
    transformRequest: function (payload) {
        if (payload.n !== "pageview" || plausiblePagePaths.indexOf(window.location.pathname) === -1) {
            return null;
        }
        payload.u = window.location.origin + window.location.pathname;
        payload.r = null;
        delete payload.p;
        delete payload.$;
        return payload;
    },
});
