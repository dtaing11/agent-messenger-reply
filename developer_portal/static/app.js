// Minimal vanilla JS: copy buttons. (No inline scripts: the CSP only allows files from 'self'.)
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const el = document.getElementById(btn.dataset.copy);
  if (el && navigator.clipboard) {
    navigator.clipboard.writeText(el.textContent.trim()).then(() => { btn.textContent = "Copied"; });
  }
});
