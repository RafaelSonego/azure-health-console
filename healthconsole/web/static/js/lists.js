"use strict";

// ------------------------------------------------------------------ lists
// Any <table data-list="key">: a header click sorts it (up, down, then the list's own order; remembered per key in this
// browser), and a long one shows LIST_PAGE rows with "show more". A cell sorts by its own or a child's data-v, else its text.
// A row marked data-attached (a detail row) moves and hides with the row above it. A list over TABLE_BOX_ROWS rows
// scrolls in its own box, so its header stays in view. `filterNs` keeps the rows whose Namespace column is that one.
const LIST_PAGE = 100;
const TABLE_BOX_ROWS = 15;
const NUMBER_CELL = /^(-?[\d,]*\.?\d+)\s*(%|ms|s|m|GiB|MiB|cores)?(\s+(\(|of\b|\/|·).*)?$/i;

function listSorts() {
  return stored.json("ahc.listSort", {});
}

function saveListSort(key, sort) {
  const sorts = listSorts();
  if (sort) sorts[key] = sort; else delete sorts[key];
  stored.set("ahc.listSort", sorts);
}

function cellValue(cell) {
  if (!cell) return null;
  const text = String(cell.dataset.v ?? cell.querySelector("[data-v]")?.dataset.v ?? cell.textContent).trim();
  if (text === "" || text === "–") return null;
  const number = text.match(NUMBER_CELL);
  return number ? Number(number[1].replace(/,/g, "")) : text;
}

function compareValues(x, y) {
  if (typeof x === "number" && typeof y === "number") return x - y;
  if (typeof x !== typeof y) return typeof x === "number" ? -1 : 1;
  return x.localeCompare(y, undefined, { numeric: true, sensitivity: "base" });
}

// Each list row with the detail rows attached to it.
function listGroups(table) {
  const groups = [];
  for (const row of table.tBodies[0].rows) {
    if ("attached" in row.dataset && groups.length) groups[groups.length - 1].push(row); else groups.push([row]);
  }
  return groups;
}

function sortList(table, sort) {
  const col = sort ? sort.col : -1;
  const keyed = listGroups(table).map((rows) => ({ rows, i: Number(rows[0].dataset.i), v: sort ? cellValue(rows[0].cells[col]) : null }));
  keyed.sort((a, b) => {
    if (!sort || (a.v === null && b.v === null)) return a.i - b.i;
    if (a.v === null || b.v === null) return a.v === null ? 1 : -1;  // empty cells last, either way
    return sort.dir * compareValues(a.v, b.v) || a.i - b.i;
  });
  table.tBodies[0].append(...keyed.flatMap((k) => k.rows));
  table.querySelectorAll("th[data-col]").forEach((th) => th.setAttribute("aria-sort",
    sort && Number(th.dataset.col) === col ? (sort.dir > 0 ? "ascending" : "descending") : "none"));
  table.listSort = sort;
}

// The column with that heading, -1 when the list has none.
function headedColumn(table, name) {
  const head = table.tHead && table.tHead.rows[0];
  return head ? [...head.cells].findIndex((th) => th.textContent.trim() === name) : -1;
}
const namespaceColumn = (table) => headedColumn(table, "Namespace");

function rowMatches(table, row, { kind: byKind = true } = {}) {
  const text = (table.dataset.filterText || "").toLowerCase(), ns = table.dataset.filterNs || "";
  if (text && !row.textContent.toLowerCase().includes(text)) return false;
  const col = ns ? namespaceColumn(table) : -1;
  if (col >= 0 && !(row.cells[col] && row.cells[col].textContent.trim() === ns)) return false;
  const kind = byKind ? table.dataset.filterKind || "" : "", kcol = kind ? headedColumn(table, "Kind") : -1;
  return kcol < 0 || (row.cells[kcol] && row.cells[kcol].textContent.trim() === kind);
}

const listBox = (table) => (table.parentElement && table.parentElement.classList.contains("table-box") ? table.parentElement : table);

function pageList(table) {
  const groups = listGroups(table);
  const filtered = !!(table.dataset.filterText || table.dataset.filterNs);
  const matching = groups.filter((rows) => rowMatches(table, rows[0]));
  const shown = new Set(matching.slice(0, Number(table.dataset.shown || LIST_PAGE)));
  for (const rows of groups) rows.forEach((row) => row.classList.toggle("paged-out", !shown.has(rows)));
  const box = listBox(table);
  let more = box.nextElementSibling && box.nextElementSibling.classList.contains("list-more") ? box.nextElementSibling : null;
  const rest = matching.length - shown.size;
  if (!rest && !filtered) { if (more) more.remove(); return; }
  if (!more) { more = document.createElement("div"); more.className = "list-more muted"; box.after(more); }
  more.innerHTML = rest ? `Showing ${shown.size.toLocaleString()} of ${matching.length.toLocaleString()}${filtered ? " matching" : ""} ·
    <button type="button" class="link" data-list-more>show ${Math.min(rest, LIST_PAGE).toLocaleString()} more</button> ·
    <button type="button" class="link" data-list-all>show all</button>`
    : `${matching.length.toLocaleString()} of ${groups.length.toLocaleString()} match`;
}

function initList(table) {
  if (table.dataset.ready || !table.tBodies[0]) return;
  table.dataset.ready = "1";
  [...table.tBodies[0].rows].forEach((row, i) => { row.dataset.i = i; });
  const head = table.tHead && table.tHead.rows[0];
  if (head) [...head.cells].forEach((th, i) => {
    if (!th.textContent.trim() || "nosort" in th.dataset) return;
    th.dataset.col = i;
    th.innerHTML = `<button type="button" class="sort">${th.innerHTML}</button>`;
  });
  const saved = listSorts()[table.dataset.list];
  if (saved && head && head.cells[saved.col] && "col" in head.cells[saved.col].dataset) sortList(table, saved);
  const count = listGroups(table).length;
  if (count > TABLE_BOX_ROWS && head && !table.closest(".table-box, .graph")) {
    const box = document.createElement("div");
    box.className = "table-box";
    table.before(box);
    box.append(table);
    const names = [...head.cells].map((th) => th.textContent.trim());
    for (const row of table.tBodies[0].rows) [...row.cells].forEach((td, i) => { if (names[i] && !td.title) td.title = names[i]; });
  }
  if (count > LIST_PAGE && !("nofilter" in table.dataset)) {
    const tools = document.createElement("div");
    tools.className = "list-tools";
    tools.innerHTML = `<input type="search" data-list-filter placeholder="Filter these ${count.toLocaleString()} rows" aria-label="Filter this list">`;
    listBox(table).before(tools);
  }
  pageList(table);
}

const listOf = (control) => {
  const near = control.closest(".list-more, .list-tools");
  const next = near && (near.classList.contains("list-more") ? near.previousElementSibling : near.nextElementSibling);
  return next ? (next.classList.contains("table-box") ? next.querySelector("table[data-list]") : next) : control.closest("table[data-list]");
};

new MutationObserver((records) => {
  for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeType !== 1) continue;
    if (node.matches("table[data-list]")) initList(node);
    node.querySelectorAll("table[data-list]").forEach(initList);
  }
}).observe(document.body, { childList: true, subtree: true });

document.addEventListener("click", (event) => {
  const th = event.target.closest("table[data-list] th[data-col]");
  if (th) {
    const table = listOf(th), col = Number(th.dataset.col), current = table.listSort;
    const sort = current && current.col === col ? (current.dir > 0 ? { col, dir: -1 } : null) : { col, dir: 1 };
    saveListSort(table.dataset.list, sort);
    sortList(table, sort);
    pageList(table);
    return;
  }
  const more = event.target.closest("[data-list-more], [data-list-all]");
  if (more) {
    const table = listOf(more);
    table.dataset.shown = "listAll" in more.dataset ? Infinity : Number(table.dataset.shown || LIST_PAGE) + LIST_PAGE;
    pageList(table);
  }
});

document.addEventListener("input", (event) => {
  if (!("listFilter" in event.target.dataset)) return;
  const table = listOf(event.target);
  table.dataset.filterText = event.target.value.trim();
  table.dataset.shown = LIST_PAGE;
  pageList(table);
});

