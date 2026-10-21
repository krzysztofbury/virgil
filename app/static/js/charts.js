// Chart.js utility functions for Virgil

// Chart instance registry for theme rebuilds
window.virgilCharts = [];

// One typographic voice for every chart: the app's face, nothing below 12px,
// axis labels that skip rather than tilt, and dot legends in place of boxes.
if (typeof Chart !== 'undefined') {
    Chart.defaults.font.family = "'DM Sans', system-ui, -apple-system, sans-serif";
    Chart.defaults.font.size = 12;
    Chart.defaults.scale.ticks.maxRotation = 0;
    Chart.defaults.scale.ticks.autoSkipPadding = 16;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.legend.labels.boxHeight = 8;
    Chart.defaults.plugins.legend.labels.padding = 16;
    // Curves that never overshoot their points: a plain smoothed line bulged
    // past the data and ran flat along the edge of the chart where it was clipped.
    Chart.defaults.elements.line.cubicInterpolationMode = 'monotone';
    // Charts draw in at once for anyone who asked the system for less motion.
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        Chart.defaults.animation = false;
    }
}

// Read chart colors from CSS custom properties
function getChartColors() {
    var style = getComputedStyle(document.documentElement);
    return {
        grid: style.getPropertyValue('--chart-grid').trim() || 'rgba(255,255,255,0.06)',
        text: style.getPropertyValue('--chart-text').trim() || '#8b8b9e',
    };
}

function registerChart(chart) {
    if (chart) window.virgilCharts.push(chart);
    return chart;
}

window.rebuildAllCharts = function() {
    // Notify charts that theme changed — they'll pick up new CSS vars on next render
    window.virgilCharts.forEach(function(chart) {
        if (chart && chart.canvas) {
            var colors = getChartColors();
            // Update scale colors
            Object.keys(chart.options.scales || {}).forEach(function(key) {
                var scale = chart.options.scales[key];
                if (scale.grid) scale.grid.color = colors.grid;
                if (scale.ticks) scale.ticks.color = colors.text;
                if (scale.angleLines) scale.angleLines.color = colors.grid;
                if (scale.pointLabels) scale.pointLabels.color = colors.text;
            });
            // Update legend colors
            if (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels) {
                chart.options.plugins.legend.labels.color = colors.text;
            }
            chart.update();
        }
    });
};

function createLineChart(canvasId, labels, datasets, options) {
    options = options || {};
    var ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    var colors = getChartColors();
    var chart = new Chart(ctx, {
        type: 'line',
        data: { labels: labels, datasets: datasets },
        options: {
            responsive: true,
            interaction: { intersect: false, mode: 'index' },
            scales: {
                x: {
                    grid: { color: colors.grid },
                    ticks: { color: colors.text, font: { size: 12 } }
                },
                y: Object.assign({
                    grid: { color: colors.grid },
                    ticks: { color: colors.text, font: { size: 12 } }
                }, options.yScale || {})
            },
            plugins: Object.assign({
                legend: {
                    display: datasets.length > 1,
                    labels: { color: colors.text }
                }
            }, options.plugins || {}),
            ...(options.chartOptions || {})
        }
    });
    return registerChart(chart);
}

function createRadarChart(canvasId, labels, datasets) {
    var ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    var colors = getChartColors();
    var chart = new Chart(ctx, {
        type: 'radar',
        data: { labels: labels, datasets: datasets },
        options: {
            responsive: true,
            scales: {
                r: {
                    beginAtZero: true,
                    max: 10,
                    ticks: {
                        stepSize: 2,
                        color: colors.text,
                        backdropColor: 'transparent'
                    },
                    grid: { color: colors.grid },
                    angleLines: { color: colors.grid },
                    pointLabels: {
                        font: { size: 12 },
                        color: colors.text
                    }
                }
            },
            plugins: {
                legend: {
                    labels: { color: colors.text }
                }
            }
        }
    });
    return registerChart(chart);
}

// Reference range band plugin for blood work charts
const refRangePlugin = {
    id: 'refRange',
    beforeDraw(chart) {
        const meta = chart.options.plugins.refRange;
        if (!meta || meta.low == null || meta.high == null) return;
        const { ctx, chartArea: { left, right }, scales: { y } } = chart;
        const yLow = y.getPixelForValue(meta.low);
        const yHigh = y.getPixelForValue(meta.high);
        ctx.save();
        ctx.fillStyle = 'rgba(34, 197, 94, 0.08)';
        ctx.fillRect(left, yHigh, right - left, yLow - yHigh);
        ctx.restore();
    }
};
Chart.register(refRangePlugin);
