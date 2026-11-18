// Focus in a diagram (core/diagram.py): hovering a box lights its connectors and what they reach and fades the
// rest; a click keeps it, empty space clears it; problems() lights every critical or warning box, filter(text) every
// box whose name or type names hold all the words typed, each with its neighbours. Loaded by the console page and
// inlined in the diagram's own tab.
function diagramFocus(box) {
  let pinned = null;  // a node id, "problems", { text }, or null
  const starts = (svg, focus) => {
    const nodes = [...svg.querySelectorAll(".dg-node")];
    if (focus === "problems") return nodes.filter((n) => n.matches(".st-critical, .st-warning"));
    if (focus && typeof focus === "object") {
      const words = focus.text.toLowerCase().split(/\s+/).filter(Boolean);
      return nodes.filter((n) => words.every((w) => (n.dataset.search || "").includes(w)));
    }
    return focus ? nodes.filter((n) => n.dataset.id === focus) : [];
  };
  const show = (focus) => {
    const svg = box.querySelector("svg.dg");
    if (!svg) return 0;
    const hits = starts(svg, focus);
    const start = new Set(hits.map((n) => n.dataset.id));
    svg.classList.toggle("focusing", !!focus);
    const near = new Set(start);
    for (const el of svg.querySelectorAll("[data-source]")) {
      const lit = start.has(el.dataset.source) || start.has(el.dataset.target);
      el.classList.toggle("lit", lit);
      if (lit) { near.add(el.dataset.source); near.add(el.dataset.target); }
    }
    for (const node of svg.querySelectorAll(".dg-node")) {
      node.classList.toggle("lit", near.has(node.dataset.id));
      node.classList.toggle("hit", typeof focus === "object" && focus !== null && start.has(node.dataset.id));
    }
    return hits.length;
  };
  box.addEventListener("mouseover", (event) => {
    const node = event.target.closest(".dg-node");
    if (node) show(node.dataset.id);
  });
  box.addEventListener("mouseout", (event) => {
    const to = event.relatedTarget && event.relatedTarget.closest ? event.relatedTarget.closest(".dg-node") : null;
    if (event.target.closest(".dg-node") && !to) show(pinned);
  });
  box.addEventListener("click", (event) => {
    if (event.target.closest("button, input, label")) return;
    const node = event.target.closest(".dg-node");
    if (!node && pinned && typeof pinned === "object") return;  // a typed filter stays until its text is cleared
    pinned = node ? node.dataset.id : null;
    show(pinned);
  });
  return {
    refresh: () => show(pinned),
    clear: () => { pinned = null; show(null); },
    problems: () => { pinned = pinned === "problems" ? null : "problems"; return show(pinned); },  // a second press clears
    filter: (text) => { pinned = (text || "").trim() ? { text } : null; return show(pinned); },
    first: () => box.querySelector(".dg-node.hit"),
  };
}
