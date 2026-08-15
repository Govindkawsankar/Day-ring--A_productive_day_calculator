// --- Dark mode toggle (runs on every page) ---
(function () {
    var toggleBtn = document.getElementById("theme-toggle");
    if (!toggleBtn) return;

    function effectiveTheme() {
        var explicit = document.documentElement.getAttribute("data-theme");
        if (explicit === "dark" || explicit === "light") return explicit;
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }

    function updateIcon() {
        toggleBtn.textContent = effectiveTheme() === "dark" ? "☀️" : "🌙";
    }

    toggleBtn.addEventListener("click", function () {
        var next = effectiveTheme() === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        try { localStorage.setItem("theme", next); } catch (e) {}
        updateIcon();
    });

    updateIcon();
})();

document.addEventListener("DOMContentLoaded", function () {
    var habitButtons = Array.from(document.querySelectorAll(".habit-item[data-habit-id]"));
    if (habitButtons.length === 0) return;

    var CIRCUMFERENCE = 565.49;

    function recalcAndRender() {
        var totalWeight = 0;
        var earnedWeight = 0;
        var catTotals = {};
        var catEarned = {};

        habitButtons.forEach(function (btn) {
            var weight = parseFloat(btn.dataset.weight) || 0;
            var cat = btn.dataset.category;
            var done = btn.dataset.done === "1";
            totalWeight += weight;
            catTotals[cat] = (catTotals[cat] || 0) + weight;
            if (done) {
                earnedWeight += weight;
                catEarned[cat] = (catEarned[cat] || 0) + weight;
            } else {
                catEarned[cat] = catEarned[cat] || 0;
            }
        });

        var overallPct = totalWeight ? Math.round((earnedWeight / totalWeight) * 100) : 0;

        var pctText = document.getElementById("score-pct-text");
        if (pctText) pctText.textContent = overallPct + "%";

        var ring = document.getElementById("orbit-progress");
        if (ring) ring.setAttribute("stroke-dashoffset", (CIRCUMFERENCE * (1 - overallPct / 100)).toFixed(2));

        var meterFill = document.getElementById("score-meter-fill");
        if (meterFill) meterFill.style.width = overallPct + "%";

        var meterText = document.getElementById("score-meter-text");
        if (meterText) meterText.textContent = earnedWeight + " / " + totalWeight;

        Object.keys(catTotals).forEach(function (cat) {
            var catPct = catTotals[cat] ? Math.round((catEarned[cat] / catTotals[cat]) * 100) : 0;
            document.querySelectorAll('.cat-pct-text[data-category="' + cssEscape(cat) + '"]').forEach(function (el) {
                el.textContent = catPct + "%";
            });
            document.querySelectorAll('.cat-bar-fill[data-category="' + cssEscape(cat) + '"]').forEach(function (el) {
                el.style.width = catPct + "%";
            });
            document.querySelectorAll('.group-pct-text[data-category="' + cssEscape(cat) + '"]').forEach(function (el) {
                el.textContent = catPct + "%";
            });
        });
    }

    function cssEscape(value) {
        return value.replace(/["\\]/g, "\\$&");
    }

    habitButtons.forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            e.preventDefault();
            var form = btn.closest("form");
            var willBeDone = btn.dataset.done !== "1";

            // Optimistic UI update — no page reload, no scroll jump
            btn.dataset.done = willBeDone ? "1" : "0";
            btn.classList.toggle("completed", willBeDone);
            var check = btn.querySelector(".check-ring");
            if (check) check.textContent = willBeDone ? "✓" : "";
            recalcAndRender();

            var formData = new FormData(form);
            formData.set("completed", willBeDone ? "1" : "0");
            fetch(form.action, { method: "POST", body: formData, redirect: "manual" }).catch(function () {
                // Revert on network failure
                btn.dataset.done = willBeDone ? "0" : "1";
                btn.classList.toggle("completed", !willBeDone);
                if (check) check.textContent = willBeDone ? "" : "✓";
                recalcAndRender();
            });
        });
    });
});
