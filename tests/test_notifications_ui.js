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
    policy: { enabled: false, countdown_seconds: 60, action: "shutdown" },
    protection: { state: "idle" },
    webhooks: {
      settings: { webhook_enabled: false, webhook_url: "", webhook_events: ["power_lost", "battery_low"] },
      dry_run: false,
      test: null,
      deliveries: [],
    },
    notifications: {
      settings: { enabled: true, sound_enabled: true, repeat_seconds: 30, ...settings },
      delivery: { available: true, ready: true, channel: "windows", last_error: null, last_delivered_at: null },
      last_error: null,
    },
  };
}

let countdownFixtureSerial = 0;

// Modern service fixtures include the identity and freshness required to cancel
// a real episode. Explicit null values exercise legacy and malformed services.
function modernCountdownFixture(data, url, options) {
  if (data?.protection?.state === "countdown" && data.protection.simulation !== true) {
    if (!("countdown_id" in data.protection)) data.protection.countdown_id = (++countdownFixtureSerial).toString(16).padStart(32, "0");
    if (!("valid_for_ms" in data.protection)) data.protection.valid_for_ms = 3000;
  }
  if (url === "/api/protection/countdown/cancel" && data?.protection && data.protection.state !== "countdown"
      && !("cancelled_countdown_id" in data)) data.cancelled_countdown_id = JSON.parse(options.body).countdown_id;
  return data;
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
  const policyFields = ["policy-low-battery", "policy-runtime", "policy-countdown", "policy-action", "policy-ups-poweroff", "policy-ups-delay"];
  get("policy-form").fields = policyFields.map(get);
  get("notifications-form").fields = ["notifications-enabled", "notifications-sound", "notifications-repeat", "notifications-persistent", "notification-theme"].map(get);
  get("webhooks-form").fields = ["webhook-enabled", "webhook-url", ...["power_lost", "power_restored", "battery_low", "overload", "ups_fault"].map((event) => "webhook-event-" + event)].map(get);
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
  const views = ["overview", "protection", "history", "system", "webhooks"];
  const nav = historyOptions.navigation ? views.map((view) => Object.assign(new Element(), { dataset: { view } })) : [];
  const pages = historyOptions.navigation ? views.map((view) => Object.assign(get(`page-${view}`), { dataset: { page: view } })) : [];
  const markupShortcuts = [...html.matchAll(/<a\b[^>]*\bdata-go-view="([^"]+)"[^>]*>/gi)].map((match) => {
    const element = Object.assign(new Element(), { dataset: { goView: match[1] } });
    for (const attribute of match[0].matchAll(/\s([\w:-]+)="([^"]*)"/g)) element.setAttribute(attribute[1], attribute[2]);
    return element;
  });
  const shortcuts = historyOptions.navigation ? [
    ...views.map((view) => Object.assign(new Element(), { dataset: { goView: view } })), ...markupShortcuts,
  ] : [];
  const metrics = ["voltage", "battery", "load"].map((metric) => Object.assign(new Element(), { dataset: { metric } }));
  const windowListeners = {};
  const navigationScrolls = [];
  const location = { hash: historyOptions.hash || "" };
  documentRef = {
      body,
      documentElement: rootElement,
      readyState: historyOptions.loading ? "loading" : "complete",
      addEventListener: (name, callback) => { documentListeners[name] = callback; },
      getElementById: (id) => domReady ? elements.get(id) ?? null : null,
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
    performance: { now: () => now },
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
        respond: (data, ok = true) => resolve({ ok, status: ok ? 200 : 400, json: async () => modernCountdownFixture(data, url, options) }),
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
  assert.equal(ui.get("notifications-state-label").textContent, "Listos");
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
  assert.equal(ui.get("test-notifications-label").textContent, "Probar aviso");
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
  assert.equal(ui.get("test-notifications-label").textContent, "Probar aviso");
});

test("notification theme drafts survive polling and save independently of the panel theme", async () => {
  const ui = await connected({ theme: "auto" });
  assert.equal(ui.get("notification-theme").value, "auto");
  assert.equal(ui.get("notification-theme").disabled, false);
  ui.get("notification-theme").value = "light";
  ui.get("notifications-form").emit("input");
  ui.poll();
  ui.calls.at(-1).respond(snapshot({ theme: "dark" }));
  await flush();
  assert.equal(ui.get("notification-theme").value, "light");
  const pending = ui.get("notifications-form").emit("submit");
  assert.equal(ui.get("notification-theme").disabled, true);
  const save = ui.calls.at(-1);
  assert.equal(save.url, "/api/notifications");
  assert.equal(JSON.parse(save.options.body).theme, "light");
  save.respond(snapshot({ theme: "light" }));
  await pending;
  assert.equal(ui.get("notification-theme").value, "light");
  assert.equal(ui.get("notification-theme").disabled, false);
  assert.equal(ui.get("notifications-feedback").textContent, "Avisos guardados");
  assert.equal(ui.storageWrites.length, 0, "the saved notification preference must not change the panel theme");
});

test("notification theme stays disabled on older monitors and keeps a draft after reconnect", async () => {
  const legacy = await connected();
  assert.equal(legacy.get("notification-theme").disabled, true);
  const ui = await connected({ theme: "dark" });
  ui.get("notification-theme").value = "light";
  ui.get("notifications-form").emit("input");
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  assert.equal(ui.get("notification-theme").disabled, true);
  ui.poll();
  ui.calls.at(-1).respond(snapshot({ theme: "dark" }));
  await flush();
  assert.equal(ui.get("notification-theme").disabled, false);
  assert.equal(ui.get("notification-theme").value, "light");
});

test("saving notices preserves unsaved protection and saving protection preserves unsaved notices", async () => {
  const ui = await connected();
  ui.get("policy-countdown").value = 90;
  ui.get("policy-form").emit("input");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  let pending = ui.get("notifications-form").emit("submit");
  ui.calls.at(-1).respond(snapshot({ sound_enabled: false }));
  await pending;
  assert.equal(ui.get("policy-countdown").value, 90);
  ui.get("notifications-repeat").value = 60;
  ui.get("notifications-form").emit("input");
  pending = ui.get("policy-form").emit("submit");
  const data = snapshot({ sound_enabled: false });
  data.policy.countdown_seconds = 90;
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
  assert.equal(ui.get("notifications-state-label").textContent, "Sin conexión");
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
  assert.equal(ui.get("notifications-state-label").textContent, "No disponible");
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
  assert.equal(ui.get("notifications-state-label").textContent, "Listos");
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
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true };
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
  assert.equal(ui.get("notifications-state-label").textContent, "Listos");
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.equal(ui.get("health-state-label").textContent, "Revisar monitor");
  assert.equal(ui.get("health-message").hidden, false);
  assert.match(ui.get("health-message").textContent, /No se puede guardar el historial/);
  assert.match(ui.get("chart-note").lastChild.textContent, /datos mostrados pueden estar incompletos/);
});

test("recovery clears the storage warning but keeps lost history visible", async () => {
  const ui = harness();
  const data = snapshot();
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true };
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
  assert.equal(ui.get("health-state-label").textContent, "Revisar monitor");
  assert.match(ui.get("health-message").textContent, /Se perdieron 5 registros/);
  ui.poll();
  data.history.dropped = 0;
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("history-notice").hidden, true);
  assert.equal(ui.get("health-state-label").textContent, "Operativo");
  assert.equal(ui.get("health-message").hidden, true);
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

test("compact health shows its three metrics and becomes unconfirmed on API loss", async () => {
  const ui = harness();
  const data = snapshot();
  data.health = { state: "healthy", message: "", uptime_seconds: 3662,
    reader_alive: true, supervisor_alive: true, read_successes: 51, read_failures: 2,
    consecutive_failures: 0, recoveries: 1, reader_restarts: 0,
    last_success: "2026-10-17T21:00:03Z", read_age_seconds: 1,
    process: { memory_bytes: 31457280, cpu_percent: 0.4 } };
  data.history = { available: true, pending: 3, last_success_at: "2026-10-17T21:00:00Z" };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Operativo");
  assert.match(ui.get("health-resources").textContent, /30 MB/);
  assert.equal(ui.get("health-last-reading").textContent, "Hace 1 s");
  assert.equal(ui.get("health-uptime").textContent, "1 h 1 min");
  assert.equal(ui.get("health-cpu").textContent, "CPU 0.4 %");
  assert.equal(ui.get("health-message").hidden, true);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Sin confirmar");
  assert.equal(ui.get("health-last-reading").textContent, "—");
  assert.equal(ui.get("health-message").hidden, false);
});

test("a stopped supervisor never appears as a healthy supervised monitor", async () => {
  const ui = harness();
  const data = snapshot();
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: false };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Revisar monitor");
  assert.equal(ui.get("health-message").hidden, false);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
});

const recordRows = (ui) => ui.get("records-body").children.filter((row) => row.children.length === 6);
const recordDetails = (ui) => ui.get("records-body").children.filter((row) => row.children.length === 1 && row.children[0].colSpan === 6);
const queryParams = (call) => new URLSearchParams(call.url.split("?")[1]);
const recordCalls = (ui, prefix = "/api/records?") => ui.calls.filter((call) => call.url.startsWith(prefix));
const lastRecordCall = (ui, prefix = "/api/records?") => recordCalls(ui, prefix).at(-1);
const emptyRecords = () => ({ records: [], total: 0, next_cursor: null });
const incidentRecord = (changes = {}) => ({
  id: "power:1", record_type: "incident", type: "power", category: "power",
  timestamp: "2026-10-17T15:00:00Z", started_at: "2026-10-17T15:00:00Z",
  ended_at: "2026-10-17T15:00:10Z", start_known: true, status: "recovered",
  duration_seconds: 10, action_result: "none", description: "Corte registrado",
  message: "Corte registrado", recovery_message: "Electricidad recuperada", actions: [], events: [], ...changes,
});
const eventRecord = (changes = {}) => ({
  id: "event:2", record_type: "event", type: "battery_low", category: "alerts",
  timestamp: "2026-10-17T15:00:00Z", description: "Batería baja registrada",
  message: "Batería baja registrada", status: null, duration_seconds: null,
  events: [{ id: 2, kind: "battery_low", timestamp: "2026-10-17T15:00:00Z", message: "Batería baja registrada" }],
  ...changes,
});
function answerRecords(ui, data = emptyRecords()) {
  lastRecordCall(ui).respond(data);
}
async function enterHistory(ui, data = emptyRecords()) {
  ui.open("history");
  answerRecords(ui, data);
  await flush();
}

test("one Registro request applies dates, category and message without changing chart period or protection", async () => {
  const ui = await connected();
  ui.get("history-period").value = "168";
  ui.get("records-from").value = "2026-10-17T10:00";
  ui.get("records-to").value = "2026-10-17T12:00";
  ui.get("event-kind").value = "alerts";
  ui.get("event-search").value = " <dato> literal ";
  const before = ui.calls.length, chartRequests = ui.historyCalls.length;
  const pending = ui.get("records-form").emit("submit");
  const call = lastRecordCall(ui), params = queryParams(call);
  assert.equal(ui.calls.length, before + 1);
  assert.equal(params.get("from"), new Date("2026-10-17T10:00").toISOString());
  assert.equal(params.get("to"), new Date("2026-10-17T12:00").toISOString());
  assert.equal(params.get("q"), "<dato> literal");
  assert.equal(params.get("kind"), "alerts");
  assert.equal(params.has("cursor"), false);
  assert.equal(params.has("sort"), false);
  assert.equal(params.has("direction"), false);
  assert.equal(ui.get("records-list").getAttribute("aria-busy"), "true");
  call.respond({ records: [
    incidentRecord({ message: "<dato> literal", description: "<dato> literal" }),
    eventRecord({ message: "<dato> literal", description: "<dato> literal" }),
  ], total: 2, next_cursor: null });
  await pending;
  const rows = recordRows(ui), details = recordDetails(ui);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].children.length, 6);
  assert.equal(rows[0].children[1].textContent, "Corte eléctrico");
  assert.match(textOf(rows[0].children[2]), /<dato> literal/);
  assert.equal(rows[0].children[3].textContent, "Recuperado");
  assert.equal(rows[0].children[4].textContent, "10 s");
  assert.equal(rows[1].children[1].textContent, "Batería baja");
  assert.match(textOf(rows[1].children[2]), /<dato> literal/);
  assert.doesNotMatch(rows[1].children[3].textContent, /Recuperado|Sin recuperación/);
  assert.equal(details.length, 2);
  assert.equal(ui.get("records-list").getAttribute("aria-busy"), "false");
  assert.match(ui.get("records-count").textContent, /2/);
  assert.equal(ui.get("history-period").value, "168");
  assert.equal(ui.historyCalls.length, chartRequests);
  assert.equal(ui.get("policy-enabled").checked, false);
  assert.equal(ui.calls.some((entry) => entry.options.method === "POST"), false);
  assert.equal(ui.calls.some((entry) => /\/api\/incidents|\/api\/events\/search/.test(entry.url)), false);
});

test("Registro failures remain local and can recover without disabling live UPS or notices", async () => {
  const ui = await connected();
  const pending = ui.get("records-form").emit("submit");
  lastRecordCall(ui).respond({ error: "Historial ocupado" }, false);
  await pending;
  assert.match(ui.get("records-error").textContent, /Historial ocupado/);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, false);
  const retry = ui.get("records-form").emit("submit");
  answerRecords(ui);
  await retry;
  assert.equal(ui.get("records-error").hidden, true);
  assert.equal(recordRows(ui).length, 0);
  assert.match(textOf(ui.get("records-body")), /No hay|Sin registros/);
});

test("open historical incidents stay historical and independent events do not invent an incident state", async () => {
  const ui = await connected();
  const pending = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [
    incidentRecord({ status: "open", ended_at: null, duration_seconds: null }),
    incidentRecord({ id: "communication:3", type: "communication", category: "connection",
      status: "open", ended_at: null, duration_seconds: null }),
    eventRecord(),
  ], total: 3, next_cursor: null });
  await pending;
  assert.equal(recordRows(ui).length, 3);
  for (const detail of recordDetails(ui).slice(0, 2)) {
    assert.match(textOf(detail), /no confirma.*problema continúe/);
    assert.match(textOf(detail), /Consulta Resumen/);
  }
  assert.doesNotMatch(recordRows(ui)[2].children[3].textContent, /Recuperado|Sin recuperación/);
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("mixed records paginate with one cursor, deduplicate record IDs and keep applied filters despite drafts", async () => {
  const ui = await connected();
  ui.get("records-from").value = "2026-10-16T08:00";
  ui.get("event-kind").value = "protection";
  ui.get("event-search").value = "cancelada";
  const first = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [incidentRecord()], total: 3, next_cursor: "records-page-2" });
  await first;
  ui.get("records-from").value = "2026-10-17T09:00";
  ui.get("event-kind").value = "alerts";
  ui.get("event-search").value = "borrador";
  const more = ui.get("more-records").emit("click");
  const call = lastRecordCall(ui), params = queryParams(call);
  assert.equal(params.get("cursor"), "records-page-2");
  assert.equal(params.get("from"), new Date("2026-10-16T08:00").toISOString());
  assert.equal(params.get("kind"), "protection");
  assert.equal(params.get("q"), "cancelada");
  call.respond({ records: [incidentRecord(), eventRecord(), eventRecord({ id: "event:4" })],
    total: 3, next_cursor: null });
  await more;
  assert.equal(recordRows(ui).length, 3);
  assert.equal(recordDetails(ui).length, 3);
  assert.equal(ui.get("more-records").hidden, true);
  assert.match(ui.get("records-count").textContent, /3/);
});

for (const outcome of ["success", "error"]) {
  test("a newer unified query discards an obsolete " + outcome + " and reloads only the latest submitted filters", async () => {
    const ui = await connected();
    ui.get("records-from").value = "2026-10-15T08:00";
    const first = ui.get("records-form").emit("submit"), obsolete = lastRecordCall(ui);
    ui.get("records-from").value = "2026-10-16T08:00";
    ui.get("event-kind").value = "connection";
    await ui.get("records-form").emit("submit");
    ui.get("records-from").value = "2026-10-17T08:00";
    ui.get("event-kind").value = "settings";
    ui.get("event-search").value = "nuevo";
    await ui.get("records-form").emit("submit");
    assert.equal(recordCalls(ui).length, 1);
    if (outcome === "success") {
      obsolete.respond({ records: [eventRecord({ message: "Obsoleto", description: "Obsoleto" })], total: 1, next_cursor: "old" });
    } else {
      obsolete.respond({ error: "Error obsoleto" }, false);
    }
    await first;
    await flush();
    assert.equal(recordCalls(ui).length, 2);
    const latest = lastRecordCall(ui), params = queryParams(latest);
    assert.equal(params.get("from"), new Date("2026-10-17T08:00").toISOString());
    assert.equal(params.get("kind"), "settings");
    assert.equal(params.get("q"), "nuevo");
    assert.equal(params.has("cursor"), false);
    assert.doesNotMatch(textOf(ui.get("records-body")), /Obsoleto/);
    assert.doesNotMatch(ui.get("records-error").textContent, /Error obsoleto/);
    latest.respond({ records: [eventRecord({ message: "Nuevo", description: "Nuevo" })], total: 1, next_cursor: null });
    await flush();
    assert.match(textOf(recordRows(ui)[0].children[2]), /Nuevo/);
    assert.equal(ui.get("records-error").hidden, true);
  });
}

test("a failed changed filter cannot restore rows or pagination from the previous query", async () => {
  const ui = await connected();
  ui.get("records-from").value = "2026-10-16T08:00";
  const first = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [incidentRecord({ message: "Consulta anterior", description: "Consulta anterior" })],
    total: 2, next_cursor: "old-page" });
  await first;
  ui.get("event-kind").value = "other";
  ui.get("event-search").value = "consulta nueva";
  const next = ui.get("records-form").emit("submit"), failed = lastRecordCall(ui);
  assert.equal(queryParams(failed).has("cursor"), false);
  failed.respond({ error: "Historial ocupado" }, false);
  await next;
  assert.equal(ui.get("more-records").hidden, true);
  assert.doesNotMatch(textOf(ui.get("records-body")), /Consulta anterior/);
  const retry = ui.get("records-form").emit("submit");
  const params = queryParams(lastRecordCall(ui));
  assert.equal(params.get("from"), new Date("2026-10-16T08:00").toISOString());
  assert.equal(params.get("kind"), "other");
  assert.equal(params.get("q"), "consulta nueva");
  assert.equal(params.has("cursor"), false);
  answerRecords(ui);
  await retry;
  assert.equal(ui.get("records-error").hidden, true);
  assert.equal(ui.get("service-notice").hidden, true);
});

test("date, category and message drafts do not query until the single Registro form is submitted", async () => {
  const ui = await connected();
  const before = ui.calls.length;
  ui.get("records-from").value = "2026-10-17T08:00";
  ui.get("records-to").value = "2026-10-18T08:00";
  ui.get("event-kind").value = "power";
  ui.get("event-search").value = "corte";
  ui.get("records-form").emit("input");
  ui.get("event-kind").emit("change");
  ui.get("event-search").emit("input");
  assert.equal(ui.calls.length, before);
  const pending = ui.get("records-form").emit("submit"), params = queryParams(lastRecordCall(ui));
  assert.equal(ui.calls.length, before + 1);
  assert.equal(params.get("kind"), "power");
  assert.equal(params.get("q"), "corte");
  assert.equal(params.get("to"), new Date("2026-10-18T08:00").toISOString());
  answerRecords(ui);
  await pending;
});

test("expired Registro pagination retains visible rows and recovers through a fresh form submission", async () => {
  const ui = harness({ navigation: true });
  ui.calls[0].respond(snapshot());
  await flush();
  await enterHistory(ui, { records: [incidentRecord()], total: 2, next_cursor: "expired" });
  const pending = ui.get("more-records").emit("click");
  lastRecordCall(ui).respond({ code: "record_cursor_expired", error: "La consulta venció. Vuelve a consultar el registro." }, false);
  await pending;
  assert.equal(recordRows(ui).length, 1);
  assert.match(ui.get("records-error").textContent, /La consulta venció/);
  assert.equal(ui.get("more-records").hidden, true);
  assert.equal(ui.get("apply-records").disabled, false);
  assert.equal(ui.get("service-notice").hidden, true);
  const refresh = ui.get("records-form").emit("submit");
  assert.equal(queryParams(lastRecordCall(ui)).has("cursor"), false);
  answerRecords(ui);
  await refresh;
  assert.equal(ui.get("records-error").hidden, true);
});

test("incident details preserve literal opening, recovery and protection messages with accessible disclosure", async () => {
  const ui = harness({ navigation: true });
  ui.calls[0].respond(snapshot());
  await flush();
  const actions = [
    { id: 2, kind: "policy_countdown", timestamp: "2026-10-17T10:00:10Z", message: "<cuenta>" },
    { id: 3, kind: "policy_action_failed", timestamp: "2026-10-17T10:00:20Z", message: "<fallo>" },
  ];
  await enterHistory(ui, { records: [incidentRecord({ start_known: false,
    timestamp: "2026-10-17T10:00:00Z", started_at: "2026-10-17T10:00:00Z",
    ended_at: "2026-10-17T10:01:00Z", duration_seconds: 60, action_result: "failed",
    description: "<inicio>", message: "<inicio>", recovery_message: "<recuperado>", actions,
    events: [
      { id: 1, kind: "power_lost", timestamp: "2026-10-17T10:00:00Z", message: "<inicio>" },
      ...actions,
      { id: 4, kind: "power_restored", timestamp: "2026-10-17T10:01:00Z", message: "<recuperado>" },
    ],
  })], total: 1, next_cursor: null });
  const row = recordRows(ui)[0], detail = recordDetails(ui)[0];
  assert.match(textOf(row), /Inicio no registrado/);
  assert.equal(row.children[4].textContent, "1 min");
  assert.equal(detail.hidden, true);
  for (const message of ["<inicio>", "<recuperado>", "<cuenta>", "<fallo>"]) assert.ok(textOf(detail).includes(message), message);
  const button = row.children[5].children[0];
  assert.equal(button.getAttribute("aria-expanded"), "false");
  assert.equal(button.getAttribute("aria-controls"), detail.id);
  button.emit("click");
  assert.equal(detail.hidden, false);
  assert.equal(button.getAttribute("aria-expanded"), "true");
  button.emit("click");
  assert.equal(detail.hidden, true);
  assert.equal(button.getAttribute("aria-expanded"), "false");
});

test("frontend renders authoritative mixed rows without regrouping overlapping incident members", async () => {
  const ui = await connected();
  const action = { id: 7, kind: "policy_action_started", timestamp: "2026-10-17T15:00:04Z", message: "Hibernación solicitada" };
  const pending = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [
    incidentRecord({ actions: [action], events: [action], action_result: "started" }),
    incidentRecord({ id: "communication:3", type: "communication", category: "connection",
      actions: [action], events: [action], action_result: "started" }),
    eventRecord(),
  ], total: 3, next_cursor: null });
  await pending;
  assert.equal(recordRows(ui).length, 3);
  assert.match(textOf(recordDetails(ui)[0]), /Hibernación solicitada/);
  assert.match(textOf(recordDetails(ui)[1]), /Hibernación solicitada/);
  assert.equal(recordRows(ui)[2].children[1].textContent, "Batería baja");
});

test("invalid date drafts neither query nor replace the previously applied category, message and pagination", async () => {
  const ui = await connected();
  ui.get("records-from").value = "2026-10-16T08:00";
  ui.get("event-kind").value = "protection";
  ui.get("event-search").value = "hibernación";
  const apply = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [incidentRecord()], total: 2, next_cursor: "valid-page" });
  await apply;
  ui.get("records-from").value = "2026-10-18T10:00";
  ui.get("records-to").value = "2026-10-17T10:00";
  ui.get("event-kind").value = "alerts";
  ui.get("event-search").value = "borrador";
  const before = ui.calls.length;
  await ui.get("records-form").emit("submit");
  assert.equal(ui.calls.length, before);
  assert.match(ui.get("records-feedback").textContent, /Desde debe ser anterior/);
  const more = ui.get("more-records").emit("click"), call = lastRecordCall(ui), params = queryParams(call);
  assert.equal(params.get("from"), new Date("2026-10-16T08:00").toISOString());
  assert.equal(params.get("kind"), "protection");
  assert.equal(params.get("q"), "hibernación");
  assert.equal(params.get("cursor"), "valid-page");
  answerRecords(ui);
  await more;
  assert.equal(ui.get("service-notice").hidden, true);
});

test("Limpiar resets every Registro filter and queries afresh while preserving the chart period", async () => {
  const ui = await connected();
  ui.get("history-period").value = "168";
  const chartRequests = ui.historyCalls.length;
  ui.get("records-from").value = "2026-10-17T10:00";
  ui.get("records-to").value = "2026-10-18T10:00";
  ui.get("event-search").value = "corte";
  ui.get("event-kind").value = "power";
  const apply = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [incidentRecord()], total: 2, next_cursor: "old" });
  await apply;
  const clear = ui.get("clear-records").emit("click"), params = queryParams(lastRecordCall(ui));
  for (const name of ["from", "to", "kind", "q", "cursor"]) assert.equal(params.has(name), false, name);
  answerRecords(ui);
  await clear;
  for (const id of ["records-from", "records-to", "event-search", "event-kind"]) assert.equal(ui.get(id).value, "", id);
  assert.equal(ui.get("history-period").value, "168");
  assert.equal(ui.historyCalls.length, chartRequests);
});

test("Registro CSV export sends applied filters without pagination or newer drafts and returns the full server file", async () => {
  const ui = await connected();
  ui.get("records-from").value = "2026-10-17T10:00";
  ui.get("records-to").value = "2026-10-18T10:00";
  ui.get("event-search").value = "corte & retorno";
  ui.get("event-kind").value = "power";
  const apply = ui.get("records-form").emit("submit");
  answerRecords(ui, { records: [incidentRecord()], total: 75, next_cursor: "csv-page" });
  await apply;
  ui.get("records-from").value = "2026-10-18T11:00";
  ui.get("event-search").value = "borrador";
  ui.get("event-kind").value = "alerts";
  const pending = ui.get("export-records").emit("click"), call = ui.calls.at(-1), params = queryParams(call);
  assert.ok(call.url.startsWith("/api/records/export.csv?"));
  assert.equal(params.get("q"), "corte & retorno");
  assert.equal(params.get("kind"), "power");
  assert.equal(params.get("from"), new Date("2026-10-17T10:00").toISOString());
  assert.equal(params.get("to"), new Date("2026-10-18T10:00").toISOString());
  assert.equal(params.has("cursor"), false);
  assert.equal(params.has("limit"), false);
  assert.equal(ui.get("export-records").disabled, true);
  const file = { size: 4096, type: "text/csv", fullHistory: true };
  call.respondBlob(file);
  await pending;
  assert.equal(ui.downloads.length, 1);
  assert.equal(ui.downloads[0], file);
  assert.equal(ui.get("export-records").disabled, false);
  assert.equal(recordRows(ui).length, 1);
  assert.equal(ui.get("more-records").hidden, false);
});

test("Registro CSV failure reports locally and permits another export without affecting protection", async () => {
  const ui = await connected();
  const pending = ui.get("export-records").emit("click");
  ui.calls.at(-1).respond({ error: "No se pudo exportar" }, false);
  await pending;
  assert.match(ui.get("records-summary").textContent, /No se pudo exportar/);
  assert.equal(ui.get("export-records").disabled, false);
  assert.equal(ui.downloads.length, 0);
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("test-notifications").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, false);
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
  assert.equal(ui.get("health-state-label").textContent, "Detenido");
  assert.equal(ui.get("energy-diagram").dataset.quality, "stopped");
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
  assert.match(ui.get("service-notice").textContent, /se detuvo por tu solicitud/);
  assert.match(ui.get("health-message").textContent, /Detuviste Elipro/);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "Elipro detenido");
  assert.equal(ui.get("notifications-state-label").textContent, "Detenidos");
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
  assert.equal(ui.get("health-state-label").textContent, "Detenido");
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.get("countdown").textContent, "—");
  assert.equal(ui.get("notifications-state-label").textContent, "Detenidos");
  assert.doesNotMatch(ui.get("notifications-delivery").textContent, /Comprueba que/);
  ui.poll();
  const data = snapshot();
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Operativo");
  assert.equal(ui.get("notifications-state-label").textContent, "Listos");
  assert.equal(ui.get("service-notice").hidden, true);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("lost again"));
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Sin confirmar");
  assert.equal(ui.get("notifications-state-label").textContent, "Sin conexión");
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
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.get("health-state-label").textContent, "Sin confirmar");
  assert.equal(ui.get("notifications-state-label").textContent, "Sin conexión");
  assert.doesNotMatch(ui.get("service-notice").textContent, /se detuvo por tu solicitud/);
});

test("persistent critical notices stay visible and are configurable when supported", async () => {
  const legacy = await connected();
  assert.equal(legacy.get("notifications-persistent-option").hidden, false);
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

test("durable notices explain recovery closure and hide the obsolete persistence switch while preserving its stored setting", async () => {
  const ui = harness();
  const data = snapshot({ persistent_critical: true });
  data.notifications.pending = [];
  ui.calls[0].respond(data); await flush();
  assert.equal(ui.get("notifications-persistent-option").hidden, true);
  assert.equal(ui.get("notifications-persistent").disabled, true);
  assert.equal(ui.get("notifications-inbox-note").hidden, false);
  assert.match(ui.get("notifications-inbox-note").textContent || html, /Los avisos se cierran al confirmarse la recuperación.*incidentes se conservan en el historial/);
  ui.get("notifications-persistent").checked = false;
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  const saving = ui.get("notifications-form").emit("submit");
  assert.equal(JSON.parse(ui.calls.at(-1).options.body).persistent_critical, true);
  ui.calls.at(-1).respond(data); await saving;
  assert.equal(ui.get("notifications-persistent-option").hidden, true);
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
  assert.equal(ui.calls.some((call) => /records|incidents|events\/search/.test(call.url)), false);
  for (const name of ["protection", "overview"]) ui.open(name);
  assert.equal(ui.intervals.filter((item) => item.ms === 2000).length, 1);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("deep links and browser back restore their section without changing live settings", async () => {
  const ui = harness({ navigation: true, hash: "#avisos" });
  assert.equal(ui.get("view-title").textContent, "Sistema");
  assert.equal(ui.get("page-system").inert, false);
  ui.calls[0].respond(snapshot());
  await flush();
  ui.open("protection");
  assert.equal(ui.location.hash, "proteccion");
  assert.equal(ui.get("page-title").focused, true);
  assert.equal(ui.nav[1].getAttribute("aria-current"), "page");
  assert.equal(ui.nav[3].getAttribute("aria-current"), "false");
  ui.backTo("#avisos");
  assert.equal(ui.get("view-title").textContent, "Sistema");
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
  ui.get("policy-countdown").value = 75;
  ui.get("policy-form").emit("input");
  ui.open("system");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  ui.open("overview");
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  ui.open("protection");
  assert.equal(ui.get("policy-countdown").value, 75);
  assert.equal(ui.get("settings-feedback").textContent, "Cambios sin guardar");
  ui.open("system");
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.get("notifications-feedback").textContent, "Cambios sin guardar");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("the protection switch outside the form preserves unsaved changes through navigation and saves through its associated form", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.policy = { ...data.policy, enabled: false, action: "hibernate", countdown_seconds: 10,
    low_battery_percent: 20, ups_poweroff: true, ups_poweroff_delay_seconds: 60 };
  data.ups_poweroff = observedSequenceCapability({ available: true, sequence_admission: { available: true } });
  await pushStatus(ui, data);
  ui.open("protection");
  const master = ui.get("policy-enabled");
  assert.equal(master.getAttribute("form"), "policy-form");
  assert.equal(ui.get("protection-master").hidden, false);
  assert.equal(ui.get("enabled-label").textContent, "Desactivada");
  master.checked = true;
  master.emit("input");
  assert.equal(ui.get("enabled-label").textContent, "Activada");
  assert.equal(ui.get("settings-feedback").textContent, "Cambios sin guardar");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  ui.open("overview");
  assert.equal(ui.get("protection-master").hidden, true);
  assert.equal(ui.get("protection-master").inert, true);
  await pushStatus(ui, data);
  ui.open("protection");
  assert.equal(ui.get("protection-master").inert, false);
  assert.equal(master.checked, true);
  assert.equal(ui.get("enabled-label").textContent, "Activada");
  const save = ui.get("policy-form").emit("submit");
  assert.equal(master.disabled, true, "saving disables the associated switch even though it is outside the form");
  const request = ui.calls.at(-1);
  const body = JSON.parse(request.options.body);
  assert.equal(body.enabled, true);
  assert.equal(body.action, "hibernate");
  assert.equal(body.countdown_seconds, 10);
  assert.equal(body.low_battery_percent, 20);
  assert.equal(body.ups_poweroff_delay_seconds, 60);
  request.respond({ ...data, policy: { ...data.policy, enabled: true } });
  await save;
  assert.equal(master.disabled, false);
  assert.equal(master.checked, true);
  assert.equal(ui.get("enabled-label").textContent, "Activada");
});

test("sidebar monitoring and critical protection controls remain accurate across all five sections", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.meta.dry_run = true;
  data.connection.last_success = new Date(Date.now() - 5000).toISOString();
  data.protection = { state: "countdown", simulation: true, countdown_remaining: 12, reason: "Prueba segura" };
  data.health = { state: "healthy", reader_alive: true, supervisor_alive: true };
  ui.calls[0].respond(data);
  await flush();
  const views = ["overview", "protection", "history", "system", "webhooks"];
  assert.equal(ui.get("development-notice").hidden, false);
  const lastReading = ui.get("last-updated").textContent;
  assert.match(lastReading, /^Hace \d+ s · /);
  assert.equal(ui.get("last-updated").title, new Date(data.connection.last_success).toLocaleString("es-PE"));
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.equal(ui.get("countdown").textContent, "00:12");
    assert.equal(ui.get("critical-cancel").hidden, false);
    assert.equal(ui.get("critical-cancel").disabled, false);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
    assert.equal(ui.get("last-updated").textContent, lastReading);
  }
  assert.equal(ui.get("health-state-label").textContent, "Operativo");
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "Monitor sin conexión");
    assert.equal(ui.get("last-updated").textContent, lastReading);
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.match(ui.get("protection-reason").textContent, /cancelación no está confirmada/);
    assert.equal(ui.get("critical-cancel").disabled, true);
  }
  ui.poll();
  ui.calls.at(-1).respond(data);
  await flush();
  for (const name of views) {
    ui.open(name);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.doesNotMatch(ui.get("protection-reason").textContent, /cancelación no está confirmada/);
    assert.equal(ui.get("critical-cancel").disabled, false);
  }
  const cancellation = ui.get("critical-cancel").emit("click");
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

test("Historial loads one Registro and legacy incident links resolve without issuing legacy queries", async () => {
  const ui = harness({ navigation: true, hash: "#incidentes" });
  assert.equal(ui.get("view-title").textContent, "Historial");
  assert.equal(ui.get("page-history").hidden, false);
  ui.calls[0].respond(snapshot());
  await flush();
  assert.equal(recordCalls(ui).length, 1);
  assert.equal(queryParams(lastRecordCall(ui)).has("from"), false);
  assert.equal(ui.calls.some((call) => /\/api\/incidents|\/api\/events\/search/.test(call.url)), false);
  answerRecords(ui);
  await flush();
  const queryCount = ui.calls.length;
  ui.open("overview");
  ui.open("history");
  assert.equal(ui.calls.length, queryCount);
  ui.get("records-from").value = "2026-10-17T10:00";
  const pending = ui.get("records-form").emit("submit");
  assert.equal(ui.calls.length, queryCount + 1);
  assert.equal(queryParams(lastRecordCall(ui)).get("from"), new Date("2026-10-17T10:00").toISOString());
  answerRecords(ui);
  await pending;
  assert.equal(ui.calls.some((entry) => entry.options.method === "POST"), false);
  assert.doesNotMatch(html, /id="page-incidents"|id="incidents-form"|id="refresh-incidents"|id="refresh-records"/);
});

test("Registro has one visible filter form, six accessible columns and all six categories", () => {
  const page = html.match(/<section id="page-history"[\s\S]*?(?=<section id="page-protection")/)[0];
  assert.equal([...page.matchAll(/<table\b/g)].length, 1);
  assert.equal([...page.matchAll(/<form\b/g)].length, 1);
  assert.equal([...page.matchAll(/<th\b[^>]*scope="col"/g)].length, 6);
  assert.match(page, /<caption\b/);
  assert.match(page, /id="records-list"[^>]*tabindex="0"/);
  assert.match(page, /id="records-list"[^>]*aria-label="[^"]+"/);
  for (const category of ["power", "connection", "protection", "alerts", "settings", "other"]) {
    assert.ok(page.includes(`value="${category}"`), category);
  }
  assert.doesNotMatch(page, /<details\b|id="event-search-form"|id="incidents-body"|id="archive-list"|incident-sort/);
});

test("section shortcuts and keyboard navigation are accessible and do not submit forms", async () => {
  const ui = harness({ navigation: true });
  ui.shortcuts[3].emit("click");
  assert.equal(ui.get("view-title").textContent, "Sistema");
  ui.nav.at(-1).emit("keydown", { key: "ArrowDown" });
  assert.equal(ui.nav[0].focused, true);
  assert.equal(ui.get("view-title").textContent, "Sistema");
  ui.nav[3].emit("keydown", { key: "Home" });
  assert.equal(ui.nav[0].focused, true);
  ui.nav[0].emit("keydown", { key: "End" });
  assert.equal(ui.nav.at(-1).focused, true);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("the compact Resumen hides its intro and its real full-register link opens Historial", async () => {
  const { ui } = await currentUi({}, { navigation: true });
  assert.equal(ui.get("view-description").hidden, true);
  assert.equal(ui.get("view-description").textContent, "");
  const link = ui.shortcuts.find((item) => item.dataset.goView === "history" && item.getAttribute("href") === "#historial");
  assert.ok(link, "Resumen must offer a real link to the full register");
  const previousCalls = ui.calls.length;
  link.emit("click");
  assert.equal(ui.get("view-title").textContent, "Historial");
  assert.equal(ui.location.hash, "historial");
  assert.equal(ui.get("page-title").focused, true);
  assert.equal(ui.calls.length, previousCalls + 1);
  assert.equal(recordCalls(ui).length, 1);
  answerRecords(ui);
  await flush();
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
  ui.shortcuts[3].emit("click");
  assert.equal(ui.navigationScrolls.length, 4);
  assert.equal(ui.get("view-title").textContent, "Sistema");
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
  ui.get("policy-countdown").value = 75;
  ui.get("policy-form").emit("input");
  ui.open("system");
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  ui.get("theme-toggle").focus();
  const initialCalls = ui.calls.length;
  const initialScrolls = ui.navigationScrolls.length;
  ui.get("theme-toggle").emit("click");
  assert.equal(ui.get("view-title").textContent, "Sistema");
  assert.equal(ui.location.hash, "sistema");
  assert.equal(ui.get("policy-countdown").value, 75);
  assert.equal(ui.get("notifications-sound").checked, false);
  assert.equal(ui.get("theme-toggle").focused, true);
  assert.equal(ui.calls.length, initialCalls);
  assert.equal(ui.navigationScrolls.length, initialScrolls);
  ui.poll();
  ui.calls.at(-1).respond(snapshot());
  await flush();
  assert.equal(ui.rootElement.dataset.theme, "light");
  assert.equal(ui.get("policy-countdown").value, 75);
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

test("Resumen and Historial share the chart while recent events remain exclusive to Resumen", async () => {
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
  assert.equal(ui.get("events-list").children.length, 1);
  assert.equal(ui.get("events-list").children[0].children[1].children[0].textContent, "Falla del UPS");
  const chart = ui.get("history-chart");
  assert.equal(ui.get("recent-events-card").hidden, false);
  ui.open("history");
  assert.equal(ui.get("recent-events-card").hidden, true);
  assert.equal(ui.get("page-history").hidden, false);
  assert.equal(ui.get("shared-history").hidden, false);
  assert.match(ui.calls.at(-1).url, /^\/api\/records\?/);
  ui.open("overview");
  assert.equal(ui.get("recent-events-card").hidden, false);
  assert.equal(ui.get("history-chart"), chart);
  assert.equal(ui.get("events-filter").value, "power");
  assert.equal(ui.get("events-list").children.length, 1);
  assert.equal(ui.get("history-period").value, "168");
  assert.equal(ui.metrics[2].getAttribute("aria-pressed"), "true");
  for (const view of ["protection", "system"]) {
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

const recentTree = (element) => [element, ...(element.children || []).flatMap(recentTree)];
const connectionIncident = (changes = {}) => incidentRecord({
  id: "connection:3", type: "connection", category: "connection", categories: ["connection"],
  timestamp: "2026-10-30T02:04:00Z", started_at: "2026-10-30T02:04:00Z",
  ended_at: "2026-10-30T11:09:00Z", duration_seconds: 32700,
  description: "Sin datos recientes y confirmados del UPS", ...changes,
});

test("Resumen replaces four raw messages with two complete incidents and only a shared history link", async () => {
  const ui = harness({ navigation: true });
  const newer = connectionIncident();
  const older = connectionIncident({ id: "connection:1", timestamp: "2026-10-29T12:32:00Z",
    started_at: "2026-10-29T12:32:00Z", ended_at: "2026-10-30T00:11:00Z", duration_seconds: 41940 });
  const data = snapshot();
  data.recent_records = [older, newer];
  data.events = [older, newer].flatMap((record, index) => [
    { id: index * 2 + 1, kind: "connection_lost", timestamp: record.started_at, message: "Mensaje de pérdida" },
    { id: index * 2 + 2, kind: "connection_restored", timestamp: record.ended_at, message: "Mensaje de recuperación" },
  ]);
  ui.calls[0].respond(data);
  await flush();
  const rows = ui.get("events-list").children;
  assert.equal(rows.length, 2);
  assert.match(textOf(rows[0]), /Interrupción de comunicación/);
  assert.match(textOf(rows[0]), /Restablecida/);
  assert.match(textOf(rows[0]), /9 h 05 min\s+sin lecturas/);
  assert.match(textOf(rows[1]), /11 h 39 min\s+sin lecturas/);
  assert.deepEqual(recentTree(rows[0]).filter((node) => node.dateTime).map((node) => node.dateTime),
    [newer.started_at, newer.ended_at].map((value) => new Date(value).toISOString()));
  assert.doesNotMatch(textOf(ui.get("events-list")), /Mensaje de pérdida|Mensaje de recuperación|Ver en mediciones|Mostrar intervalo/);
  assert.equal(recentTree(ui.get("events-list")).some((node) => node.href || node.type === "button" || node.listeners?.click), false);
  assert.equal(ui.get("recent-events-caption").textContent, "Últimos incidentes");
  assert.equal(ui.get("overview-history-link").getAttribute("data-go-view"), "history");
  assert.doesNotMatch(html, /id="event-count"/);
  assert.equal(recordCalls(ui).length, 0, "the summary comes with status, independently of the full history query");
  assert.equal(ui.get("toast").hidden, true, "historical incidents are not new notifications");
});

test("recent incidents never turn missing boundaries or an open archive record into a confirmed outage duration", async () => {
  const ui = harness();
  const data = snapshot();
  data.recent_records = [
    connectionIncident({ id: "connection:open", status: "open", ended_at: null, duration_seconds: 99999 }),
    connectionIncident({ id: "connection:unknown", start_known: false, started_at: null, duration_seconds: 32700 }),
    connectionIncident({ id: "connection:invalid", ended_at: "not-a-date", duration_seconds: 32700 }),
  ];
  ui.calls[0].respond(data);
  await flush();
  const rows = ui.get("events-list").children;
  assert.equal(rows.length, 3);
  assert.match(textOf(ui.get("events-list")), /Sin cierre registrado/);
  assert.match(textOf(ui.get("events-list")), /Inicio no registrado/);
  for (const row of rows) {
    assert.match(textOf(row), /Duración sin confirmar/);
    assert.doesNotMatch(textOf(row), /\d+ h \d+ min\s+sin lecturas|En curso|Ahora|NaN|Invalid Date/);
  }
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("recent incident filters include related protection without changing measurements or rebuilding unchanged rows", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.recent_records = [connectionIncident(),
    incidentRecord({ categories: ["power", "protection"] }),
    eventRecord({ kind: "ups_fault", type: "ups_fault", message: "Falla registrada", description: "Falla registrada" }),
    eventRecord({ id: "event:5", kind: "policy_unknown", type: "policy_unknown", category: "protection",
      description: "unknown", message: "unknown", events: [] }),
  ];
  ui.calls[0].respond(data);
  await flush();
  const graph = ui.get("history-chart"), requests = [...ui.historyCalls];
  const filter = (value) => { ui.get("events-filter").value = value; ui.get("events-filter").emit("change"); };
  filter("connection");
  assert.equal(ui.get("events-list").children.length, 1);
  assert.match(textOf(ui.get("events-list")), /Interrupción de comunicación/);
  filter("power");
  assert.equal(ui.get("events-list").children.length, 2, "Energía includes electrical incidents and UPS alerts");
  assert.match(textOf(ui.get("events-list")), /Falla registrada/);
  assert.doesNotMatch(textOf(ui.get("events-list")), /Interrupción de comunicación/);
  filter("protection");
  assert.equal(ui.get("events-list").children.length, 2);
  assert.match(textOf(ui.get("events-list")), /Protección suspendida por falta de lecturas válidas/);
  filter("all");
  assert.equal(ui.get("events-list").children.length, 4);
  const stableRows = [...ui.get("events-list").children];
  await pushStatus(ui, JSON.parse(JSON.stringify(data)));
  stableRows.forEach((row, index) => assert.equal(ui.get("events-list").children[index], row));
  assert.equal(ui.get("history-chart"), graph);
  assert.deepEqual(ui.historyCalls, requests);
  assert.equal(ui.get("recent-events-caption").textContent, "Actividad reciente");
});

test("grouped recent records do not replace raw-event notification identity or replay alerts while filtering", async () => {
  const ui = harness();
  const data = snapshot();
  data.recent_records = [connectionIncident()];
  data.events = [{ id: 1, kind: "connection_lost", timestamp: "2026-10-30T02:04:00Z", message: "Datos interrumpidos" }];
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("toast").hidden, true);
  data.events.unshift({ id: 2, kind: "connection_restored", timestamp: "2026-10-30T11:09:00Z", message: "Lectura nueva confirmada" });
  await pushStatus(ui, data);
  assert.equal(ui.get("toast").textContent, "Lectura nueva confirmada");
  assert.equal(ui.get("events-list").children.length, 1);
  ui.advance(10000);
  ui.runTimeouts();
  assert.equal(ui.get("toast").hidden, true);
  ui.get("events-filter").value = "connection";
  ui.get("events-filter").emit("change");
  await pushStatus(ui, data);
  assert.equal(ui.get("toast").hidden, true);
  delete data.recent_records;
  await pushStatus(ui, data);
  assert.equal(ui.get("events-list").children.length, 2, "legacy raw events remain separate instead of guessing pairs");
  assert.match(textOf(ui.get("events-list")), /Lectura nueva confirmada/);
  assert.doesNotMatch(textOf(ui.get("events-list")), /9 h 05 min/);
});

test("recent activity failures are local, do not restore a raw list as grouped incidents and recover on the next status", async () => {
  const ui = harness();
  const data = snapshot();
  data.recent_records = null;
  data.recent_records_error = "No se pudo consultar el registro reciente.";
  data.events = [{ id: 1, kind: "connection_lost", timestamp: "2026-10-30T02:04:00Z", message: "Pérdida histórica" }];
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("service-notice").hidden, true);
  assert.equal(ui.get("recent-events-error").hidden, false);
  assert.match(ui.get("recent-events-error").textContent, /No se pudo consultar/);
  assert.match(textOf(ui.get("events-list")), /No se pudo cargar la actividad/);
  assert.doesNotMatch(textOf(ui.get("events-list")), /Pérdida histórica|Sin eventos registrados/);
  data.recent_records = [connectionIncident()];
  delete data.recent_records_error;
  await pushStatus(ui, data);
  assert.equal(ui.get("recent-events-error").hidden, true);
  assert.match(textOf(ui.get("events-list")), /9 h 05 min/);
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
    assert.equal(ui.get("connection-badge").className, `connection-indicator ups-power-indicator ${state}`);
    assert.equal(ui.get("connection-badge").title, label);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, label);
  }
  assert.equal(ui.get("connection-badge").getAttribute("role"), "status");
  const indicator = html.match(/<span\b[^>]*id="connection-badge"[^>]*>([\s\S]*?)<\/svg>/)?.[1];
  assert.match(indicator || "", /<svg\b[^>]*aria-hidden="true"/);
  assert.equal(ui.get("connection-badge").getAttribute("role"), "status");
  assert.match(html, /class="sr-only">Conectando/);
});

for (const transition of ["suspended", "resuming"]) {
  test(`Windows ${transition} shows an expected reading pause, hides old telemetry and resumes normally`, async () => {
    const ui = harness();
    const data = snapshot();
    data.system_power = { state: transition, generation: 1, resume_remaining: transition === "resuming" ? 14 : null };
    // A status response can briefly contain a still-recent sample from before
    // hibernation. A known Windows transition must take precedence over it.
    data.sample = liveSample({ on_battery: true, low_battery: true, input_voltage: 221 });
    data.health = { state: transition, reader_alive: true, supervisor_alive: true };
    ui.calls[0].respond(data);
    await flush();
    assert.equal(ui.get("energy-diagram").dataset.quality, transition);
    assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
    assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
    assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
    assert.equal(ui.get("input-voltage").textContent, "—");
    assert.equal(ui.get("critical-dialog").open, false);
    assert.equal(ui.get("connection-badge").className, `connection-indicator ups-power-indicator ${transition}`);
    assert.equal(ui.get("connection-badge").lastElementChild.textContent,
      transition === "suspended" ? "Lecturas en pausa por Windows" : "Reanudando lectura del UPS");
    assert.equal(ui.get("connection-notice").hidden, false);
    assert.doesNotMatch(ui.get("connection-notice").textContent, /cable USB|Sin conexión|problema de comunicación/);
    assert.equal(ui.get("health-state-label").textContent, transition === "suspended" ? "En pausa" : "Reanudando");
    assert.equal(ui.get("health-state").classList.contains("warning"), false);
    assert.equal(ui.get("test-shutdown").disabled, true);
    assert.match(ui.get("test-description").textContent, /reanude las lecturas/);
    await ui.get("test-shutdown").emit("click");
    assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);

    ui.poll();
    data.system_power.state = "running";
    data.connection.state = "connected";
    data.sample = liveSample();
    data.health.state = "healthy";
    ui.calls.at(-1).respond(data);
    await flush();
    assert.equal(ui.get("energy-diagram").dataset.quality, "live");
    assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
    assert.equal(ui.get("connection-notice").hidden, true);
    assert.equal(ui.get("health-state-label").textContent, "Operativo");
    assert.equal(ui.get("test-shutdown").disabled, false);
  });
}

test("a reading failure after the Windows resume interval still asks for attention", async () => {
  const ui = harness();
  const data = snapshot();
  data.system_power = { state: "resuming", generation: 2, resume_remaining: 1 };
  data.connection = { state: "resuming" };
  data.health = { state: "resuming", reader_alive: true, supervisor_alive: true };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("health-state-label").textContent, "Reanudando");

  ui.poll();
  data.system_power = { state: "running", generation: 2, resume_remaining: null };
  data.connection = { state: "stale", error: "No se recuperaron las lecturas del UPS después de reanudar Windows." };
  data.sample = null;
  data.health = { ...data.health, state: "degraded", message: data.connection.error };
  ui.calls.at(-1).respond(data);
  await flush();
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "Datos desactualizados");
  assert.equal(ui.get("connection-notice").hidden, false);
  assert.match(ui.get("connection-notice").textContent, /No se recuperaron las lecturas/);
  assert.equal(ui.get("health-state-label").textContent, "Revisar monitor");
  assert.equal(ui.get("health-state").classList.contains("warning"), true);
  assert.equal(ui.get("health-message").textContent, data.connection.error);
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

test("invalid or unproven percentages stay unavailable while unrecognized estimates remain informational", async () => {
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
  assert.equal(ui.get("policy-battery-field").hidden, false, "native USB percentage protection is visible and configurable");
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

test("energy flow follows confirmed source and measured voltage without inventing unknown supply", async () => {
  const { ui, data } = await currentUi();
  const diagram = ui.get("energy-diagram");
  assert.equal(diagram.dataset.mode, "line");
  assert.equal(diagram.dataset.quality, "live");
  assert.equal(diagram.dataset.inputActive, "true");
  assert.equal(diagram.dataset.outputPowered, "true");
  for (const [changes, mode, input, output] of [
    [{ on_battery: false, raw_status: "AVR mode" }, "line", true, true],
    [{ on_battery: true, raw_status: "Battery mode", input_voltage: 0 }, "battery", false, true],
    [{ on_battery: true, raw_status: "Battery mode", input_voltage: 220 }, "battery", false, true],
    [{ on_battery: false, fault: true, raw_status: "Fault mode" }, "fault", false, false],
    [{ on_battery: null, raw_status: "Unrecognized mode", fault: false }, "unknown", false, false],
  ]) {
    data.sample = liveSample(changes, ui.now());
    await pushStatus(ui, data);
    assert.equal(diagram.dataset.mode, mode);
    assert.equal(diagram.dataset.quality, "live");
    assert.equal(diagram.dataset.inputActive, String(input));
    assert.equal(diagram.dataset.outputPowered, String(output));
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  assert.doesNotMatch(html, /class="power-card\b|id="battery-tag"/);
});

test("a live source does not claim energized output without positive output voltage", async () => {
  const { ui, data } = await currentUi();
  const diagram = ui.get("energy-diagram");
  for (const onBattery of [false, true]) {
    for (const voltage of [0, null, undefined, NaN]) {
      data.sample = liveSample({ on_battery: onBattery, output_voltage: voltage }, ui.now());
      await pushStatus(ui, data);
      assert.equal(diagram.dataset.outputPowered, "false");
      assert.equal(ui.get("battery-flow-note").hidden, true);
      assert.doesNotMatch(ui.get("power-detail").textContent, /Tus equipos reciben energía|está alimentando tus equipos/);
      if (onBattery) assert.equal(ui.get("power-state").textContent, "Funcionando con batería");
    }
  }
  data.sample = liveSample({ load_percent: 0, output_voltage: 220 }, ui.now());
  await pushStatus(ui, data);
  assert.equal(diagram.dataset.inputActive, "true");
  assert.equal(diagram.dataset.outputPowered, "true", "zero load does not invalidate measured energized output");
  assert.equal(ui.get("load-percent").textContent, "0");
});

test("losing the monitor clears active streams and supply notes until fresh confirmation returns", async () => {
  const { ui, data } = await currentUi({ on_battery: true, input_voltage: 0 });
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "true");
  assert.equal(ui.get("battery-flow-note").hidden, false);
  assert.equal(ui.get("input-supply-note").hidden, false);
  ui.poll();
  ui.calls.at(-1).reject(new TypeError("offline"));
  await flush();
  assert.equal(ui.get("energy-diagram").dataset.quality, "disconnected");
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
  assert.equal(ui.get("battery-flow-note").hidden, true);
  assert.equal(ui.get("input-supply-note").hidden, true);
  assert.equal(ui.get("input-supply-note").textContent, "");
  data.sample = liveSample({}, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "true");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "true");
  assert.equal(ui.get("battery-flow-note").hidden, true);
  assert.equal(ui.get("input-supply-note").hidden, true);
});

test("mains input motion needs a positive input reading and battery notes never invent missing zeroes", async () => {
  const { ui, data } = await currentUi();
  const diagram = ui.get("energy-diagram");
  for (const [voltage, state] of [[0, "Entrada de red sin tensión"], [null, "Entrada de red sin confirmar"], [undefined, "Entrada de red sin confirmar"], [NaN, "Entrada de red sin confirmar"]]) {
    data.sample = liveSample({ input_voltage: voltage }, ui.now());
    await pushStatus(ui, data);
    assert.equal(diagram.dataset.mode, "line");
    assert.equal(diagram.dataset.inputActive, "false");
    assert.equal(diagram.dataset.outputPowered, "true");
    assert.equal(ui.get("power-state").textContent, state);
    assert.equal(ui.get("input-supply-note").hidden, true);
    assert.equal(ui.get("input-supply-note").textContent, "");
  }
  for (const [voltage, absent] of [[0, true], [null, false], [undefined, false], [220, false]]) {
    data.sample = liveSample({ on_battery: true, input_voltage: voltage }, ui.now());
    await pushStatus(ui, data);
    assert.equal(diagram.dataset.inputActive, "false");
    assert.equal(ui.get("input-supply-note").hidden, !absent);
    assert.equal(ui.get("input-supply-note").textContent, absent ? "Sin red" : "");
    assert.equal(ui.get("battery-flow-note").hidden, false);
    assert.match(ui.get("battery-flow-note").textContent, /En uso/);
  }
});

test("battery estimates change the labelled meter without claiming charging or battery supply", async () => {
  const { ui, data } = await currentUi({ battery_percent: null, battery_percent_estimate: 5, battery_estimate_method: "fixture" });
  assert.equal(ui.get("battery-note").textContent, "Carga estimada");
  assert.equal(ui.get("battery-runtime").hidden, true);
  assert.equal(ui.get("battery-flow-note").hidden, true);
  assert.equal(ui.get("energy-diagram").dataset.mode, "line");
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "true");
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample = liveSample({ on_battery: null, battery_percent_estimate: 90, battery_estimate_method: "fixture" }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.mode, "unknown");
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
  assert.equal(ui.get("battery-flow-note").hidden, true);
  assert.equal(ui.get("battery-runtime").hidden, true);
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
    assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
    assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
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
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "false");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "false");
  assert.equal(ui.get("battery-flow-note").hidden, true);
  assert.equal(ui.get("input-supply-note").hidden, true);
  assert.equal(ui.get("battery-percent").textContent, "—");
  assert.equal(ui.get("load-percent").textContent, "—");
  data.sample = liveSample({ battery_percent: 79 }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("energy-diagram").dataset.quality, "live");
  assert.equal(ui.get("energy-diagram").dataset.inputActive, "true");
  assert.equal(ui.get("energy-diagram").dataset.outputPowered, "true");
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

test("vertical battery meter accepts reported and unknown estimated zero without treating the unknown method as protection", async () => {
  const { ui, data } = await currentUi({ battery_percent_estimate: 0, battery_estimate_method: "fixture-v1" });
  assert.equal(ui.get("battery-percent").textContent, "0");
  assert.equal(ui.get("battery-bar").style.height, "0%");
  assert.equal(ui.get("battery-note").textContent, "Carga estimada");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.match(ui.get("battery-note").title, /Estimación informativa.*no se usa/);
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
  for (const view of ["history", "overview", "protection", "system"]) ui.open(view);
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

test("confirmed telemetry recovery closes the fallback dialog without acknowledgment and a recurrence reopens it", async () => {
  const { ui, data } = await currentUi({ low_battery: true });
  data.sample.low_battery = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("critical-dialog").modalCloses, 1);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample.low_battery = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-dialog").modalShows, 2);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

function pendingNotice(ui, changes = {}) {
  return { id: 101, kind: "power_lost", title: "Corte de energía", message: "El UPS está funcionando con batería.",
    timestamp: new Date(ui.now()).toISOString(), resolved_at: null, ...changes };
}

test("notification priorities promote critical alarms and the real countdown without acknowledging displaced incidents", async () => {
  const { ui, data } = await currentUi();
  const outage = pendingNotice(ui, { priority: 1 });
  data.notifications.pending = [outage];
  await pushStatus(ui, data);
  const fault = pendingNotice(ui, { id: 102, kind: "ups_fault", title: "Falla del UPS", priority: 2 });
  data.notifications.pending.push(fault);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Falla del UPS");
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.notificationId), ["102", "101"]);
  const countdown = pendingNotice(ui, { id: 103, kind: "policy_countdown", priority: 3 });
  data.notifications.pending.push(countdown);
  data.policy.action = "hibernate";
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 30 };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Hibernación programada");
  assert.equal(ui.get("critical-urgency").hidden, false);
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.notificationId), ["103", "102", "101"]);
  assert.equal(ui.get("critical-dialog").modalShows, 1);
  data.notifications.pending[2] = { ...countdown, requires_attention: false, resolved_at: new Date(ui.now()).toISOString() };
  data.protection = { state: "cancelled" };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Falla del UPS");
  assert.equal(ui.get("critical-urgency").hidden, true);
  assert.equal(data.notifications.pending.length, 3);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("equal-priority arrivals keep the visible incident stable and simulations cannot displace a real critical alarm", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui, { id: 102, kind: "ups_fault", title: "Falla del UPS", priority: 2 })];
  await pushStatus(ui, data);
  data.notifications.pending.unshift(pendingNotice(ui, { id: 101, kind: "overload", title: "Sobrecarga", priority: 2,
    timestamp: new Date(ui.now() - 10000).toISOString() }));
  data.protection = { state: "countdown", simulation: true, countdown_remaining: 30 };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Falla del UPS");
  assert.equal(ui.get("critical-list").children[0].dataset.notificationId, "102");
  assert.equal(ui.get("critical-urgency").hidden, true);
});

test("a real countdown without an episode identity cannot use the general cancellation endpoint", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, countdown_id: null, countdown_remaining: 30 };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás sin confirmar");
  await ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("native-style countdown freshness also disables panel cancellation after three seconds without resolving the incident", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui, { kind: "policy_countdown", priority: 3 })];
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 30 };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-cancel").disabled, false);
  ui.advance(3000); ui.tick();
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás sin confirmar");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(data.notifications.pending.length, 1);
  await ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("real cancellation requires a matching confirmation and never retries through the legacy endpoint", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 30 };
  await pushStatus(ui, data);
  const id = data.protection.countdown_id;
  const cancellation = ui.get("critical-cancel").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/protection/countdown/cancel");
  assert.deepEqual(JSON.parse(call.options.body), { countdown_id: id });
  call.respond({ ...data, protection: { state: "cancelled" }, cancelled_countdown_id: "f".repeat(32) });
  await cancellation;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(ui.get("critical-feedback").textContent, /No se confirmó la cancelación/);
  assert.equal(ui.calls.filter((request) => request.options.method === "POST").length, 1);
  assert.equal(ui.calls.some((request) => request.url === "/api/protection/cancel"), false);
});

test("the same countdown cannot gain seconds when its rounded deadline changes", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 30 };
  await pushStatus(ui, data);
  ui.advance(1000); ui.tick();
  assert.equal(ui.get("critical-countdown-time").textContent, "00:29");
  data.protection.countdown_remaining = 31;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-countdown-time").textContent, "00:29");
  const oldId = data.protection.countdown_id;
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 60 };
  await pushStatus(ui, data);
  assert.notEqual(data.protection.countdown_id, oldId);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
});

test("an elapsed real countdown never claims that Windows completed its action", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 1 };
  await pushStatus(ui, data);
  ui.advance(1000); ui.tick();
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás sin confirmar");
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("a five-second outage closes on recovery while unread incidents and event history remain intact", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  const outage = pendingNotice(ui);
  data.sample = liveSample({ on_battery: true }, ui.now());
  data.notifications.pending = [outage];
  await pushStatus(ui, data);
  const dialog = ui.get("critical-dialog");
  assert.equal(dialog.open, true);
  assert.equal(ui.get("critical-title").textContent, "Corte de energía");
  ui.advance(5000);
  const recoveryTime = new Date(ui.now()).toISOString();
  data.sample = liveSample({}, ui.now());
  data.events = [
    { id: 10, kind: "power_lost", timestamp: outage.timestamp, message: outage.message },
    { id: 11, kind: "power_restored", timestamp: recoveryTime, message: "Volvió el suministro eléctrico." },
  ];
  data.notifications.pending = [{ ...outage, resolved_at: recoveryTime }, pendingNotice(ui, {
    id: 102, kind: "power_restored", title: "Suministro eléctrico recuperado", message: "Volvió el suministro eléctrico.",
  })];
  await pushStatus(ui, data);
  assert.equal(dialog.open, false);
  assert.equal(dialog.modalShows, 1);
  assert.equal(dialog.modalCloses, 1);
  assert.equal(data.notifications.pending.length, 2, "automatic closure cannot mark durable notices as read");
  assert.ok(data.notifications.pending.every((notice) => !notice.acknowledged_at));
  ui.advance(10000); ui.runTimeouts();
  for (const view of ["system", "history", "overview"]) ui.open(view);
  assert.equal(dialog.open, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  await pushStatus(ui, data);
  assert.equal(dialog.open, false);
  assert.equal(data.events.length, 2, "automatic closure must preserve event history");
  data.notifications.pending.push(pendingNotice(ui, { id: 103 }));
  data.sample = liveSample({ on_battery: true }, ui.now());
  await pushStatus(ui, data);
  assert.equal(dialog.open, true, "a new outage needs attention independently of the recovered unread incident");
  assert.equal(dialog.modalShows, 2);
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.notificationId), ["103"]);
});

test("an outage completed between panel polls remains in the inbox without opening a modal or reopening on reload", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui, { timestamp: new Date(ui.now() - 5000).toISOString(), resolved_at: new Date(ui.now()).toISOString() }),
    pendingNotice(ui, { id: 102, kind: "power_restored", title: "Suministro eléctrico recuperado", message: "Volvió la electricidad." })];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("critical-dialog").modalShows, 0);
  const reloaded = harness();
  reloaded.calls[0].respond(data); await flush();
  assert.equal(reloaded.get("critical-dialog").open, false);
  assert.equal(data.notifications.pending.length, 2);
  assert.equal(reloaded.calls.some((call) => call.options.method === "POST"), false);
});

test("the modal follows requires_attention and retains other active incidents when an outage recovers", async () => {
  const { ui, data } = await currentUi();
  const outage = pendingNotice(ui, { requires_attention: true });
  const fault = pendingNotice(ui, { id: 102, kind: "ups_fault", title: "Falla del UPS", requires_attention: true });
  data.notifications.pending = [outage, fault];
  await pushStatus(ui, data);
  data.notifications.pending = [{ ...outage, requires_attention: false }, fault];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.notificationId), ["102"]);
  assert.equal(ui.get("critical-title").textContent, "Falla del UPS");
  assert.equal(ui.get("critical-dialog").modalShows, 1);
  data.notifications.pending[1] = { ...fault, requires_attention: false };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(data.notifications.pending.length, 2);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("informational recovery notices use the compatibility fallback without opening a modal", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = ["power_restored", "connection_restored", "monitor_restarted", "policy_cancelled", "ups_poweroff_reviewed"]
    .map((kind, index) => pendingNotice(ui, { id: index + 1, kind }));
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(data.notifications.pending.length, 5);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("fresh normal telemetry cannot dismiss unresolved reviews or unconfirmed Windows and UPS actions", async () => {
  const { ui, data } = await currentUi();
  const kinds = ["ups_poweroff_unconfirmed", "ups_cancel_test_unconfirmed", "ups_battery_test_unconfirmed",
    "ups_battery_test_windows_unconfirmed", "windows_action_unconfirmed", "ups_poweroff_blocked"];
  data.notifications.pending = kinds.map((kind, index) => pendingNotice(ui, { id: index + 1, kind }));
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.deepEqual(ui.get("critical-list").children.map((row) => row.dataset.kind), kinds);
  data.sample = liveSample({}, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children.length, kinds.length);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("stale telemetry and API loss do not imply recovery of an unresolved inbox incident", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui, { requires_attention: true })];
  await pushStatus(ui, data);
  data.connection = { state: "stale" };
  data.sample = liveSample({}, ui.now() - 20000);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children[0].dataset.state, "unconfirmed");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("a recovered countdown notice cannot acknowledge or cancel a protection countdown that remains active", async () => {
  const { ui, data } = await currentUi();
  const countdown = pendingNotice(ui, { kind: "policy_countdown", requires_attention: true });
  data.notifications.pending = [countdown];
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  data.notifications.pending = [{ ...countdown, requires_attention: false, resolved_at: new Date(ui.now()).toISOString() }];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown").hidden, false);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.equal(ui.get("critical-cancel").disabled, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  data.protection = { state: "idle" };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

for (const result of ["blocked", "executed"]) {
  test(`recovering an incident keeps a ${result} protection result available for review`, async () => {
    const { ui, data } = await currentUi();
    const outage = pendingNotice(ui);
    data.notifications.pending = [outage];
    await pushStatus(ui, data);
    data.notifications.pending = [{ ...outage, requires_attention: false, resolved_at: new Date(ui.now()).toISOString() }];
    data.protection = { state: result, reason: "Revisa el resultado de la protección.", simulation: false };
    await pushStatus(ui, data);
    assert.equal(ui.get("critical-dialog").open, true);
    assert.equal(ui.get("critical-list").children.length, 0);
    assert.match(ui.get("critical-description").textContent, /Revisa el resultado/);
    assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  });
}

test("Entendido failures and unconfirmed responses leave every selected notice pending for a safe retry", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui)];
  await pushStatus(ui, data);
  let acknowledgment = ui.get("critical-ack").emit("click");
  ui.calls.at(-1).respond({ error: "No se pudo guardar en disco." }, false); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children.length, 1);
  assert.match(ui.get("critical-feedback").textContent, /No se confirmó Entendido.*No se pudo guardar en disco.*siguen pendientes/);
  assert.equal(ui.get("critical-ack").disabled, false);
  acknowledgment = ui.get("critical-ack").emit("click");
  ui.calls.at(-1).respond(data); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(ui.get("critical-feedback").textContent, /no confirmó Entendido/);
  acknowledgment = ui.get("critical-ack").emit("click");
  ui.calls.at(-1).reject(new TypeError("offline")); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children[0].dataset.state, "unconfirmed");
  const calls = ui.calls.length;
  await ui.get("critical-ack").emit("click");
  assert.equal(ui.calls.length, calls, "offline acknowledgment must not be cached as read locally");
  assert.match(ui.get("critical-feedback").textContent, /Recupera la conexión.*siguen pendientes/);
  await pushStatus(ui, data);
  acknowledgment = ui.get("critical-ack").emit("click");
  data.notifications.pending = [];
  ui.calls.at(-1).respond(data); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, false);
});

test("a notice arriving during Entendido remains unread and needs its own acknowledgment", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui)];
  await pushStatus(ui, data);
  const acknowledgment = ui.get("critical-ack").emit("click");
  const request = ui.calls.at(-1);
  data.notifications.pending = [pendingNotice(ui, { id: 102, kind: "ups_fault", title: "Falla del UPS" })];
  request.respond(data); await acknowledgment;
  assert.deepEqual(JSON.parse(request.options.body), { ids: [101] });
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children.length, 1);
  assert.equal(ui.get("critical-list").children[0].dataset.notificationId, "102");
  const next = ui.get("critical-ack").emit("click");
  assert.deepEqual(JSON.parse(ui.calls.at(-1).options.body), { ids: [102] });
  data.notifications.pending = [];
  ui.calls.at(-1).respond(data); await next;
  assert.equal(ui.get("critical-dialog").open, false);
});

test("Entendido commits more than 200 pending notices in bounded batches without acknowledging new arrivals", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = Array.from({ length: 201 }, (_unused, index) => pendingNotice(ui, { id: index + 1 }));
  await pushStatus(ui, data);
  const acknowledgment = ui.get("critical-ack").emit("click");
  const first = ui.calls.at(-1);
  assert.equal(JSON.parse(first.options.body).ids.length, 200);
  data.notifications.pending = [pendingNotice(ui, { id: 201 }), pendingNotice(ui, { id: 202, kind: "ups_fault" })];
  first.respond(data); await flush();
  const second = ui.calls.at(-1);
  assert.notEqual(first, second);
  assert.deepEqual(JSON.parse(second.options.body), { ids: [201] });
  data.notifications.pending = [pendingNotice(ui, { id: 202, kind: "ups_fault" })];
  second.respond(data); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children.length, 1);
  assert.equal(ui.get("critical-list").children[0].dataset.notificationId, "202");
});

test("cancelling protection never acknowledges an unread electricity incident", async () => {
  const { ui, data } = await currentUi();
  data.notifications.pending = [pendingNotice(ui)];
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  const cancellation = ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/protection/countdown/cancel");
  data.protection = { state: "cancelled", simulation: false };
  ui.calls.at(-1).respond(data); await cancellation;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-list").children[0].dataset.notificationId, "101");
  assert.equal(ui.calls.some((call) => call.url === "/api/notifications/ack"), false);
  const acknowledgment = ui.get("critical-ack").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/notifications/ack");
  data.notifications.pending = [];
  ui.calls.at(-1).respond(data); await acknowledgment;
  assert.equal(ui.get("critical-dialog").open, false);
});

test("native Entendido is reflected in the panel and active telemetry does not recreate acknowledged inbox incidents", async () => {
  const { ui, data } = await currentUi();
  data.sample = liveSample({ low_battery: true }, ui.now());
  data.notifications.pending = [pendingNotice(ui, { kind: "battery_low", title: "Batería baja del UPS" })];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-list").children.length, 1, "the persisted condition and telemetry cannot duplicate the same incident");
  ui.get("critical-dialog").close();
  assert.equal(ui.get("critical-dialog").open, true, "closing the window programmatically is not Entendido");
  data.notifications.pending = [];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false, "a native acknowledgment is already recorded by the backend");
  const reloaded = harness();
  reloaded.calls[0].respond(data); await flush();
  assert.equal(reloaded.get("critical-dialog").open, false, "the durable inbox is authoritative across clients");
});

test("native Entendido acknowledges its matching countdown notice without cancelling the active protection", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  data.notifications.pending = [pendingNotice(ui, { kind: "policy_countdown", title: "Cuenta regresiva de protección" })];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  data.notifications.pending = [];
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, false);
  assert.equal(ui.get("countdown").textContent, "01:00");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
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

test("Escape keeps the alert pending until the explicit Entendido action restores focus", async () => {
  const ui = harness();
  ui.get("theme-toggle").focus();
  const data = snapshot();
  data.sample = liveSample({ fault: true }, ui.now());
  ui.calls[0].respond(data); await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  ui.get("critical-dialog").emit("cancel");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.document.activeElement, ui.get("critical-ack"));
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.document.activeElement, ui.get("theme-toggle"));
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
});

test("real protection countdowns open a cancelable dialog in every view without requiring physical alarms", async () => {
  for (const view of ["overview", "protection", "history", "system", "webhooks"]) {
    const { ui, data } = await currentUi({}, { navigation: true });
    ui.open(view);
    ui.get("policy-countdown").value = "999";
    ui.get("policy-form").emit("input");
    data.policy.enabled = true;
    data.sample = liveSample({ on_battery: true, low_battery: false, fault: false, load_percent: 8 }, ui.now());
    data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString(), reason: "Tiempo máximo con batería alcanzado" };
    await pushStatus(ui, data);
    assert.equal(ui.get("critical-dialog").open, true, view);
    assert.equal(ui.get("critical-list").children.length, 0);
    assert.equal(ui.get("critical-title").textContent, "Apagado programado");
    assert.equal(ui.get("critical-countdown").hidden, false);
    assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
    assert.equal(ui.get("critical-cancel").disabled, false);
    assert.equal(ui.get("critical-cancel").textContent, "Cancelar protección");
    assert.equal(ui.get("protection-alert").hidden, false);
    assert.equal(ui.get("policy-countdown").value, "999", "dirty controls cannot replace the confirmed deadline");
    assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  }
});

test("an initial simulation countdown opens its own dialog and uses the service duration", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.policy.countdown_seconds = 60;
  data.protection = { state: "countdown", simulation: true, countdown_remaining: 15 };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás de prueba");
  assert.match(ui.get("critical-description").textContent, /no apagará ni hibernará/);
  assert.equal(ui.get("critical-countdown-time").textContent, "00:15");
  assert.equal(ui.get("critical-cancel").textContent, "Cancelar protección");
  assert.equal(ui.get("critical-cancel").disabled, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("a hibernation countdown remains visible without fresh UPS readings and through monitor reconnection", async () => {
  const { ui, data } = await currentUi();
  data.policy.action = "hibernate";
  data.sample = null;
  data.connection = { state: "disconnected" };
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Hibernación programada");
  const dialog = ui.get("critical-dialog");
  assert.equal(dialog.open, true);
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  ui.advance(1000); ui.tick();
  assert.equal(dialog.open, true);
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás sin confirmar");
  assert.equal(ui.get("critical-countdown-time").textContent, "00:59");
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.match(ui.get("critical-description").textContent, /cancelación no está confirmada/);
  await pushStatus(ui, data);
  assert.equal(dialog.modalShows, 1);
  assert.equal(ui.get("critical-cancel").disabled, false);
});

test("Escape cannot acknowledge or cancel a countdown and a later countdown opens again after Entendido", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  ui.get("critical-dialog").emit("cancel");
  assert.equal(ui.get("critical-dialog").open, true);
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, false);
  assert.equal(ui.get("critical-cancel").disabled, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  data.protection.deadline = new Date(ui.now() + 60750).toISOString();
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false, "wall-clock rounding is not a new countdown");
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false, "reconnection cannot duplicate an acknowledged countdown");
  data.protection.deadline = new Date(ui.now() + 120000).toISOString();
  data.protection.countdown_id = (++countdownFixtureSerial).toString(16).padStart(32, "0");
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-dialog").modalShows, 2);
  assert.equal(ui.get("critical-countdown-time").textContent, "02:00");
});

test("successful countdown cancellation closes a countdown-only dialog after monitor confirmation", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: true, deadline: new Date(ui.now() + 15000).toISOString() };
  await pushStatus(ui, data);
  const pending = ui.get("critical-cancel").emit("click");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-cancel").disabled, true);
  assert.equal(ui.calls.at(-1).url, "/api/protection/cancel");
  ui.calls.at(-1).respond({ ...data, protection: { state: "cancelled", simulation: true } });
  await pending;
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.match(ui.get("toast").textContent, /confirmó la cancelación de la prueba/);
  data.protection = { state: "countdown", simulation: true, deadline: new Date(ui.now() + 15000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true, "a confirmed end rearms the next episode even with the same deadline");
});

test("an unconfirmed cancellation keeps a countdown-only dialog open and cancelable", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", simulation: false, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  const pending = ui.get("critical-cancel").emit("click");
  ui.calls.at(-1).respond(data);
  await pending;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-cancel").disabled, false);
  assert.equal(ui.get("critical-countdown").hidden, false);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.match(ui.get("critical-feedback").textContent, /No se confirmó la cancelación/);
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
  assert.equal(request.url, "/api/protection/countdown/cancel");
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
  assert.equal(ui.get("critical-cancel").disabled, true);
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
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00", "rounding must not prolong the same episode");
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

test("the activation switch preserves test-mode warnings without redundant policy badges", async () => {
  const ui = harness();
  const data = snapshot();
  data.meta.dry_run = true;
  data.policy.enabled = true;
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("development-notice").hidden, false);
  assert.equal(ui.get("enabled-label").textContent, "Modo de prueba");
  assert.equal(ui.document.getElementById("policy-state"), null);
  assert.equal(ui.document.getElementById("policy-action-description"), null);
  data.meta.dry_run = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("development-notice").hidden, true);
  assert.equal(ui.get("enabled-label").textContent, "Activada");
  data.meta.simulation_source = true;
  data.meta.bridge_required = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("development-notice").hidden, false);
  assert.equal(ui.get("bridge-policy-note").hidden, false);
  data.policy.enabled = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("enabled-label").textContent, "Desactivada");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

for (const blocked of [true, false]) {
  test(`saving enabled protection reports ${blocked ? "test mode" : "real capability"} from the service`, async () => {
    const ui = await connected();
    ui.get("policy-enabled").checked = true;
    ui.get("policy-form").emit("input");
    const pending = ui.get("policy-form").emit("submit");
    const save = ui.calls.at(-1);
    assert.equal(save.url, "/api/policy");
    const data = snapshot();
    data.policy.enabled = true;
    data.meta.dry_run = blocked;
    save.respond(data);
    await pending;
    assert.match(ui.get("toast").textContent, blocked ? /modo de prueba.*siguen bloqueados/ : /Protección automática activada en esta PC/);
    assert.equal(ui.get("toast").classList.contains("warning"), blocked);
    const body = JSON.parse(save.options.body);
    assert.equal(body.enabled, true);
    assert.equal(Object.hasOwn(body, "allow_power_actions"), false);
    assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
  });
}

test("legacy unknown policy messages use Spanish labels in recent events and new toasts", async () => {
  const ui = harness();
  const data = snapshot();
  data.events = [{ id: 1, kind: "policy_unknown", timestamp: new Date().toISOString(), message: "unknown" }];
  ui.calls[0].respond(data);
  await flush();
  assert.match(textOf(ui.get("events-list")), /Protección suspendida por falta de lecturas válidas/);
  assert.doesNotMatch(textOf(ui.get("events-list")), /unknown/);
  data.events.unshift({ ...data.events[0], id: 2 });
  await pushStatus(ui, data);
  assert.match(ui.get("toast").textContent, /Protección suspendida por falta de lecturas válidas/);
  assert.doesNotMatch(ui.get("toast").textContent, /unknown/);
  data.events.unshift({ ...data.events[0], id: 3, message: "Esperando una lectura válida tras actualizar la protección." });
  await pushStatus(ui, data);
  assert.match(ui.get("toast").textContent, /Esperando una lectura válida tras actualizar/);
});

test("simulation uses saved timing and requires saving edited protection fields first", async () => {
  const ui = await connected();
  assert.match(ui.get("test-description").textContent, /60 s para Windows/);
  assert.equal(ui.get("test-shutdown").disabled, false);
  ui.get("policy-countdown").value = 90;
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("test-shutdown").disabled, true);
  assert.match(ui.get("test-description").textContent, /Guarda los cambios/);
  const initialRequests = ui.calls.length;
  await ui.get("test-shutdown").emit("click");
  assert.equal(ui.calls.length, initialRequests);
  const save = ui.get("policy-form").emit("submit");
  assert.equal(JSON.parse(ui.calls.at(-1).options.body).countdown_seconds, 90);
  const data = snapshot();
  data.policy.countdown_seconds = 90;
  ui.calls.at(-1).respond(data);
  await save;
  assert.equal(ui.get("test-shutdown").disabled, false);
  assert.match(ui.get("test-description").textContent, /90 s para Windows/);
  const simulation = ui.get("test-shutdown").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/protection/test");
  assert.deepEqual(JSON.parse(ui.calls.at(-1).options.body), { include_ups: false });
  data.protection = { state: "countdown", simulation: true, countdown_remaining: 90, deadline: new Date(ui.now() + 90000).toISOString() };
  ui.calls.at(-1).respond(data);
  await simulation;
  assert.equal(ui.get("countdown").textContent, "01:30");
  assert.equal(ui.get("critical-countdown-time").textContent, "01:30");
});

function upsCapability(changes = {}) {
  return { available: false, validation: "pending", reason: "Pendiente de validación física del UPS.",
    delay_seconds: null, manual_restart: true, authorized: false, state: "idle",
    simulation_delay_seconds: 180, validated_delay: false, ...changes };
}

test("the UPS power option stays disabled without a capability or physical validation", async () => {
  const ui = await connected();
  assert.equal(ui.get("policy-ups-poweroff").checked, false);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.match(ui.get("ups-power-capability").textContent, /no informa si puede apagar el UPS/);
  const data = snapshot();
  data.meta.source = "native";
  data.meta.model = "Forza NT-2402PRO";
  data.ups_poweroff = upsCapability({ reason: "<validación> pendiente" });
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.equal(ui.get("ups-power-capability").textContent, "<validación> pendiente");
  assert.match(ui.get("ups-power-delay").textContent, /De 1 a 9 min.*solicitud de apagado/);
  assert.match(ui.get("ups-power-restart").textContent, /todavía no está confirmado/);
  assert.match(ui.get("test-description").textContent, /Sin apagado adicional del UPS/);
  data.ups_poweroff = upsCapability({ validation: "incompatible", reason: "" });
  await pushStatus(ui, data);
  assert.match(ui.get("ups-power-capability").textContent, /no es compatible/);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("legacy policy saves explicitly keep the optional UPS action disabled", async () => {
  const ui = await connected();
  const pending = ui.get("policy-form").emit("submit");
  const body = JSON.parse(ui.calls.at(-1).options.body);
  assert.equal(body.ups_poweroff, false);
  assert.equal(body.action, "shutdown");
  assert.equal(Object.hasOwn(body, "authorized"), false);
  assert.equal(Object.hasOwn(body, "validation"), false);
  ui.calls.at(-1).respond(snapshot());
  await pending;
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
});

test("available UPS opt-in survives dirty polls and reconnect without enabling it remotely", async () => {
  const ui = harness({ navigation: true });
  const data = snapshot();
  data.policy.ups_poweroff = false;
  data.ups_poweroff = upsCapability({ available: true, validation: "validated", authorized: true,
    delay_seconds: 240, simulation_delay_seconds: 240, validated_delay: true, reason: "Perfil validado disponible." });
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  assert.equal(ui.get("ups-power-enabled-label").textContent, "Desactivado");
  assert.match(ui.get("ups-power-delay").textContent, /De 1 a 9 min/);
  ui.get("policy-ups-poweroff").checked = true;
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("ups-power-enabled-label").textContent, "Activado");
  ui.open("history");
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.equal(ui.get("ups-power-enabled-label").textContent, "Activado");
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.match(ui.get("ups-power-capability").textContent, /Sin comunicación/);
  await pushStatus(ui, data);
  ui.open("protection");
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.equal(ui.get("settings-feedback").textContent, "Cambios sin guardar");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  const pending = ui.get("policy-form").emit("submit");
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  const body = JSON.parse(ui.calls.at(-1).options.body);
  assert.equal(body.ups_poweroff, true);
  assert.equal(body.action, "shutdown");
  assert.equal(Object.hasOwn(body, "allow_power_actions"), false);
  ui.calls.at(-1).respond({ ...data, policy: { ...data.policy, ups_poweroff: true } });
  await pending;
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  assert.equal(ui.get("ups-power-enabled-label").textContent, "Activado");
});

test("a saved UPS opt-in can be turned off after capability degrades", async () => {
  const ui = harness();
  const data = snapshot();
  data.policy.enabled = true;
  data.policy.ups_poweroff = true;
  data.ups_poweroff = upsCapability({ state: "blocked", reason: "El perfil físico dejó de estar disponible." });
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.match(ui.get("ups-power-state").textContent, /bloqueado.*protección de Windows/);
  ui.get("policy-ups-poweroff").checked = false;
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  const pending = ui.get("policy-form").emit("submit");
  const body = JSON.parse(ui.calls.at(-1).options.body);
  assert.equal(body.enabled, true);
  assert.equal(body.ups_poweroff, false);
  ui.calls.at(-1).respond({ ...data, policy: { ...data.policy, ups_poweroff: false } });
  await pending;
  assert.equal(ui.get("policy-ups-poweroff").checked, false);
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
});

test("Windows and UPS requests never claim that Windows ended or the UPS physically cut power", async () => {
  const ui = await connected();
  const data = snapshot();
  data.policy.enabled = true;
  data.protection = { state: "executed", simulation: false, reason: "Solicitud aceptada" };
  data.ups_poweroff = upsCapability({ state: "submitted" });
  await pushStatus(ui, data);
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.match(html, /no espera una confirmación de que Windows terminó/);
  assert.equal(ui.get("ups-power-state").textContent, "Comando enviado al UPS. El corte físico no está confirmado.");
  data.ups_poweroff.state = "unconfirmed";
  await pushStatus(ui, data);
  assert.match(ui.get("ups-power-state").textContent, /No se pudo confirmar/);
  assert.equal(ui.get("connection-badge").lastElementChild.textContent, "UPS conectado");
});

test("sequence simulation represents the saved Windows and UPS delays without claiming physical actions", async () => {
  const ui = harness();
  const data = snapshot();
  data.policy.ups_poweroff = true;
  data.policy.ups_poweroff_delay_seconds = 180;
  data.ups_poweroff = upsCapability({ available: true, can_configure: true });
  ui.calls[0].respond(data);
  await flush();
  assert.match(html, /id="test-shutdown"[^>]*>Simular secuencia</);
  assert.match(ui.get("test-description").textContent, /60 s guardados.*simulado.*180 s desde el envío/);
  const pending = ui.get("test-shutdown").emit("click");
  assert.equal(ui.calls.at(-1).url, "/api/protection/test");
  assert.deepEqual(JSON.parse(ui.calls.at(-1).options.body), { include_ups: true });
  data.protection = { state: "countdown", simulation: true, phase: "windows_countdown", countdown_remaining: 60,
    deadline: new Date(ui.now() + 60000).toISOString() };
  data.ups_poweroff.simulation = { phase: "windows_countdown", remaining: 60, delay_seconds: 180,
    validated_delay: false, windows_requested: false, ups_cut_simulated: false };
  ui.calls.at(-1).respond(data);
  await pending;
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.equal(ui.get("critical-sequence-note").hidden, false);
  assert.match(ui.get("critical-sequence-note").textContent, /no se ejecutan acciones reales/);
  ui.advance(60000);
  data.protection = { state: "countdown", simulation: true, phase: "ups_delay", countdown_remaining: 180,
    deadline: new Date(ui.now() + 180000).toISOString() };
  data.ups_poweroff.simulation = { ...data.ups_poweroff.simulation, phase: "ups_delay", remaining: 180, windows_requested: true };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").modalShows, 1);
  assert.equal(ui.get("critical-title").textContent, "Simulación: espera para apagar el UPS");
  assert.equal(ui.get("critical-countdown-time").textContent, "03:00");
  assert.equal(ui.get("critical-cancel").textContent, "Cancelar protección");
  assert.match(ui.get("critical-sequence-note").textContent, /simulado.*180 s desde el envío/);
  assert.match(ui.get("critical-sequence-note").textContent, /ni se corta la energía/);
  assert.match(ui.get("protection-reason").textContent, /No se confirma que Windows haya terminado/);
  ui.advance(1000); ui.tick();
  assert.equal(ui.get("critical-countdown-time").textContent, "02:59");
  ui.advance(179000);
  data.protection = { state: "idle", simulation: true, phase: "complete", countdown_remaining: 0, deadline: null };
  data.ups_poweroff.simulation = { ...data.ups_poweroff.simulation, phase: "complete", remaining: 0, ups_cut_simulated: true };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-title").textContent, "Simulación finalizada");
  assert.match(ui.get("critical-description").textContent, /Windows y el UPS permanecen encendidos/);
  assert.equal(ui.get("critical-countdown").hidden, true);
  assert.equal(ui.get("critical-cancel").hidden, true);
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
});

test("the simulated UPS delay has no second cancellation opportunity", async () => {
  const { ui, data } = await currentUi();
  data.ups_poweroff = upsCapability({ simulation: { phase: "ups_delay", remaining: 180, delay_seconds: 180, validated_delay: false, windows_requested: true } });
  data.protection = { state: "countdown", simulation: true, phase: "ups_delay", cancelable: false, countdown_remaining: 180, deadline: new Date(ui.now() + 180000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown").hidden, false);
  assert.equal(ui.get("critical-cancel").hidden, true);
  assert.equal(ui.get("critical-cancel").disabled, true);
  await ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  data.protection = { ...data.protection, cancelable: undefined };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-cancel").hidden, true, "legacy phase fallback also prevents a second cancel action");
});

test("acknowledging the Windows simulation also acknowledges its following UPS phase", async () => {
  const { ui, data } = await currentUi();
  data.ups_poweroff = upsCapability();
  data.protection = { state: "countdown", simulation: true, phase: "windows_countdown", countdown_remaining: 60, deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  ui.get("critical-ack").emit("click");
  data.protection = { state: "countdown", simulation: true, phase: "ups_delay", countdown_remaining: 180, deadline: new Date(ui.now() + 240000).toISOString() };
  data.ups_poweroff.simulation = { phase: "ups_delay", delay_seconds: 180, validated_delay: false, windows_requested: true };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, false);
  assert.equal(ui.get("critical-cancel").textContent, "Cancelar protección");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("legacy Windows-only simulations do not promise an unplanned UPS phase", async () => {
  const { ui, data } = await currentUi();
  data.ups_poweroff = upsCapability({ state: "blocked" });
  data.protection = { state: "countdown", simulation: true, phase: "windows_countdown", countdown_remaining: 60,
    deadline: new Date(ui.now() + 60000).toISOString() };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(ui.get("critical-sequence-note").textContent, /Simulación de la cuenta atrás de Windows/);
  assert.doesNotMatch(ui.get("critical-sequence-note").textContent, /Después.*UPS/);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
});

test("the native pending-capability contract cancels the Windows phase and starts a fresh sequence", async () => {
  const ui = harness();
  const data = snapshot();
  data.policy.ups_poweroff = false;
  data.ups_poweroff = upsCapability({ state: "blocked", simulation: {
    phase: "windows_countdown", remaining: 60, delay_seconds: 180, validated_delay: false,
    windows_requested: true, ups_cut_simulated: false,
  } });
  data.protection = { state: "countdown", phase: "windows_countdown", simulation: true, countdown_remaining: 60,
    deadline: new Date(ui.now() + 60000).toISOString(), windows_request: null, reason: "Prueba sin apagado real" };
  ui.calls[0].respond(data);
  await flush();
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.match(ui.get("critical-sequence-note").textContent, /no se ejecutan acciones reales/);
  const cancellation = ui.get("critical-cancel").emit("click");
  data.protection = { ...data.protection, state: "idle", phase: "cancelled", countdown_remaining: null, deadline: null };
  data.ups_poweroff.simulation = { ...data.ups_poweroff.simulation, phase: "cancelled", remaining: 0 };
  ui.calls.at(-1).respond(data);
  await cancellation;
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, true);
  const next = ui.get("test-shutdown").emit("click");
  data.protection = { ...data.protection, state: "countdown", phase: "windows_countdown", countdown_remaining: 60,
    deadline: new Date(ui.now() + 60000).toISOString() };
  data.ups_poweroff.simulation = { ...data.ups_poweroff.simulation, phase: "windows_countdown", remaining: 60, windows_requested: false };
  ui.calls.at(-1).respond(data);
  await next;
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown-time").textContent, "01:00");
  assert.equal(ui.get("critical-title").textContent, "Cuenta atrás de prueba");
  assert.match(ui.get("critical-sequence-note").textContent, /Después se representará la espera del UPS/);
  assert.equal(ui.get("policy-ups-poweroff").checked, false);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 2);
});

function observedSequenceCapability(changes = {}) {
  return upsCapability({ available: false, can_configure: true, authorized: true, validation: "validated",
    reserve_mode: "observed_sequence", delay_seconds: 180, simulation_delay_seconds: 180, validated_delay: true,
    reason: "Secuencia de hibernación comprobada; su ejecución requiere condiciones recientes.",
    sequence_validation: { max_countdown_seconds: 10 }, sequence_admission: { available: false, reason: "Esperando una lectura reciente con batería." },
    restart_behavior: "automatic_on_mains", restart_behavior_observed: true, automatic_restart_accepted: true, manual_restart: false, ...changes });
}

test("countdown input and policy saves accept ten to one hundred twenty seconds and reject out-of-range edits", async () => {
  const { ui, data } = await currentUi();
  assert.equal(ui.get("policy-countdown").getAttribute("min"), "10");
  assert.equal(ui.get("policy-countdown").getAttribute("max"), "120");
  assert.match(html, /id="policy-countdown-note"[^>]*>Puedes cancelar/);
  for (const value of ["9", "121", "10.5", "", "not-a-number"]) {
    ui.get("policy-countdown").value = value;
    ui.get("policy-form").emit("input");
    await ui.get("policy-form").emit("submit");
    assert.match(ui.get("settings-feedback").textContent, /entero de 10 a 120/);
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  ui.get("policy-countdown").value = "120";
  ui.get("policy-form").emit("input");
  const pending = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  assert.equal(JSON.parse(request.options.body).countdown_seconds, 120);
  request.respond({ ...data, policy: { ...data.policy, countdown_seconds: 120 } });
  await pending;
});

test("UPS settings stay configurable while current admission is false and the preview describes sending before Windows completion", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, action: "hibernate", countdown_seconds: 15, ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  data.ups_poweroff = observedSequenceCapability();
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  assert.equal(ui.get("protection-preview-wait").textContent, "Aviso inmediato.");
  assert.equal(ui.get("protection-preview-action").textContent, "A los 15 s");
  assert.equal(ui.get("protection-preview-action-detail").textContent, "Se solicita hibernar Windows");
  assert.equal(ui.get("protection-preview-ups").textContent, "+3 min");
  assert.equal(ui.get("protection-preview-ups-detail").textContent, "Ahora está bloqueado. Revisa las condiciones del UPS.");
  assert.match(html, /temporizador del UPS no espera una confirmación de que Windows terminó/);
  assert.match(ui.get("ups-power-sequence-warning").textContent, /Esperando una lectura/);
  assert.equal(ui.get("ups-power-restart").textContent, "El UPS enciende sus salidas automáticamente cuando vuelve la luz.");
  data.policy.ups_poweroff = false;
  data.ups_poweroff = observedSequenceCapability({ available: true, can_configure: false });
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-ups-poweroff").disabled, true);
  assert.equal(ui.get("policy-ups-delay").disabled, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("saving longer cancellation and UPS delays preserves both actions without a local ten-second restriction", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, enabled: true, action: "hibernate", countdown_seconds: 15, ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  data.ups_poweroff = observedSequenceCapability();
  await pushStatus(ui, data);
  ui.get("policy-countdown").value = "120";
  ui.get("policy-ups-delay").value = "9";
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("protection-preview-action").textContent, "A los 120 s");
  assert.equal(ui.get("protection-preview-ups").textContent, "+9 min");
  assert.doesNotMatch(ui.get("ups-power-sequence-warning").textContent, /fuera.*secuencia|no se programará/);
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-countdown").value, "120");
  assert.equal(ui.get("policy-ups-delay").value, "9");
  const pending = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  const body = JSON.parse(request.options.body);
  assert.equal(body.countdown_seconds, 120);
  assert.equal(Object.hasOwn(body, "on_battery_seconds"), false);
  assert.equal(body.ups_poweroff_delay_seconds, 540);
  assert.equal(body.ups_poweroff, true);
  request.respond({ ...data, policy: { ...data.policy, countdown_seconds: 120, ups_poweroff_delay_seconds: 540 } });
  await pending;
  assert.equal(ui.get("protection-preview-ups").textContent, "+9 min");
});

test("the configured thirty-second countdown survives loading and unrelated changes without reintroducing an initial wait", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, countdown_seconds: 30, action: "hibernate", ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  data.ups_poweroff = observedSequenceCapability();
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-countdown").value, 30);
  assert.equal(ui.get("policy-countdown-note").textContent, "Puedes cancelar durante estos 30 segundos.");
  ui.get("policy-ups-delay").value = "4";
  ui.get("policy-form").emit("input");
  const pending = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  const body = JSON.parse(request.options.body);
  assert.equal(body.countdown_seconds, 30);
  assert.equal(body.ups_poweroff_delay_seconds, 240);
  assert.equal(Object.hasOwn(body, "on_battery_seconds"), false);
  request.respond({ ...data, policy: body });
  await pending;
  assert.equal(ui.get("policy-countdown").value, 30);
});

test("the three-step preview updates its single countdown and Windows action while leaving eligibility to the backend", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, action: "hibernate", countdown_seconds: 15, ups_poweroff: true };
  data.ups_poweroff = observedSequenceCapability();
  await pushStatus(ui, data);
  ui.get("policy-countdown").value = "30";
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("policy-countdown-label").textContent, "Tiempo antes de hibernar");
  assert.equal(ui.get("ups-power-delay").textContent, "De 1 a 9 min, desde la solicitud de hibernación.");
  assert.equal(ui.get("policy-countdown-note").textContent, "Puedes cancelar durante estos 30 segundos.");
  assert.equal(ui.get("protection-preview-action-detail").textContent, "Se solicita hibernar Windows");
  assert.equal(ui.get("battery-protection-subtitle").textContent, "Adelantar la hibernación");
  ui.get("policy-action").value = "shutdown";
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("policy-countdown-label").textContent, "Tiempo antes de apagar");
  assert.equal(ui.get("ups-power-delay").textContent, "De 1 a 9 min, desde la solicitud de apagado.");
  assert.equal(ui.get("protection-preview-wait").textContent, "Aviso inmediato.");
  assert.equal(ui.get("protection-preview-action").textContent, "A los 30 s");
  assert.equal(ui.get("protection-preview-action-detail").textContent, "Se solicita apagar Windows");
  assert.equal(ui.get("battery-protection-subtitle").textContent, "Adelantar el apagado de Windows");
  assert.equal(ui.get("protection-preview-ups").textContent, "+3 min");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  const pending = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  const body = JSON.parse(request.options.body);
  assert.equal(body.action, "shutdown");
  assert.equal(body.countdown_seconds, 30);
  assert.equal(body.ups_poweroff_delay_seconds, 180);
  assert.equal(body.ups_poweroff, true);
  assert.equal(Object.hasOwn(body, "on_battery_seconds"), false);
  request.respond({ ...data, policy: body });
  await pending;
  assert.equal(ui.get("battery-protection-subtitle").textContent, "Adelantar el apagado de Windows");
});

for (const countdown of [10, 30, 120]) {
  test(`configured UPS simulation at ${countdown} seconds does not impose a ten-second profile limit`, async () => {
    const { ui, data } = await currentUi();
    data.policy = { ...data.policy, action: "hibernate", countdown_seconds: countdown, ups_poweroff: true, ups_poweroff_delay_seconds: 240 };
    data.ups_poweroff = observedSequenceCapability();
    await pushStatus(ui, data);
    assert.match(ui.get("test-description").textContent, new RegExp(`${countdown} s.*240 s desde el envío`));
    const pending = ui.get("test-shutdown").emit("click");
    const request = ui.calls.at(-1);
    assert.equal(request.url, "/api/protection/test");
    assert.deepEqual(JSON.parse(request.options.body), { include_ups: true });
    request.respond({ ...data, protection: { state: "countdown", simulation: true, phase: "windows_countdown", countdown_remaining: countdown, deadline: new Date(ui.now() + countdown * 1000).toISOString() } });
    await pending;
    assert.match(ui.get("toast").textContent, /Windows y UPS no ejecutarán acciones reales/);
    assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
  });
}

function reviewableUpsCapability(changes = {}) {
  return observedSequenceCapability({ can_review: true, state: "unconfirmed",
    operation: { state: "review_required", operation_id: "own-production-operation", confirmation: "unconfirmed" }, ...changes });
}

test("closing an automatic UPS cycle review requires a manual acknowledgement and sends only metadata for its operation", async () => {
  const { ui, data } = await currentUi();
  assert.equal(ui.get("ups-power-review").hidden, true);
  data.policy = { ...data.policy, enabled: true, action: "hibernate", countdown_seconds: 10, ups_poweroff: true };
  data.ups_poweroff = reviewableUpsCapability();
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review").hidden, false);
  assert.equal(ui.get("ups-power-review-submit").disabled, true);
  await ui.get("ups-power-review-submit").emit("click");
  ui.tick();
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  ui.get("ups-power-review-ack").checked = true;
  ui.get("ups-power-review-ack").emit("change");
  ui.get("policy-form").emit("input", { target: ui.get("ups-power-review-ack") });
  assert.notEqual(ui.get("settings-feedback").textContent, "Cambios sin guardar");
  const pending = ui.get("ups-power-review-submit").emit("click");
  const request = ui.calls.at(-1);
  assert.equal(request.url, "/api/ups/poweroff/review");
  assert.deepEqual(JSON.parse(request.options.body), { operation_id: "own-production-operation", acknowledge_review: true });
  await ui.get("ups-power-review-submit").emit("click");
  request.respond({ ...data, ups_poweroff: { ...data.ups_poweroff, can_review: false, state: "completed",
    operation: { state: "completed", operation_id: "own-production-operation", confirmation: "unconfirmed", physical_confirmation: false } } });
  await pending;
  assert.equal(ui.get("ups-power-review").hidden, true);
  assert.equal(ui.get("ups-power-review-ack").checked, false);
  assert.equal(ui.get("toast").textContent, "Revisión cerrada; próxima protección preparada. El corte anterior no se confirma automáticamente.");
  assert.equal(ui.get("toast").classList.contains("error"), false);
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
});

test("production review requires backend eligibility, a known pending operation and current healthy mains output", async () => {
  const { ui, data } = await currentUi();
  for (const phase of ["review_required", "submitted", "armed", "uncertain", "cancel_submitted"]) {
    data.ups_poweroff = reviewableUpsCapability({ operation: { state: phase, operation_id: "own-production-operation" } });
    await pushStatus(ui, data);
    assert.equal(ui.get("ups-power-review").hidden, false, phase);
    assert.equal(ui.get("ups-power-review-ack").disabled, false, phase);
  }
  data.ups_poweroff.can_review = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review").hidden, true);
  data.ups_poweroff = reviewableUpsCapability({ operation: { state: "arming", operation_id: "not-submitted" } });
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review").hidden, true);
  data.ups_poweroff = reviewableUpsCapability();
  for (const change of [{ on_battery: true }, { low_battery: true }, { fault: true }, { output_voltage: 0 }, { timestamp: new Date(ui.now() - 16000).toISOString() }]) {
    data.sample = liveSample(change, ui.now());
    await pushStatus(ui, data);
    assert.equal(ui.get("ups-power-review-ack").disabled, true);
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("production review acknowledgement resets when operation, eligibility or API communication changes", async () => {
  const { ui, data } = await currentUi();
  data.ups_poweroff = reviewableUpsCapability();
  await pushStatus(ui, data);
  ui.get("ups-power-review-ack").checked = true;
  ui.get("ups-power-review-ack").emit("change");
  data.ups_poweroff.operation.operation_id = "another-production-operation";
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review-ack").checked, false);
  ui.get("ups-power-review-ack").checked = true;
  ui.get("ups-power-review-ack").emit("change");
  data.ups_poweroff.can_review = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review-ack").checked, false);
  data.ups_poweroff.can_review = true;
  await pushStatus(ui, data);
  ui.get("ups-power-review-ack").checked = true;
  ui.get("ups-power-review-ack").emit("change");
  ui.poll(); ui.calls.at(-1).reject(new TypeError("network failure")); await flush();
  assert.equal(ui.get("ups-power-review-ack").checked, false);
  assert.equal(ui.get("ups-power-review").hidden, true);
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-review-ack").checked, false);
  assert.equal(ui.get("ups-power-review-submit").disabled, true);
});

test("an unconfirmed production review never retries or claims the previous physical cut was verified", async () => {
  const { ui, data } = await currentUi();
  data.ups_poweroff = reviewableUpsCapability();
  await pushStatus(ui, data);
  ui.get("ups-power-review-ack").checked = true;
  ui.get("ups-power-review-ack").emit("change");
  const pending = ui.get("ups-power-review-submit").emit("click");
  ui.calls.at(-1).reject(new TypeError("network failure"));
  await pending;
  assert.equal(ui.get("ups-power-review-submit").disabled, true);
  assert.match(ui.get("ups-power-review-feedback").textContent, /revisión sin confirmar.*No se ha confirmado automáticamente el corte anterior/);
  ui.tick();
  await ui.get("ups-power-review-submit").emit("click");
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
});

test("Protection is the second sidebar destination and keyboard order matches the production navigation", async () => {
  const expected = ["overview", "protection", "history", "system", "webhooks"];
  const sidebar = html.match(/<nav class="sidebar-nav"[\s\S]*?<\/nav>/)?.[0];
  assert.deepEqual([...sidebar.matchAll(/data-view="([^"]+)"/g)].map((match) => match[1]), expected);
  const { ui } = await currentUi({}, { navigation: true });
  assert.deepEqual(ui.nav.map((button) => button.dataset.view), expected);
  ui.nav[0].emit("keydown", { key: "ArrowDown" });
  assert.equal(ui.nav[1].focused, true);
  ui.nav[1].emit("click");
  assert.equal(ui.get("view-title").textContent, "Protección");
  assert.equal(ui.location.hash, "proteccion");
  ui.nav[1].emit("keydown", { key: "ArrowDown" });
  assert.equal(ui.nav[2].focused, true);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("production Protection contains settings, harmless simulation and cycle review without manual test cards or dialogs", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.policy = { ...data.policy, enabled: true, action: "hibernate", countdown_seconds: 10, ups_poweroff: true };
  data.ups_poweroff = observedSequenceCapability();
  data.ups_cancel_test = { state: "finished", result: "cancel_observed_by_user" };
  data.ups_battery_test = { state: "finished", result: "automatic_return_observed", windows_request: "unconfirmed" };
  await pushStatus(ui, data);
  ui.open("protection");
  for (const id of ["ups-cancel-test-heading", "ups-cancel-test-start", "ups-cancel-test-dialog", "ups-battery-test-heading", "ups-battery-test-start", "ups-battery-test-dialog", "ups-battery-test-observation"]) assert.equal(ui.document.getElementById(id), null, id);
  assert.doesNotMatch(html, /PRUEBA FÍSICA MANUAL|ups-cancel-test-card|ups-battery-test-card/);
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.equal(ui.get("policy-countdown").value, 10);
  assert.equal(ui.get("policy-ups-poweroff").checked, true);
  assert.ok(ui.document.getElementById("test-shutdown"));
  assert.ok(ui.document.getElementById("ups-power-review"));
  assert.equal(ui.get("ups-cancel-test-urgent").hidden, true);
  assert.equal(ui.get("ups-battery-test-urgent").hidden, true);
  ui.tick();
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  assert.doesNotMatch(source, /post\("\/api\/ups\/(?:cancel-test|battery-test)/);
});

test("a real manual test still pending on the backend remains visible and blocks production mutations without lab controls", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.ups_cancel_test = { state: "uncertain", result: "pending_bit_not_reported", operation_id: "diagnostic-operation", remaining: 170,
    message: "El UPS no informó el corte pendiente. Hiberna manualmente ahora; el corte puede ocurrir al vencer el plazo." };
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-cancel-test-urgent").hidden, false);
  assert.match(ui.get("ups-cancel-test-urgent-message").textContent, /Hiberna manualmente ahora.*Tiempo calculado desde el envío: 02:50/);
  for (const name of ["overview", "protection", "system"]) {
    ui.open(name);
    assert.equal(ui.get("ups-cancel-test-urgent").hidden, false);
  }
  for (const id of ["save-policy", "policy-enabled", "test-shutdown", "stop-monitor", "confirm-stop-monitor"]) assert.equal(ui.get(id).disabled, true, id);
  await ui.get("policy-form").emit("submit");
  await ui.get("test-shutdown").emit("click");
  await ui.get("confirm-stop-monitor").emit("click");
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.match(ui.get("ups-cancel-test-urgent-message").textContent, /último estado recibido.*no se considera cancelada/);
  assert.equal(ui.document.getElementById("ups-cancel-test-start"), null);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("pending battery diagnostics distinguish a rejected request from unconfirmed hibernation and retain review without an operation id", async () => {
  const { ui, data } = await currentUi();
  data.ups_battery_test = { state: "cancel_submitted", windows_request: "unconfirmed", operation_id: "diagnostic-battery-operation", remaining: 160 };
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-battery-test-urgent").hidden, false);
  assert.match(ui.get("ups-battery-test-urgent-message").textContent, /hibernación quedó sin confirmar.*no demuestra.*haya terminado/);
  assert.doesNotMatch(ui.get("ups-battery-test-urgent-message").textContent, /rechazó/);
  data.ups_battery_test.windows_request = "failed";
  await pushStatus(ui, data);
  assert.match(ui.get("ups-battery-test-urgent-message").textContent, /Windows rechazó.*cancelación intentada no está confirmada/);
  data.ups_battery_test = { state: "review_required", operation_id: null };
  await pushStatus(ui, data);
  assert.match(ui.get("ups-battery-test-urgent-message").textContent, /requiere revisión desde el diagnóstico/);
  assert.equal(ui.get("save-policy").disabled, true);
  data.ups_battery_test = { state: "finished", result: "automatic_return_observed" };
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-battery-test-urgent").hidden, true);
  assert.equal(ui.get("save-policy").disabled, false);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("Protection is always expanded and its preview uses one Windows countdown and an independent UPS delay", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.policy = { enabled: false, ups_poweroff: true, action: "hibernate" };
  data.ups_poweroff = observedSequenceCapability();
  await pushStatus(ui, data);
  ui.open("protection");
  const page = html.match(/<section id="page-protection"[\s\S]*?(?=<section id="page-system")/)[0];
  assert.doesNotMatch(page, /id="settings-toggle"|aria-expanded|disclosure-arrow/);
  assert.doesNotMatch(page, /<details\b|<summary\b|Último bloqueo/, "operating details are always visible without block history");
  assert.match(page, /<h5 id="ups-power-details-heading">Detalles de funcionamiento<\/h5>/);
  assert.equal(ui.get("ups-power-details").hidden, false);
  assert.equal(ui.document.getElementById("ups-power-last-block"), null);
  assert.equal(ui.document.getElementById("settings-toggle"), null);
  assert.equal(ui.get("settings-panel").hidden, false);
  assert.equal(ui.get("policy-countdown").value, 15);
  assert.equal(ui.get("policy-ups-delay").value, 3);
  assert.equal(ui.get("protection-preview-wait").textContent, "Aviso inmediato.");
  assert.equal(ui.get("protection-preview-action").textContent, "A los 15 s");
  assert.equal(ui.get("protection-preview-action-detail").textContent, "Se solicita hibernar Windows");
  assert.equal(ui.get("protection-preview-ups").textContent, "+3 min");
  ui.get("policy-countdown").value = "120";
  ui.get("policy-ups-delay").value = "1";
  ui.get("policy-form").emit("input");
  assert.equal(ui.document.getElementById("policy-on-battery"), null);
  assert.equal(ui.get("protection-preview-action").textContent, "A los 120 s");
  assert.equal(ui.get("protection-preview-ups").textContent, "+1 min");
  assert.equal(ui.get("protection-preview-ups-detail").textContent, "Ahora está bloqueado. Revisa las condiciones del UPS.");
  assert.match(html, /temporizador del UPS no espera una confirmación de que Windows terminó/);
  const notes = page.match(/<ul class="protection-notes">[\s\S]*?<\/ul>/)[0];
  assert.match(notes, /Si vuelve la luz durante la cuenta atrás, se cancela la actuación/);
  assert.match(notes, /id="ups-power-restart"/);
  assert.match(notes, /El temporizador del UPS no espera una confirmación de que Windows terminó/);
  assert.match(notes, /0 % desactiva la prioridad por porcentaje.*alarma crítica del UPS sigue atendida/);
  assert.match(notes, /porcentaje estimado no es una medición directa del UPS/);
});

test("the only protection-cancel button cancels Windows and UPS through one endpoint and can be reopened from the live banner", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.policy = { ...data.policy, enabled: true, action: "hibernate", ups_poweroff: true, countdown_seconds: 15, ups_poweroff_delay_seconds: 180 };
  data.ups_poweroff = observedSequenceCapability();
  data.protection = { state: "countdown", simulation: false, countdown_remaining: 15, deadline: new Date(ui.now() + 15000).toISOString() };
  await pushStatus(ui, data);
  assert.equal([...html.matchAll(/<button\b[^>]*>Cancelar protección<\/button>/g)].length, 1);
  assert.equal(ui.document.getElementById("cancel-shutdown"), null);
  assert.equal(ui.get("critical-dialog").open, true);
  ui.get("critical-ack").emit("click");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, false);
  ui.get("open-protection-countdown").emit("click");
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-cancel").textContent, "Cancelar protección");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  const pending = ui.get("critical-cancel").emit("click");
  const request = ui.calls.at(-1);
  assert.equal(request.url, "/api/protection/countdown/cancel");
  assert.deepEqual(JSON.parse(request.options.body), { countdown_id: data.protection.countdown_id });
  request.respond({ ...data, protection: { state: "cancelled", suppressed: true }, ups_poweroff: { ...data.ups_poweroff, operation: { state: "idle" } } });
  await pending;
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("protection-alert").hidden, true);
  assert.equal(ui.calls.filter((call) => call.options.method === "POST").length, 1);
});

test("UPS delay is an integer from one to nine minutes and the policy payload contains seconds", async () => {
  const { ui, data } = await currentUi();
  assert.equal(ui.get("policy-ups-delay").getAttribute("min"), "1");
  assert.equal(ui.get("policy-ups-delay").getAttribute("max"), "9");
  for (const value of ["0", "10", "1.5", "", "not-a-number"]) {
    ui.get("policy-ups-delay").value = value;
    ui.get("policy-form").emit("input");
    await ui.get("policy-form").emit("submit");
    assert.match(ui.get("settings-feedback").textContent, /entero de 1 a 9 minutos/);
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  ui.get("policy-ups-delay").value = "9";
  ui.get("policy-form").emit("input");
  const pending = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  assert.equal(JSON.parse(request.options.body).ups_poweroff_delay_seconds, 540);
  request.respond({ ...data, policy: { ...data.policy, ups_poweroff_delay_seconds: 540 } });
  await pending;
  assert.equal(ui.get("policy-ups-delay").value, 9);
});

test("the global countdown banner is absent outside a real or simulated active countdown", async () => {
  const { ui, data } = await currentUi();
  for (const protection of [{ state: "idle" }, { state: "blocked" }, { state: "executed" }, { state: "cancelled" }, { state: "idle", simulation: true, phase: "complete" }]) {
    data.protection = protection;
    await pushStatus(ui, data);
    assert.equal(ui.get("protection-alert").hidden, true);
  }
  for (const simulation of [false, true]) {
    data.protection = { state: "countdown", simulation, countdown_remaining: 15 };
    await pushStatus(ui, data);
    assert.equal(ui.get("protection-alert").hidden, false);
  }
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("an unavailable UPS keeps the exact backend reason visible without disabling Windows protection", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, enabled: true, action: "hibernate", ups_poweroff: true, countdown_seconds: 15, ups_poweroff_delay_seconds: 180 };
  const reason = "La lectura, carga o tensión quedó fuera de la secuencia observada; requiere una nueva lectura válida de red.";
  data.ups_poweroff = observedSequenceCapability({ available: false, reason, sequence_admission: { available: false, reason } });
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-capability").textContent, reason);
  assert.equal(ui.get("ups-power-capability").hidden, false);
  assert.equal(ui.get("ups-power-availability").textContent, "Bloqueado ahora");
  assert.equal(ui.get("ups-power-sequence-warning").hidden, true);
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("the preview precedes settings and keeps separate steps for the warning, Windows and the UPS", async () => {
  const { ui, data } = await currentUi();
  const page = html.match(/<section id="page-protection"[\s\S]*?(?=<section id="page-system")/)[0];
  assert.ok(page.indexOf('class="protection-preview"') < page.indexOf('id="policy-fields"'));
  assert.doesNotMatch(page, /id="ups-power-sequence"/);
  assert.doesNotMatch(page, /Espera antes del aviso|policy-on-battery|policy-wait-note/);
  assert.match(page, /Tiempo antes de hibernar/);
  assert.match(page, /Si vuelve la luz durante la cuenta atrás, se cancela la acción y el aviso se cierra al confirmarse la recuperación\. El incidente se conserva en el historial\./);
  assert.match(page, /Apagar el UPS después de/);
  data.policy = { ...data.policy, countdown_seconds: 15, action: "hibernate", ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  await pushStatus(ui, data);
  assert.equal(ui.get("protection-preview-countdown").textContent, "Corte detectado");
  assert.equal(ui.get("protection-preview-wait").textContent, "Aviso inmediato.");
  assert.equal(ui.get("protection-preview-action").textContent, "A los 15 s");
  assert.equal(ui.get("protection-preview-action-detail").textContent, "Se solicita hibernar Windows");
  assert.equal(ui.get("protection-preview-ups").textContent, "+3 min");
  data.policy.ups_poweroff = false;
  await pushStatus(ui, data);
  assert.equal(ui.get("protection-preview-ups").textContent, "El UPS permanece encendido");
});

for (const phase of ["blocked", "executed"]) {
  test(`${phase} protection still opens its global notice without showing a countdown banner or repeating after acknowledgement`, async () => {
    const { ui, data } = await currentUi();
    data.protection = { state: phase, reason: phase === "blocked" ? "La solicitud de protección requiere atención." : "Windows recibió la solicitud." };
    await pushStatus(ui, data);
    assert.equal(ui.get("protection-alert").hidden, true);
    assert.equal(ui.get("critical-dialog").open, true);
    assert.equal(ui.get("critical-cancel").hidden, true);
    assert.equal(ui.get("critical-countdown").hidden, true);
    assert.match(ui.get("critical-title").textContent, phase === "blocked" ? /requiere atención/ : /Solicitud enviada a Windows/);
    ui.get("critical-ack").emit("click");
    await pushStatus(ui, data);
    assert.equal(ui.get("critical-dialog").open, false);
    assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  });
}

test("an explicit non-cancelable Windows phase never exposes or sends a cancellation", async () => {
  const { ui, data } = await currentUi();
  data.protection = { state: "countdown", phase: "windows_countdown", cancelable: false, countdown_remaining: 15 };
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.equal(ui.get("critical-countdown").hidden, false);
  assert.equal(ui.get("critical-cancel").hidden, true);
  await ui.get("critical-cancel").emit("click");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("the production timeline has three inline SVG device icons and each input keeps its helper directly below it", () => {
  const page = html.match(/<section id="page-protection"[\s\S]*?(?=<section id="page-system")/)[0];
  const timeline = page.match(/<section class="protection-preview"[\s\S]*?<\/section>/)[0];
  assert.deepEqual([...timeline.matchAll(/class="preview-icon" data-icon="([^"]+)"/g)].map((match) => match[1]), ["plug", "laptop", "ups"]);
  assert.equal([...timeline.matchAll(/<svg\b/g)].length, 3);
  assert.doesNotMatch(timeline, /<img|data:image/);
  assert.doesNotMatch(page, /Protege tu trabajo|Una cuenta atrás para cancelar toda|id="policy-state"|id="policy-action-description"|Se programa cuando se solicita/);
  const countdownLabel = page.match(/<label><span id="policy-countdown-label"[\s\S]*?<\/label>/)[0];
  const upsLabel = page.match(/<label class="ups-delay-field">[\s\S]*?<\/label>/)[0];
  assert.doesNotMatch(page, /policy-on-battery|policy-wait-note/);
  assert.match(countdownLabel, /id="policy-countdown"[\s\S]*id="policy-countdown-note"[^>]*>Puedes cancelar/);
  assert.match(upsLabel, /id="policy-ups-delay"[\s\S]*id="ups-power-delay"/);
});

test("healthy UPS availability uses the four condition rows while blocked operations keep the exact reason visible", async () => {
  const { ui, data } = await currentUi({}, { navigation: true });
  data.ups_poweroff = observedSequenceCapability({ available: true, reason: "Disponible para configurar.", sequence_admission: { available: true }, operation: { state: "idle" } });
  await pushStatus(ui, data);
  ui.open("protection");
  assert.equal(ui.get("view-description").textContent, "Elige qué hacer cuando se corte la luz.");
  for (const phase of ["idle", "completed", "cancelled"]) {
    data.ups_poweroff.operation.state = phase;
    await pushStatus(ui, data);
    assert.equal(ui.get("ups-power-capability").hidden, true, "healthy explanatory text does not duplicate the current condition rows");
    assert.equal(ui.get("ups-power-availability").textContent, "Disponible ahora");
    assert.equal(ui.get("ups-power-checks").hidden, false);
    assert.equal(ui.get("ups-power-checks").children.length, 4);
    assert.ok(ui.get("ups-power-checks").children.every((row) => row.dataset.state === "passed"), phase);
  }
  data.ups_poweroff.available = false;
  data.ups_poweroff.can_configure = true;
  data.ups_poweroff.reason = "Falta una lectura reciente sin batería baja.";
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-capability").hidden, false);
  assert.match(ui.get("ups-power-capability").textContent, /lectura reciente sin batería baja/);
  data.ups_poweroff.available = false;
  data.ups_poweroff.operation.state = "review_required";
  data.ups_poweroff.reason = "La operación anterior requiere revisión.";
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-capability").hidden, false);
  assert.match(ui.get("ups-power-capability").textContent, /requiere revisión/);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

function fourUpsBlockers(changes = {}) {
  return ["connection", "windows", "operation", "controlled_shutdown"].map((key) => ({ key, blocked: false, reason: null, reasons: [], ...changes[key] }));
}

test("UPS protection shows exactly four current blockers and ignores removed voltage, load and historical prerequisites", async () => {
  const { ui, data } = await currentUi();
  data.policy = { ...data.policy, enabled: true, action: "hibernate", countdown_seconds: 10, ups_poweroff: true, ups_poweroff_delay_seconds: 60 };
  data.ups_poweroff = observedSequenceCapability({ available: true, reason: "Condiciones actuales válidas.",
    blockers: fourUpsBlockers(),
    sequence_admission: { available: true, checks: [
      { key: "battery_voltage", label: "Límite de voltaje antiguo", passed: false, value: 25.18 },
      { key: "load_percent", label: "Carga inferior al 100 %", passed: false, value: 100 },
      { key: "sequence_continuity", label: "Red eléctrica previa", passed: false },
    ] } });
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-availability").textContent, "Disponible ahora");
  assert.equal(ui.get("ups-power-checks").children.length, 4);
  assert.deepEqual(ui.get("ups-power-checks").children.map((row) => row.dataset.check), ["connection", "windows", "operation", "controlled_shutdown"]);
  assert.match(textOf(ui.get("ups-power-checks")), /UPS conectado y compatible.*Windows preparado para hibernar.*Sin pruebas ni apagados pendientes.*Estado de la batería y del UPS/);
  assert.ok(ui.get("ups-power-checks").children.every((row) => row.dataset.state === "passed"));
  assert.match(ui.get("ups-power-conditions-note").textContent, /voltaje y la carga no bloquean/);
  assert.doesNotMatch(textOf(ui.get("ups-power-checks")), /25[.,]18|100 %|Red eléctrica previa/);
  assert.equal(ui.get("protection-preview-action").textContent, "A los 10 s");
  assert.equal(ui.get("protection-preview-ups").textContent, "+1 min");
  assert.equal(ui.get("protection-preview-ups-detail").textContent, "Corte programado del UPS.");
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("a failed operating check keeps its exact reason visible while the user edits either timer", async () => {
  const { ui, data } = await currentUi();
  const reason = "El UPS reporta batería crítica; una orden podría cortar inmediatamente. Se prioriza hibernar Windows.";
  data.policy = { ...data.policy, enabled: true, action: "hibernate", ups_poweroff: true, countdown_seconds: 10, ups_poweroff_delay_seconds: 60 };
  data.ups_poweroff = observedSequenceCapability({ available: false, reason, reason_code: "low_battery",
    checks: [{ key: "low_battery", label: "Sin alarma crítica del UPS", passed: false, blocking: true, severity: "critical", reason }],
    sequence_admission: { available: false, reason } });
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-availability").textContent, "Bloqueado ahora");
  assert.match(textOf(ui.get("ups-power-checks")), /Bloqueado/);
  assert.equal(ui.get("ups-power-checks").children[3].dataset.state, "blocked");
  assert.ok(textOf(ui.get("ups-power-checks")).includes(reason));
  ui.get("policy-countdown").value = "30";
  ui.get("policy-ups-delay").value = "3";
  ui.get("policy-form").emit("input");
  assert.equal(ui.get("ups-power-capability").hidden, false);
  assert.equal(ui.get("ups-power-capability").textContent, reason);
  assert.equal(ui.get("protection-preview-ups-detail").textContent, "Ahora está bloqueado. Revisa las condiciones del UPS.");
  assert.equal(ui.get("policy-enabled").checked, true);
  assert.equal(ui.get("policy-ups-poweroff").disabled, false);
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.equal(ui.get("ups-power-availability").textContent, "Sin confirmar");
  assert.equal(ui.get("ups-power-checks").children[0].dataset.state, "unknown");
  assert.match(textOf(ui.get("ups-power-checks")), /Sin confirmar/);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("each of the four blocker categories displays its exact reasons without hiding other categories", async () => {
  const { ui, data } = await currentUi();
  const cases = [
    ["connection", "El UPS no responde por USB."],
    ["windows", "La hibernación no está disponible en Windows."],
    ["operation", "Ya hay una orden de apagado pendiente."],
    ["controlled_shutdown", "El UPS reporta batería crítica; la orden podría cortar inmediatamente."],
  ];
  for (const [key, reason] of cases) {
    data.ups_poweroff = observedSequenceCapability({ available: false, reason,
      blockers: fourUpsBlockers({ [key]: { blocked: true, reason, reasons: [reason] } }),
      sequence_admission: { available: false, reason } });
    await pushStatus(ui, data);
    const rows = ui.get("ups-power-checks").children;
    assert.equal(rows.length, 4);
    assert.equal(rows.filter((row) => row.dataset.state === "blocked").length, 1);
    const row = rows.find((item) => item.dataset.check === key);
    assert.equal(row.dataset.state, "blocked");
    assert.ok(textOf(row).includes(reason));
    assert.equal(textOf(row).split(reason).length - 1, 1, "the reason is not duplicated between reason and reasons");
    assert.equal(ui.get("ups-power-availability").textContent, "Bloqueado ahora");
  }
  const reason = "El UPS reporta una falla.";
  const detail = "No se puede garantizar que respete el retardo configurado.";
  data.ups_poweroff.blockers = fourUpsBlockers({ controlled_shutdown: { blocked: true, reason, reasons: [detail] } });
  await pushStatus(ui, data);
  const row = ui.get("ups-power-checks").children[3];
  assert.ok(textOf(row).includes(reason));
  assert.ok(textOf(row).includes(detail));
  data.ups_poweroff.blockers = fourUpsBlockers({ controlled_shutdown: { blocked: true, reason: `${reason} ${detail}`, reasons: [reason, detail] } });
  await pushStatus(ui, data);
  assert.equal(textOf(ui.get("ups-power-checks").children[3]).split(reason).length - 1, 1, "an aggregate reason does not repeat its details");
  ui.poll(); ui.calls.at(-1).reject(new TypeError("offline")); await flush();
  assert.equal(ui.get("ups-power-availability").textContent, "Sin confirmar");
  assert.ok(ui.get("ups-power-checks").children.every((item) => item.dataset.state === "unknown"));
  assert.doesNotMatch(textOf(ui.get("ups-power-checks")), /El UPS reporta una falla/, "a stale alarm is not shown as a current condition");
});

test("native USB exposes a percentage threshold and saves it without changing either configured timer", async () => {
  const { ui, data } = await currentUi({ battery_percent_estimate: 35, battery_estimate_method: "nt2402pro-pmv-qs-v1" });
  data.policy = { ...data.policy, enabled: true, action: "hibernate", low_battery_percent: 20, countdown_seconds: 30, ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-battery-field").hidden, false);
  assert.equal(ui.get("policy-low-battery").value, 20);
  assert.equal(ui.get("policy-low-battery").getAttribute("aria-describedby"), "policy-battery-help policy-battery-source");
  assert.equal(ui.get("policy-battery-source").textContent, "Actual: 35 % estimado");
  assert.match(ui.get("policy-battery-source").title, /no mide directamente/);
  assert.match(ui.get("policy-battery-help").textContent, /Si estás usando batería.*10 s como máximo.*retardo del UPS no cambia/);
  assert.match(ui.get("battery-note").title, /protección por porcentaje/);
  const page = html.match(/<section id="page-protection"[\s\S]*?(?=<section id="page-system")/)[0];
  assert.match(page, /Batería baja/);
  assert.equal([...page.matchAll(/<input\b[^>]*type="number"/g)].length, 4, "only percentage, hidden legacy runtime, Windows countdown and UPS delay inputs exist");
  ui.get("policy-low-battery").value = "15";
  ui.get("policy-form").emit("input");
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-low-battery").value, "15", "polling preserves an edited threshold");
  const saved = ui.get("policy-form").emit("submit");
  const request = ui.calls.at(-1);
  assert.equal(request.url, "/api/policy");
  const body = JSON.parse(request.options.body);
  assert.equal(body.low_battery_percent, 15);
  assert.equal(body.countdown_seconds, 30);
  assert.equal(body.ups_poweroff_delay_seconds, 180);
  request.respond({ ...data, policy: { ...data.policy, low_battery_percent: 15 } }); await saved;
  assert.equal(ui.get("policy-low-battery").value, 15);
});

test("percentage alerts honor the saved threshold, prefer native charge and label estimates explicitly", async () => {
  const { ui, data } = await currentUi({ on_battery: true, battery_percent: 30, battery_percent_estimate: 5, battery_estimate_method: "nt2402pro-pmv-qs-v1" });
  data.policy.low_battery_percent = 10;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.match(ui.get("policy-battery-source").textContent, /30 %.*reportado por el UPS/);
  data.sample = liveSample({ on_battery: true, battery_percent_estimate: 11, battery_estimate_method: "nt2402pro-pmv-qs-v1" }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample = liveSample({ on_battery: true, battery_percent_estimate: 10, battery_estimate_method: "nt2402pro-pmv-qs-v1" }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(textOf(ui.get("critical-list")), /Carga estimada de batería: 10 %.*Umbral configurado: 10 %/);
  assert.equal(ui.get("battery-bar").classList.contains("low"), true);
});

test("an unknown twenty-percent estimate stays informational and cannot claim percentage priority", async () => {
  const { ui, data } = await currentUi({ on_battery: true, battery_percent_estimate: 20, battery_estimate_method: "unknown-estimator" });
  data.policy.low_battery_percent = 20;
  await pushStatus(ui, data);
  assert.equal(ui.get("battery-percent").textContent, "20");
  assert.equal(ui.get("battery-note").textContent, "Carga estimada");
  assert.match(ui.get("battery-note").title, /Estimación informativa.*no se usa en la protección por porcentaje/);
  assert.match(ui.get("policy-battery-source").textContent, /20 %.*estimado.*Estimación informativa.*no se usa/);
  assert.match(ui.get("policy-battery-help").textContent, /estimación actual es solo informativa/);
  assert.doesNotMatch(ui.get("policy-battery-help").textContent, /se reduce a 10 s/);
  assert.equal(ui.get("battery-bar").classList.contains("low"), false);
  assert.equal(ui.get("policy-battery-emergency").hidden, true);
  assert.equal(ui.get("critical-dialog").open, false);
  data.sample.low_battery = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(textOf(ui.get("critical-list")), /El UPS ha enviado la señal de batería baja/);
});

test("legacy informational readings do not become additional blocking rows", async () => {
  const { ui, data } = await currentUi();
  for (const value of [null, 0]) {
    data.ups_poweroff = observedSequenceCapability({ available: true, sequence_admission: { available: true },
      checks: [{ key: "battery_voltage", label: "Voltaje de batería informativo", passed: true, blocking: false, severity: "info", value }] });
    await pushStatus(ui, data);
    assert.equal(ui.get("ups-power-availability").textContent, "Disponible ahora");
    assert.equal(ui.get("ups-power-checks").children.length, 4);
    assert.ok(ui.get("ups-power-checks").children.every((row) => row.dataset.state === "passed"));
    assert.doesNotMatch(textOf(ui.get("ups-power-checks")), /Voltaje|Requisito|Bloqueado/);
  }
});

test("percentage emergency explains the shorter Windows countdown and keeps the selected UPS delay", async () => {
  const { ui, data } = await currentUi({ on_battery: true, battery_percent_estimate: 18, battery_estimate_method: "nt2402pro-pmv-qs-v1" });
  data.policy = { ...data.policy, enabled: true, action: "hibernate", low_battery_percent: 20, countdown_seconds: 120, ups_poweroff: true, ups_poweroff_delay_seconds: 180 };
  data.protection = { state: "countdown", countdown_remaining: 8, configured_countdown_seconds: 120, effective_countdown_seconds: 10,
    emergency: { active: true, source: "estimated_percentage", percent: 18, threshold_percent: 20, seconds_limit: 10, immediate: false } };
  await pushStatus(ui, data);
  assert.equal(ui.get("policy-battery-emergency").hidden, false);
  assert.match(ui.get("policy-battery-emergency").textContent, /estimada: 18 %.*umbral de 20 %.*10 s.*conserva el retardo/);
  assert.equal(ui.get("policy-countdown").value, 120, "an emergency does not overwrite the saved normal countdown");
  assert.equal(ui.get("policy-ups-delay").value, 3);
  assert.equal(ui.get("critical-countdown-time").textContent, "00:08", "the visible countdown uses the actual deadline rather than the normal configured duration");
  data.sample.low_battery = true;
  data.protection.countdown_remaining = 0;
  data.protection.emergency = { active: true, source: "ups_critical_alarm", percent: 18, threshold_percent: 20, seconds_limit: 10, immediate: true };
  await pushStatus(ui, data);
  assert.match(ui.get("policy-battery-emergency").textContent, /batería crítica.*prioridad inmediata.*No se programa.*podría cortar la energía/);
  assert.equal(ui.get("policy-ups-delay").value, 3);
});

test("zero disables percentage priority visibly while the UPS critical alarm still raises attention", async () => {
  const { ui, data } = await currentUi({ on_battery: true, battery_percent: 50 });
  data.policy.low_battery_percent = 0;
  data.sample = liveSample({ on_battery: true, battery_percent_estimate: 0, battery_estimate_method: "nt2402pro-pmv-qs-v1" }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, false);
  assert.match(ui.get("policy-battery-help").textContent, /prioridad por porcentaje está desactivada.*alarma crítica/);
  assert.equal(ui.get("policy-battery-emergency").hidden, true);
  data.sample.low_battery = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(textOf(ui.get("critical-list")), /El UPS ha enviado la señal de batería baja/);
});

test("a resolved UPS block closes after recovery and reload while its durable incident remains outside Protection", async () => {
  const { ui, data } = await currentUi();
  const reason = "Se perdió la comunicación USB antes de enviar la orden de apagado.";
  const blockedAt = new Date(ui.now()).toISOString();
  data.notifications.pending = [pendingNotice(ui, { kind: "ups_poweroff_blocked", title: "Corte del UPS bloqueado", message: reason })];
  data.ups_poweroff = observedSequenceCapability({ available: false, reason, sequence_admission: { available: false, reason },
    last_block: { reason, timestamp: blockedAt } });
  data.sample = liveSample({ on_battery: true }, ui.now());
  await pushStatus(ui, data);
  assert.equal(ui.get("critical-dialog").open, true);
  assert.match(textOf(ui.get("critical-list")), /Corte del UPS bloqueado.*comunicación USB.*Pendiente de Entendido/);
  ui.advance(5000);
  data.sample = liveSample({}, ui.now());
  data.ups_poweroff.available = true;
  data.ups_poweroff.reason = "Condiciones actuales válidas.";
  data.ups_poweroff.sequence_admission = { available: true };
  data.notifications.pending[0].resolved_at = new Date(ui.now()).toISOString();
  await pushStatus(ui, data);
  assert.equal(ui.get("ups-power-availability").textContent, "Disponible ahora");
  assert.equal(ui.get("critical-dialog").open, false);
  assert.equal(ui.get("ups-power-details").hidden, false);
  assert.equal(ui.document.getElementById("ups-power-block-history"), null);
  assert.equal(ui.document.getElementById("ups-power-last-block"), null);
  const reloaded = harness();
  reloaded.calls[0].respond(data); await flush();
  assert.equal(reloaded.get("critical-dialog").open, false);
  assert.equal(data.notifications.pending.length, 1);
  assert.equal(reloaded.calls.some((call) => call.options.method === "POST"), false);
  assert.equal(reloaded.document.getElementById("ups-power-last-block"), null);
  await pushStatus(reloaded, data);
  assert.equal(reloaded.get("critical-dialog").open, false);
});

async function connectedWebhooks(settings = {}, options = {}) {
  const ui = harness(options);
  const data = snapshot();
  Object.assign(data.webhooks.settings, settings);
  ui.calls[0].respond(data);
  await flush();
  return { ui, data };
}

test("Webhooks follows Sistema, opens by deep link and exposes the five default event switches", async () => {
  const { ui } = await connectedWebhooks({}, { navigation: true, hash: "#webhooks" });
  assert.equal(ui.get("page-webhooks").hidden, false);
  assert.equal(ui.get("view-title").textContent, "Webhooks");
  assert.equal(ui.get("view-description").textContent, "Configura y gestiona tus integraciones con sistemas externos.");
  assert.equal(ui.nav.at(-2).dataset.view, "system");
  assert.equal(ui.nav.at(-1).dataset.view, "webhooks");
  assert.equal(ui.get("webhook-enabled").checked, false);
  assert.equal(ui.get("webhook-url").value, "");
  for (const event of ["power_lost", "power_restored", "battery_low", "overload", "ups_fault"]) {
    assert.equal(ui.get("webhook-event-" + event).checked, ["power_lost", "battery_low"].includes(event));
    assert.equal(ui.get("webhook-event-" + event).getAttribute("role"), "switch");
    assert.equal(ui.get("webhook-event-" + event).disabled, false, "Paused webhooks still permit editing event choices");
  }
  assert.equal(ui.get("webhook-url").disabled, false);
  assert.equal(ui.get("test-webhook").disabled, true);
  assert.equal(ui.get("webhook-event-count").textContent, "2 de 5 seleccionados");
  assert.equal(ui.get("shared-history").hidden, true);
  assert.equal(ui.intervals.filter((interval) => interval.ms === 2000).length, 1);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("pausing webhooks saves the master switch without clearing its URL or selected events", async () => {
  const settings = { webhook_enabled: true, webhook_url: "https://n8n.example.test/webhook/elipro", webhook_events: ["power_lost", "battery_low", "ups_fault"] };
  const { ui, data } = await connectedWebhooks(settings);
  ui.get("webhook-enabled").checked = false;
  ui.get("webhooks-form").emit("input");
  assert.equal(ui.get("webhook-url").value, settings.webhook_url);
  assert.equal(ui.get("webhook-event-ups_fault").checked, true);
  assert.equal(ui.get("test-webhook").disabled, true);
  const pending = ui.get("webhooks-form").emit("submit");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/webhooks");
  assert.equal(call.options.headers["X-UPS-Token"], "test-token");
  assert.deepEqual(JSON.parse(call.options.body), { ...settings, webhook_enabled: false });
  data.webhooks.settings.webhook_enabled = false;
  call.respond(data);
  await pending;
  await pushStatus(ui, data);
  assert.equal(ui.get("webhook-url").value, settings.webhook_url);
  assert.equal(ui.get("webhook-event-ups_fault").checked, true);
  assert.equal(ui.get("webhook-event-count").textContent, "3 de 5 seleccionados");
  assert.equal(ui.get("webhook-enabled").checked, false);
  assert.match(ui.get("webhooks-feedback").textContent, /desactivados.*conservada/);
  ui.get("webhook-enabled").checked = true;
  ui.get("webhooks-form").emit("input");
  assert.equal(ui.get("webhook-url").value, settings.webhook_url);
  assert.equal(ui.get("webhook-event-ups_fault").checked, true);
});

test("webhook drafts survive polling, navigation and saves of unrelated desktop notification settings", async () => {
  const { ui, data } = await connectedWebhooks({}, { navigation: true });
  ui.open("webhooks");
  ui.get("webhook-enabled").checked = true;
  ui.get("webhook-url").value = "https://n8n.example.test/draft";
  ui.get("webhook-event-power_restored").checked = true;
  ui.get("webhooks-form").emit("input");
  ui.open("system");
  await pushStatus(ui, data);
  ui.get("notifications-sound").checked = false;
  ui.get("notifications-form").emit("input");
  const pending = ui.get("notifications-form").emit("submit");
  data.notifications.settings.sound_enabled = false;
  ui.calls.at(-1).respond(data);
  await pending;
  ui.open("webhooks");
  assert.equal(ui.get("webhook-enabled").checked, true);
  assert.equal(ui.get("webhook-url").value, "https://n8n.example.test/draft");
  assert.equal(ui.get("webhook-event-power_restored").checked, true);
  assert.equal(ui.get("webhook-event-count").textContent, "3 de 5 seleccionados");
  assert.equal(ui.get("webhooks-feedback").textContent, "Cambios sin guardar");
  assert.deepEqual(ui.calls.filter((call) => call.options.method === "POST").map((call) => call.url), ["/api/notifications"]);
});

test("testing a dirty webhook requires saving and never posts the draft destination", async () => {
  const { ui } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  ui.get("webhook-url").value = "https://n8n.example.test/unsaved";
  ui.get("webhooks-form").emit("input");
  await ui.get("test-webhook").emit("click");
  assert.match(ui.get("webhooks-feedback").textContent, /Guarda los cambios antes de probar/);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
  ui.get("webhook-url").value = "https://n8n.example.test/saved";
  ui.get("webhooks-form").emit("input");
  assert.equal(ui.get("webhooks-feedback").textContent, "");
});

test("a queued webhook test keeps status polling active until HTTP success is confirmed", async () => {
  const { ui, data } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  const pending = ui.get("test-webhook").emit("click");
  const call = ui.calls.at(-1);
  assert.equal(call.url, "/api/webhooks/test");
  assert.equal(call.options.headers["X-UPS-Token"], "test-token");
  assert.deepEqual(JSON.parse(call.options.body), {});
  data.webhooks.test = { id: "test-1", state: "queued", message: "Prueba en cola." };
  call.respond(data);
  await pending;
  assert.equal(ui.get("test-webhook").disabled, true);
  assert.match(ui.get("webhooks-test-feedback").textContent, /cola/);
  assert.doesNotMatch(ui.get("webhooks-test-feedback").textContent, /correctamente|éxito/);
  data.webhooks.test = { id: "test-1", state: "sending", message: "Enviando prueba." };
  const beforePoll = ui.calls.length;
  await pushStatus(ui, data);
  assert.equal(ui.calls.length, beforePoll + 1);
  assert.equal(ui.calls.at(-1).url, "/api/status");
  assert.match(ui.get("webhooks-test-feedback").textContent, /Enviando/);
  data.webhooks.test = { id: "test-1", state: "success", message: "Prueba recibida.", http_status: 204 };
  await pushStatus(ui, data);
  assert.match(ui.get("webhooks-test-feedback").textContent, /Prueba recibida.*HTTP 204/);
  assert.equal(ui.get("webhooks-test-feedback").classList.contains("error"), false);
  assert.equal(ui.get("test-webhook").disabled, false);
  assert.deepEqual(ui.calls.filter((request) => request.options.method === "POST").map((request) => request.url), ["/api/webhooks/test"]);
});

test("webhook HTTP errors remain local and a later cancelled test never claims successful delivery", async () => {
  const { ui, data } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  data.webhooks.test = { id: "test-error", state: "error", message: "El destino devolvió HTTP 500.", http_status: 500 };
  await pushStatus(ui, data);
  assert.equal(ui.get("webhooks-test-feedback").classList.contains("error"), true);
  assert.match(ui.get("webhooks-test-feedback").textContent, /HTTP 500/);
  assert.equal(ui.get("save-webhooks").disabled, false);
  assert.equal(ui.get("save-notifications").disabled, false);
  assert.equal(ui.get("test-webhook").disabled, false);
  assert.equal(ui.get("service-notice").hidden, true);
  data.webhooks.test = { id: "test-cancelled", state: "cancelled", message: "" };
  await pushStatus(ui, data);
  assert.match(ui.get("webhooks-test-feedback").textContent, /canceló al cambiar la configuración/);
  assert.equal(ui.get("webhooks-test-feedback").classList.contains("error"), false);
  assert.equal(ui.get("test-webhook").disabled, false);
});

test("dry-run tests explicitly report simulation and never display a misleading delivery success", async () => {
  const { ui, data } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  data.webhooks.dry_run = true;
  await pushStatus(ui, data);
  assert.equal(ui.get("webhooks-dry-run").hidden, false);
  const pending = ui.get("test-webhook").emit("click");
  data.webhooks.test = { id: "test-dry", state: "simulated", message: "Prueba aceptada." };
  ui.calls.at(-1).respond(data);
  await pending;
  assert.match(ui.get("webhooks-test-feedback").textContent, /simulada.*--dry-run.*No se envió/);
  assert.equal(ui.get("webhooks-test-feedback").classList.contains("simulated"), true);
  assert.equal(ui.get("webhooks-test-feedback").classList.contains("error"), false);
  assert.equal(ui.get("test-webhook").disabled, false);
});

test("API loss disables every webhook control and reconnect preserves unsaved URL and event edits", async () => {
  const { ui, data } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  ui.get("webhook-url").value = "https://n8n.example.test/draft";
  ui.get("webhook-event-overload").checked = true;
  ui.get("webhooks-form").emit("input");
  const pending = ui.poll();
  ui.calls.at(-1).reject(new TypeError("network failure"));
  await pending;
  for (const field of ui.get("webhooks-form").fields) assert.equal(field.disabled, true);
  assert.equal(ui.get("save-webhooks").disabled, true);
  assert.equal(ui.get("test-webhook").disabled, true);
  await pushStatus(ui, data);
  assert.equal(ui.get("webhook-url").value, "https://n8n.example.test/draft");
  assert.equal(ui.get("webhook-event-overload").checked, true);
  assert.equal(ui.get("save-webhooks").disabled, false);
  for (const field of ui.get("webhooks-form").fields) assert.equal(field.disabled, false);
});

test("webhook delivery history starts empty and exposes the bounded retry policy", async () => {
  const { ui } = await connectedWebhooks();
  assert.equal(ui.get("webhook-deliveries-empty").hidden, false);
  assert.equal(ui.get("webhook-deliveries-scroll").hidden, true);
  assert.equal(ui.get("webhook-deliveries-body").children.length, 0);
  assert.equal(ui.get("webhook-deliveries-count").textContent, "0 entregas");
  assert.match(html, /Máximo 2 intentos · Espera entre intentos: 5 s · Timeout por intento: 5 s/);
  const table = html.match(/<table id="webhook-deliveries-table"[\s\S]*?<\/table>/)[0];
  assert.deepEqual([...table.matchAll(/<th scope="col">([^<]+)<\/th>/g)].map((match) => match[1]), ["Fecha/hora", "Evento", "Estado", "Intentos", "Resultado"]);
});

test("webhook deliveries show real states and attempts, newest first, with the last fifty bounded", async () => {
  const { ui, data } = await connectedWebhooks();
  data.webhooks.deliveries = Array.from({ length: 52 }, (_unused, index) => ({
    id: `delivery-${index}`, event: index === 51 ? "power_lost" : "battery_low", timestamp: new Date(ui.now() - (51 - index) * 1000).toISOString(),
    state: index === 51 ? "success" : "error", attempts: index === 51 ? 1 : 2, max_attempts: 2,
    message: index === 51 ? "Recibido" : "Sin respuesta del destino", http_status: index === 51 ? 204 : null,
  }));
  await pushStatus(ui, data);
  const rows = ui.get("webhook-deliveries-body").children;
  assert.equal(rows.length, 50);
  assert.equal(ui.get("webhook-deliveries-count").textContent, "50 entregas");
  assert.equal(ui.get("webhook-deliveries-empty").hidden, true);
  assert.equal(ui.get("webhook-deliveries-scroll").hidden, false);
  assert.equal(rows[0].dataset.deliveryId, "delivery-51");
  assert.equal(rows.at(-1).dataset.deliveryId, "delivery-2");
  assert.match(textOf(rows[0].children[1]), /Corte de energía.*power_lost/);
  assert.equal(rows[0].children[2].children[0].textContent, "Entregado");
  assert.equal(rows[0].children[2].children[0].classList.contains("webhook-delivery-success"), true);
  assert.equal(rows[0].children[3].textContent, "1/2");
  assert.equal(rows[0].children[4].textContent, "Recibido (HTTP 204)");
  assert.equal(rows[1].children[2].children[0].textContent, "Fallido");
  assert.equal(rows[1].children[2].children[0].classList.contains("webhook-delivery-error"), true);
  assert.equal(rows[1].children[3].textContent, "2/2");
});

test("retrying webhook tests block duplicate clicks while polling updates the retry and final result", async () => {
  const { ui, data } = await connectedWebhooks({ webhook_enabled: true, webhook_url: "https://n8n.example.test/saved" });
  data.webhooks.test = {
    id: "retry-test", event: "webhook_test", timestamp: new Date(ui.now()).toISOString(), state: "retrying",
    attempts: 1, max_attempts: 2, message: "El destino no respondió.", next_retry_at: new Date(ui.now() + 5000).toISOString(),
  };
  data.webhooks.deliveries = [data.webhooks.test];
  await pushStatus(ui, data);
  assert.equal(ui.get("test-webhook").disabled, true);
  assert.equal(ui.get("test-webhook-label").textContent, "Esperando reintento…");
  assert.match(ui.get("webhooks-test-feedback").textContent, /primer intento falló.*Reintento en 5 s.*2\/2/);
  let row = ui.get("webhook-deliveries-body").children[0];
  assert.equal(row.children[2].children[0].textContent, "Reintento en 5 s");
  assert.equal(row.children[2].children[0].classList.contains("webhook-delivery-retrying"), true);
  assert.equal(row.children[3].textContent, "1/2");
  const beforeClick = ui.calls.length;
  await ui.get("test-webhook").emit("click");
  assert.equal(ui.calls.length, beforeClick);
  ui.advance(2000);
  await pushStatus(ui, data);
  row = ui.get("webhook-deliveries-body").children[0];
  assert.equal(row.children[2].children[0].textContent, "Reintento en 3 s");
  data.webhooks.test = { ...data.webhooks.test, state: "sending", attempts: 2, message: "Enviando segundo intento.", next_retry_at: null };
  data.webhooks.deliveries = [data.webhooks.test];
  await pushStatus(ui, data);
  assert.equal(ui.get("test-webhook").disabled, true);
  data.webhooks.test = { ...data.webhooks.test, state: "success", message: "Prueba recibida.", http_status: 200 };
  data.webhooks.deliveries = [data.webhooks.test];
  await pushStatus(ui, data);
  row = ui.get("webhook-deliveries-body").children[0];
  assert.equal(row.children[2].children[0].textContent, "Entregado");
  assert.equal(row.children[3].textContent, "2/2");
  assert.equal(ui.get("test-webhook").disabled, false);
  assert.match(ui.get("webhooks-test-feedback").textContent, /recibida.*HTTP 200/);
  assert.equal(ui.calls.some((call) => call.options.method === "POST"), false);
});

test("all webhook delivery states are readable and untrusted event names and results remain literal text", async () => {
  const { ui, data } = await connectedWebhooks();
  const created = [];
  const createElement = ui.document.createElement;
  ui.document.createElement = (tag) => { created.push(tag); return createElement(tag); };
  const states = ["queued", "sending", "retrying", "success", "error", "cancelled", "simulated", "unexpected"];
  data.webhooks.deliveries = states.map((state, index) => ({
    id: `state-${state}`, event: index === 0 ? '<img src=x onerror="alert(1)">' : "webhook_test",
    timestamp: new Date(ui.now() - index * 1000).toISOString(), state,
    attempts: state === "queued" || state === "simulated" ? 0 : 1, max_attempts: 2,
    message: index === 0 ? "<script>alert('delivery')</script>" : "Resultado literal",
  }));
  await pushStatus(ui, data);
  const rows = ui.get("webhook-deliveries-body").children;
  assert.deepEqual(rows.map((row) => row.children[2].children[0].textContent), ["En cola", "Enviando", "Reintento en 5 s", "Entregado", "Fallido", "Cancelado", "Simulado", "No disponible"]);
  assert.match(textOf(rows[0].children[1]), /<img src=x onerror="alert\(1\)">/);
  assert.equal(rows[0].children[4].textContent, "<script>alert('delivery')</script>");
  assert.equal(rows[0].children[3].textContent, "0/2");
  assert.equal(created.includes("img"), false);
  assert.equal(created.includes("script"), false);
  data.webhooks.deliveries = [];
  await pushStatus(ui, data);
  assert.equal(ui.get("webhook-deliveries-body").children.length, 0);
  assert.equal(ui.get("webhook-deliveries-empty").hidden, false);
  assert.equal(ui.get("webhook-deliveries-scroll").hidden, true);
});
