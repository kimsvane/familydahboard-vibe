const state = {
  summary: null,
  calendarEvents: [],
  schoolEvents: [],
  calendarCursor: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  selectedDate: new Date(),
  schoolWeekStart: startOfWeek(new Date()),
  activeView: localStorage.getItem("fd-view") || "today",
  refreshTimer: null,
  clockTimer: null,
  settings: {},
  cameras: [],
  cameraBroken: new Set(),
  cameraLive: new Set(),
  detectKey: null,
  reminderLists: [],
  icloudNote: null,
  cameraTimer: null,
  detectTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const appShell = $("#app-shell");
const loginScreen = $("#login-screen");
const modalRoot = $("#modal-root");
const toastRoot = $("#toast-root");

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

function initials(name) {
  return String(name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function localDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function parseDateKey(value) {
  const [year, month, day] = String(value).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function startOfWeek(value) {
  const date = new Date(value);
  const offset = (date.getDay() + 6) % 7;
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() - offset);
  return date;
}

function formatDate(value, options = { weekday: "long", day: "numeric", month: "long" }) {
  return new Intl.DateTimeFormat("da-DK", options).format(typeof value === "string" ? parseDateKey(value) : value);
}

function formatShortDate(value) {
  if (!value) return "";
  const text = String(value);
  const date = /^\d{4}-\d{2}-\d{2}$/.test(text) ? parseDateKey(text) : new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  return new Intl.DateTimeFormat("da-DK", { day: "numeric", month: "short" }).format(date);
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("da-DK", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function eventDateKey(event) {
  return event.local_date || String(event.start_at || "").slice(0, 10);
}

function formatEventTime(event) {
  if (event.all_day) return "Hele dagen";
  const start = event.local_start_time || formatTime(event.start_at);
  const end = event.local_end_time || formatTime(event.end_at);
  return `${start} – ${end}`;
}

function getCookie(name) {
  return document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`))?.split("=").slice(1).join("=") || "";
}

async function api(path, options = {}) {
  const request = { credentials: "same-origin", ...options, headers: { ...(options.headers || {}) } };
  if (request.body && !(request.body instanceof FormData)) {
    request.headers["Content-Type"] = "application/json";
    if (typeof request.body !== "string") request.body = JSON.stringify(request.body);
  }
  if (["POST", "PUT", "PATCH", "DELETE"].includes((request.method || "GET").toUpperCase())) {
    const csrf = getCookie("fd_csrf");
    if (csrf) request.headers["X-FD-CSRF"] = csrf;
  }
  const response = await fetch(path, request);
  if (response.status === 401) {
    showLogin();
    throw new Error("Login required");
  }
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Noget gik galt");
  return payload;
}

function setConnection(online, label) {
  const pill = $("#connection-pill");
  pill.classList.toggle("online", online);
  pill.classList.toggle("offline", !online);
  pill.innerHTML = `<span class="status-dot"></span> ${escapeHtml(label || (online ? "Opdateret" : "Offline"))}`;
}

function showToast(message, error = false) {
  const toast = document.createElement("div");
  toast.className = `toast${error ? " error" : ""}`;
  toast.textContent = message;
  toastRoot.append(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

function showLogin() {
  if (state.clockTimer) window.clearInterval(state.clockTimer);
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  if (state.cameraTimer) window.clearInterval(state.cameraTimer);
  state.cameraTimer = null;
  if (state.detectTimer) window.clearTimeout(state.detectTimer);
  state.detectTimer = null;
  appShell.hidden = true;
  loginScreen.hidden = false;
  window.setTimeout(() => $("#login-password")?.focus(), 0);
}

function showApp() {
  loginScreen.hidden = true;
  appShell.hidden = false;
  updateClock();
  if (state.clockTimer) window.clearInterval(state.clockTimer);
  state.clockTimer = window.setInterval(updateClock, 1000);
  if (state.refreshTimer) window.clearInterval(state.refreshTimer);
  state.refreshTimer = window.setInterval(() => loadSummary(true), 60000);
  loadCameras(true);
  if (state.settings.notes_imap_configured) loadIcloudNote(true);
  measureChrome();
  window.addEventListener("resize", measureChrome);
  if (window.ResizeObserver) new ResizeObserver(measureChrome).observe(document.querySelector(".topbar"));
}

/// Topbar og navigation er sticky, så oversigten skal trække deres højde fra
/// skærmhøjeden for at kunne fylde resten uden at siden kommer til at scrolle.
function measureChrome() {
  const topbar = document.querySelector(".topbar");
  const nav = document.querySelector(".main-nav");
  if (!topbar || !nav) return;
  const height = Math.round(topbar.getBoundingClientRect().height + nav.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--chrome-height", `${height}px`);
}

function updateClock() {
  const now = new Date();
  const showSeconds = state.summary?.settings?.show_seconds !== "false";
  $("#clock").textContent = new Intl.DateTimeFormat("da-DK", { hour: "2-digit", minute: "2-digit", second: showSeconds ? "2-digit" : undefined }).format(now);
  $("#top-date").textContent = formatDate(now);
}

function greeting() {
  const hour = new Date().getHours();
  if (hour < 11) return "Godmorgen";
  if (hour < 17) return "God eftermiddag";
  return "Godaften";
}

function renderSummary() {
  const summary = state.summary;
  if (!summary) return;
  const settings = summary.settings || {};
  state.settings = settings;
  const displayName = settings.display_name || "Familiedashboard";
  $("#brand-title").textContent = displayName;
  $("#brand-subtitle").textContent = settings.location_name || "Din families hjem";
  $("#hero-greeting").textContent = settings.greeting || greeting();
  $("#hero-title").textContent = settings.header_title || "Familietid";
  const today = summary.date || localDateKey(new Date());
  const calendarEvents = (summary.events || []).filter((event) => event.source_kind !== "school");
  const schoolEvents = (summary.events || []).filter((event) => event.source_kind === "school");
  const todayEvents = calendarEvents.filter((event) => eventDateKey(event) === today);
  const todaySchoolEvents = schoolEvents.filter((event) => eventDateKey(event) === today);
  $("#today-event-count").textContent = todayEvents.length;
  $("#today-event-foot").textContent = todayEvents.length ? (todayEvents.length === 1 ? "familieaftale i dag" : "familieaftaler i dag") : "Ingen familieaftaler";
  const nextBirthday = (summary.birthdays || [])[0];
  $("#next-birthday-count").textContent = nextBirthday ? (nextBirthday.days_until === 0 ? "I dag" : `${nextBirthday.days_until}d`) : "—";
  $("#next-birthday-name").textContent = nextBirthday ? nextBirthday.name : "Endnu ingen fødselsdage";
  renderWeather(summary.weather, settings);
  renderTodayEvents(calendarEvents);
  renderTodaySchoolEvents(todaySchoolEvents);
  renderBirthdayPreview(summary.birthdays || []);
  renderMembers(summary.members || []);
  renderChecklist(summary.checklist || []);
  renderBirthdayGrid(summary.birthdays || []);
  renderSources(summary.sources || []);
  renderFrames(summary.frames || []);
  renderSettings(summary);
  renderIcloudNote();
  $("#last-updated").textContent = `Opdateret ${formatTime(summary.generated_at)}`;
  $("#dashboard-url").textContent = window.location.origin;
}

function renderWeather(weather, settings) {
  const icon = $("#weather-icon");
  const temperature = $("#weather-temperature");
  const location = $("#weather-location");
  if (!weather) {
    icon.textContent = "—";
    temperature.textContent = "--°";
    location.textContent = settings.location_name || "Vejr";
    return;
  }
  const code = Number(weather.weather_code);
  const labels = { 0: "☼", 1: "◐", 2: "◒", 3: "☁", 45: "≈", 48: "≈", 51: "·", 61: "·", 63: "·", 65: "·", 71: "❄", 73: "❄", 75: "❄", 80: "·", 81: "·", 82: "·", 95: "ϟ" };
  const unit = settings.temperature_unit === "fahrenheit" ? "°F" : "°C";
  const celsius = Number(weather.temperature);
  const value = settings.temperature_unit === "fahrenheit" ? celsius * 9 / 5 + 32 : celsius;
  icon.textContent = labels[code] || "◌";
  temperature.textContent = `${Number.isFinite(value) ? Math.round(value) : "--"}°`;
  location.textContent = weather.location || settings.location_name || "Vejr";
}

function eventItem(event, compact = false) {
  const sourceLabel = event.source_kind === "school" ? `Skole · ${event.source_name || "Skoleskema"}` : event.source_name || "Kalender";
  return `<div class="event-item" data-event-id="${escapeHtml(event.id)}"><span class="event-color" style="background:${escapeHtml(event.source_color || "#5c7cfa")}"></span><div class="event-main"><p class="event-title">${escapeHtml(event.title)}</p><div class="event-meta">${event.location ? `<span>${escapeHtml(event.location)}</span>` : ""}<span>${escapeHtml(sourceLabel)}</span></div></div><span class="event-time">${escapeHtml(compact ? formatEventTime(event) : formatEventTime(event))}</span></div>`;
}

function renderTodayEvents(events) {
  const target = $("#today-events");
  if (!events.length) {
    target.innerHTML = `<div class="empty-state">Der er ingen familieaftaler de næste dage.<br>Tilføj en kalender under indstillinger.</div>`;
    return;
  }
  target.innerHTML = events.slice(0, 8).map((event) => eventItem(event)).join("");
}

function groupEventsBySource(events) {
  const grouped = new Map();
  for (const event of events) {
    const sourceId = String(event.source_id || event.source_name || "calendar");
    if (!grouped.has(sourceId)) grouped.set(sourceId, { name: event.source_name || "Kalender", color: event.source_color || "#5c7cfa", events: [] });
    grouped.get(sourceId).events.push(event);
  }
  return [...grouped.values()];
}

function renderTodaySchoolEvents(events) {
  const target = $("#today-school-events");
  if (!events.length) {
    target.innerHTML = `<div class="empty-state">Ingen skoletimer i dag.</div>`;
    return;
  }
  target.innerHTML = groupEventsBySource(events).map((group) => `<section class="agenda-group"><h3><i style="background:${escapeHtml(group.color)}"></i><span class="agenda-source-name">${escapeHtml(group.name)}</span><span class="agenda-count">${group.events.length}</span></h3><div class="event-list">${group.events.map((event) => eventItem(event)).join("")}</div></section>`).join("");
}

function birthdayItem(birthday) {
  const days = birthday.days_until === 0 ? "I dag" : birthday.days_until === 1 ? "I morgen" : `Om ${birthday.days_until} dage`;
  return `<div class="birthday-item"><div class="avatar" style="background:${escapeHtml(birthday.color || "#f59e0b")}">${escapeHtml(initials(birthday.name))}</div><div class="birthday-copy"><strong>${escapeHtml(birthday.name)}</strong><span>${escapeHtml(formatDate(birthday.next_occurrence, { day: "numeric", month: "long" }))} · ${birthday.age} år</span></div><span class="birthday-days">${escapeHtml(days)}</span></div>`;
}

function renderBirthdayPreview(birthdays) {
  const target = $("#today-birthdays");
  target.innerHTML = birthdays.length ? birthdays.slice(0, 4).map(birthdayItem).join("") : `<div class="empty-state">Tilføj fødselsdage for at se dem her.</div>`;
}

function renderMembers(members) {
  const target = $("#today-members");
  target.innerHTML = members.length ? members.map((member) => `<div class="member-item"><div class="avatar" style="background:${escapeHtml(member.color || "#5c7cfa")}">${member.avatar_url ? `<img class="avatar-image" src="${escapeHtml(member.avatar_url)}" alt="">` : escapeHtml(initials(member.name))}</div><div class="member-copy"><strong>${escapeHtml(member.name)}</strong><span>Familiemedlem</span></div></div>`).join("") : `<div class="empty-state">Ingen familiemedlemmer endnu.</div>`;
}

function renderChecklist(items) {
  const target = $("#today-checklist");
  target.innerHTML = items.length ? items.slice(0, 12).map((item) => `<div class="check-row ${item.done ? "done" : ""}"><input type="checkbox" data-check-id="${item.id}" ${item.done ? "checked" : ""} aria-label="${escapeHtml(item.text)}"><label>${escapeHtml(item.text)}</label>${item.source === "icloud" ? `<span class="check-badge">iCloud</span>` : ""}<span class="check-actions"><button class="icon-button" data-edit-check="${item.id}" type="button" aria-label="Rediger opgave" title="Rediger">✎</button><button class="icon-button" data-delete-check="${item.id}" type="button" aria-label="Slet opgave" title="Slet">×</button></span></div>`).join("") : `<div class="empty-state">Ingen opgaver lige nu.</div>`;
}

function renderIcloudNote() {
  const card = $("#icloud-note-card");
  const configured = state.settings.notes_imap_configured;
  const note = state.icloudNote;
  card.hidden = !configured;
  if (!configured) return;
  $("#icloud-note-title").textContent = (note?.title || "") || $("#setting-notes-title").value || "iCloud-note";
  const body = $("#today-icloud-note");
  if (note?.error && note.error !== "disabled") {
    body.innerHTML = `<p class="muted small-copy">${escapeHtml(note.error)}</p><p class="muted small-copy">Tjek Apple-id/app-specifikt password under <a href="#" data-view-link="settings" class="text-button">Indstillinger</a>.</p>`;
  } else if (!note?.enabled) {
    body.innerHTML = `<p class="muted small-copy">iCloud-note er gemt, men visningen er slået fra – marker “Aktivér visning af note” under <a href="#" data-view-link="settings" class="text-button">Indstillinger</a> og gem igen.</p>`;
  } else if (note?.found === false) {
    const diagnostic = note.debug
      ? `<p class="muted small-copy debug-copy"><strong>Diagnostik:</strong> ${escapeHtml(note.debug)}</p>`
      : "";
    body.innerHTML = `<p class="muted small-copy">Noten <strong>“${escapeHtml(note.title || $("#setting-notes-title").value || "")}”</strong> blev ikke fundet i iCloud. Tjek at titlen i “Note-titel”-feltet staver præcist som i Notes-appen – kun en note med nøjagtig samme titel vises.</p>${diagnostic}`;
  } else if (note.content) {
    body.innerHTML = `<p>${escapeHtml(note.content)}</p>`;
  } else {
    body.innerHTML = `<p class="muted small-copy">Noten er tom eller blev ikke fundet endnu.</p>`;
  }
}

function renderBirthdayGrid(birthdays) {
  const target = $("#birthday-grid");
  if (!birthdays.length) {
    target.innerHTML = `<div class="empty-state">Ingen fødselsdage endnu. Tryk på “Tilføj fødselsdag” for at komme i gang.</div>`;
    return;
  }
  target.innerHTML = birthdays.map((birthday) => `<article class="panel birthday-card ${birthday.is_today ? "is-today" : ""}" style="color:${escapeHtml(birthday.color || "#f5c76b")}"><div class="card-actions"><button class="icon-button" data-edit-birthday="${birthday.id}" type="button" aria-label="Rediger">✎</button><button class="icon-button" data-delete-birthday="${birthday.id}" type="button" aria-label="Slet">×</button></div><p class="eyebrow" style="color:inherit">${birthday.is_today ? "I dag" : birthday.days_until === 1 ? "I morgen" : `Om ${birthday.days_until} dage`}</p><div class="big-days">${birthday.days_until === 0 ? "I dag" : birthday.days_until}</div><p class="birthday-name">${escapeHtml(birthday.name)}</p><p class="birthday-meta">${escapeHtml(formatDate(birthday.next_occurrence, { day: "numeric", month: "long", year: "numeric" }))} · ${birthday.age} år</p>${birthday.notes ? `<p class="birthday-meta">${escapeHtml(birthday.notes)}</p>` : ""}</article>`).join("");
}

function sourceStatus(source) {
  if (source.last_error) return "Fejl";
  if (source.last_synced_at) return `Synk ${formatShortDate(source.last_synced_at)}`;
  return "Ikke synk";
}

function sourceActions(source) {
  return `<div class="source-actions"><span class="source-status ${source.last_error ? "error" : ""}">${escapeHtml(sourceStatus(source))}</span><button class="icon-button" data-sync-source="${source.id}" type="button" aria-label="Synk">↻</button><button class="icon-button" data-edit-source="${source.id}" type="button" aria-label="Rediger">✎</button><button class="icon-button" data-delete-source="${source.id}" type="button" aria-label="Slet">×</button></div>`;
}

function renderSources(sources) {
  const target = $("#source-list");
  const calendars = sources.filter((source) => (source.kind || "calendar") === "calendar");
  target.innerHTML = calendars.length ? calendars.map((source) => `<div class="source-item"><div class="source-info"><strong>${escapeHtml(source.name)}</strong><span>${escapeHtml(source.url)}</span></div>${sourceActions(source)}</div>`).join("") : `<div class="empty-state">Ingen familiekalendere endnu. Tilføj en delt ICS- eller WebCal-url.</div>`;
}

function renderFrames(frames) {
  const target = $("#frame-grid");
  if (!frames.length) {
    target.innerHTML = `<div class="empty-state frame-empty">Ingen widgets endnu. Tilføj en iframe, for eksempel Home Assistant eller en nyhedsside.</div>`;
    return;
  }
  target.innerHTML = frames.map((frame) => `<article class="panel frame-item" style="min-height:${frame.height + 54}px"><div class="frame-header"><div><strong>${escapeHtml(frame.name)}</strong><span> · iframe</span></div><div class="frame-actions"><button class="icon-button" data-edit-frame="${frame.id}" type="button" aria-label="Rediger">✎</button><button class="icon-button" data-delete-frame="${frame.id}" type="button" aria-label="Slet">×</button></div></div><iframe src="${escapeHtml(frame.url)}" title="${escapeHtml(frame.name)}" height="${frame.height}" loading="lazy" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads"></iframe></article>`).join("");
}

function renderSettings(summary) {
  const settings = summary.settings || {};
  $("#setting-display-name").value = settings.display_name || "";
  $("#setting-header-title").value = settings.header_title || "";
  $("#setting-greeting").value = settings.greeting || "";
  $("#setting-location").value = settings.location_name || "";
  $("#setting-latitude").value = settings.latitude || "";
  $("#setting-longitude").value = settings.longitude || "";
  const target = $("#member-settings-list");
  target.innerHTML = summary.members?.length ? summary.members.map((member) => `<div class="settings-list-item"><div class="member-item"><div class="avatar" style="background:${escapeHtml(member.color || "#5c7cfa")}">${escapeHtml(initials(member.name))}</div><div class="member-copy"><strong>${escapeHtml(member.name)}</strong><span>Familiemedlem</span></div></div><div class="frame-actions"><button class="icon-button" data-edit-member="${member.id}" type="button" aria-label="Rediger">✎</button><button class="icon-button" data-delete-member="${member.id}" type="button" aria-label="Slet">×</button></div></div>`).join("") : `<div class="empty-state">Ingen medlemmer endnu.</div>`;
  const sources = summary.sources || [];
  const calendarCount = sources.filter((source) => (source.kind || "calendar") === "calendar").length;
  const schoolCount = sources.filter((source) => source.kind === "school").length;
  $("#settings-status").textContent = `${calendarCount} kalendere · ${schoolCount} skoleskemaer · ${summary.frames?.length || 0} widgets · ${summary.birthdays?.length || 0} fødselsdage · ${state.cameras.length} kameraer`;
  const remindersForm = $("#icloud-reminders-form");
  const remindersSource = settings.reminders_source === "caldav" ? "caldav" : "bridge";
  remindersForm.elements.reminders_source.value = remindersSource;
  remindersForm.elements.reminders_enabled.checked = settings.reminders_enabled === true || settings.reminders_enabled === "true";
  remindersForm.elements.reminders_username.value = settings.reminders_username || "";
  remindersForm.elements.reminders_bridge_url.value = settings.reminders_bridge_url || "";
  remindersForm.elements.reminders_sync_minutes.value = settings.reminders_sync_minutes || "";
  toggleRemindersSourceFields(remindersSource);
  populateReminderListSelect(settings.reminders_list_href, settings.reminders_list_name);
  const remindersBadge = $("#reminders-status");
  const remindersSourceLabel = remindersSource === "bridge" ? "Mac mini" : "iCloud CalDAV";
  remindersBadge.textContent = settings.reminders_configured ? `Aktiv via ${remindersSourceLabel}` : "Ikke konfigureret";
  remindersBadge.classList.toggle("ok", !!settings.reminders_configured);
  remindersBadge.classList.toggle("error", !settings.reminders_configured);
  if (settings.reminders_configured && !settings.reminders_list_href) remindersBadge.textContent = "Manglende liste";
  if (settings.reminders_last_error) {
    const hint = remindersSource === "bridge"
      ? "Kontrollér at FamilyBridge kører på Mac mini'en, at adressen og tokenet er korrekte, og at Påmindelser er godkendt under Systemindstillinger > Anonymitet og sikkerhed > Påmindelser."
      : "Brug et app-specifikt password oprettet på appleid.apple.com (kræver to-faktor-login) og kopiér det præcist, uden mellemrum.";
    $("#reminders-help").innerHTML = `Sidste fejl: <span class="error-text">${escapeHtml(settings.reminders_last_error)}</span><br>${hint}`;
  }
  const notesForm = $("#icloud-notes-form");
  notesForm.elements.notes_imap_enabled.checked = settings.notes_imap_enabled === true || settings.notes_imap_enabled === "true";
  notesForm.elements.notes_imap_username.value = settings.notes_imap_username || "";
  notesForm.elements.notes_imap_host.value = settings.notes_imap_host || "imap.mail.me.com";
  notesForm.elements.notes_imap_note_title.value = settings.notes_imap_note_title || "";
  const notesSource = settings.notes_source === "imap" ? "imap" : "bridge";
  notesForm.elements.notes_source.value = notesSource;
  const notesImapFields = $("#notes-imap-fields");
  if (notesImapFields) notesImapFields.hidden = notesSource !== "imap";
  const notesBadge = $("#notes-status");
  notesBadge.textContent = settings.notes_imap_configured
    ? (settings.reminders_bridge_configured && notesSource === "bridge" ? "Aktiv via Mac mini" : "Aktiv")
    : "Ikke konfigureret";
  notesBadge.classList.toggle("ok", !!settings.notes_imap_configured);
  notesBadge.classList.toggle("error", !settings.notes_imap_configured);
  if (settings.notes_imap_last_error) {
    $("#notes-help").innerHTML = `Sidste fejl: <span class="error-text">${escapeHtml(settings.notes_imap_last_error)}</span>`;
  }
  const reolinkForm = $("#reolink-form");
  reolinkForm.elements.reolink_poll_seconds.value = settings.reolink_poll_seconds || 5;
  reolinkForm.elements.reolink_close_delay.value = settings.reolink_close_delay ?? 0;
  const loggingForm = $("#logging-form");
  if (loggingForm) loggingForm.elements.log_level.value = settings.log_level || "warning";
}

function toggleRemindersSourceFields(source) {
  const bridge = $("#reminders-bridge-fields");
  const caldav = $("#reminders-caldav-fields");
  if (bridge) bridge.hidden = source !== "bridge";
  if (caldav) caldav.hidden = source === "bridge";
}

function populateReminderListSelect(href = "", name = "") {
  const select = $("#setting-reminders-list");
  const lists = state.reminderLists || [];
  const options = lists.map((list) => `<option value="${escapeHtml(list.href)}" data-name="${escapeHtml(list.name)}" ${href === list.href ? "selected" : ""}>${escapeHtml(list.name)}</option>`).join("");
  const fallback = !lists.length && href ? `<option value="${escapeHtml(href)}" data-name="${escapeHtml(name)}" selected>${escapeHtml(name || href)}</option>` : "";
  select.innerHTML = `<option value="" data-name="">— Vælg liste —</option>${options}${fallback}`;
}

function openModal(title, body, onSubmit, submitLabel = "Gem") {
  modalRoot.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="modal-title"><h2 id="modal-title">${escapeHtml(title)}</h2><form id="modal-form">${body}<div class="modal-actions"><button class="button secondary" data-close-modal type="button">Annullér</button><button class="button primary" type="submit">${escapeHtml(submitLabel)}</button></div></form></div>`;
  modalRoot.hidden = false;
  const form = $("#modal-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("button[type=submit]", form);
    button.disabled = true;
    try {
      await onSubmit(Object.fromEntries(new FormData(form).entries()));
      closeModal();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("[data-close-modal]", modalRoot)?.focus();
}

function closeModal() {
  modalRoot.hidden = true;
  modalRoot.innerHTML = "";
}

function field(label, name, value = "", type = "text", extra = "") {
  return `<label for="field-${name}">${escapeHtml(label)}</label><input id="field-${name}" name="${name}" type="${type}" value="${escapeHtml(value)}" ${extra}>`;
}

function openCalendarModal(source = null, kind = "calendar") {
  const selectedKind = source?.kind || kind;
  const isSchool = selectedKind === "school";
  const title = source ? `Rediger ${isSchool ? "skoleskema" : "kalender"}` : isSchool ? "Tilføj skoleskema" : "Tilføj kalender";
  const palette = isSchool ? ["#4fd1a1", "#f5c76b", "#a78bfa", "#f472b6"] : ["#5c7cfa", "#38bdf8", "#a78bfa", "#fb923c"];
  const peerCount = (state.summary?.sources || []).filter((item) => (item.kind || "calendar") === selectedKind).length;
  const defaultColor = palette[peerCount % palette.length];
  const kindControl = source ? `<div><label for="field-source-kind">Visning</label><select id="field-source-kind" name="kind"><option value="calendar" ${isSchool ? "" : "selected"}>Familiekalender</option><option value="school" ${isSchool ? "selected" : ""}>Skoleskema</option></select></div>` : `<input type="hidden" name="kind" value="${selectedKind}"><div class="source-kind-summary"><span>Vises som</span><strong>${isSchool ? "Skoleskema" : "Familiekalender"}</strong></div>`;
  const body = `${field("Navn", "name", source?.name || (isSchool ? "Skole" : "Familiekalender"), "text", "required maxlength=120")}${field("ICS / WebCal URL", "url", source?.url || "", "url", "required maxlength=2000")}<div class="form-row"><div><label for="field-source-type">Format</label><select id="field-source-type" name="source_type"><option value="ics" ${source?.source_type === "webcal" ? "" : "selected"}>ICS</option><option value="webcal" ${source?.source_type === "webcal" ? "selected" : ""}>WebCal</option></select></div>${kindControl}</div><label for="field-color">Farve</label><input id="field-color" name="color" type="color" value="${escapeHtml(source?.color || defaultColor)}"><label class="check-label"><input type="checkbox" name="enabled" ${source?.enabled !== false ? "checked" : ""}> Aktivér abonnementet</label>`;
  openModal(title, body, async (values) => {
    const payload = { name: values.name, url: values.url, source_type: values.source_type, kind: values.kind, color: values.color, enabled: values.enabled === "on" };
    const result = await api(source ? `/api/calendars/${source.id}` : "/api/calendars", { method: source ? "PATCH" : "POST", body: payload });
    if (source) {
      await synchroniseSingle(source.id);
    } else {
      let syncResult;
      try {
        syncResult = await api(`/api/calendars/${result.calendar.id}/sync`, { method: "POST" });
      } catch (error) {
        syncResult = { ok: false, error: error.message };
      }
      showToast(syncResult.ok ? "Abonnementet er tilføjet og synkroniseret" : `Abonnementet er tilføjet, men synkroniseringen fejlede: ${syncResult.error || "ukendt fejl"}`, !syncResult.ok);
      await loadSummary();
    }
  }, source ? "Gem ændringer" : title);
}

function openBirthdayModal(birthday = null) {
  const body = `${field("Navn", "name", birthday?.name || "", "text", "required maxlength=120")}${field("Fødselsdato", "birth_date", birthday?.birth_date || "", "date", "required")}<label for="field-birthday-color">Farve</label><input id="field-birthday-color" name="color" type="color" value="${escapeHtml(birthday?.color || "#f59e0b")}">${field("Noter", "notes", birthday?.notes || "", "text", "maxlength=2000")}`;
  openModal(birthday ? "Rediger fødselsdag" : "Tilføj fødselsdag", body, async (values) => {
    const payload = { name: values.name, birth_date: values.birth_date, color: values.color, notes: values.notes || "" };
    await api(birthday ? `/api/birthdays/${birthday.id}` : "/api/birthdays", { method: birthday ? "PATCH" : "POST", body: payload });
    await loadSummary();
  }, birthday ? "Gem ændringer" : "Tilføj");
}

function openFrameModal(frame = null) {
  const body = `${field("Navn", "name", frame?.name || "", "text", "required maxlength=120")}${field("URL", "url", frame?.url || "", "url", "required maxlength=2000")}${field("Højde", "height", frame?.height || 320, "number", 'min="160" max="1400" step="1" required')}<label for="field-accent">Accentfarve</label><input id="field-accent" name="accent" type="color" value="${escapeHtml(frame?.accent || "#5c7cfa")}">`;
  openModal(frame ? "Rediger widget" : "Tilføj iframe", body, async (values) => {
    const payload = { name: values.name, url: values.url, height: Number(values.height), accent: values.accent, visible: true };
    await api(frame ? `/api/frames/${frame.id}` : "/api/frames", { method: frame ? "PATCH" : "POST", body: payload });
    await loadSummary();
  }, frame ? "Gem ændringer" : "Tilføj widget");
}

function openMemberModal(member = null) {
  const body = `${field("Navn", "name", member?.name || "", "text", "required maxlength=120")}<label for="field-member-color">Farve</label><input id="field-member-color" name="color" type="color" value="${escapeHtml(member?.color || "#5c7cfa")}">${field("Avatar URL (valgfrit)", "avatar_url", member?.avatar_url || "", "url", "maxlength=2000")}`;
  openModal(member ? "Rediger medlem" : "Tilføj medlem", body, async (values) => {
    const payload = { name: values.name, color: values.color, avatar_url: values.avatar_url || null };
    await api(member ? `/api/members/${member.id}` : "/api/members", { method: member ? "PATCH" : "POST", body: payload });
    await loadSummary();
  }, member ? "Gem ændringer" : "Tilføj medlem");
}

function detectLabel(type) {
  const labels = { person: "Person", vehicle: "Køretøj", car: "Bil", pet: "Kæledyr", animal: "Dyr", package: "Pakke" };
  return labels[String(type || "").toLowerCase()] || type || "Aktivitet";
}

function renderCameras() {
  const target = $("#camera-grid");
  if (!state.cameras.length) {
    target.innerHTML = `<div class="empty-state">Ingen kameraer endnu. Tryk på “Tilføj kamera” for at komme i gang.</div>`;
    $("#camera-activity-log").hidden = true;
    return;
  }
  target.innerHTML = state.cameras.map((camera) => `<article class="panel camera-item"><div class="camera-header"><div class="camera-name"><strong>${escapeHtml(camera.name)}</strong><span>${escapeHtml(camera.host)}</span></div><div class="camera-actions">${camera.live_stream_url ? `<button class="icon-button camera-live-btn" data-live-camera="${camera.id}" type="button" aria-label="Vis live-stream" title="Live-stream">▶</button>` : ""}<button class="icon-button" data-test-camera="${camera.id}" type="button" aria-label="Test forbindelse" title="Test">↻</button><button class="icon-button" data-edit-camera="${camera.id}" type="button" aria-label="Rediger kamera" title="Rediger">✎</button><button class="icon-button" data-delete-camera="${camera.id}" type="button" aria-label="Slet kamera" title="Slet">×</button></div></div><div class="camera-snapshot" data-camera-snapshot-box>${camera.live_stream_url ? `<span class="camera-live">LIVE</span>` : ""}<img data-cam-img data-cam-id="${camera.id}" src="/api/cameras/${camera.id}/snapshot?t=${Date.now()}" alt="Snapshot fra ${escapeHtml(camera.name)}" loading="lazy"></div></article>`).join("");
  $$("img[data-cam-img]", target).forEach((image) => {
    image.addEventListener("error", () => {
      const box = image.closest("[data-camera-snapshot-box]");
      state.cameraBroken.add(String(image.dataset.camId));
      image.style.display = "none";
      let overlay = box?.querySelector(".camera-overlay");
      if (box && !overlay) {
        overlay = document.createElement("div");
        overlay.className = "camera-overlay";
        overlay.textContent = "Kunne ikke vise billede – tjek forbindelse, og at kameraet er online";
        box.append(overlay);
      }
    });
    image.addEventListener("load", () => state.cameraBroken.delete(String(image.dataset.camId)));
  });
  updateLiveButtons();
}

function updateLiveButtons() {
  $$("button[data-live-camera]").forEach((button) => {
    const id = String(button.dataset.liveCamera);
    button.classList.toggle("active", state.cameraLive.has(id));
    button.textContent = state.cameraLive.has(id) ? "■" : "▶";
    button.title = state.cameraLive.has(id) ? "Stop live-stream" : "Vis live-stream";
  });
}

function toggleCameraLive(cameraId) {
  const id = String(cameraId);
  const image = document.querySelector(`img[data-cam-id="${id}"]`);
  if (!image) return;
  image.style.display = "";
  image.closest("[data-camera-snapshot-box]")?.querySelector(".camera-overlay")?.remove();
  state.cameraBroken.delete(id);
  if (state.cameraLive.has(id)) {
    state.cameraLive.delete(id);
    image.src = `/api/cameras/${id}/snapshot?t=${Date.now()}`;
  } else {
    state.cameraLive.add(id);
    image.src = `/api/cameras/${id}/stream?max_seconds=300&t=${Date.now()}`;
  }
  updateLiveButtons();
}

function refreshCameraSnapshots() {
  $$("#camera-grid img[data-cam-img]").forEach((image) => {
    const id = String(image.dataset.camId);
    if (state.cameraBroken.has(id)) return;
    if (state.cameraLive.has(id)) return;
    image.src = `/api/cameras/${id}/snapshot?t=${Date.now()}`;
  });
}

function renderActivity(activity) {
  const recent = activity.recent || [];
  const panel = $("#camera-activity-log");
  const list = $("#camera-activity-list");
  if (!recent.length) { panel.hidden = true; return; }
  panel.hidden = false;
  const activeIds = new Set((activity.active || []).map((camera) => String(camera.id)));
  list.innerHTML = recent.map((entry) => {
    const ongoing = !entry.ended_at;
    const when = ongoing ? `Startet ${formatTime(entry.started_at)}` : `${formatTime(entry.started_at)} – ${formatTime(entry.ended_at)}`;
    return `<div class="activity-item${activeIds.has(String(entry.camera_id)) ? " active" : ""}"><span>${escapeHtml(entry.camera_name || "Kamera")}</span><span class="activity-type">${escapeHtml(detectLabel(entry.detection_type))}</span><span class="activity-time">${escapeHtml(when)}</span></div>`;
  }).join("");
}

function handleDetections(activity) {
  if (state.detectTimer) window.clearTimeout(state.detectTimer);
  state.detectTimer = null;
  const popup = $("#detect-popup");
  const active = activity.active || [];
  if (!active.length) {
    popup.hidden = true;
    popup.innerHTML = "";
    state.detectKey = null;
    return;
  }
  const key = active.map((camera) => `${camera.id}:${camera.since}`).join("|");
  if (state.detectKey !== key) {
    state.detectKey = key;
    popup.innerHTML = active.map((camera) => {
      const types = (camera.types || []).map((item) => detectLabel(item.label || item.type)).join(" og ") || "Aktivitet";
      const configured = state.cameras.find((item) => String(item.id) === String(camera.id));
      const live = (configured?.live_stream_url || "").trim();
      const src = live ? `/api/cameras/${camera.id}/stream?max_seconds=300&t=${Date.now()}` : `/api/cameras/${camera.id}/snapshot?t=${Date.now()}`;
      return `<div class="detect-card"><div class="detect-head"><h2>${escapeHtml(camera.name)}</h2><span class="detect-types">${escapeHtml(types)}</span></div>${live ? `<span class="camera-live detect-live">LIVE</span>` : ""}<img src="${src}" alt="Kamerabillede"><div class="detect-foot"><span>Detekteret ${escapeHtml(formatTime(camera.since))}</span><span>Lukker automatisk</span></div></div>`;
    }).join("");
  }
  popup.hidden = false;
  const closeDelay = Number(activity.close_delay || 0);
  if (closeDelay > 0) state.detectTimer = window.setTimeout(() => { const root = $("#detect-popup"); root.hidden = true; }, closeDelay * 1000);
}

async function pollCameras() {
  try {
    const activity = await api("/api/cameras/activity");
    renderActivity(activity);
    handleDetections(activity);
    const interval = Math.max(2, Number(activity.poll_seconds) || 5);
    if (state.cameraTimer) window.clearInterval(state.cameraTimer);
    state.cameraTimer = window.setInterval(() => { refreshCameraSnapshots(); pollCameras(); }, interval * 1000);
  } catch (error) {
    if (state.cameraTimer) window.clearInterval(state.cameraTimer);
    state.cameraTimer = null;
  }
}

async function loadCameras(silent = true) {
  try {
    const result = await api("/api/cameras");
    state.cameras = result.cameras || [];
    renderCameras();
    if (!state.cameraTimer) pollCameras();
  } catch (error) {
    if (!silent) showToast(error.message, true);
  }
}

async function deleteCamera(cameraId) {
  if (!window.confirm("Vil du slette dette kamera?")) return;
  try {
    await api(`/api/cameras/${cameraId}`, { method: "DELETE" });
    state.cameraBroken.delete(String(cameraId));
    await loadCameras();
    showToast("Kameraet er slettet");
  } catch (error) {
    showToast(error.message, true);
  }
}

function splitCameraHost(url) {
  try {
    const parsed = new URL(url);
    const scheme = parsed.protocol.replace(":", "") || "http";
    const port = parsed.port ? Number(parsed.port) : (scheme === "https" ? 443 : 80);
    return { scheme, host: parsed.hostname, port };
  } catch {
    return { scheme: "http", host: url || "", port: 80 };
  }
}

function openCameraModal(camera = null) {
  const title = camera ? "Rediger kamera" : "Tilføj kamera";
  const pieces = splitCameraHost(camera?.host);
  const protocol = `<label for="field-scheme">Protokol</label><select id="field-scheme" name="scheme"><option value="http" ${pieces.scheme !== "https" ? "selected" : ""}>HTTP</option><option value="https" ${pieces.scheme === "https" ? "selected" : ""}>HTTPS</option></select>`;
  const passwordExtra = camera ? " placeholder=\"Uændret hvis tomt\"" : "";
  const body = `${field("Navn", "name", camera?.name || "", "text", "required maxlength=120")}${field("IP-adresse eller hostnavn", "host", pieces.host, "text", "required maxlength=250 placeholder='192.168.1.219'")}<div class="form-row"><div>${protocol}</div><div><label for="field-port">Web-port</label><input id="field-port" name="port" type="number" value="${escapeHtml(pieces.port)}" min="1" max="65535" step="1" required></div></div><p class="muted small-copy">Web-porten bruges til kameraets API (snapshots + AI-tilstand). Reolink-standarder: HTTP 80, HTTPS 443 – nyere NVR'er accepterer ofte kun HTTPS. Prøv HTTP 80 først, derefter 443. Kanal 0 er den første stream.</p>${field("Brugernavn", "username", camera?.username || "", "text", "maxlength=320")}${field("Password", "password", "", "password", `maxlength=200${passwordExtra}`)}${field("Kanal (0–31)", "channel", camera?.channel ?? 0, "number", "min=0 max=31 step=1")}${field("Live-stream URL (RTSP)", "live_stream_url", camera?.live_stream_url || "", "text", "maxlength=2000 placeholder='rtsp://brugernavn:kode@192.168.1.174:554/h264Preview_01_main'")}<p class="muted small-copy">Live-streamen bruges til video i aktivitets-popuppen og i “▶” på kameraet. Dashboardet konverterer RTSP automatisk via ffmpeg, så det kan vises i browseren. Brug kameraets RTSP-adresse, fx <code>rtsp://brugernavn:kode@IP:554/h264Preview_01_main</code> (mainstream) eller <code>…/h264Preview_01_sub</code> (let sub-stream). Lad feltet stå tomt, hvis du ikke vil streame.</p><div class="form-row"><label class="check-label"><input type="checkbox" name="person_enabled" ${camera?.person_enabled !== false ? "checked" : ""}> Person-alarm</label><label class="check-label"><input type="checkbox" name="vehicle_enabled" ${camera?.vehicle_enabled !== false ? "checked" : ""}> Køretøj-alarm</label><label class="check-label"><input type="checkbox" name="snapshots_enabled" ${camera?.snapshots_enabled !== false ? "checked" : ""}> Snapshots</label></div>`;
  openModal(title, body, async (values) => {
    const payload = {
      name: values.name,
      host: `${values.scheme}://${values.host.trim()}:${Number(values.port) || (values.scheme === "https" ? 443 : 80)}`,
      username: (values.username || "").trim(),
      password: values.password || "",
      has_password: Boolean(state.cameras.find((item) => item.id === camera?.id)?.has_password),
      channel: Number(values.channel || 0),
      live_stream_url: (values.live_stream_url || "").trim(),
      person_enabled: values.person_enabled === "on",
      vehicle_enabled: values.vehicle_enabled === "on",
      snapshots_enabled: values.snapshots_enabled === "on",
    };
    await api(camera ? `/api/cameras/${camera.id}` : "/api/cameras", { method: camera ? "PATCH" : "POST", body: payload });
    showToast(camera ? "Kameraet er opdateret" : "Kameraet er tilføjet");
    await loadCameras();
  }, title);
}

async function testCamera(cameraId) {
  if (!state.summary) return;
  try {
    const result = await api(`/api/cameras/${cameraId}/test`, { method: "POST" });
    const lines = result.message ? String(result.message).split("|").filter(Boolean) : [];
    showToast(result.ok ? (lines[0] || "Kameraet svarer") : (result.message || "Test fejlede"), !result.ok);
  } catch (error) {
    showToast(error.message, true);
  }
}

function openIcloudNoteModal() {
  const note = state.icloudNote;
  const body = `<label for="field-note-content">Indhold</label><textarea id="field-note-content" name="content" maxlength="10000" placeholder="Skriv her…">${escapeHtml(note?.content || "")}</textarea>`;
  openModal("Rediger iCloud-note", body, async (values) => {
    await api("/api/notes/icloud/save", { method: "POST", body: { content: values.content || "" } });
    await loadIcloudNote();
    await loadSummary(true);
    showToast("Noten er gemt til iCloud");
  }, "Gem note");
}

async function loadIcloudNote(silent = true) {
  try {
    state.icloudNote = await api("/api/notes/icloud");
    renderIcloudNote();
  } catch (error) {
    if (!silent) showToast(error.message, true);
  }
}

function openChecklistItemModal(item) {
  const body = `${field("Opgave", "text", item?.text || "", "text", "required maxlength=500")}`;
  openModal("Rediger opgave", body, async (values) => {
    await api(`/api/checklist/${item.id}`, { method: "PATCH", body: { text: values.text } });
    await loadSummary(true);
    showToast("Opgaven er opdateret");
  }, "Gem");
}

async function deleteChecklistItem(itemId, icloud) {
  if (!window.confirm(icloud ? "Slet opgaven fra den delte iCloud-liste?" : "Vil du slette denne opgave?")) return;
  try {
    await api(`/api/checklist/${itemId}`, { method: "DELETE" });
    await loadSummary(true);
    showToast("Opgaven er slettet");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function synchroniseSingle(id) {
  try {
    const result = await api(`/api/calendars/${id}/sync`, { method: "POST" });
    showToast(result.ok ? "Kalenderen er synkroniseret" : `Synkronisering fejlede: ${result.error || "ukendt fejl"}`, !result.ok);
    await loadSummary();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function deleteResource(path, message) {
  if (!window.confirm(message)) return;
  try {
    await api(path, { method: "DELETE" });
    await loadSummary();
    showToast("Slettet");
  } catch (error) {
    showToast(error.message, true);
  }
}

function findResource(type, id) {
  const list = type === "source" ? state.summary.sources : type === "birthday" ? state.summary.birthdays : type === "frame" ? state.summary.frames : state.summary.members;
  return list?.find((item) => String(item.id) === String(id));
}

function showView(view) {
  const available = $$("[data-view-panel]");
  const target = available.find((panel) => panel.dataset.viewPanel === view) ? view : "today";
  state.activeView = target;
  localStorage.setItem("fd-view", target);
  $$(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === target));
  available.forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === target));
  if (target === "calendar") loadCalendarEvents();
  if (target === "school") loadSchoolEvents();
  if (target === "cameras") loadCameras(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadSummary(silent = false) {
  try {
    state.summary = await api("/api/dashboard/summary?days=14");
    renderSummary();
    showApp();
    setConnection(true);
    if (state.activeView === "calendar") loadCalendarEvents();
    if (state.activeView === "school") loadSchoolEvents();
    if (!silent) showToast("Dashboard opdateret");
  } catch (error) {
    setConnection(false, "Kunne ikke opdatere");
    if (!silent) showToast(error.message, true);
  }
}

async function loadCalendarEvents() {
  if (!state.summary) return;
  const cursor = state.calendarCursor;
  const start = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  const end = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0);
  try {
    const result = await api(`/api/events?start=${localDateKey(start)}&end=${localDateKey(end)}&kind=calendar`);
    state.calendarEvents = result.events || [];
    renderCalendar();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadSchoolEvents() {
  if (!state.summary) return;
  const start = state.schoolWeekStart;
  const end = new Date(start);
  end.setDate(end.getDate() + 4);
  try {
    const result = await api(`/api/events?start=${localDateKey(start)}&end=${localDateKey(end)}&kind=school`);
    state.schoolEvents = result.events || [];
    renderSchoolSchedule();
  } catch (error) {
    showToast(error.message, true);
  }
}

function schoolEventItem(event) {
  return `<article class="school-event" style="border-color:${escapeHtml(event.source_color || "#4fd1a1")}"><strong>${escapeHtml(event.title)}</strong><span>${escapeHtml(formatEventTime(event))}</span>${event.location ? `<small>${escapeHtml(event.location)}</small>` : ""}</article>`;
}

function renderSchoolSchedule() {
  const start = state.schoolWeekStart;
  const end = new Date(start);
  end.setDate(end.getDate() + 4);
  const sources = (state.summary?.sources || []).filter((source) => source.kind === "school");
  const target = $("#school-source-list");
  $("#school-range-title").textContent = `${formatShortDate(start)} – ${formatShortDate(end)}`;
  $("#school-event-count").textContent = `${state.schoolEvents.length} ${state.schoolEvents.length === 1 ? "time" : "timer"}`;
  if (!sources.length) {
    target.innerHTML = `<div class="panel empty-state">Ingen skoleskemaer endnu. Tilføj hvert barns Aula- eller skoleabonnement separat.</div>`;
    return;
  }
  const weekdays = Array.from({ length: 5 }, (_, index) => {
    const day = new Date(start);
    day.setDate(day.getDate() + index);
    return day;
  });
  target.innerHTML = sources.map((source) => {
    const sourceEvents = state.schoolEvents.filter((event) => String(event.source_id) === String(source.id));
    const days = weekdays.map((day) => {
      const key = localDateKey(day);
      const events = sourceEvents.filter((event) => eventDateKey(event) === key);
      return `<section class="school-day${key === (state.summary?.date || localDateKey(new Date())) ? " today" : ""}"><header><span>${escapeHtml(formatDate(day, { weekday: "short" }))}</span><strong>${escapeHtml(formatDate(day, { day: "numeric", month: "short" }))}</strong></header><div class="school-event-list">${events.length ? events.map(schoolEventItem).join("") : `<span class="school-empty">Ingen timer</span>`}</div></section>`;
    }).join("");
    return `<article class="panel school-source-card${source.enabled ? "" : " disabled"}" style="--source-color:${escapeHtml(source.color || "#4fd1a1")}"><div class="school-source-header"><div><p class="eyebrow">${source.enabled ? "Skoleskema" : "Pauseret skoleskema"}</p><h2>${escapeHtml(source.name)}</h2><span>${sourceEvents.length} ${sourceEvents.length === 1 ? "time denne uge" : "timer denne uge"}</span></div>${sourceActions(source)}</div><div class="school-week-scroll"><div class="school-week-grid">${days}</div></div></article>`;
  }).join("");
}

function renderCalendar() {
  const cursor = state.calendarCursor;
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - mondayOffset);
  const grid = $("#calendar-grid");
  const todayKey = state.summary?.date || localDateKey(new Date());
  const selectedKey = localDateKey(state.selectedDate);
  const showSourceNames = new Set(state.calendarEvents.map((event) => event.source_id)).size > 1;
  grid.innerHTML = "";
  for (let index = 0; index < 42; index += 1) {
    const day = new Date(start);
    day.setDate(start.getDate() + index);
    const key = localDateKey(day);
    const events = state.calendarEvents.filter((event) => eventDateKey(event) === key);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `calendar-day${day.getMonth() !== cursor.getMonth() ? " outside" : ""}${key === todayKey ? " today" : ""}${key === selectedKey ? " selected" : ""}`;
    button.dataset.date = key;
    button.innerHTML = `<span class="day-number">${day.getDate()}</span>${events.slice(0, 2).map((event) => `<span class="day-event-label" title="${escapeHtml(`${event.source_name || "Kalender"}: ${event.title}`)}">${escapeHtml(`${showSourceNames ? `${event.source_name || "Kalender"}: ` : ""}${event.title}`)}</span>`).join("")}<span class="day-dots">${events.slice(0, 4).map((event) => `<i class="day-dot" style="background:${escapeHtml(event.source_color || "#5c7cfa")}"></i>`).join("")}</span>`;
    grid.append(button);
  }
  $("#calendar-range-title").textContent = formatDate(cursor, { month: "long", year: "numeric" });
  $("#calendar-count").textContent = `${state.calendarEvents.length} aftaler`;
  renderAgenda();
}

function renderAgenda() {
  const key = localDateKey(state.selectedDate);
  const events = state.calendarEvents.filter((event) => eventDateKey(event) === key);
  const groups = groupEventsBySource(events);
  $("#agenda-date").textContent = formatDate(state.selectedDate, { weekday: "long", day: "numeric", month: "long" });
  $("#agenda-list").innerHTML = groups.length ? groups.map((group) => `<section class="agenda-group"><h3><i style="background:${escapeHtml(group.color)}"></i><span class="agenda-source-name">${escapeHtml(group.name)}</span><span class="agenda-count">${group.events.length}</span></h3><div class="event-list">${group.events.map((event) => eventItem(event)).join("")}</div></section>`).join("") : `<div class="empty-state">Ingen aftaler denne dato.</div>`;
}

function selectCalendarWeek(offset) {
  const next = new Date(state.selectedDate);
  next.setDate(next.getDate() + offset * 7);
  state.selectedDate = next;
  state.calendarCursor = new Date(next.getFullYear(), next.getMonth(), 1);
  loadCalendarEvents();
}

function selectSchoolWeek(offset) {
  const next = new Date(state.schoolWeekStart);
  next.setDate(next.getDate() + offset * 7);
  state.schoolWeekStart = next;
  loadSchoolEvents();
}

function bindEvents() {
  const saveLogLevel = () => {
    const select = $("#setting-log-level");
    if (!select) return;
    api("/api/settings", { method: "PATCH", body: { log_level: select.value } })
      .then(() => showToast("Log-indstillinger gemt"))
      .catch((error) => showToast(error.message, true));
  };
  document.addEventListener("click", (event) => {
    if (event.target && event.target.id === "logging-save") saveLogLevel();
  });
  document.addEventListener("submit", (event) => {
    if (!event.target || event.target.id !== "logging-form") return;
    event.preventDefault();
    saveLogLevel();
  });
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = $("#login-error");
    const button = $("button[type=submit]", event.currentTarget);
    error.textContent = "";
    button.disabled = true;
    try {
      await api("/api/auth/login", { method: "POST", body: { password: $("#login-password").value } });
      $("#login-password").value = "";
      await loadSummary();
    } catch (requestError) {
      error.textContent = requestError.message === "Wrong password" ? "Forkert adgangskode" : requestError.message;
    } finally {
      button.disabled = false;
    }
  });
  $$(".nav-button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  $$("[data-view-link]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewLink)));
  $("#refresh-button").addEventListener("click", () => loadSummary());
  $("#settings-shortcut").addEventListener("click", () => showView("settings"));
  $("#logout-button").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); showLogin(); });
  $("#add-calendar-button").addEventListener("click", () => openCalendarModal(null, "calendar"));
  $("#add-school-button").addEventListener("click", () => openCalendarModal(null, "school"));
  $("#add-birthday-button").addEventListener("click", () => openBirthdayModal());
  $("#add-frame-button").addEventListener("click", () => openFrameModal());
  $("#add-member-button").addEventListener("click", () => openMemberModal());
  $("#settings-add-calendar").addEventListener("click", () => openCalendarModal(null, "calendar"));
  $("#settings-add-school").addEventListener("click", () => openCalendarModal(null, "school"));
  $("#settings-add-birthday").addEventListener("click", () => openBirthdayModal());
  $("#settings-add-frame").addEventListener("click", () => openFrameModal());
  $("#add-camera-button").addEventListener("click", () => openCameraModal());
  $("#settings-add-camera").addEventListener("click", () => openCameraModal());
  $("#cameras-refresh").addEventListener("click", async () => { await loadCameras(false); refreshCameraSnapshots(); showToast("Kameraer opdateret"); });
  $("#icloud-note-edit").addEventListener("click", () => openIcloudNoteModal());
  $("#icloud-reminders-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    delete values.reminders_list_href;
    delete values.reminders_list_name;
    const select = $("#setting-reminders-list");
    const selected = select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
    if (selected && selected.value) {
      values.reminders_list_href = selected.value;
      values.reminders_list_name = selected.dataset.name || "";
    }
    if (!values.reminders_app_password) delete values.reminders_app_password;
    if (!values.reminders_bridge_token) delete values.reminders_bridge_token;
    if (values.reminders_source === "caldav") {
      delete values.reminders_bridge_url;
      delete values.reminders_bridge_token;
    } else {
      delete values.reminders_username;
      delete values.reminders_app_password;
    }
    values.reminders_enabled = values.reminders_enabled === "on";
    values.reminders_sync_minutes = Number(values.reminders_sync_minutes) || undefined;
    try { await api("/api/settings", { method: "PATCH", body: values }); await loadSummary(true); showToast("Påmindelser gemt"); } catch (error) { showToast(error.message, true); }
  });
  $("#reminders-fetch-lists").addEventListener("click", async () => {
    const button = $("#reminders-fetch-lists");
    const help = $("#reminders-help");
    button.disabled = true;
    // Backendkaldet læser gemte indstillinger, så uændrede felter skal gemmes først.
    const form = $("#icloud-reminders-form");
    const typedSource = form.elements.reminders_source.value === "caldav" ? "caldav" : "bridge";
    const typedUrl = (form.elements.reminders_bridge_url.value || "").trim();
    const saved = state.settings || {};
    const hasToken = (form.elements.reminders_bridge_token.value || "").trim() || saved.reminders_bridge_configured;
    const dirty =
      typedSource !== (saved.reminders_source === "caldav" ? "caldav" : "bridge") ||
      (typedSource === "bridge" && typedUrl && typedUrl !== (saved.reminders_bridge_url || "")) ||
      (typedSource === "bridge" && !saved.reminders_bridge_configured && !hasToken) ||
      (typedSource === "caldav" && !saved.reminders_configured);
    if (dirty) {
      help.innerHTML = "Gem først de nye påmindelsesindstillinger, og hent så lister igen.";
      showToast("Gem indstillingerne først", true);
      button.disabled = false;
      return;
    }
    try {
      const result = await api("/api/reminders/lists");
      if (result.error) {
        const source = state.settings?.reminders_source === "caldav" ? "caldav" : "bridge";
        const hint = source === "caldav"
          ? "Tjek Apple-id og app-specifikt password (oprettet på appleid.apple.com med to-faktor-login slået til), og kopiér passwordet præcist uden mellemrum."
          : "Tjek at FamilyBridge kører på Mac mini'en, at IP-adresse og port (8787) er korrekte, og at tokenet er kopieret fra FamilyBridge --token. Godkend også Påmindelser under Systemindstillinger > Anonymitet og sikkerhed > Påmindelser.";
        help.innerHTML = `Fejl: <span class="error-text">${escapeHtml(result.error)}</span><br>${hint}`;
        showToast(`Kunne ikke hente lister: ${result.error}`, true);
        state.reminderLists = [];
      } else {
        state.reminderLists = result.lists || [];
        help.textContent = state.reminderLists.length ? `${state.reminderLists.length} påmindelseslister fundet – vælg en og gem.` : "Ingen påmindelseslister fundet på kontoen.";
        showToast(state.reminderLists.length ? `${state.reminderLists.length} lister fundet` : "Ingen lister fundet", !state.reminderLists.length);
      }
      populateReminderListSelect(state.settings.reminders_list_href, state.settings.reminders_list_name);
    } catch (error) {
      help.innerHTML = `Fejl: <span class="error-text">${escapeHtml(error.message)}</span>`;
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#reminders-sync-now").addEventListener("click", async () => {
    const source = state.settings?.reminders_source === "caldav" ? "caldav" : "bridge";
    const hint = source === "caldav" ? "fyld Apple-id, password og vælg en liste" : "indtast Mac'ens adresse og token, og vælg en liste";
    try {
      const result = await api("/api/reminders/sync", { method: "POST" });
      showToast(result.synced ? `Checklisten er synkroniseret (${result.count || 0} opgaver)` : (result.reason === "disabled" ? `Synk deaktiveret – ${hint} under Indstillinger` : `Synk fejlede: ${result.error || "ukendt fejl"}`), !result.synced);
      await loadSummary(true);
    } catch (error) { showToast(error.message, true); }
  });
  $("#reminders-test").addEventListener("click", async () => {
    const button = $("#reminders-test");
    button.disabled = true;
    try {
      const result = await api("/api/reminders/test", { method: "POST" });
      $("#reminders-help").innerHTML = result.ok
        ? `<span class="ok-text">${escapeHtml(result.message)}</span>`
        : `Forbindelse fejlede: <span class="error-text">${escapeHtml(result.message)}</span>`;
      showToast(result.ok ? "Forbindelsen virker" : result.message, !result.ok);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#setting-reminders-source").addEventListener("change", (event) => {
    toggleRemindersSourceFields(event.target.value);
    state.reminderLists = [];
    populateReminderListSelect();
  });
  $("#setting-notes-source").addEventListener("change", (event) => {
    const fields = $("#notes-imap-fields");
    if (fields) fields.hidden = event.target.value !== "imap";
  });
  $("#icloud-notes-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    if (!values.notes_imap_app_password) delete values.notes_imap_app_password;
    if (values.notes_source === "bridge") {
      delete values.notes_imap_username;
      delete values.notes_imap_app_password;
      delete values.notes_imap_host;
    }
    values.notes_imap_enabled = values.notes_imap_enabled === "on";
    try {
      await api("/api/settings", { method: "PATCH", body: values });
      await loadSummary(true);
      await loadIcloudNote();
      const note = state.icloudNote;
      if (note?.error && note.error !== "disabled") $("#notes-help").innerHTML = `Sidste fejl: <span class="error-text">${escapeHtml(note.error)}</span>`;
      showToast(note?.error && note.error !== "disabled" ? "Indstillinger gemt – men noten kunne ikke hentes" : "Note-indstillinger gemt");
    } catch (error) { showToast(error.message, true); }
  });
  $("#notes-fetch-now").addEventListener("click", async () => {
    try {
      await loadIcloudNote(false);
      const note = state.icloudNote;
      if (note?.error && note.error !== "disabled") {
        $("#notes-help").innerHTML = `Sidste fejl: <span class="error-text">${escapeHtml(note.error)}</span>`;
        showToast(note.error, true);
      } else if (note?.content) {
        showToast("Noten er hentet");
      } else if (note?.found === false) {
        $("#notes-help").innerHTML = `Noten “${escapeHtml(note.title || "")}” blev ikke fundet. <span class="debug-copy">${escapeHtml(note.debug || "")}</span>`;
        showToast("Noten blev ikke fundet – se detaljer under noten", true);
      } else {
        showToast("Noten er tom eller ikke fundet", true);
      }
    } catch (error) { showToast(error.message, true); }
  });
  $("#reolink-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    values.reolink_poll_seconds = Number(values.reolink_poll_seconds) || undefined;
    values.reolink_close_delay = Number(values.reolink_close_delay) || 0;
    try { await api("/api/settings", { method: "PATCH", body: values }); await loadSummary(true); await loadCameras(); showToast("Kamera-indstillinger gemt"); } catch (error) { showToast(error.message, true); }
  });
  $("#copy-url-button").addEventListener("click", async () => { await navigator.clipboard?.writeText(window.location.origin); showToast("Adresse kopieret"); });
  $("#quick-checklist-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = $("#quick-checklist-input");
    if (!input.value.trim()) return;
    try { await api("/api/checklist", { method: "POST", body: { text: input.value.trim() } }); input.value = ""; await loadSummary(true); } catch (error) { showToast(error.message, true); }
  });
  $("#today-checklist").addEventListener("change", async (event) => {
    const id = event.target.dataset.checkId;
    if (!id) return;
    try { await api(`/api/checklist/${id}`, { method: "PATCH", body: { done: event.target.checked } }); await loadSummary(true); } catch (error) { showToast(error.message, true); }
  });
  $("#settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form).entries());
    try { await api("/api/settings", { method: "PATCH", body: values }); await loadSummary(true); showToast("Indstillinger gemt"); } catch (error) { showToast(error.message, true); }
  });
  $("#calendar-prev-month").addEventListener("click", () => { state.calendarCursor = new Date(state.calendarCursor.getFullYear(), state.calendarCursor.getMonth() - 1, 1); loadCalendarEvents(); });
  $("#calendar-next-month").addEventListener("click", () => { state.calendarCursor = new Date(state.calendarCursor.getFullYear(), state.calendarCursor.getMonth() + 1, 1); loadCalendarEvents(); });
  $("#calendar-prev").addEventListener("click", () => selectCalendarWeek(-1));
  $("#calendar-next").addEventListener("click", () => selectCalendarWeek(1));
  $("#calendar-today").addEventListener("click", () => { state.selectedDate = new Date(); state.calendarCursor = new Date(new Date().getFullYear(), new Date().getMonth(), 1); loadCalendarEvents(); });
  $("#school-prev").addEventListener("click", () => selectSchoolWeek(-1));
  $("#school-next").addEventListener("click", () => selectSchoolWeek(1));
  $("#school-today").addEventListener("click", () => { state.schoolWeekStart = startOfWeek(new Date()); loadSchoolEvents(); });
  $("#calendar-grid").addEventListener("click", (event) => { const day = event.target.closest("[data-date]"); if (day) { state.selectedDate = parseDateKey(day.dataset.date); renderCalendar(); } });
  document.addEventListener("click", async (event) => {
    const target = event.target.closest("button, [data-view-link]");
    if (!target) return;
    if (target.dataset.viewLink) { showView(target.dataset.viewLink); return; }
    if (target.dataset.editSource) openCalendarModal(findResource("source", target.dataset.editSource));
    if (target.dataset.deleteSource) deleteResource(`/api/calendars/${target.dataset.deleteSource}`, " Vil du fjerne denne kalender?");
    if (target.dataset.syncSource) synchroniseSingle(target.dataset.syncSource);
    if (target.dataset.editBirthday) openBirthdayModal(findResource("birthday", target.dataset.editBirthday));
    if (target.dataset.deleteBirthday) deleteResource(`/api/birthdays/${target.dataset.deleteBirthday}`, "Vil du slette denne fødselsdag?");
    if (target.dataset.editFrame) openFrameModal(findResource("frame", target.dataset.editFrame));
    if (target.dataset.deleteFrame) deleteResource(`/api/frames/${target.dataset.deleteFrame}`, "Vil du slette denne widget?");
    if (target.dataset.editMember) openMemberModal(findResource("member", target.dataset.editMember));
    if (target.dataset.deleteMember) deleteResource(`/api/members/${target.dataset.deleteMember}`, "Vil du slette dette familiemedlem?");
    if (target.dataset.editCamera) openCameraModal(state.cameras.find((camera) => String(camera.id) === String(target.dataset.editCamera)));
    if (target.dataset.deleteCamera) deleteCamera(target.dataset.deleteCamera);
    if (target.dataset.testCamera) testCamera(target.dataset.testCamera);
    if (target.dataset.liveCamera) toggleCameraLive(target.dataset.liveCamera);
    if (target.dataset.editCheck) openChecklistItemModal(state.summary.checklist.find((item) => String(item.id) === String(target.dataset.editCheck)));
    if (target.dataset.deleteCheck) deleteChecklistItem(target.dataset.deleteCheck, state.summary.checklist.find((item) => String(item.id) === String(target.dataset.deleteCheck))?.source === "icloud");
  });
  modalRoot.addEventListener("click", (event) => { if (event.target === modalRoot || event.target.closest("[data-close-modal]")) closeModal(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !modalRoot.hidden) closeModal(); });
  window.addEventListener("online", () => loadSummary(true));
  window.addEventListener("offline", () => setConnection(false, "Offline"));
}

async function init() {
  bindEvents();
  showView(state.activeView);
  try {
    const status = await api("/api/auth/status");
    if (!status.authenticated) { showLogin(); return; }
    await loadSummary();
  } catch (error) {
    showLogin();
  }
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
}

init();
