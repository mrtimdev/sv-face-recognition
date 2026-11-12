"use strict";

const byId = id => document.getElementById(id);
const nav = [...document.querySelectorAll("[data-page]")];
const actions = [...document.querySelectorAll("[data-action]")];
const reportForm = byId("report-filter");
const employeeForm = byId("employee-filter");
const settingsForm = byId("settings-form");
const PAGE_SIZE = 50;
let user = null;
let page = "";
let pageVersion = 0;
let requestVersion = 0;
let engineBusy = false;
let previewUrl = "";
let reportQuery = new URLSearchParams();
let employeeSearch = "";
let reportOffset = 0;
let employeeOffset = 0;
let employeeSyncPending = false;
let lastLiveState = 0;
let running = false;

const can = permission => user?.permissions.includes(permission);
const display = (id, value) => { byId(id).textContent = value ?? "—"; };

async function api(path, {method = "GET", body, raw = false} = {}) {
  const requestingUser = user;
  const headers = method === "GET" ? {} : {"X-SV-Face": "1"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    if (response.status === 401 && requestingUser && requestingUser === user) showSignIn(failure.detail);
    throw new Error(failure.detail || "The dashboard request failed.");
  }
  if (response.status === 204) return null;
  return raw ? response : response.json();
}

function showSignIn(message = "") {
  pageVersion += 1;
  user = null;
  page = "";
  reportQuery = new URLSearchParams();
  employeeSearch = "";
  reportOffset = employeeOffset = 0;
  employeeSyncPending = false;
  reportForm.reset();
  reportForm.elements.preset.dispatchEvent(new Event("change"));
  employeeForm.reset();
  settingsForm.reset();
  for (const id of ["account-name", "camera-source", "report-rows", "report-sync", "employee-rows", "current-source"]) {
    display(id, "");
  }
  clearPreview();
  byId("dashboard").hidden = true;
  byId("sign-in").hidden = false;
  display("login-error", message);
}

function notice(message = "", error = false) {
  const element = byId("notice");
  element.textContent = message;
  element.hidden = !message;
  element.classList.toggle("error", error);
}

function showDashboard(account) {
  user = account;
  byId("sign-in").hidden = true;
  byId("dashboard").hidden = false;
  display("account-name", account.name);
  nav.forEach(button => { button.hidden = !can(button.dataset.permission); });
  byId("export").hidden = !can("export_data");
  byId("engine-actions").hidden = !can("control_engine");
  const first = nav.find(button => !button.hidden);
  if (first) openPage(first.dataset.page);
}

function openPage(name) {
  page = name;
  pageVersion += 1;
  lastLiveState = 0;
  running = false;
  notice();
  nav.forEach(button => {
    const selected = button.dataset.page === name;
    byId(button.dataset.page).hidden = !selected;
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  clearPreview();
  if (page === "reports") loadReport();
  if (page === "employees") loadEmployees();
  if (page === "settings") loadSettings();
}

function clearPreview() {
  const image = byId("camera-frame");
  image.hidden = true;
  image.removeAttribute("src");
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = "";
  byId("preview-message").hidden = false;
}

function updateLive(state) {
  const recording = state.running && !state.paused;
  display("camera-status", state.running ? (state.status || "Starting").toLowerCase() : "Stopped");
  display("fps", state.camera_fps);
  display("faces", state.faces);
  display("verified", state.verified_today);
  display("camera-source", state.source);
  display("recording", recording ? "Recording enabled" : state.paused ? "Paused" : "Stopped");
  byId("recording").classList.toggle("recording", recording);
  display("hrm-status", state.hrm_status);
  display("storage-status", state.running ? (state.storage_ready ? `${state.queue ?? 0} waiting to save` : "Storage unavailable") : "");
  display("preview-message", state.running ? "Waiting for camera preview…" : "Start the engine to see the live camera.");
  actions.forEach(button => {
    const action = button.dataset.action;
    button.hidden = !(action === "start" ? !state.running :
      state.running && (action === "stop" || (state.paused ? action === "resume" : action === "pause")));
    button.disabled = engineBusy;
  });
  if (!state.running) clearPreview();
}

async function pollLive() {
  const version = pageVersion;
  try {
    if (page === "live" && !document.hidden && !engineBusy) {
      if (Date.now() - lastLiveState >= 1000) {
        const state = await api("/api/live");
        if (version !== pageVersion || engineBusy) return;
        updateLive(state);
        lastLiveState = Date.now();
        running = state.running;
      }
      if (running) {
        const response = await api("/api/preview", {raw: true});
        const jpeg = response && await response.blob();
        if (version !== pageVersion || engineBusy) return;
        if (jpeg) {
          const previous = previewUrl;
          previewUrl = URL.createObjectURL(jpeg);
          const image = byId("camera-frame");
          image.src = previewUrl;
          image.hidden = false;
          byId("preview-message").hidden = true;
          if (previous) URL.revokeObjectURL(previous);
        } else {
          clearPreview();
        }
      }
    }
  } catch (error) {
    if (version === pageVersion) {
      clearPreview();
      display("preview-message", error.message);
    }
  } finally {
    setTimeout(pollLive, 220);
  }
}

function renderRows(id, rows, width, empty) {
  const body = byId(id);
  const fragment = document.createDocumentFragment();
  for (const values of rows) {
    const row = fragment.appendChild(document.createElement("tr"));
    values.forEach(value => { row.insertCell().textContent = value ?? "—"; });
  }
  if (!rows.length) {
    const cell = fragment.appendChild(document.createElement("tr")).insertCell();
    cell.colSpan = width;
    cell.textContent = empty;
  }
  body.replaceChildren(fragment);
}

function pagination(id, total, offset, count) {
  const footer = byId(id);
  footer.querySelector("span").textContent = total ? `${offset + 1}–${offset + count} of ${total}` : "0 results";
  footer.querySelector('[data-step="-1"]').disabled = offset === 0;
  footer.querySelector('[data-step="1"]').disabled = offset + PAGE_SIZE >= total;
}

async function loadReport() {
  const view = pageVersion;
  const request = ++requestVersion;
  try {
    const query = new URLSearchParams(reportQuery);
    query.set("offset", reportOffset);
    const data = await api(`/api/attendance?${query}`);
    if (view !== pageVersion || request !== requestVersion) return;
    renderRows("report-rows", data.rows.map(row => [row.time, row.name, row.employee_id, row.status,
      `${row.duration.toFixed(1)} s`]), 5, "No attendance records match these filters.");
    pagination("report-pages", data.total, reportOffset, data.rows.length);
    display("report-sync", `${data.outbox.pending} pending sync · ${data.outbox.synced} synced`);
  } catch (error) {
    if (view === pageVersion && request === requestVersion) notice(error.message, true);
  }
}

async function loadEmployees() {
  const view = pageVersion;
  const request = ++requestVersion;
  try {
    const query = new URLSearchParams({search: employeeSearch, offset: employeeOffset});
    const data = await api(`/api/employees?${query}`);
    if (view !== pageVersion || request !== requestVersion) return;
    renderRows("employee-rows", data.rows.map(row => [row.employee_name, row.employee_id, row.department || "—",
      row.hrm_status || row.status, row.samples, row.angles]), 6, "No employees match this search.");
    pagination("employee-pages", data.total, employeeOffset, data.rows.length);
    byId("sync-employees").disabled = data.syncing;
    if (data.syncing) {
      employeeSyncPending = true;
      setTimeout(() => { if (page === "employees" && view === pageVersion) loadEmployees(); }, 2000);
    } else if (employeeSyncPending) {
      employeeSyncPending = false;
      notice(data.sync_error || "Employee directory updated.", Boolean(data.sync_error));
    }
  } catch (error) {
    if (view === pageVersion && request === requestVersion) notice(error.message, true);
  }
}

function fillSettings(settings) {
  display("current-source", `Current source · ${settings.source}`);
  const select = settingsForm.elements.attendance_mode;
  select.replaceChildren(...Object.entries(settings.modes).map(([key, label]) => new Option(label, key)));
  select.value = settings.attendance_mode;
  settingsForm.elements.source.value = "";
  settingsForm.querySelectorAll("input, select, button").forEach(input => { input.disabled = settings.running; });
  display("settings-help", settings.running ? "Stop the engine before changing camera settings." :
    "Leave the camera source blank to keep the current camera.");
}

async function loadSettings() {
  const view = pageVersion;
  try {
    const data = await api("/api/settings");
    if (view === pageVersion) fillSettings(data);
  } catch (error) {
    if (view === pageVersion) notice(error.message, true);
  }
}

byId("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  display("login-error", "");
  try {
    const account = await api("/api/session", {method: "POST", body: Object.fromEntries(new FormData(form))});
    form.reset();
    showDashboard(account);
  } catch (error) {
    form.elements.password.value = "";
    display("login-error", error.message);
  } finally {
    button.disabled = false;
  }
});

byId("sign-out").addEventListener("click", async () => {
  try {
    await api("/api/session", {method: "DELETE"});
    showSignIn();
  } catch (error) {
    notice(error.message, true);
  }
});

nav.forEach(button => button.addEventListener("click", () => openPage(button.dataset.page)));
actions.forEach(button => button.addEventListener("click", async () => {
  engineBusy = true;
  actions.forEach(action => { action.disabled = true; });
  notice();
  try {
    await api(`/api/engine/${button.dataset.action}`, {method: "POST"});
    if (button.dataset.action === "stop") clearPreview();
  } catch (error) {
    notice(error.message, true);
  } finally {
    lastLiveState = 0;
    engineBusy = false;
  }
}));

reportForm.elements.preset.addEventListener("change", () => {
  const custom = reportForm.elements.preset.value === "custom";
  reportForm.querySelectorAll(".custom-date").forEach(label => {
    label.hidden = !custom;
    label.querySelector("input").required = custom;
  });
});
reportForm.addEventListener("submit", event => {
  event.preventDefault();
  notice();
  const values = new FormData(reportForm);
  if (values.get("preset") === "custom") values.set("preset", "all");
  else {
    values.delete("start");
    values.delete("end");
  }
  reportQuery = new URLSearchParams(values);
  reportOffset = 0;
  loadReport();
});
employeeForm.addEventListener("submit", event => {
  event.preventDefault();
  notice();
  employeeSearch = employeeForm.elements.search.value;
  employeeOffset = 0;
  loadEmployees();
});
for (const button of byId("report-pages").querySelectorAll("button")) {
  button.addEventListener("click", () => {
    reportOffset = Math.max(0, reportOffset + Number(button.dataset.step) * PAGE_SIZE);
    loadReport();
  });
}
for (const button of byId("employee-pages").querySelectorAll("button")) {
  button.addEventListener("click", () => {
    employeeOffset = Math.max(0, employeeOffset + Number(button.dataset.step) * PAGE_SIZE);
    loadEmployees();
  });
}
byId("export").addEventListener("click", async () => {
  try {
    const response = await api(`/api/attendance.csv?${reportQuery}`, {raw: true});
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = "attendance.csv";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    notice(error.message, true);
  }
});
byId("sync-employees").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await api("/api/employees/sync", {method: "POST"});
    employeeSyncPending = true;
    notice("Syncing the employee directory…");
    loadEmployees();
  } catch (error) {
    button.disabled = false;
    notice(error.message, true);
  }
});
settingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  const button = settingsForm.querySelector("button");
  button.disabled = true;
  try {
    const settings = await api("/api/settings", {method: "PUT", body: Object.fromEntries(new FormData(settingsForm))});
    if (page === "settings") {
      fillSettings(settings);
      notice("Camera settings saved.");
    }
  } catch (error) {
    button.disabled = false;
    notice(error.message, true);
  }
});

api("/api/session").then(showDashboard).catch(error => {
  showSignIn(error.message === "Sign in to continue." ? "" : error.message);
});
pollLive();
