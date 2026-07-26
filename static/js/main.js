/* BizManager Multi-Shop ERP — main.js */

function toggleSidebar() {
  const s = document.getElementById("sidebar");
  const o = document.getElementById("overlay");
  if (s) s.classList.toggle("open");
  if (o) o.classList.toggle("open");
}

function toggleDark() {
  const html = document.documentElement;
  const dark  = html.getAttribute("data-theme") === "dark";
  html.setAttribute("data-theme", dark ? "light" : "dark");
  localStorage.setItem("bms_theme", dark ? "light" : "dark");
  updateDarkBtn();
}

function updateDarkBtn() {
  const btn = document.getElementById("darkBtn");
  if (!btn) return;
  btn.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙";
}

// Apply saved theme immediately
(function(){
  const saved = localStorage.getItem("bms_theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
  document.addEventListener("DOMContentLoaded", updateDarkBtn);
})();

// ── Update_031: tappable table rows ─────────────────────────────────────
// Any <tr class="row-link" data-href="..."> becomes tap/click-to-navigate,
// app-wide, from this single delegated listener — no per-table JS needed.
// A tap that lands on a REAL interactive element inside the row (a link,
// button, form, input, select, or anything marked .no-row-link) keeps
// that element's own behavior instead of navigating the row, so action
// buttons ("View", "Pay", "Return", checkboxes, etc.) inside a tappable
// row are never accidentally swallowed by the row-level navigation.
document.addEventListener("click", function (e) {
  const row = e.target.closest("tr.row-link[data-href]");
  if (!row) return;
  if (e.target.closest("a, button, input, select, textarea, label, form, .no-row-link")) return;
  const href = row.dataset.href;
  if (href) window.location.href = href;
});

// Auto-dismiss alerts after 4 s
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".alert").forEach(a => {
    setTimeout(() => {
      a.style.transition = "opacity .5s";
      a.style.opacity    = "0";
      setTimeout(() => a.remove(), 500);
    }, 4000);
  });
});

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
