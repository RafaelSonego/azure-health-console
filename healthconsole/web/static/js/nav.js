"use strict";

// ------------------------------------------------------------------ start

document.querySelector(".views").addEventListener("click", (event) => {
  const menu = event.target.closest(".nav-menu-btn");
  if (menu) {
    for (const p of document.querySelectorAll(".nav-panel")) if (p.id !== menu.dataset.menu) p.classList.add("hidden");
    $(menu.dataset.menu).classList.toggle("hidden");
    return;
  }
  const view = event.target.closest("[data-view]")?.dataset.view;
  if (view) {
    for (const p of document.querySelectorAll(".nav-panel")) p.classList.add("hidden");
    showView(view);
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".nav-menu")) for (const p of document.querySelectorAll(".nav-panel")) p.classList.add("hidden");
});

// A menu in the top bar is underlined, and names the open view, when that view lives in it.
function renderNavMenus() {
  if (state.personas.length) applyPersona();  // the Tools menu gains entries as connections load
  for (const button of document.querySelectorAll(".nav-menu-btn")) {
    const panel = $(button.dataset.menu);
    const open = panel.querySelector(`[data-view="${CSS.escape(state.view || "")}"]`);
    button.classList.toggle("active", !!open);
    button.querySelector(".nav-current").textContent = open ? `: ${open.dataset.short || open.textContent.trim()}` : "";
  }
  const tools = !$("tab-servicenow").classList.contains("hidden") || $("tool-tabs").children.length > 0;
  $("tools-menu").classList.toggle("hidden", !tools);
}

