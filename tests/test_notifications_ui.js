"use strict";

// Exercise the browser controller without a Windows session or real notifications.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static/index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "static/app.js"), "utf8");

class Element {
  constructor(ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.open = false;
    this.isConnected = true;
    this.modalShows = 0;
    this.modalCloses = 0;
    this.selectors = new Map();
    const classes = new Set();
    this.classList = {
      add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)),
      toggle: (item, force) => {
        const add = force === undefined ? !classes.has(item) : force;
        if (add) classes.add(item); else classes.delete(item);
      },
      contains: (item) => classes.has(item),
    };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...children) { children.forEach((child) => { if (child && typeof child === "object") child.parentElement = this; }); this.children.push(...children); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  get lastChild() { if (!this.children.length) this.append(new Element(this.ownerDocument)); return this.children.at(-1); }
  get lastElementChild() { return this.lastChild; }
  querySelector(selector) {
    if (!this.selectors.has(selector)) this.selectors.set(selector, new Element(this.ownerDocument));
    return this.selectors.get(selector);
  }
  querySelectorAll() { return this.fields || []; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  reportValidity() { return true; }
  scrollIntoView() {}
  focus() { this.focused = true; if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  click() { this.clicked = true; }
  remove() { this.removed = true; this.isConnected = false; }
  contains(element) { return element === this || this.children.some((child) => child?.contains?.(element)); }
  showModal() { this.open = true; this.modalShows += 1; }
  close() { if (!this.open) return; this.open = false; this.modalCloses += 1; this.emit("close"); }
  emit(name, details = {}) {
    const event = { defaultPrevented: false, target: this, currentTarget: this,
      preventDefault() { this.defaultPrevented = true; }, ...details };
    const result = this.listeners[name]?.(event);
    if (name === "cancel" && !event.defaultPrevented) this.close();
    return result;
  }
}

function liveSample(changes = {}, at = Date.now()) {
  return { timestamp: new Date(at).toISOString(), input_voltage: 220, output_voltage: 220,
    frequency_hz: 59.9, load_percent: 8, battery_voltage: 27.4, battery_percent: null,
    low_battery: false, fault: false, on_battery: false, raw_status: "Line mode", ...changes };
}

const textOf = (element) => [element?.textContent || "", ...(element?.children || []).map(textOf)].join(" ");

function snapshot(settings = {}) {
  return {
    csrf_token: "test-token",
    connection: { state: "connected" }, sample: liveSample(), events: [],
    meta: { source: "native" },
    policy: { enabled: false, on_battery_seconds: 300, countdown_seconds: 60, action: "shutdown" },
    protection: { state: "idle" },
    notifications: {
      settings: { enabled: true, sound_enabled: true, repeat_seconds: 30, ...settings },
      delivery: { available: true, ready: true, channel: "windows", last_error: null, last_delivered_at: null },
      last_error: null,
    },
  };
}

function harness(historyOptions = {}) {
  let documentRef;
  let now = historyOptions.now ?? Date.now();
  class TestDate extends Date {
    constructor(...values) { super(...(values.length ? values : [now])); }
    static now() { return now; }
  }
  const elements = new Map([...html.matchAll(/<[a-z][^>]*\bid="([^"]+)"[^>]*>/gi)].map((match) => {
    const element = new Element();
    element.tagName = match[0].match(/^<([a-z]+)/i)[1].toUpperCase();
    for (const attribute of match[0].matchAll(/\s([\w:-]+)(?:="([^"]*)")?/g)) {
      const [, name, value = ""] = attribute;
      element.setAttribute(name, value);
      if (name.startsWith("data-")) element.dataset[name.slice(5).replace(/-([a-z])/g, (_all, letter) => letter.toUpperCase())] = value;
      if (["hidden", "disabled", "open"].includes(name)) element[name] = true;
      if (name === "class") { element.className = value; element.classList.add(...value.split(/\s+/)); }
      if (name === "value") element.value = value;
    }
    return [match[1], element];
  }));
  const get = (id) => {
    assert.ok(elements.has(id), `HTML must include #${id}`);
    return elements.get(id);
  };
  const policyFields = ["policy-enabled", "policy-on-battery", "policy-low-battery", "policy-runtime", "policy-countdown", "policy-action"];
  get("policy-form").fields = policyFields.map(get);
  get("notifications-form").fields = ["notifications-enabled", "notifications-sound", "notifications-repeat", "notifications-persistent"].map(get);
  const selectors = new Map();
  const intervals = [];
  const timeouts = new Map();
  let timeoutId = 0;
  const calls = [];
  const downloads = [];
  const body = new Element();
  const rootElement = new Element();
  const documentListeners = {};
  let domReady = !historyOptions.loading;
  const storedPreferences = new Map(Object.entries(historyOptions.storage || {}));
  const storageWrites = [];
  const historyCalls = [];
  const pendingHistory = [];
  const views = ["overview", "incidents", "history", "protection", "notifications", "system"];
  const nav = historyOptions.navigation ? views.map((view) => Object.assign(new Element(), { dataset: { view } })) : [];
  const pages = historyOptions.navigation ? views.map((view) => Object.assign(get(`page-${view}`), { dataset: { page: view } })) : [];
  const shortcuts = historyOptions.navigation ? views.map((view) => Object.assign(new Element(), { dataset: { goView: view } })) : [];
  const metrics = ["voltage", "battery", "load"].map((metric) => Object.assign(new Element(), { dataset: { metric } }));
  const windowListeners = {};
  const navigationScrolls = [];
  const location = { hash: historyOptions.hash || "" };
  documentRef = {
      body,
      documentElement: rootElement,
      readyState: historyOptions.loading ? "loading" : "complete",
      addEventListener: (name, callback) => { documentListeners[name] = callback; },
      getElementById: (id) => domReady ? get(id) : null,
      querySelector: (selector) => {
        if (!selectors.has(selector)) selectors.set(selector, new Element(documentRef));
        return selectors.get(selector);
      },
      querySelectorAll: (selector) => selector === ".sidebar-nav [data-view]" ? nav : selector === "[data-page]" ? pages : selector === "[data-go-view]" ? shortcuts : selector === "[data-metric]" ? metrics : [],
      createElement: () => new Element(documentRef), createElementNS: () => new Element(documentRef),
      createTextNode: (text) => Object.assign(new Element(documentRef), { textContent: text }),
      activeElement: body,
      contains: (element) => element?.isConnected === true,
  };
  [...elements.values(), body, rootElement, ...nav, ...shortcuts, ...metrics].forEach((element) => { element.ownerDocument = documentRef; });
  const context = vm.createContext({
    document: documentRef,
    window: { location, addEventListener: (name, callback) => { windowListeners[name] = callback; },
      scrollTo: (options) => { navigationScrolls.push(options); },
      history: { replaceState: (_state, _title, url) => { location.hash = url; } },
      matchMedia: (query) => ({ matches: query.includes("prefers-reduced-motion") ? historyOptions.reducedMotion === true : true }), URL: {
      createObjectURL: (blob) => { downloads.push(blob); return "blob:test-download"; }, revokeObjectURL() {},
    } },
    localStorage: {
      getItem: (key) => { if (historyOptions.storageUnavailable) throw new Error("Storage blocked"); return storedPreferences.get(key) ?? null; },
      setItem: (key, value) => {
        if (historyOptions.storageUnavailable) throw new Error("Storage blocked");
        storedPreferences.set(key, value);
        storageWrites.push({ key, value });
      },
    },
    setTimeout: (callback, ms) => { timeouts.set(++timeoutId, { callback, due: now + ms }); return timeoutId; },
    clearTimeout: (id) => timeouts.delete(id),
    setInterval: (callback, ms) => intervals.push({ callback, ms }),
    Date: TestDate, Intl, AbortController, TypeError, URLSearchParams,
    fetch: (url, options = {}) => {
      if (url.startsWith("/api/history")) {
        historyCalls.push(url);
        if (historyOptions.deferredHistory) return new Promise((resolve, reject) => {
          const pending = { url, settled: false,
            respond: (data, ok = true) => { pending.settled = true; resolve({ ok, status: ok ? 200 : 500, json: async () => data }); },
            reject: (error) => { pending.settled = true; reject(error); },
          };
          pendingHistory.push(pending);
        });
        const hours = new URLSearchParams(url.split("?")[1]).get("hours");
        const samples = historyOptions.samplesByHours?.[hours] || historyOptions.samples || [];
        return Promise.resolve({
        ok: !historyOptions.error, status: historyOptions.error ? 500 : 200,
        json: async () => historyOptions.error ? { error: historyOptions.error } : { samples },
      });
      }
      return new Promise((resolve, reject) => calls.push({
        url, options, reject,
        respond: (data, ok = true) => resolve({ ok, status: ok ? 200 : 400, json: async () => data }),
        respondBlob: (blob) => resolve({ ok: true, status: 200, blob: async () => blob }),
      }));
    },
  });
  vm.runInContext(source, context, { filename: "static/app.js" });
  const poll = () => intervals.find((item) => item.ms === 2000).callback();
  const pollHistory = () => intervals.find((item) => item.ms === 30000).callback();
  return { get, calls, historyCalls, pendingHistory, poll, pollHistory, downloads, body, rootElement, nav, pages, shortcuts, metrics, location, intervals, navigationScrolls, storedPreferences, storageWrites,
    document: documentRef,
    now: () => now,
    advance: (milliseconds) => { now += milliseconds; },
    tick: () => intervals.filter((entry) => entry.ms === 1000).forEach((entry) => entry.callback()),
    runTimeouts: () => { for (const [id, entry] of [...timeouts]) if (entry.due <= now) { timeouts.delete(id); entry.callback(); } },
    ready: () => { domReady = true; context.document.readyState = "interactive"; documentListeners.DOMContentLoaded?.(); delete documentListeners.DOMContentLoaded; },
    open: (view) => nav[views.indexOf(view)].emit("click"),
    hashChange: (hash) => { location.hash = hash; windowListeners.hashchange?.(); },
    backTo: (hash) => { location.hash = hash; windowListeners.popstate?.(); },
  };
}

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }
async function pushStatus(ui, data) {
  ui.poll();
  const request = ui.calls.findLast((call) => call.url === "/api/status");
  request.respond(data);
  await flush();
}

async function currentUi(changes = {}, options = {}) {
  const ui = harness(options);
  const data = snapshot();
  data.sample = liveSample(changes, ui.now());
  ui.calls[0].respond(data);
  await flush();
  return { ui, data };
}

async function connected(settings) {
  const ui = harness();
  ui.calls[0].respond(snapshot(settings));
  await flush();
  assert.equal(ui.get("notifications-state").textContent, "Listo");
  return ui;
}

test("notification test is separate from power actions and remains available with protection enabled", async () => {
  const ui = harness();
  const data = snapshot({ enabled: false, sound_enabled: false });
  data.policy.enabled = true;
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("test-shutdown").disabled, true);
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("test-notifications").textContent, "Probar aviso");
  const pending = ui.get("test-notifications").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/notifications/test");
  assert.equal(call.options.headers["X-UPS-Token"], "test-token");
  assert.deepEqual(JSON.parse(call.options.body), {});
  call.respond(data);
  await pending;
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.equal(ui.get("notifications-enabled").checked, false);
  assert.equal(ui.get("notifications-feedback").textContent, "Prueba solicitada");
});

test("late status GET cannot overwrite a confirmed notification save", async () => {
  const ui = await connected();
  ui.poll();
  const stale = ui.calls.at(-1);
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  const pending = ui.get("notifications-form").emit("submit");
  assert.equal(ui.get("save-policy").disabled, true);
  assert.equal(ui.get("test-notifications").disabled, true);
  const save = ui.calls.at(-1);
  assert.equal(save.url, "/api/notifications");
  assert.deepEqual(JSON.parse(save.options.body), { enabled: true, sound_enabled: false, repeat_seconds: 30 });
  save.respond(snapshot({ sound_enabled: false }));
  await pending;
  stale.respond(snapshot());
  await flush();
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.get("test-notifications").textContent, "Probar aviso");
});

test("saving notices preserves unsaved protection and saving protection preserves unsaved notices", async () => {
  const ui = await connected();
  ui.get("policy-on-battery").value = 12;
  ui.get("policy-form").emit("input");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  let pending = ui.get("notifications-form").emit("submit");
  ui.calls.at(-1).respond(snapshot({ sound_enabled: false }));
  await pending;
  assert.equal(ui.get("policy-on-battery").value, 12);
  ui.get("notifications-repeat").value = 60;
  ui.get("notifications-form").emit("input");
  pending = ui.get("policy-form").emit("submit");
  const data = snapshot({ sound_enabled: false });
  data.policy.on_battery_seconds = 720;
  ui.calls.at(-1).respond(data);
  await pending;
  assert.equal(ui.get("notifications-repeat").value, 60);
  assert.equal(ui.get("notifications-feedback").textContent, "Cambios sin guardar");
});

test("API loss disables all notice controls, then reconnect keeps unsaved edits", async () => {
  const ui = await connected();
  ui.get("notifications-repeat").value = 60;
  ui.get("notifications-form").emit("input");
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  for (const id of ["notifications-enabled", "notifications-sound", "notifications-repeat", "test-notifications", "save-notifications"]) {
    assert.equal(ui.get(id).disabled, true, id);
  }
  assert.equal(ui.get("notifications-state").textContent, "Sin conexión");
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  assert.equal(ui.get("notifications-repeat").disabled, false);
  assert.equal(ui.get("notifications-repeat").value, 60);
});

test("delivery errors use text and unavailable Windows backend disables test", async () => {
  const ui = harness();
  const data = snapshot();
  data.notifications.delivery = { available: false, ready: false, last_error: "<error> no disponible" };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("notifications-state").textContent, "No disponible");
  assert.equal(ui.get("test-notifications").disabled, true);
  assert.equal(ui.get("notifications-error").hidden, false);
  assert.equal(ui.get("notifications-error").textContent, "Último error: <error> no disponible");
});

test("a rejected notification test is reported without claiming delivery", async () => {
  const ui = await connected();
  const pending = ui.get("test-notifications").emit("click");
  const data = snapshot();
  data.notifications.last_notification = { accepted: false };
  data.notifications.last_error = "La cola no está disponible.";
  ui.calls.at(-1).respond(data);
  await pending;
  assert.equal(ui.get("notifications-feedback").textContent, "Prueba sin confirmar");
  assert.equal(ui.get("toast").textContent, "La cola no está disponible.");
});

test("sound failure is separate from the available visual notification channel", async () => {
  const ui = harness();
  const data = snapshot();
  data.notifications.delivery.last_sound_error = "PlaySound no respondió.";
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("notifications-state").textContent, "Listo");
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("notifications-error").hidden, true);
  assert.equal(ui.get("notifications-sound-error").hidden, false);
  assert.equal(ui.get("notifications-sound-error").textContent, "El canal de avisos visuales sigue disponible; error de sonido: PlaySound no respondió.");
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  assert.equal(ui.get("notifications-sound-error").hidden, true);
});

test("fallback popup status reports opening without claiming the user read it", async () => {
  const ui = harness();
  const data = snapshot();
  Object.assign(data.notifications.delivery, {
    popup_available: true,
    last_popup_shown_at: "2026-10-17T23:00:00Z",
    last_popup_hidden_at: null,
    popup_error: null,
  });
  ui.calls[0].respond(data);
  await flush();
  assert.match(ui.get("notifications-delivery").textContent, /^Último aviso de Elipro abierto:/);
  assert.match(ui.get("notifications-delivery").textContent, /no confirma que lo hayas leído/);
  assert.equal(ui.get("notifications-popup-error").hidden, true);
  ui.poll();
  data.notifications.delivery.popup_error = "No se pudo crear la ventana.";
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("notifications-popup-error").hidden, false);
  assert.equal(ui.get("notifications-popup-error").textContent, "Ventana de respaldo de Elipro: No se pudo crear la ventana.");
});

test("healthy history and older snapshots do not add a warning", async () => {
  const ui = await connected();
  assert.equal(ui.get("history-notice").hidden, true);
  ui.poll();
  const data = snapshot();
  data.history = { available: true, state: "healthy", pending: 0, dropped: 0, message: "Historial activo." };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("history-notice").hidden, true);
  assert.match(ui.get("chart-note").lastChild.textContent, /Registro cada/);
});

test("history failure is separate from USB readings, notices and disabled shutdown", async () => {
  const ui = harness();
  const data = snapshot();
  data.sample = liveSample({ input_voltage: 219 });
  data.history = { available: false, state: "degraded", pending: 3, dropped: 2, message: "No se pudo guardar <dato>." };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("history-notice").hidden, false);
  assert.match(ui.get("history-notice").textContent, /guardado del historial está interrumpido/);
  assert.match(ui.get("history-notice").textContent, /lecturas actuales y los avisos continúan/);
  assert.match(ui.get("history-notice").textContent, /apagado automático sigue desactivado/);
  assert.match(ui.get("history-notice").textContent, /Registros pendientes: 3/);
  assert.match(ui.get("history-notice").textContent, /Registros descartados durante la falla: 2/);
  assert.equal(ui.get("history-notice").title, "No se pudo guardar <dato>.");
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
  assert.match(ui.get("input-voltage").textContent, /219/);
  assert.equal(ui.get("notifications-state").textContent, "Listo");
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.match(ui.get("chart-note").lastChild.textContent, /datos mostrados pueden estar incompletos/);
});

test("recovery clears the storage warning but keeps lost history visible", async () => {
  const ui = harness();
  const data = snapshot();
  data.history = { available: false, state: "recovering", pending: 4, dropped: 0 };
  ui.calls[0].respond(data);
  await flush();
  assert.match(ui.get("history-notice").textContent, /historial se está recuperando/);
  ui.poll();
  data.history = { available: true, state: "healthy", pending: 0, dropped: 5 };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.match(ui.get("history-notice").textContent, /historial está disponible, pero perdió registros/);
  assert.match(ui.get("history-notice").textContent, /Registros descartados durante la falla: 5/);
  assert.doesNotMatch(ui.get("history-notice").textContent, /está interrumpido/);
  ui.poll();
  data.history.dropped = 0;
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("history-notice").hidden, true);
});

test("configuration fallback warning remains visible after history recovers", async () => {
  const ui = harness();
  const data = snapshot();
  data.history = { available: true, state: "healthy", pending: 0, dropped: 0, configuration_warning: "No se pudieron recuperar los ajustes al iniciar." };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("history-notice").hidden, false);
  assert.match(ui.get("history-notice").textContent, /No se pudieron recuperar los ajustes al iniciar/);
  assert.match(ui.get("history-notice").textContent, /apagado automático sigue desactivado/);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.match(ui.get("chart-note").lastChild.textContent, /Registro cada/);
});

test("history query failure does not mark the monitor offline and can recover", async () => {
  const history = { error: "La consulta no respondió." };
  const ui = harness(history);
  ui.calls[0].respond(snapshot());
  await flush();
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("history-notice").hidden, false);
  assert.match(ui.get("history-notice").textContent, /No se puede consultar el historial/);
  assert.doesNotMatch(ui.get("history-notice").textContent, /guardado del historial está interrumpido/);
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("chart-empty").querySelector("strong").textContent, "Historial no disponible");
  history.error = null;
  await ui.pollHistory();
  assert.equal(ui.get("history-notice").hidden, true);
  assert.match(ui.get("chart-note").lastChild.textContent, /Registro cada/);
});

test("health shows reader, supervisor and process metrics, then becomes unconfirmed on API loss", async () => {
  const ui = harness();
  const data = snapshot();
  data.health = { state: "healthy", message: "Lector supervisado.", uptime_seconds: 3662,
    reader_alive: true, supervisor_alive: true, read_successes: 51, read_failures: 2,
    consecutive_failures: 0, recoveries: 1, reader_restarts: 0,
    last_success: "2026-10-17T21:00:03Z", read_age_seconds: 1,
    process: { memory_bytes: 31457280, cpu_percent: 0.4 } };
  data.history = { available: true, pending: 3, last_success_at: "2026-10-17T21:00:00Z" };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("health-state").textContent, "Supervisado");
  assert.equal(ui.get("health-workers").textContent, "Activo / Activo");
  assert.equal(ui.get("health-reads").textContent, "51 / 2");
  assert.match(ui.get("health-resources").textContent, /30 MB/);
  assert.match(ui.get("health-storage").textContent, /3 pendientes/);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  assert.equal(ui.get("health-state").textContent, "Sin confirmar");
  assert.equal(ui.get("health-workers").textContent, "—");
});

test("a stopped supervisor never appears as a healthy supervised monitor", async () => {
  const ui = harness();
  const data = snapshot();
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: false };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("health-state").textContent, "Revisar monitor");
  assert.equal(ui.get("health-workers").textContent, "Activo / Detenido");
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
});

test("incident and archive filters convert local dates to ISO without changing power settings", async () => {
  const ui = await connected();
  ui.get("records-from").value = "2026-10-17T10:00";
  ui.get("records-to").value = "2026-10-17T12:00";
  ui.get("event-search").value = " batería <dato> ";
  ui.get("event-kind").value = "battery_low";
  const pending = ui.get("refresh-records").emit("click");
  const incidentCall = ui.calls.find((call) => call.url.startsWith("/api/incidents?"));
  const eventCall = ui.calls.find((call) => call.url.startsWith("/api/events/search?"));
  const incidentParams = new URLSearchParams(incidentCall.url.split("?")[1]);
  const eventParams = new URLSearchParams(eventCall.url.split("?")[1]);
  assert.equal(incidentParams.get("from"), new Date("2026-10-17T10:00").toISOString());
  assert.equal(incidentParams.get("to"), new Date("2026-10-17T12:00").toISOString());
  assert.equal(incidentParams.has("q"), false);
  assert.equal(eventParams.get("q"), "batería <dato>");
  assert.equal(eventParams.get("kind"), "battery_low");
  incidentCall.respond({ incidents: [{ id: "power:1", type: "power", status: "recovered",
    started_at: "2026-10-17T15:00:00Z", ended_at: "2026-10-17T15:00:10Z", duration_seconds: 10,
    message: "<dato> literal", actions: [], start_known: true }], total: 1, next_cursor: null });
  eventCall.respond({ events: [{ id: 2, kind: "battery_low", timestamp: "2026-10-17T15:00:00Z", message: "<dato> literal" }], total: 1, next_cursor: null });
  await pending;
  assert.equal(ui.get("incidents-count").textContent, "1 / 1");
  assert.match(ui.get("incidents-list").children[0].children[0].textContent, /Corte eléctrico · Recuperado/);
  assert.equal(ui.get("incidents-list").children[0].children[3].textContent, "<dato> literal");
  assert.equal(ui.get("incidents-list").children[0].children[2].textContent, "Duración detectada: 10 s");
  assert.equal(ui.get("archive-list").children[0].children[1].children[1].textContent, "Batería baja del UPS");
  assert.match(ui.get("archive-summary").textContent, /1 evento mostrado de 1/);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("archive failures stay inside their section and incident errors do not mark USB offline", async () => {
  const ui = await connected();
  const pending = ui.get("refresh-records").emit("click");
  ui.calls.find((call) => call.url.startsWith("/api/incidents?")).respond({ error: "Historial ocupado" }, false);
  ui.calls.find((call) => call.url.startsWith("/api/events/search?")).reject(new TypeError("offline"));
  await pending;
  assert.equal(ui.get("incidents-error").hidden, false);
  assert.match(ui.get("incidents-error").textContent, /Historial ocupado/);
  assert.equal(ui.get("archive-error").hidden, false);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
  assert.equal(ui.get("test-notifications").disabled, false);
});

test("historical incidents without recovery do not claim an ongoing outage or communication failure", async () => {
  const ui = harness();
  const data = snapshot();
  data.sample = liveSample();
  ui.calls[0].respond(data);
  await flush();
  const pending = ui.get("refresh-records").emit("click");
  ui.calls.find((call) => call.url.startsWith("/api/incidents?")).respond({ incidents: [
    { id: "power:1", type: "power", status: "open", started_at: "2026-10-17T10:00:00Z", ended_at: null, duration_seconds: 3600, actions: [] },
    { id: "communication:2", type: "communication", status: "open", started_at: "2026-10-17T11:00:00Z", ended_at: null, duration_seconds: 1800, actions: [] },
  ], total: 2, next_cursor: null });
  ui.calls.find((call) => call.url.startsWith("/api/events/search?")).respond({ events: [], total: 0, next_cursor: null });
  await pending;
  const power = ui.get("incidents-list").children[0];
  const communication = ui.get("incidents-list").children[1];
  assert.match(power.children[1].textContent, /Sin recuperación registrada/);
  assert.doesNotMatch(power.children[1].textContent, /En curso/);
  assert.equal(power.children[2].textContent, "Tiempo desde la detección: 1 h 0 min");
  assert.match(power.children[3].textContent, /no confirma que el UPS siga en batería/);
  assert.match(communication.children[3].textContent, /no confirma que siga habiendo un problema de comunicación/);
  assert.equal(ui.get("power-state").textContent, "Red eléctrica activa");
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
});

test("incident and event pagination use independent cursors and avoid duplicate rows", async () => {
  const ui = await connected();
  const first = ui.get("refresh-records").emit("click");
  ui.calls.find((call) => call.url.startsWith("/api/incidents?")).respond({ incidents: [{ id: "usb:1", type: "communication", status: "open", actions: [] }], total: 2, next_cursor: "inc-next" });
  ui.calls.find((call) => call.url.startsWith("/api/events/search?")).respond({ events: [{ id: 5, kind: "connection_lost", message: "USB desconectado" }], total: 2, next_cursor: "ev-next" });
  await first;
  assert.equal(ui.get("more-incidents").hidden, false);
  assert.equal(ui.get("more-events").hidden, false);
  assert.equal(ui.get("archive-list").children[0].children[1].children[1].textContent, "Comunicación con el UPS perdida");
  const incidents = ui.get("more-incidents").emit("click");
  const incidentCall = ui.calls.at(-1);
  assert.equal(new URLSearchParams(incidentCall.url.split("?")[1]).get("cursor"), "inc-next");
  incidentCall.respond({ incidents: [{ id: "usb:1", actions: [] }, { id: "power:2", type: "power", status: "open", actions: [] }], total: 2, next_cursor: null });
  await incidents;
  assert.equal(ui.get("incidents-list").children.length, 2);
  assert.equal(ui.get("incidents-count").textContent, "2 / 2");
  assert.equal(ui.get("more-incidents").hidden, true);
  const events = ui.get("more-events").emit("click");
  const eventCall = ui.calls.at(-1);
  assert.equal(new URLSearchParams(eventCall.url.split("?")[1]).get("cursor"), "ev-next");
  eventCall.respond({ events: [{ id: 4, message: "Evento anterior" }], total: 2, next_cursor: null });
  await events;
  assert.equal(ui.get("archive-list").children.length, 2);
  assert.equal(ui.get("more-events").hidden, true);
});

test("new record queries discard late results from older filters", async () => {
  const ui = await connected();
  const first = ui.get("refresh-records").emit("click");
  const oldIncidents = ui.calls.find((call) => call.url.startsWith("/api/incidents?"));
  const oldEvents = ui.calls.find((call) => call.url.startsWith("/api/events/search?"));
  ui.get("event-search").value = "nuevo";
  await ui.get("event-search-form").emit("submit");
  oldIncidents.respond({ incidents: [{ id: "old" }], total: 1 });
  oldEvents.respond({ events: [{ id: 9, message: "Obsoleto" }], total: 1 });
  await first;
  await flush();
  const nextIncidents = ui.calls.filter((call) => call.url.startsWith("/api/incidents?")).at(-1);
  const nextEvents = ui.calls.filter((call) => call.url.startsWith("/api/events/search?")).at(-1);
  assert.notEqual(nextIncidents, oldIncidents);
  assert.match(nextEvents.url, /q=nuevo/);
  nextIncidents.respond({ incidents: [], total: 0, next_cursor: null });
  nextEvents.respond({ events: [{ id: 10, message: "Nuevo" }], total: 1, next_cursor: null });
  await flush();
  assert.equal(ui.get("archive-list").children.length, 1);
  assert.equal(ui.get("archive-list").children[0].children[1].children[0].textContent, "Nuevo");
});

test("invalid date ranges are explained before requesting history", async () => {
  const ui = await connected();
  const before = ui.calls.length;
  ui.get("records-from").value = "2026-10-18T10:00";
  ui.get("records-to").value = "2026-10-17T10:00";
  await ui.get("records-form").emit("submit");
  assert.equal(ui.calls.length, before);
  assert.match(ui.get("records-feedback").textContent, /Desde debe ser anterior/);
  assert.equal(ui.get("service-notice").hidden, true);
});

test("backup downloads a binary file without changing protection or claiming it was saved", async () => {
  const ui = await connected();
  const pending = ui.get("download-backup").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/backup.zip");
  assert.equal(call.options.method, undefined);
  assert.equal(ui.get("download-diagnostics").disabled, true);
  const blob = { zip: "binary payload" };
  call.respondBlob(blob);
  await pending;
  assert.equal(ui.downloads[0], blob);
  assert.equal(ui.body.children.at(-1).download, "elipro-respaldo.zip");
  assert.equal(ui.body.children.at(-1).clicked, true);
  assert.equal(ui.body.children.at(-1).removed, true);
  assert.match(ui.get("maintenance-feedback").textContent, /entregado al navegador/);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.equal(ui.get("download-backup").disabled, false);
});

test("CSV export applies all current filters without pagination and isolates download errors", async () => {
  const ui = await connected();
  ui.get("event-search").value = "corte & retorno";
  ui.get("event-kind").value = "power_lost";
  ui.get("records-from").value = "2026-10-17T10:00";
  const pending = ui.get("export-events").emit("click");
  const call = ui.calls.at(-1);
  assert.ok(call.url.startsWith("/api/events/export.csv?"));
  const params = new URLSearchParams(call.url.split("?")[1]);
  assert.equal(params.get("q"), "corte & retorno");
  assert.equal(params.get("kind"), "power_lost");
  assert.equal(params.get("from"), new Date("2026-10-17T10:00").toISOString());
  assert.equal(params.has("cursor"), false);
  call.respond({ error: "No se pudo exportar" }, false);
  await pending;
  assert.match(ui.get("archive-summary").textContent, /No se pudo exportar/);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("test-notifications").disabled, false);
});

test("exiting the monitor requires an explicit second click and never requests a Windows action", async () => {
  const ui = await connected();
  const before = ui.calls.length;
  ui.get("stop-monitor").emit("click");
  assert.equal(ui.get("stop-confirmation").hidden, false);
  assert.equal(ui.calls.length, before);
  ui.get("cancel-stop-monitor").emit("click");
  assert.equal(ui.get("stop-confirmation").hidden, true);
  ui.get("stop-monitor").emit("click");
  const pending = ui.get("confirm-stop-monitor").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/service/stop");
  assert.equal(call.options.method, "POST");
  assert.deepEqual(JSON.parse(call.options.body), {});
  call.respond({ stopped: true });
  await pending;
  assert.equal(ui.get("test-notifications").disabled, true);
  assert.equal(ui.get("health-state").textContent, "Detenido");
  assert.equal(ui.get("energy-diagram").dataset.quality, "stopped");
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.match(ui.get("service-notice").textContent, /se detuvo por tu solicitud/);
  assert.match(ui.get("health-message").textContent, /Detuviste Elipro/);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "Elipro detenido");
  assert.equal(ui.get("notifications-state").textContent, "Detenidos");
  assert.match(ui.get("notifications-delivery").textContent, /reanudarán cuando vuelvas a iniciarlo/);
  assert.equal(ui.get("policy-enabled").checked, false);
});

test("acknowledged stop remains explicit during failed polls and clears after confirmed reconnection", async () => {
  const ui = await connected();
  ui.poll();
  const countdownData = snapshot();
  countdownData.protection = { state: "countdown", deadline: new Date(Date.now() + 60000).toISOString(), simulation: true };
  ui.calls.at(-1).respond(countdownData);
  await flush();
  assert.equal(ui.get("protection-alert").hidden, false);
  const pending = ui.get("confirm-stop-monitor").emit("click");
  ui.calls.at(-1).respond({ stopped: true });
  await pending;
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("closed"));
  await flush();
  assert.equal(ui.get("health-state").textContent, "Detenido");
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.get("countdown").textContent, "—");
  assert.equal(ui.get("notifications-state").textContent, "Detenidos");
  assert.doesNotMatch(ui.get("notifications-delivery").textContent, /Comprueba que/);
  ui.poll();
  const data = snapshot();
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("health-state").textContent, "Supervisado");
  assert.equal(ui.get("notifications-state").textContent, "Listo");
  assert.equal(ui.get("service-notice").hidden, true);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("lost again"));
  await flush();
  assert.equal(ui.get("health-state").textContent, "Sin confirmar");
  assert.equal(ui.get("notifications-state").textContent, "Sin conexión");
  assert.doesNotMatch(ui.get("service-notice").textContent, /se detuvo por tu solicitud/);
});

test("unacknowledged API loss retains countdown warnings without claiming Elipro stopped", async () => {
  const ui = harness();
  const data = snapshot();
  data.protection = { state: "countdown", deadline: new Date(Date.now() + 60000).toISOString(), reason: "Prueba en curso", simulation: true };
  ui.calls[0].respond(data);
  await flush();
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("no response"));
  await flush();
  assert.equal(ui.get("protection-alert").hidden, false);
  assert.match(ui.get("protection-reason").textContent, /cuenta atrás puede seguir activa/);
  assert.equal(ui.get("cancel-shutdown").disabled, true);
  assert.equal(ui.get("health-state").textContent, "Sin confirmar");
  assert.equal(ui.get("notifications-state").textContent, "Sin conexión");
  assert.doesNotMatch(ui.get("service-notice").textContent, /se detuvo por tu solicitud/);
});

test("persistent critical notices are only exposed when supported and persist independently of protection", async () => {
  const legacy = await connected();
  assert.equal(legacy.get("notifications-persistent-option").hidden, true);
  assert.equal(legacy.get("notifications-persistent").disabled, true);
  const ui = await connected({ persistent_critical: false });
  assert.equal(ui.get("notifications-persistent-option").hidden, false);
  assert.equal(ui.get("notifications-persistent").disabled, false);
  ui.get("notifications-persistent").checked = true;
  ui.get("notifications-form").emit("input");
  const pending = ui.get("notifications-form").emit("submit");
  const call = ui.calls.at(-1);
  assert.equal(JSON.parse(call.options.body).persistent_critical, true);
  call.respond(snapshot({ persistent_critical: true }));
  await pending;
  assert.equal(ui.get("notifications-persistent").checked, true);
  assert.equal(ui.get("policy-enabled").checked, false);
});

test("safe rule checks require backend evidence and leave live policy untouched", async () => {
  const ui = await connected();
  const pending = ui.get("run-validation").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/validation/run");
  assert.equal(call.options.headers["X-UPS-Token"], "test-token");
  assert.deepEqual(JSON.parse(call.options.body), {});
  call.respond({ source: "isolated_simulation", os_actions_executed: 0, live_settings_changed: false,
    passed: true, scenarios: [{ name: "return_to_mains", label: "Regreso de electricidad", passed: true, detail: "Cancela la cuenta atrás simulada." }] });
  await pending;
  assert.match(ui.get("validation-summary").textContent, /simuladas superadas/);
  assert.match(ui.get("validation-results").children[0].children[0].textContent, /Regreso de electricidad/);
  assert.equal(ui.get("policy-enabled").checked, false);
  const invalid = ui.get("run-validation").emit("click");
  ui.calls.at(-1).respond({ source: "unexpected", os_actions_executed: 0, live_settings_changed: false, passed: true, scenarios: [] });
  await invalid;
  assert.match(ui.get("validation-summary").textContent, /no confirmó las condiciones de seguridad/);
  assert.equal(ui.get("service-notice").hidden, true);
});

test("physical validation reports confirmed cut while keeping USB and Windows tests pending", async () => {
  const ui = await connected();
  const pending = ui.get("review-physical-tests").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/validation");
  ui.calls.at(-1).respond({ available: true, record: {
    real_cut: { status: "passed", date: "2026-10-17", seconds_detected: 11.9,
      session_preserved_user_confirmed: true, visual_and_sound_user_confirmed: true },
    physical_usb_reconnection: { status: "pending_user_test" },
    real_windows_shutdown: { status: "not_tested_disabled" },
  } });
  await pending;
  assert.match(ui.get("physical-test-summary").textContent, /Sesión conservada, confirmado por ti/);
  assert.match(ui.get("physical-test-summary").textContent, /USB: pendiente/);
  assert.match(ui.get("physical-test-summary").textContent, /Windows: sin probar/);
  assert.match(ui.get("physical-test-summary").textContent, /apagado automático está desactivado/);
});

test("stability observation supports starting, status from snapshots and stopping without stopping Elipro", async () => {
  const ui = await connected();
  const start = ui.get("start-stability").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/stability/start");
  assert.deepEqual(JSON.parse(call.options.body), { hours: 48 });
  call.respond({ state: "running", elapsed_seconds: 0, target_seconds: 172800, samples: 0, read_failures: 0, report_available: true });
  await start;
  assert.match(ui.get("stability-summary").textContent, /Observación en curso/);
  assert.equal(ui.get("start-stability").disabled, true);
  assert.equal(ui.get("stop-stability").hidden, false);
  assert.equal(ui.get("download-stability").hidden, false);
  ui.poll();
  const data = snapshot();
  data.health = { observation: { state: "running", elapsed_seconds: 60, target_seconds: 172800, samples: 30, read_failures: 1, report_available: true } };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.match(ui.get("stability-summary").textContent, /30 comprobaciones/);
  assert.match(ui.get("stability-summary").textContent, /1 errores de lectura/);
  const stop = ui.get("stop-stability").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/stability/stop");
  ui.calls.at(-1).respond({ state: "interrupted", elapsed_seconds: 60, target_seconds: 172800, samples: 30, report_available: true });
  await stop;
  assert.match(ui.get("stability-summary").textContent, /Observación interrumpida/);
  assert.equal(ui.get("stop-stability").hidden, true);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, false);
});

test("stability query failures remain separate from live USB status", async () => {
  const ui = await connected();
  const pending = ui.get("refresh-stability").emit("click");
  ui.calls.at(-1).respond({ error: "Informe ocupado" }, false);
  await pending;
  assert.equal(ui.get("stability-error").hidden, false);
  assert.match(ui.get("stability-error").textContent, /Informe ocupado/);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
});

test("section navigation defaults to Resumen and keeps one status polling loop", async () => {
  const ui = harness({ navigation: true });
  assert.equal(ui.location.hash, "#resumen");
  assert.equal(ui.get("view-title").textContent, "Resumen");
  assert.equal(ui.get("page-overview").hidden, false);
  for (const page of ui.pages.slice(1)) {
    assert.equal(page.hidden, true);
    assert.equal(page.inert, true);
    assert.equal(page.getAttribute("aria-hidden"), "true");
  }
  assert.equal(ui.nav[0].getAttribute("aria-current"), "page");
  ui.calls[0].respond(snapshot());
  await flush();
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  assert.equal(ui.calls.some((call) => /incidents|events\/search|stability/.test(call.url)), false);
  for (const name of ["protection", "notifications", "overview"]) ui.open(name);
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("deep links and browser back restore their section without changing live settings", async () => {
  const ui = harness({ navigation: true, hash: "#avisos" });
  assert.equal(ui.get("view-title").textContent, "Avisos");
  assert.equal(ui.get("page-notifications").inert, false);
  ui.calls[0].respond(snapshot());
  await flush();
  ui.open("protection");
  assert.equal(ui.location.hash, "proteccion");
  assert.equal(ui.get("page-title").focused, true);
  assert.equal(ui.nav[3].getAttribute("aria-current"), "page");
  assert.equal(ui.nav[4].getAttribute("aria-current"), "false");
  ui.backTo("#avisos");
  assert.equal(ui.get("view-title").textContent, "Avisos");
  ui.hashChange("#resumen");
  assert.equal(ui.get("view-title").textContent, "Resumen");
  ui.hashChange("#<script>invalid</script>");
  assert.equal(ui.get("view-title").textContent, "Resumen");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("unsaved protection and notice edits survive navigation and status updates", async () => {
  const ui = harness({ navigation: true });
  ui.calls[0].respond(snapshot());
  await flush();
  ui.open("protection");
  ui.get("policy-on-battery").value = 17;
  ui.get("policy-form").emit("input");
  ui.open("notifications");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  ui.open("overview");
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  ui.open("protection");
  assert.equal(ui.get("policy-on-battery").value, 17);
  assert.equal(ui.get("settings-feedback").textContent, "Cambios sin guardar");
  ui.open("notifications");
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.get("notifications-feedback").textContent, "Cambios sin guardar");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("sidebar monitoring and critical protection controls remain accurate across all six sections", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.meta.dry_run = true;
  data.connection.last_success = new Date(Date.now() - 5000).toISOString();
  data.protection = { state: "countdown", simulation: true, countdown_remaining: 12, reason: "Prueba segura" };
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true,
    observation: { state: "running", elapsed_seconds: 3600, target_seconds: 172800 } };
  ui.calls[0].respond(data);
  await flush();
  const views = ["overview", "incidents", "history", "protection", "notifications", "system"];
  assert.equal(ui.get("development-notice").hidden, false);
  const lastReading = ui.get("last-updated").textContent;
  assert.match(lastReading, /^Hace \d+ s · /);
  assert.equal(ui.get("last-updated").title, new Date(data.connection.last_success).toLocaleString("es-PE"));
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.equal(ui.get("countdown").textContent, "00:12");
    assert.equal(ui.get("cancel-shutdown").hidden, false);
    assert.equal(ui.get("cancel-shutdown").disabled, false);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
    assert.equal(ui.get("last-updated").textContent, lastReading);
  }
  assert.equal(ui.get("health-state").textContent, "Supervisado");
  assert.match(ui.get("stability-summary").textContent, /1 h 0 min de 48 h/);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "Monitor sin conexión");
    assert.equal(ui.get("last-updated").textContent, lastReading);
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.match(ui.get("protection-reason").textContent, /cancelación no está confirmada/);
    assert.equal(ui.get("cancel-shutdown").disabled, true);
  }
  ui.poll();
  ui.calls.at(-1).respond(data);
  await flush();
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.doesNotMatch(ui.get("protection-reason").textContent, /cancelación no está confirmada/);
    assert.equal(ui.get("cancel-shutdown").disabled, false);
  }
  const cancellation = ui.get("cancel-shutdown").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/protection/cancel");
  ui.calls.at(-1).respond({ ...data, meta: { ...data.meta, dry_run: false }, protection: { state: "cancelled", simulation: true } });
  await cancellation;
  assert.equal(ui.get("development-notice").hidden, true);
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("protection-alert").hidden, true);
  }
  assert.equal(ui.get("toast").textContent, "El monitor confirmó la cancelación de la prueba.");
});

test("Incidentes and Historial load on entry and use independent date filters", async () => {
  const ui = harness({ navigation: true });
  ui.calls[0].respond(snapshot());
  await flush();
  ui.get("records-from").value = "2026-10-17T10:00";
  ui.get("incidents-from").value = "2026-10-16T08:00";
  ui.open("incidents");
  let call = ui.calls.at(-1);
  assert.match(call.url, /^\/api\/incidents\?/);
  assert.equal(new URLSearchParams(call.url.split("?")[1]).get("from"), new Date("2026-10-16T08:00").toISOString());
  call.respond({ incidents: [], next_cursor: null, total: 0 });
  await flush();
  ui.open("history");
  call = ui.calls.at(-1);
  assert.match(call.url, /^\/api\/events\/search\?/);
  assert.equal(new URLSearchParams(call.url.split("?")[1]).get("from"), new Date("2026-10-17T10:00").toISOString());
  call.respond({ events: [], next_cursor: null, total: 0 });
  await flush();
  const queryCount = ui.calls.length;
  ui.open("incidents");
  ui.open("history");
  assert.equal(ui.calls.length, queryCount);
  const pending = ui.get("refresh-records").emit("click");
  assert.equal(ui.calls.length, queryCount + 1);
  assert.match(ui.calls.at(-1).url, /^\/api\/events\/search\?/);
  ui.calls.at(-1).respond({ events: [], next_cursor: null, total: 0 });
  await pending;
  assert.equal(ui.calls.some((entry) => entry.options.method === "POST"), false);
});

test("section shortcuts and keyboard navigation are accessible and do not submit forms", async () => {
  const ui = harness({ navigation: true });
  ui.shortcuts[4].emit("click");
  assert.equal(ui.get("view-title").textContent, "Avisos");
  ui.nav[4].emit("keydown", { key: "ArrowDown" });
  assert.equal(ui.nav[5].focused, true);
  assert.equal(ui.get("view-title").textContent, "Avisos");
  ui.nav[5].emit("keydown", { key: "Home" });
  assert.equal(ui.nav[0].focused, true);
  ui.nav[0].emit("keydown", { key: "End" });
  assert.equal(ui.nav[5].focused, true);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("moving between sections resets long-page scroll while keeping the new heading focusable", () => {
  const ui = harness({ navigation: true });
  assert.equal(ui.navigationScrolls.length, 0);
  ui.open("protection");
  assert.equal(ui.navigationScrolls.length, 1);
  assert.equal(ui.navigationScrolls[0].top, 0);
  assert.equal(ui.navigationScrolls[0].left, 0);
  assert.equal(ui.navigationScrolls[0].behavior, "instant");
  assert.equal(ui.get("page-title").focused, true);
  ui.open("protection");
  assert.equal(ui.navigationScrolls.length, 2, "an explicit click returns even the current long section to its top");
  ui.backTo("#resumen");
  assert.equal(ui.navigationScrolls.length, 3, "browser back resets the newly selected section");
  ui.hashChange("#resumen");
  assert.equal(ui.navigationScrolls.length, 3, "a duplicate hash event must not reset scrolling");
  ui.shortcuts[4].emit("click");
  assert.equal(ui.navigationScrolls.length, 4);
  assert.equal(ui.get("view-title").textContent, "Avisos");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("Resumen voltage chart respects collection gaps and omits unsupported battery runtime", async () => {
  const now = Date.now();
  const ui = harness({ navigation: true, samples: [
    { timestamp: new Date(now - 30000).toISOString(), input_voltage: 220, output_voltage: 220 },
    { timestamp: new Date(now - 20000).toISOString(), input_voltage: 221, output_voltage: 220 },
    { timestamp: new Date(now - 10000).toISOString(), input_voltage: 220, output_voltage: 220, gap_before: true },
  ] });
  const data = snapshot();
  data.sample = liveSample({ runtime_seconds: null, battery_voltage: 27.4 });
  ui.calls[0].respond(data);
  await flush();
  const chart = ui.get("history-chart");
  const lines = chart.children.filter((item) => item.getAttribute("d"));
  assert.equal(lines.length, 2);
  assert.equal((lines[0].getAttribute("d").match(/M/g) || []).length, 2);
  assert.equal((lines[0].getAttribute("d").match(/L/g) || []).length, 1);
  assert.equal(ui.get("battery-runtime").hidden, true);
  assert.equal(ui.get("battery-unit").textContent, "%");
  assert.equal(ui.get("battery-percent").textContent, "—");
  assert.equal(ui.get("battery-note").textContent, "No disponible");
  assert.equal(ui.get("battery-voltage").textContent, "27.4");
});

test("a saved theme is applied in the head before the dashboard starts", async () => {
  const ui = harness({ navigation: true, loading: true, storage: { "elipro-theme": "light" } });
  assert.equal(ui.rootElement.dataset.theme, "light");
  assert.equal(ui.rootElement.style.colorScheme, "light");
  assert.equal(ui.calls.length, 0, "head initialization must not access dashboard data before its DOM exists");
  assert.equal(ui.intervals.length, 0);
  const bootstrap = html.match(/<script\b[^>]*src="\/app.js"[^>]*><\/script>/);
  assert.ok(bootstrap, "the theme bootstrap must be an external script allowed by the existing CSP");
  assert.doesNotMatch(bootstrap[0], /\b(?:async|defer)\b/, "the bootstrap must run before initial paint");
  assert.ok(bootstrap.index < html.indexOf('<link rel="stylesheet"'), "the CSP-compatible external bootstrap must precede CSS");
  const scriptBodies = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1].trim());
  assert.equal(scriptBodies.some(Boolean), false, "no inline script may bypass the existing CSP");
  ui.ready();
  assert.equal(ui.get("theme-toggle").getAttribute("aria-label"), "Cambiar a modo oscuro");
  assert.equal(ui.get("theme-toggle").getAttribute("aria-pressed"), "true");
  assert.equal(ui.calls[0].url, "/api/status");
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  ui.ready();
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  ui.calls[0].respond(snapshot());
  await flush();
});

test("dark remains the default and the theme toggle saves only a browser preference", async () => {
  const ui = harness({ navigation: true });
  assert.equal(ui.rootElement.dataset.theme, "dark");
  assert.equal(ui.get("theme-toggle").getAttribute("aria-label"), "Cambiar a modo claro");
  assert.equal(ui.get("theme-toggle").getAttribute("aria-pressed"), "false");
  const initialCalls = ui.calls.length;
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.rootElement.dataset.theme, "light");
  assert.equal(ui.get("theme-toggle").getAttribute("title"), "Cambiar a modo oscuro");
  assert.equal(ui.storedPreferences.get("elipro-theme"), "light");
  assert.deepEqual(ui.storageWrites, [{ key: "elipro-theme", value: "light" }]);
  assert.equal(ui.calls.length, initialCalls);
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.rootElement.dataset.theme, "dark");
  assert.equal(ui.storedPreferences.get("elipro-theme"), "dark");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  const restored = harness({ storage: Object.fromEntries(ui.storedPreferences) });
  assert.equal(restored.rootElement.dataset.theme, "dark");
});

test("switching theme preserves the active section, dirty forms, focus and monitoring", async () => {
  const ui = harness({ navigation: true });
  ui.calls[0].respond(snapshot());
  await flush();
  ui.open("protection");
  ui.get("policy-on-battery").value = 17;
  ui.get("policy-form").emit("input");
  ui.open("notifications");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  ui.get("theme-toggle").focus();
  const initialCalls = ui.calls.length;
  const initialScrolls = ui.navigationScrolls.length;
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.get("view-title").textContent, "Avisos");
  assert.equal(ui.location.hash, "avisos");
  assert.equal(ui.get("policy-on-battery").value, 17);
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.get("theme-toggle").focused, true);
  assert.equal(ui.calls.length, initialCalls);
  assert.equal(ui.navigationScrolls.length, initialScrolls);
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  assert.equal(ui.rootElement.dataset.theme, "light");
  assert.equal(ui.get("policy-on-battery").value, 17);
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("unavailable browser storage cannot stop the theme toggle or UPS monitoring", async () => {
  const ui = harness({ navigation: true, storageUnavailable: true });
  assert.equal(ui.rootElement.dataset.theme, "dark");
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.rootElement.dataset.theme, "light");
  ui.calls[0].respond(snapshot());
  await flush();
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("policy-enabled").checked, false);
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.rootElement.dataset.theme, "dark");
  assert.equal(ui.get("theme-toggle").getAttribute("aria-label"), "Cambiar a modo claro");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  const invalid = harness({ storage: { "elipro-theme": "<script>" } });
  assert.equal(invalid.rootElement.dataset.theme, "dark");
});

test("Resumen and Historial share one chart and recent-event filter without extra queries", async () => {
  const ui = harness({ navigation: true, storage: { "elipro-history": JSON.stringify({ hours: 168, metric: "load" }) } });
  const data = snapshot();
  data.events = [
    { id: 2, kind: "ups_fault", timestamp: new Date().toISOString(), message: "Falla del UPS" },
    { id: 1, kind: "connection_restored", timestamp: new Date().toISOString(), message: "USB recuperado" },
  ];
  ui.calls[0].respond(data);
  await flush();
  assert.deepEqual(ui.historyCalls, ["/api/history?hours=168"]);
  assert.equal(ui.get("shared-history").hidden, false);
  assert.equal(ui.get("shared-history").inert, false);
  assert.equal(ui.get("page-history").hidden, true);
  ui.get("events-filter").value = "power";
  ui.get("events-filter").emit("change");
  assert.equal(ui.get("event-count").textContent, "1 / 2");
  assert.equal(ui.get("events-list").children[0].children[1].children[0].textContent, "Falla del UPS");
  const chart = ui.get("history-chart");
  ui.open("history");
  assert.equal(ui.get("page-history").hidden, false);
  assert.equal(ui.get("shared-history").hidden, false);
  assert.match(ui.calls.at(-1).url, /^\/api\/events\/search\?/);
  ui.open("overview");
  assert.equal(ui.get("history-chart"), chart);
  assert.equal(ui.get("events-filter").value, "power");
  assert.equal(ui.get("event-count").textContent, "1 / 2");
  assert.equal(ui.get("history-period").value, "168");
  assert.equal(ui.metrics[2].getAttribute("aria-pressed"), "true");
  for (const view of ["incidents", "protection", "notifications", "system"]) {
    ui.open(view);
    assert.equal(ui.get("shared-history").hidden, true);
    assert.equal(ui.get("shared-history").inert, true);
    assert.equal(ui.get("shared-history").getAttribute("aria-hidden"), "true");
  }
  ui.get("theme-toggle").emit("click");
  assert.deepEqual(ui.historyCalls, ["/api/history?hours=168"]);
  assert.equal(ui.intervals.filter((item) => item.ms === 30000).length, 1);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "the shared surface must not duplicate any IDs");
  assert.doesNotMatch(html, /summary-history-spark|summary-health|summary-last-incident|details-row/);
});

for (const outcome of ["failure", "success"]) {
  test(`an obsolete history ${outcome} cannot replace the newly selected period`, async () => {
    const ui = harness({ navigation: true, deferredHistory: true });
    ui.calls[0].respond(snapshot());
    const obsolete = ui.pendingHistory[0];
    ui.get("history-period").value = "0.25";
    ui.get("history-period").emit("change");
    assert.equal(ui.pendingHistory.length, 1, "one history reader at a time");
    const samples = (voltage) => [{ timestamp: new Date(Date.now() - 10000).toISOString(), input_voltage: voltage }];
    if (outcome === "failure") obsolete.reject(new TypeError("old query failed"));
    else obsolete.respond({ samples: samples(100) });
    await flush();
    const current = ui.pendingHistory.at(-1);
    assert.notEqual(current, obsolete);
    assert.equal(current.url, "/api/history?hours=0.25");
    assert.equal(ui.get("history-notice").hidden, true);
    assert.equal(ui.get("history-chart").children.some((element) => element.getAttribute("d")), false);
    current.respond({ samples: samples(220) });
    await flush();
    assert.equal(ui.get("history-chart").children.some((element) => element.getAttribute("d")), true);
    assert.equal(ui.pendingHistory.filter((request) => !request.settled).length, 0);
    assert.equal(ui.historyCalls.length, 2, "no mini chart requests follow a response");
    assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  });
}

test("connection point keeps an accessible state for fresh, stale and disconnected UPS data", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.sample = liveSample();
  for (const [state, label] of [["connected", "UPS conectado"], ["stale", "Datos desactualizados"], ["disconnected", "Sin conexión al UPS"]]) {
    if (state !== "connected") ui.poll();
    data.connection.state = state;
    ui.calls.at(-1).respond(data);
    await flush();
    assert.equal(ui.get("connection-badge").className, `connection-indicator ${state}`);
    assert.equal(ui.get("connection-badge").title, label);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, label);
  }
  assert.equal(ui.get("connection-badge").getAttribute("role"), "status");
  const model = html.match(/<div class="diagram-model">([\s\S]*?)<\/div>/)?.[1];
  assert.ok(model?.includes('id="connection-badge"') && model.includes("FORZA NT-2402PRO"));
  assert.match(html, /class="sr-only">Conectando/);
});

test("battery charge is primary, estimates are labelled and voltage stays secondary", async () => {
  const ui = harness();
  const data = snapshot();
  data.sample = liveSample({ battery_percent: 83, battery_percent_estimate: 40, battery_estimate_method: "fixture_v1", battery_voltage: 27.41 });
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("battery-percent").textContent, "83");
  assert.equal(ui.get("battery-unit").textContent, "%");
  assert.equal(ui.get("battery-voltage").textContent, "27.41");
  assert.equal(ui.get("battery-note").textContent, "Carga reportada");
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample.battery_percent = null;
  data.sample.on_battery = true;
  ui.poll(); ui.calls.at(-1).respond(data); await flush();
  assert.equal(ui.get("battery-percent").textContent, "40");
  assert.equal(ui.get("battery-note").textContent, "Carga estimada");
  assert.match(ui.get("battery-note").title, /fixture_v1/);
  assert.equal(ui.get("energy-diagram").dataset.mode, "battery");
  assert.equal(ui.get("battery-bar").style.height, "40%");
  data.sample.low_battery = true;
  ui.poll(); ui.calls.at(-1).respond(data); await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(textOf(ui.get("critical-list")), /Batería baja/);
});

test("invalid or unproven percentages stay unavailable and an estimate cannot fabricate an alarm", async () => {
  const ui = harness();
  const data = snapshot();
  const cases = [
    { battery_percent: -1, battery_percent_estimate: 60 },
    { battery_percent: 101, battery_percent_estimate: NaN, battery_estimate_method: "fixture_v1" },
    { battery_percent: true, battery_percent_estimate: 70, battery_estimate_method: "" },
    { battery_percent: null, battery_percent_estimate: 0, battery_estimate_method: "fixture_v1" },
    { battery_percent: 0 },
  ];
  for (const [index, value] of cases.entries()) {
    if (index) ui.poll();
    data.sample = liveSample({ battery_voltage: 25, ...value });
    ui.calls.at(-1).respond(data); await flush();
    assert.equal(ui.get("battery-percent").textContent, index < 3 ? "—" : "0");
    assert.equal(ui.get("battery-note").textContent, index < 3 ? "No disponible" : index === 3 ? "Carga estimada" : "Carga reportada");
    assert.equal(ui.get("battery-track").dataset.available, String(index >= 3));
    assert.equal(ui.get("critical-dialog").open, index === 4);
  }
  ui.get("critical-ack").emit("click");
  data.sample = liveSample({ battery_voltage: 25, battery_percent_estimate: 5, battery_estimate_method: "fixture_v1" });
  ui.poll(); ui.calls.at(-1).respond(data); await flush();
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("policy-battery-field").hidden, true, "an estimate does not enable the native power-policy threshold");
  assert.equal(ui.get("protection-alert").hidden, true);
});

test("battery history separates reported and estimated charge, breaks method changes and never backfills volts", async () => {
  const now = Date.now();
  const sample = (seconds, values) => ({ timestamp: new Date(now - seconds * 1000).toISOString(), ...values });
  const ui = harness({ navigation: true, storage: { "elipro-history": JSON.stringify({ hours: 1, metric: "battery" }) }, samples: [
    sample(70, { battery_voltage: 27.4 }),
    sample(60, { battery_percent: 90 }),
    sample(50, { battery_percent: 89 }),
    sample(40, { battery_percent_estimate: 80, battery_estimate_method: "fixture_v1" }),
    sample(30, { battery_percent_estimate: 79, battery_estimate_method: "fixture_v1" }),
    sample(20, { battery_percent_estimate: 78, battery_estimate_method: "fixture_v2" }),
    sample(10, { battery_percent_estimate: 77, battery_estimate_method: "fixture_v2", gap_before: true }),
  ] });
  ui.calls[0].respond(snapshot()); await flush();
  const paths = ui.get("history-chart").children.filter((item) => item.getAttribute("d"));
  assert.equal(paths.length, 2);
  assert.equal(paths[0].getAttribute("stroke-dasharray"), null);
  assert.equal((paths[0].getAttribute("d").match(/M/g) || []).length, 1);
  assert.equal((paths[0].getAttribute("d").match(/L/g) || []).length, 1);
  assert.equal(paths[1].getAttribute("stroke-dasharray"), "6 4");
  assert.equal((paths[1].getAttribute("d").match(/M/g) || []).length, 3);
  assert.equal((paths[1].getAttribute("d").match(/L/g) || []).length, 1);
  const legend = ui.get("chart-legend").children;
  assert.equal(legend[0].children[1].textContent, "Carga reportada");
  assert.equal(legend[1].children[1].textContent, "Carga estimada");
  assert.equal(legend[2].textContent, "PORCENTAJE");
});

test("battery voltage fallback is selected only from the visible period", async () => {
  const now = Date.now();
  const ui = harness({ navigation: true, storage: { "elipro-history": JSON.stringify({ hours: 0.25, metric: "battery" }) }, samples: [
    { timestamp: new Date(now - 3600000).toISOString(), battery_percent: 90 },
    { timestamp: new Date(now - 20000).toISOString(), battery_voltage: 27.4 },
    { timestamp: new Date(now - 10000).toISOString(), battery_voltage: 27.3 },
  ] });
  ui.calls[0].respond(snapshot()); await flush();
  assert.equal(ui.get("chart-legend").children[0].children[1].textContent, "Voltaje de batería");
  assert.equal(ui.get("chart-legend").children.at(-1).textContent, "VOLTIOS");
  assert.match(ui.get("history-chart").getAttribute("aria-label"), /voltaje de batería/);
  assert.ok(ui.historyCalls.every((url) => url === "/api/history?hours=0.25"));
  ui.metrics[2].emit("click");
  assert.equal(ui.metrics[2].getAttribute("aria-pressed"), "true");
  assert.equal(JSON.parse(ui.storedPreferences.get("elipro-history")).metric, "load");
});

test("the shared chart uses its mobile width for readable axes and correctly positioned tooltips", async () => {
  const ui = harness({ navigation: true, samples: [{ timestamp: new Date(Date.now() - 10000).toISOString(), input_voltage: 220 }] });
  ui.calls[0].respond(snapshot()); await flush();
  const chart = ui.get("history-chart");
  chart.clientWidth = 320;
  chart.getBoundingClientRect = () => ({ left: 0, width: 320 });
  ui.get("chart-tooltip").offsetWidth = 100;
  ui.metrics[0].emit("click");
  assert.equal(chart.getAttribute("viewBox"), "0 0 320 230");
  chart.emit("pointermove", { clientX: 305 });
  assert.equal(ui.get("chart-tooltip").hidden, false);
  assert.match(ui.get("chart-tooltip").textContent, /Entrada: 220 V/);
  assert.doesNotMatch(ui.get("chart-tooltip").style.left, /NaN/);
  chart.emit("pointerleave");
  assert.equal(ui.get("chart-tooltip").hidden, true);
});

test("energy flow follows fresh line, AVR, battery and fault telemetry without claiming unknown modes", async () => {
  const { ui, data } = await currentUi();
  const diagram = ui.get("energy-diagram");
  assert.equal(diagram.dataset.mode, "line");
  assert.equal(diagram.dataset.quality, "live");
  for (const [changes, mode] of [
    [{ on_battery: false, raw_status: "AVR mode" }, "line"],
    [{ on_battery: true, raw_status: "Battery mode", input_voltage: 0 }, "battery"],
    [{ on_battery: null, raw_status: "Fault mode", fault: true }, "fault"],
    [{ on_battery: null, raw_status: "Unrecognized mode", fault: false }, "unknown"],
  ]) {
    data.sample = liveSample(changes, ui.now());
    await pushStatus(ui, data);
    assert.equal(diagram.dataset.mode, mode);
    assert.equal(diagram.dataset.quality, "live");
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  assert.doesNotMatch(html, /class="power-card\b|id="battery-tag"/);
});

test("diagram quality distinguishes unconfirmed, stale and disconnected readings and recovers", async () => {
  const ui = harness();
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.equal(ui.get("energy-diagram").dataset.quality, "starting");
  const data = snapshot();
  data.sample = liveSample({}, ui.now());
  ui.calls[0].respond(data); await flush();
  for (const state of ["stale", "disconnected"]) {
    data.connection.state = state;
    await pushStatus(ui, data);
    assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
    assert.equal(ui.get("energy-diagram").dataset.quality, state);
    assert.equal(ui.get("input-voltage").textContent, "—");
  }
  data.connection.state = "connected";
  data.sample = null;
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.notEqual(ui.get("energy-diagram").dataset.quality, "live");
  for (const timestamp of [undefined, "not-a-date", new Date(ui.now() + 5001).toISOString()]) {
    data.sample = liveSample({ timestamp }, ui.now());
    await pushStatus(ui, data);
    assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
    assert.notEqual(ui.get("energy-diagram").dataset.quality, "live");
  }
  data.sample = liveSample({}, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.mode, "line");
  assert.equal(ui.get("energy-diagram").dataset.quality, "live");
});

test("a displayed sample expires after fifteen seconds even without another fetch", async () => {
  const { ui, data } = await currentUi({ battery_percent: 80 });
  const fetches = ui.calls.length;
  ui.advance(15000); ui.tick();
  assert.equal(ui.get("energy-diagram").dataset.quality, "live");
  ui.advance(1); ui.tick();
  assert.equal(ui.calls.length, fetches);
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.equal(ui.get("energy-diagram").dataset.quality, "stale");
  assert.equal(ui.get("battery-percent").textContent, "—");
  assert.equal(ui.get("load-percent").textContent, "—");
  data.sample = liveSample({ battery_percent: 79 }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.quality, "live");
  assert.equal(ui.get("battery-percent").textContent, "79");
});

test("vertical load meter preserves zero, caps the graphic at one hundred and exposes unavailable values", async () => {
  const { ui, data } = await currentUi({ load_percent: 0 });
  assert.equal(ui.get("load-meter").getAttribute("role"), "meter");
  for (const [load, height, label] of [[0, "0%", "0"], [100, "100%", "100"], [130, "100%", "130"], [null, "0%", "—"]]) {
    data.sample = liveSample({ load_percent: load }, ui.now());
    await pushStatus(ui, data);
    assert.equal(ui.get("load-bar").style.height, height);
    assert.equal(ui.get("load-percent").textContent, label);
    if (load === null) assert.equal(ui.get("load-meter").getAttribute("aria-valuenow"), null);
    else assert.equal(ui.get("load-meter").getAttribute("aria-valuenow"), String(Math.min(load, 100)));
  }
});

test("vertical battery meter accepts reported and estimated zero without converting estimates into alarms", async () => {
  const { ui, data } = await currentUi({ battery_percent_estimate: 0, battery_estimate_method: "fixture-v1" });
  assert.equal(ui.get("battery-percent").textContent, "0");
  assert.equal(ui.get("battery-bar").style.height, "0%");
  assert.equal(ui.get("battery-note").textContent, "Carga estimada");
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample = liveSample({ battery_percent: 0 }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("battery-percent").textContent, "0");
  assert.equal(ui.get("battery-bar").style.height, "0%");
  assert.equal(ui.get("battery-note").textContent, "Carga reportada");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children[0].dataset.kind, "battery_low");
});

test("critical dialog opens on initial current alarms and orders fault, overload and real low battery", async () => {
  const { ui } = await currentUi({ fault: true, load_percent: 130, low_battery: true });
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-dialog").modalShows, 1);
  assert.equal(ui.get("critical-title").textContent, "Falla del UPS");
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.kind), ["ups_fault", "overload", "battery_low"]);
  assert.ok(ui.get("critical-list").children.every((row) => row.dataset.state === "active"));
  const tag = html.match(/<dialog\b[^>]*id="critical-dialog"[^>]*>/)?.[0];
  assert.ok(tag);
  assert.match(tag, /role="alertdialog"/);
  assert.match(tag, /aria-labelledby="critical-title"/);
  assert.match(tag, /aria-describedby="critical-description"/);
});

test("historical critical events and stale alarm flags never open a current alarm dialog", async () => {
  const ui = harness();
  const data = snapshot();
  data.sample = liveSample({}, ui.now());
  data.events = [{ id: 1, kind: "ups_fault", timestamp: new Date(ui.now() - 60000).toISOString(), message: "Falla anterior" }];
  ui.calls[0].respond(data); await flush();
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample = liveSample({ timestamp: new Date(ui.now() - 15001).toISOString(), fault: true, low_battery: true, load_percent: 130 }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample = liveSample({ timestamp: undefined, fault: true }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("acknowledged alarms do not reopen on polls or navigation but a confirmed new episode does", async () => {
  const { ui, data } = await currentUi({ fault: true }, { navigation: true });
  const dialog = ui.get("critical-dialog");
  const originalRow = ui.get("critical-list").children[0];
  ui.get("critical-ack").focus();
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-list").children[0], originalRow, "polling must preserve existing dialog nodes");
  assert.equal(ui.document.activeElement, ui.get("critical-ack"));
  assert.equal(dialog.modalShows, 1);
  ui.get("critical-ack").emit("click");
  assert.equal(dialog.open, false);
  for (const view of ["history", "overview", "protection", "notifications"]) ui.open(view);
  await pushStatus(ui, data);
  assert.equal(dialog.open, false);
  assert.equal(dialog.modalShows, 1);
  data.sample.fault = false;
  await pushStatus(ui, data);
  data.sample.fault = true;
  await pushStatus(ui, data);
  assert.equal(dialog.open, true);
  assert.equal(dialog.modalShows, 2);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("a different new alarm opens independently while another active condition remains acknowledged", async () => {
  const { ui, data } = await currentUi({ fault: true });
  ui.get("critical-ack").emit("click");
  data.sample.load_percent = 100;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Sobrecarga del UPS");
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.kind), ["overload"]);
});

test("a missing load reading cannot rearm an acknowledged overload episode", async () => {
  const { ui, data } = await currentUi({ load_percent: 120 });
  ui.get("critical-ack").emit("click");
  data.sample.load_percent = null;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample.load_percent = 120;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample.load_percent = 20;
  await pushStatus(ui, data);
  data.sample.load_percent = 120;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Sobrecarga del UPS");
});

test("a pending overload becomes unconfirmed rather than resolved when its load reading disappears", async () => {
  const { ui, data } = await currentUi({ load_percent: 120 });
  const row = ui.get("critical-list").children[0];
  data.sample.load_percent = null;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Estado sin confirmar");
  assert.equal(row.dataset.state, "unconfirmed");
  assert.match(textOf(row), /Sin confirmar/);
  data.sample.load_percent = 120;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-list").children[0], row);
  assert.equal(row.dataset.state, "active");
});

test("recovery before acknowledgment stays visible as resolved until the user acknowledges it", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  const row = ui.get("critical-list").children[0];
  data.sample.low_battery = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Alerta resuelta");
  assert.equal(ui.get("critical-list").children[0], row);
  assert.equal(row.dataset.state, "resolved");
  assert.match(textOf(row), /Resuelta/i);
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("lost communication marks pending alarms unconfirmed and cannot reset acknowledgment", async () => {
  const { ui, data } = await currentUi({ fault: true });
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Estado sin confirmar");
  assert.equal(ui.get("critical-list").children[0].dataset.state, "unconfirmed");
  ui.get("critical-ack").emit("click");
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false, "uncertainty is not a confirmed clear of the old incident");
  data.sample.fault = false;
  await pushStatus(ui, data);
  data.sample.fault = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
});

test("Escape acknowledges the dialog and restores focus without posting a power action", async () => {
  const ui = harness();
  ui.get("theme-toggle").focus();
  const data = snapshot();
  data.sample = liveSample({ fault: true }, ui.now());
  ui.calls[0].respond(data); await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  ui.get("critical-dialog").emit("cancel");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.document.activeElement, ui.get("theme-toggle"));
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("critical countdown cancellation uses the confirmed endpoint and does not acknowledge real alarms", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString(), reason: "Batería baja" };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown").hidden, false);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.equal(ui.get("critical-cancel").disabled, false);
  const cancellation = ui.get("critical-cancel").emit("click");
  const request = ui.calls.at(-1);
  assert.equal(request.url, "/api/protection/cancel");
  assert.equal(request.options.headers["X-UPS-Token"], "test-token");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-cancel").disabled, true);
  const callsWhilePending = ui.calls.length;
  ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.length, callsWhilePending, "a second click cannot send another cancellation while pending");
  request.respond({ ...data, protection: { state: "cancelled", simulation: false } });
  await cancellation;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children[0].dataset.state, "active");
  const posts = ui.calls.filter((call) => call.options.method === "POST").length;
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, posts);
});

test("uncertain countdown keeps its last deadline in the dialog while disabling cancellation", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  ui.advance(1000); ui.tick();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown-time").textContent, "00:59");
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.get("cancel-shutdown").disabled, true);
});

test("acknowledgment closes only the physical alert and leaves the confirmed countdown running", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, false);
  assert.equal(ui.get("countdown").textContent, "01:00");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("an unconfirmed modal cancellation stays visible and does not acknowledge the physical condition", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  const cancellation = ui.get("critical-cancel").emit("click");
  ui.calls.at(-1).respond(data);
  await cancellation;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-feedback").hidden, false);
  assert.match(ui.get("critical-feedback").textContent, /No se confirmó la cancelación/);
  assert.equal(ui.get("critical-cancel").disabled, false);
  assert.equal(ui.get("critical-list").children[0].dataset.state, "active");
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
});

test("a refreshed deadline in the same active countdown cannot erase an unconfirmed cancellation error", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  const cancellation = ui.get("critical-cancel").emit("click");
  ui.calls.at(-1).respond(data);
  await cancellation;
  const feedback = ui.get("critical-feedback").textContent;
  assert.equal(ui.get("critical-feedback").hidden, false);
  assert.match(feedback, /No se confirmó la cancelación/);
  data.protection = { ...data.protection, deadline: new Date(ui.now() + 61000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-feedback").hidden, false);
  assert.equal(ui.get("critical-feedback").textContent, feedback);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:01");
});

test("a new countdown clears feedback about an earlier confirmed cancellation in the still-open dialog", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  const cancellation = ui.get("critical-cancel").emit("click");
  ui.calls.at(-1).respond({ ...data, protection: { state: "cancelled", simulation: false } });
  await cancellation;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-feedback").hidden, false);
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 120000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown-time").textContent, "02:00");
  assert.equal(ui.get("critical-feedback").hidden, true);
});

test("theme transitions run only for manual changes and honor reduced motion", () => {
  const ui = harness({ storage: { "elipro-theme": "light" } });
  assert.equal(ui.rootElement.classList.contains("theme-transitioning"), false);
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.rootElement.classList.contains("theme-transitioning"), true);
  ui.advance(350); ui.runTimeouts();
  assert.equal(ui.rootElement.classList.contains("theme-transitioning"), false);
  const reduced = harness({ reducedMotion: true });
  reduced.get("theme-toggle").emit("click");
  assert.equal(reduced.rootElement.dataset.theme, "light");
  assert.equal(reduced.rootElement.classList.contains("theme-transitioning"), false);
  const styles = fs.readFileSync(path.join(root, "static/style.css"), "utf8");
  assert.ok(/\.theme-toggle\s*\{[^}]*width:\s*100%/s.test(styles), "the theme toggle must fill the sidebar control width");
  assert.match(styles, /prefers-reduced-motion:\s*reduce/);
});
