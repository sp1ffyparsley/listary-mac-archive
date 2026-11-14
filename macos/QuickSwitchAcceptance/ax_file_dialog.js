#!/usr/bin/osascript -l JavaScript

"use strict";

const MAX_NODES = 700;
const MAX_DEPTH = 9;

function attr(element, name) {
  try {
    const matches = element.attributes.whose({ name })();
    if (!matches || matches.length === 0) return null;
    return matches[0].value();
  } catch (_error) {
    return null;
  }
}

function stringValue(value) {
  if (value === null || value === undefined) return "";
  try {
    return String(value);
  } catch (_error) {
    return "";
  }
}

function role(element) {
  return stringValue(attr(element, "AXRole"));
}

function elementSignature(element) {
  return [
    attr(element, "AXTitle"),
    attr(element, "AXDescription"),
    attr(element, "AXIdentifier"),
    attr(element, "AXHelp"),
  ]
    .map(stringValue)
    .join(" ")
    .toLowerCase();
}

function children(element) {
  try {
    return element.uiElements();
  } catch (_error) {
    return [];
  }
}

function walk(element, callback, depth = 0, state = { count: 0 }) {
  if (!element || depth > MAX_DEPTH || state.count >= MAX_NODES) return;
  state.count += 1;
  callback(element, depth);
  const rows = children(element);
  for (const child of rows) {
    if (state.count >= MAX_NODES) break;
    walk(child, callback, depth + 1, state);
  }
}

function windowNumber(window) {
  const ax = attr(window, "AXWindowNumber");
  if (ax !== null && ax !== undefined && String(ax).length > 0) return String(ax);
  try {
    const value = window.id();
    if (value !== null && value !== undefined && String(value).length > 0) return String(value);
  } catch (_error) {}
  return "";
}

function detectKind(element) {
  let sawOpen = false;
  let sawSave = false;
  let sawCancel = false;
  walk(element, (row) => {
    if (role(row) !== "AXButton") return;
    const sig = elementSignature(row);
    if (sig.includes("cancel")) sawCancel = true;
    if (sig.includes("save")) sawSave = true;
    if (sig.includes("open") || sig.includes("choose")) sawOpen = true;
  });
  if (!sawCancel) return "unknown";
  if (sawSave) return "save";
  if (sawOpen) return "open";
  return "unknown";
}

function filePath(value) {
  const raw = stringValue(value).trim();
  if (!raw) return "";
  try {
    if (raw.startsWith("file://")) {
      const url = $.NSURL.URLWithString($(raw));
      if (url && url.path) return ObjC.unwrap(url.path.stringByStandardizingPath);
    }
    if (raw.startsWith("/")) return ObjC.unwrap($(raw).stringByStandardizingPath);
  } catch (_error) {}
  return "";
}

function pathEvidence(panel) {
  const evidence = [];
  function add(element, attributeName, score) {
    const path = filePath(attr(element, attributeName));
    if (!path) return;
    evidence.push({
      path,
      score,
      source: role(element) + "." + attributeName,
      identifier: stringValue(attr(element, "AXIdentifier")),
    });
  }
  add(panel, "AXDocument", 100);
  add(panel, "AXURL", 95);
  walk(panel, (row) => {
    const rowRole = role(row);
    const sig = elementSignature(row);
    let boost = 0;
    if (rowRole === "AXPathControl") boost = 80;
    else if (sig.includes("where") || sig.includes("location") || sig.includes("path")) boost = 60;
    if (!boost) return;
    add(row, "AXURL", boost + 5);
    add(row, "AXDocument", boost + 4);
    add(row, "AXValue", boost);
  });
  const bestByPath = new Map();
  for (const row of evidence) {
    const previous = bestByPath.get(row.path);
    if (!previous || row.score > previous.score) bestByPath.set(row.path, row);
  }
  const unique = Array.from(bestByPath.values()).sort((a, b) => b.score - a.score);
  if (unique.length === 0) {
    return { status: "readback_unavailable", current_folder: null, evidence: [] };
  }
  const bestScore = unique[0].score;
  const best = unique.filter((row) => row.score === bestScore);
  if (best.length !== 1) {
    return { status: "readback_ambiguous", current_folder: null, evidence: unique };
  }
  return { status: "ok", current_folder: best[0].path, evidence: unique };
}

function selectedPathEvidence(root) {
  const selected = [];
  walk(root, (row) => {
    if (attr(row, "AXSelected") !== true) return;
    for (const attributeName of ["AXURL", "AXDocument", "AXValue"]) {
      const path = filePath(attr(row, attributeName));
      if (!path) continue;
      selected.push({
        path,
        source: role(row) + "." + attributeName,
        identifier: stringValue(attr(row, "AXIdentifier")),
      });
    }
  });
  const unique = [];
  const seen = new Set();
  for (const row of selected) {
    if (seen.has(row.path)) continue;
    seen.add(row.path);
    unique.push(row);
  }
  if (unique.length === 0) return { status: "none", selected_path: null, evidence: [] };
  if (unique.length > 1) return { status: "ambiguous", selected_path: null, evidence: unique };
  return { status: "ok", selected_path: unique[0].path, evidence: unique };
}

function folderMatches(expected, panel) {
  const target = filePath(expected);
  if (!target || !panel) return false;
  const values = [];
  if (panel.current_folder) values.push(panel.current_folder);
  for (const row of panel.readback_evidence || []) {
    if (row && row.path) values.push(row.path);
  }
  for (const raw of values) {
    const value = filePath(raw);
    if (!value) continue;
    if (value === target) return true;
    try {
      const parent = ObjC.unwrap($(value).stringByDeletingLastPathComponent.stringByStandardizingPath);
      if (parent === target) return true;
    } catch (_error) {}
  }
  return false;
}

function candidatePanel(process, rootWindow, element, index) {
  const kind = detectKind(element);
  if (kind === "unknown") return null;
  const rootNumber = windowNumber(rootWindow);
  const elementNumber = windowNumber(element);
  const stableNumber = elementNumber || rootNumber;
  if (!stableNumber) {
    return {
      unstable: true,
      kind,
      role: role(element),
      title: stringValue(attr(element, "AXTitle")),
    };
  }
  const pid = Number(process.unixId());
  const identity = {
    pid,
    process_name: stringValue(process.name()),
    window_number: stableNumber,
    role: role(element),
    subrole: stringValue(attr(element, "AXSubrole")),
    kind,
    title: stringValue(attr(element, "AXTitle")),
    identifier: stringValue(attr(element, "AXIdentifier")),
    candidate_index: index,
  };
  const readback = pathEvidence(element);
  const selection = selectedPathEvidence(element);
  identity.panel_token = JSON.stringify(identity);
  identity.context_token = identity.panel_token;
  identity.current_folder = readback.current_folder;
  identity.current_directory = readback.current_folder;
  identity.readback_status = readback.status;
  identity.readback_evidence = readback.evidence;
  identity.selected_path = selection.selected_path;
  identity.selection_status = selection.status;
  identity.selection_evidence = selection.evidence;
  return { unstable: false, identity, element };
}

function detectPanel() {
  ObjC.import("AppKit");
  ObjC.import("ApplicationServices");
  ObjC.import("Foundation");
  if (!Boolean($.AXIsProcessTrusted())) {
    return { status: "accessibility_denied", permission: "denied" };
  }
  const systemEvents = Application("System Events");
  const processes = systemEvents.applicationProcesses.whose({ frontmost: true })();
  if (!processes || processes.length === 0) {
    return { status: "panel_not_found", permission: "granted", reason: "no_frontmost_process" };
  }
  const process = processes[0];
  let roots = [];
  try {
    roots = process.windows();
  } catch (_error) {
    return { status: "panel_not_found", permission: "granted", reason: "frontmost_windows_unavailable" };
  }
  let index = 0;
  let unstable = null;
  const matches = [];
  for (const rootWindow of roots) {
    const possible = [rootWindow];
    walk(rootWindow, (row) => {
      const rowRole = role(row);
      if (rowRole === "AXSheet" || rowRole === "AXDialog") possible.push(row);
    });
    for (const element of possible) {
      const row = candidatePanel(process, rootWindow, element, index++);
      if (!row) continue;
      if (row.unstable) unstable = row;
      else matches.push(row);
    }
  }
  if (matches.length === 0) {
    if (unstable) {
      return { status: "panel_identity_unstable", permission: "granted", panel: unstable };
    }
    return { status: "panel_not_found", permission: "granted", reason: "no_open_save_signature" };
  }
  // Prefer the most deeply nested modal candidate when both a parent window and
  // its AXSheet expose the same Open/Save controls.
  const chosen = matches[matches.length - 1];
  return { status: "ok", permission: "granted", panel: chosen.identity, _element: chosen.element };
}

function samePanel(expectedToken) {
  const current = detectPanel();
  if (current.status !== "ok") return current;
  if (current.panel.panel_token !== expectedToken) {
    return { status: "panel_replaced", permission: "granted", panel: current.panel };
  }
  return current;
}

function focusedTextField(process) {
  let found = null;
  let roots = [];
  try {
    roots = process.windows();
  } catch (_error) {
    return null;
  }
  for (const rootWindow of roots) {
    walk(rootWindow, (row) => {
      if (found) return;
      const rowRole = role(row);
      if (rowRole !== "AXTextField" && rowRole !== "AXComboBox") return;
      if (attr(row, "AXFocused") === true) found = row;
    });
    if (found) return found;
  }
  return null;
}

function setValue(element, value) {
  try {
    element.value = value;
    return true;
  } catch (_error) {
    try {
      const values = element.attributes.whose({ name: "AXValue" })();
      if (values.length > 0) {
        values[0].value = value;
        return true;
      }
    } catch (_ignored) {}
  }
  return false;
}

function navigate(expectedToken, targetPath) {
  const current = samePanel(expectedToken);
  if (current.status !== "ok") return current;
  const target = filePath(targetPath);
  if (!target) return { status: "navigation_failed", reason: "target_not_absolute" };
  if (folderMatches(target, current.panel)) {
    return { status: "already_at_target", panel: current.panel, stale_object_recovered: false };
  }
  const systemEvents = Application("System Events");
  const processes = systemEvents.applicationProcesses.whose({ frontmost: true })();
  if (!processes || processes.length === 0) return { status: "panel_closed" };
  const process = processes[0];
  let staleRecovered = false;

  function openAndFillGoToFolder() {
    systemEvents.keystroke("g", { using: ["command down", "shift down"] });
    const app = Application.currentApplication();
    app.includeStandardAdditions = true;
    app.delay(0.12);
    const field = focusedTextField(process);
    if (!field || !setValue(field, target)) {
      throw new Error("Go to Folder field was not Accessibility-settable.");
    }
    systemEvents.keyCode(36);
  }

  try {
    openAndFillGoToFolder();
  } catch (_firstError) {
    const recovered = samePanel(expectedToken);
    if (recovered.status !== "ok") return recovered;
    staleRecovered = true;
    try {
      openAndFillGoToFolder();
    } catch (secondError) {
      return {
        status: "navigation_failed",
        reason: stringValue(secondError && secondError.message ? secondError.message : secondError),
        stale_object_recovered: staleRecovered,
      };
    }
  }

  for (let attempt = 0; attempt < 50; attempt += 1) {
    const app = Application.currentApplication();
    app.includeStandardAdditions = true;
    app.delay(0.05);
    const after = samePanel(expectedToken);
    if (after.status !== "ok") return after;
    if (folderMatches(target, after.panel)) {
      return {
        status: "navigated",
        method: "scoped_go_to_folder_with_verified_ax_readback",
        panel: after.panel,
        stale_object_recovered: staleRecovered,
      };
    }
  }
  const after = samePanel(expectedToken);
  return {
    status: "readback_mismatch",
    method: "scoped_go_to_folder_with_verified_ax_readback",
    expected_path: target,
    panel: after.panel || null,
    stale_object_recovered: staleRecovered,
  };
}

function finderContext() {
  ObjC.import("AppKit");
  ObjC.import("ApplicationServices");
  ObjC.import("Foundation");
  if (!Boolean($.AXIsProcessTrusted())) {
    return { status: "accessibility_denied", permission: "denied" };
  }
  const systemEvents = Application("System Events");
  const finders = systemEvents.applicationProcesses.whose({ name: "Finder" })();
  if (!finders || finders.length === 0) return { status: "finder_not_running" };
  const finder = finders[0];
  let windows = [];
  try {
    windows = finder.windows();
  } catch (_error) {
    return { status: "finder_window_unavailable" };
  }
  let chosen = null;
  for (const window of windows) {
    if (attr(window, "AXMain") === true) {
      chosen = window;
      break;
    }
  }
  if (!chosen && windows.length > 0) chosen = windows[0];
  if (!chosen) return { status: "finder_window_unavailable" };
  const path = filePath(attr(chosen, "AXDocument"));
  if (path) {
    const selection = selectedPathEvidence(chosen);
    let frontmost = false;
    try {
      frontmost = Boolean(finder.frontmost());
    } catch (_error) {}
    const processId = Number(finder.unixId());
    const stableWindowNumber = windowNumber(chosen);
    const contextIdentity = {
      process_id: processId,
      window_number: stableWindowNumber,
      source: "finder",
    };
    return {
      status: "ok",
      current_folder: path,
      current_directory: path,
      selected_path: selection.selected_path,
      selection_status: selection.status,
      selection_evidence: selection.evidence,
      process_id: processId,
      window_number: stableWindowNumber,
      context_token: JSON.stringify(contextIdentity),
      finder_frontmost: frontmost,
    };
  }
  return { status: "finder_path_unavailable" };
}

function cleanForJson(value) {
  if (!value || typeof value !== "object") return value;
  const copy = {};
  for (const key of Object.keys(value)) {
    if (key === "_element") continue;
    copy[key] = value[key];
  }
  return copy;
}

function run(argv) {
  const action = String((argv && argv[0]) || "");
  let result;
  if (action === "probe") result = detectPanel();
  else if (action === "navigate") result = navigate(String(argv[1] || ""), String(argv[2] || ""));
  else if (action === "finder-context") result = finderContext();
  else result = { status: "invalid_action", action };
  return JSON.stringify(cleanForJson(result));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { filePath, paths: { MAX_NODES, MAX_DEPTH } };
}
