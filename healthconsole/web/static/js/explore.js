"use strict";

// ----------------------------------------------------------------- explore

const AKS_TYPE = "microsoft.containerservice/managedclusters";
const AI_TYPE = "microsoft.insights/components";
const SITE_TYPE = "microsoft.web/sites";
const APIM_TYPE = "microsoft.apimanagement/service";
// "Is it OK and what happened" first, then where else it's reported, then configuration.
const TABS = [
  ["overview", "Overview"], ["timeline", "Timeline"], ["metrics", "Metrics"],
  ["workloads", "Workloads", (n) => n.type === AKS_TYPE],
  ["nodes", "Nodes", (n) => n.type === AKS_TYPE],
  ["kubectl", "kubectl", (n) => n.type === AKS_TYPE],
  ["appinsights", "Application Insights", (n) => [AI_TYPE, SITE_TYPE, APIM_TYPE].includes(n.type)],
];
const CONFIG_TABS = [["details", "Details"], ["scaling", "Scaling"], ["diagnostics", "Diagnostics"], ["vulnerabilities", "Vulnerabilities"],
  ["recommendations", "Recommendations"], ["cost", "Cost"]];
// Plus ServiceNow and one tab per monitoring tool, when this tenant has a turned-on connection of them.
const allTabs = () => [...TABS, ...(state.snEnabled ? [["servicenow", "ServiceNow"]] : []),
  ...toolKinds().map((kind) => [`tool-${kind}`, toolLabel(kind)]), ...CONFIG_TABS];
// The tabs in three labelled groups (one unlabelled row when only one group has tabs).
const TAB_GROUPS = [["Health", (key) => !TOOL_TAB(key) && !CONFIG_TABS.some(([k]) => k === key)], ["Tools", (key) => TOOL_TAB(key)],
  ["Configuration", (key) => CONFIG_TABS.some(([k]) => k === key)]];
const TOOL_TAB = (key) => key === "kubectl" || key === "servicenow" || key.startsWith("tool-");
function tabGroupsHtml(visible, tab) {
  const button = ([key, label]) => `<button type="button" data-tab="${key}" class="${key === tab ? "active" : ""}">${label}</button>`;
  const groups = TAB_GROUPS.map(([name, test]) => [name, visible.filter(([key]) => test(key))]).filter(([, tabs]) => tabs.length);
  if (groups.length < 2) return visible.map(button).join("");
  return groups.map(([name, tabs]) => `<span class="tab-group" role="group" aria-label="${name}"><span class="tab-group-label">${name}</span>${tabs.map(button).join("")}</span>`).join("");
}
const SEVERITY_STATUS = { HIGH: "critical", MEDIUM: "warning", LOW: "info", INFO: "info" };
const PIM_TIP = "If you're eligible for a role through PIM, activate it, then click Refresh access.";
const PIM_ACTIVATE_URL = "https://portal.azure.com/#view/Microsoft_Azure_PIMCommon/ActivationMenuBlade/~/azurerbac";
const exploreCache = new Map();

function table(columns, rows, key = columns.join("|")) {
  if (!rows.length) return '<p class="muted">Nothing to show.</p>';
  return `<table data-list="${esc(key)}"><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r) => `<tr>${r.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

