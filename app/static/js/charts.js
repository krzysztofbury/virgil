// Chart.js utility functions for Virgil

// Chart instance registry for theme rebuilds
window.virgilCharts = [];

// Read chart colors from CSS custom properties
function getChartColors() {
    var style = getComputedStyle(document.documentElement);
    return {
        grid: style.getPropertyValue('--chart-grid').trim() || 'rgba(255,255,255,0.06)',
        text: style.getPropertyValue('--chart-text').trim() || '#8b8b9e',
        textMuted: style.getPropertyValue('--chart-text-muted').trim() || '#55556a',
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
                    ticks: { color: colors.text, font: { size: 11 } }
                },
                y: Object.assign({
                    grid: { color: colors.grid },
                    ticks: { color: colors.text, font: { size: 11 } }
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

function createSparkline(canvasId, labels, data, color) {
    color = color || 'rgb(168, 85, 247)';
    var ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    var chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                data: data,
                borderColor: color,
                backgroundColor: color.replace('rgb', 'rgba').replace(')', ', 0.1)'),
                fill: true,
                tension: 0.4,
                pointRadius: 0,
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: { x: { display: false }, y: { display: false } },
            plugins: { legend: { display: false }, tooltip: { enabled: false } },
            elements: { point: { radius: 0 } },
        }
    });
    return registerChart(chart);
}

function createBarChart(canvasId, labels, datasets, options) {
    options = options || {};
    var ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    var colors = getChartColors();
    var chart = new Chart(ctx, {
        type: 'bar',
        data: { labels: labels, datasets: datasets },
        options: {
            responsive: true,
            scales: {
                x: { grid: { color: colors.grid }, ticks: { color: colors.text, font: { size: 11 } } },
                y: Object.assign({ grid: { color: colors.grid }, ticks: { color: colors.text, font: { size: 11 } } }, options.yScale || {}),
            },
            plugins: Object.assign({
                legend: { display: datasets.length > 1, labels: { color: colors.text } }
            }, options.plugins || {}),
            ...(options.chartOptions || {})
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
