ObjC.import("ApplicationServices");

function attribute(element, name) {
  try {
    return element.attributes.byName(name).value();
  } catch (_) {
    return null;
  }
}

function inspectFocus(argv) {
  const expectedPid = Number(argv[0] || "0");
  const systemEvents = Application("System Events");
  const trusted = $.AXIsProcessTrusted();
  if (!trusted) {
    return { status: "accessibility_denied", permission: "denied" };
  }
  const processes = systemEvents.applicationProcesses.whose({ unixId: expectedPid })();
  if (!processes || processes.length !== 1) {
    return { status: "process_not_found", permission: "granted", pid: expectedPid };
  }
  const process = processes[0];
  let focused = null;
  try {
    focused = process.attributes.byName("AXFocusedUIElement").value();
  } catch (_) {
    return { status: "focused_element_unavailable", permission: "granted", pid: expectedPid };
  }
  const identifier = attribute(focused, "AXIdentifier");
  const role = attribute(focused, "AXRole");
  const result = {
    status: identifier === "ListaryWindowFixtureQuery" ? "ok" : "focus_mismatch",
    permission: "granted",
    pid: expectedPid,
    focused_identifier: identifier,
    focused_role: role,
    query_focused: identifier === "ListaryWindowFixtureQuery"
  };
  return result;
}

function run(argv) {
  return JSON.stringify(inspectFocus(argv || []));
}
