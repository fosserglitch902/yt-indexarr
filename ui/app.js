"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const fmtSize = (bytes) => {
  if (!bytes && bytes !== 0) return "—";
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + " " + units[i];
};
const fmtPct = (p) => (Math.round(p * 100) + "%");
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATE_LABEL = {
  downloading: "Downloading", downloadingUP: "Uploading",
  completed: "Completed", uploading: "Completed", stalledUP: "Seeding",
  checkingUP: "Checking", pausedDL: "Paused", pausedUP: "Paused",
  metaDL: "Resolving", queuedDL: "Queued", queuedUP: "Queued",
  checkingDL: "Checking", error: "Error",
};

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (res.status === 403) { showLogin(); throw new Error("auth required"); }
  return res;
}

/* ---------- history ---------- */

function badge(text, cls) {
  const b = el("span", "badge" + (cls ? " " + cls : ""), text);
  return b;
}

function metaLine(item) {
  const parts = [];
  if (item.channel) parts.push(esc(item.channel));
  if (item.category) parts.push(esc(item.category));
  parts.push(fmtSize(item.size));
  parts.push("added " + fmtTime(item.added_on));
  if (item.completion_on && item.progress >= 1) parts.push("done " + fmtTime(item.completion_on));
  return el("div", "meta", parts.join(" · "));
}

function poster(item) {
  const img = el("img");
  img.loading = "lazy";
  img.alt = "";
  img.src = item.poster_url || item.thumbnail_url ||
    "data:image/svg+xml;utf8," + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="54">' +
      '<rect width="100%" height="100%" fill="#232936"/></svg>');
  return img;
}

function episodeRow(ep) {
  const row = el("div", "ep");
  row.append(el("span", "num", "#" + esc(ep.index)));
  if (ep.url) {
    const a = el("a", null, esc(ep.title));
    a.href = ep.url;
    a.target = "_blank";
    a.rel = "noopener";
    row.append(a);
  } else {
    row.append(el("span", null, esc(ep.title)));
  }
  row.append(el("span", "spacer"));
  if (ep.size) row.append(el("span", "meta", fmtSize(ep.size)));
  row.append(badge(STATE_LABEL[ep.state] || ep.state || "—",
    ep.state === "error" ? "err" : (ep.state === "completed" || ep.state === "downloading") ? "" : "warn"));
  if (ep.error) row.append(badge(esc(ep.error), "err"));
  return row;
}

function renderItem(item) {
  const isPlaylist = item.is_playlist && item.episodes && item.episodes.length > 0;
  const top = el("div", "item");
  top.append(poster(item));

  const body = el("div", "body");
  const name = el("div", "name");
  if (item.url) {
    const a = el("a", null, esc(item.title));
    a.href = item.url;
    a.target = "_blank";
    a.rel = "noopener";
    name.append(a);
  } else {
    name.append(esc(item.title || "(untitled)"));
  }
  body.append(name, metaLine(item));

  const badges = el("div", "badges");
  badges.append(badge(STATE_LABEL[item.state] || item.state || "—",
    item.state === "error" ? "err" : (item.state === "downloading") ? "warn" : ""));
  if (item.is_playlist) badges.append(badge("playlist " + item.episodes.length + " eps"));
  if (item.error) badges.append(badge(esc(item.error), "err"));
  if (item.tags) badges.append(badge(esc(item.tags)));
  body.append(badges);

  if (item.state === "downloading" && item.progress < 1) {
    const p = el("div", "progress");
    const fill = el("div");
    fill.style.width = fmtPct(item.progress);
    p.append(fill);
    body.append(p);
  }
  top.append(body);

  if (isPlaylist) {
    const det = el("details", "item");
    const sum = el("summary");
    sum.append(top);
    det.append(sum);
    const eps = el("div", "episodes");
    item.episodes.forEach((ep) => eps.append(episodeRow(ep)));
    det.append(eps);
    return det;
  }
  return top;
}

async function refreshHistory() {
  const list = $("#view-history");
  let res;
  try {
    res = await api("/api/ui/history");
  } catch (e) {
    list.replaceChildren(el("div", "empty", "Could not load history."));
    return;
  }
  const data = await res.json();
  const items = data.items || [];
  list.replaceChildren();
  if (!items.length) {
    list.append(el("div", "empty", "No downloads yet — add a torrent via qBittorrent to get started."));
    return;
  }
  items.forEach((it) => list.append(renderItem(it)));
}

/* ---------- settings ---------- */

function fieldRow(s, isCookies) {
  const row = el("div", "field");
  const label = el("label", null, s.key.replace(/_/g, " "));
  row.append(label);

  const controls = el("div", "controls");
  let input;
  if (s.kind === "select") {
    input = el("select");
    input.name = s.key;
    (s.options || []).forEach((opt) => {
      const o = el("option", null, opt);
      o.value = opt;
      input.append(o);
    });
    input.value = s.value || s.default || "";
  } else if (s.kind === "bool") {
    input = el("select");
    input.name = s.key;
    [["1", "On"], ["0", "Off"]].forEach(([v, t]) => {
      const o = el("option", null, t);
      o.value = v;
      input.append(o);
    });
    input.value = s.value || s.default || "0";
  } else {
    input = el("input");
    input.type = s.kind === "password" ? "password" : "text";
    input.name = s.key;
    input.placeholder = s.secret && s.set ? "(already set — leave blank to keep)" : s.default || "";
    input.autocomplete = s.secret ? "new-password" : "off";
  }
  controls.append(input);

  const hints = [];
  if (s.env_set) hints.push("set by compose (overrides UI)");
  else if (s.file_set) hints.push("set via UI");
  if (s.secret && s.set && s.env_set) hints.push("secret from env");
  if (hints.length) {
    const hint = el("span", "hint", hints.join(" · "));
    controls.append(hint);
  }
  row.append(controls);
  return row;
}

async function loadSettings() {
  const box = $("#view-settings");
  let res;
  try {
    res = await api("/api/ui/settings");
  } catch (e) {
    box.replaceChildren(el("div", "empty", "Could not load settings."));
    return;
  }
  const data = await res.json();

  const form = el("form", "panel");
  form.id = "settings-form";
  (data.settings || []).forEach((s) => form.append(fieldRow(s, false)));

  form.append(fieldRow({
    key: "YT_QBT_COOKIES", kind: "text", secret: false, set: false,
    value: data.cookies.path, env_set: data.cookies.env_set, file_set: data.cookies.file_set,
  }, false));

  const cookiesLabel = el("label", null, "paste cookies.txt contents");
  const cookies = el("textarea");
  cookies.name = "cookies_content";
  cookies.placeholder = "Paste Netscape-format cookies here to write them to the file above…";
  const cHint = el("span", "hint",
    data.cookies.active
      ? "Cookies enabled (" + (data.cookies.env_set ? "path from compose env" : "path from UI") + "). Leave blank to keep the current file."
      : "No cookies configured — YouTube 403s may require them.");
  const cCtl = el("div", "controls");
  cCtl.append(cookies, cHint);
  const cRow = el("div", "field");
  cRow.append(cookiesLabel, cCtl);
  form.append(cRow);

  const actions = el("div", "actions");
  const save = el("button", "save", "Save");
  save.type = "submit";
  const msg = el("span");
  msg.id = "save-msg";
  actions.append(save, msg);
  form.append(actions);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const settings = {};
    (data.settings || []).forEach((s) => {
      const f = form.elements[s.key];
      if (f) settings[s.key] = f.value;
    });
    const cookiesContent = form.elements["cookies_content"].value;
    msg.textContent = "";
    msg.className = "";
    try {
      const r = await api("/api/ui/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings, cookies: cookiesContent }),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) {
        msg.textContent = "Saved.";
        msg.className = "ok";
        cookies.value = "";
        loadSettings();
      } else {
        msg.textContent = j.error || "Save failed.";
        msg.className = "err";
      }
    } catch (e) {
      msg.textContent = "Save failed.";
      msg.className = "err";
    }
  });

  box.replaceChildren(form);
}

/* ---------- login ---------- */

async function showLogin() {
  const overlay = $("#login-overlay");
  if (!overlay.hidden) return;
  overlay.hidden = false;
  $("#login-error").textContent = "";
}
function hideLogin() {
  $("#login-overlay").hidden = true;
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const body = new URLSearchParams({
    username: f.elements.username.value,
    password: f.elements.password.value,
  });
  const res = await fetch("/api/v2/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  const text = await res.text();
  if (res.ok && text.trim() === "Ok.") {
    hideLogin();
    f.reset();
    refreshHistory();
    loadSettings();
  } else {
    $("#login-error").textContent = "Sign-in failed.";
  }
});

/* ---------- boot ---------- */

$("#tab-history").addEventListener("click", () => switchView("history"));
$("#tab-settings").addEventListener("click", () => switchView("settings"));

function switchView(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "history") refreshHistory();
  else loadSettings();
}

loadSettings();
refreshHistory();
setInterval(refreshHistory, 5000);