"use strict";

(() => {
  const themeStorageKey = "elipro-theme";
  let selectedTheme = "dark";
  try {
    const savedTheme = localStorage.getItem(themeStorageKey);
    if (savedTheme === "light" || savedTheme === "dark") selectedTheme = savedTheme;
  } catch { /* Theme storage is optional; retain the established dark appearance. */ }

  function applyTheme(theme, persist = false) {
    selectedTheme = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = selectedTheme;
    document.documentElement.style.colorScheme = selectedTheme;
    const color = document.querySelector('meta[name="theme-color"]');
    color?.setAttribute("content", selectedTheme === "light" ? "#f3f6f8" : "#0b1015");
    const toggle = document.getElementById("theme-toggle");
    if (toggle) {
      const label = `Cambiar a modo ${selectedTheme === "light" ? "oscuro" : "claro"}`;
      toggle.setAttribute("aria-label", label);
      toggle.setAttribute("title", label);
      toggle.setAttribute("aria-pressed", String(selectedTheme === "light"));
    }
    if (persist) {
      try { localStorage.setItem(themeStorageKey, selectedTheme); }
      catch { /* The active theme still works when browser storage is unavailable. */ }
    }
  }

  // This external script precedes the stylesheet so a saved theme applies before paint.
  applyTheme(selectedTheme);

  function initializeApp() {
  const $ = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const state = {
    status: null,
    history: [],
    overviewHistory: [],
    overviewHistoryBusy: false,
    overviewHistoryError: null,
    overviewGeneration: 0,
    metric: "voltage",
    hours: 1,
    token: null,
    policyInitialized: false,
    policyDirty: false,
    notificationsInitialized: false,
    notificationsDirty: false,
    statusBusy: false,
    historyBusy: false,
    historyQueryError: null,
    toastTimer: null,
    chart: null,
    seenEvents: null,
    events: [],
    eventFilter: "all",
    serviceAvailable: false,
    lastStatusAt: null,
    mutation: null,
    historyInterval: 10000,
    statusGeneration: 0,
    recordVersion: 0,
    recordsLoaded: false,
    incidents: [],
    incidentCursor: null,
    incidentQuery: "",
    incidentsBusy: false,
    archive: [],
    archiveCursor: null,
    archiveBusy: false,
    archiveQuery: "",
    downloadBusy: false,
    stopAcknowledged: false,
    validationBusy: false,
    observation: null,
    observationLoaded: false,
    observationBusy: false,
    physicalTestsBusy: false,
    activeView: "overview",
    navigationReady: false,
    incidentsLoaded: false,
    archiveLoaded: false,
    incidentVersion: 0,
  };
  const views = {
    overview: { hash: "resumen", title: "Resumen", description: "Lo esencial de tu UPS, en tiempo real." },
    incidents: { hash: "incidentes", title: "Incidentes", description: "Cortes eléctricos y problemas de comunicación, con su recuperación." },
    history: { hash: "historial", title: "Historial", description: "Consulta mediciones y eventos, busca por fecha y exporta tus registros." },
    protection: { hash: "proteccion", title: "Protección", description: "Configura cuándo proteger tu sesión y comprueba las reglas de forma segura." },
    notifications: { hash: "avisos", title: "Avisos", description: "Notificaciones de Windows y sonido, incluso con el panel cerrado." },
    system: { hash: "sistema", title: "Sistema", description: "Salud del monitor, observación de estabilidad, diagnóstico y respaldos." },
  };
  const clockFormat = new Intl.DateTimeFormat("es-PE", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const shortClockFormat = new Intl.DateTimeFormat("es-PE", { hour: "2-digit", minute: "2-digit", hour12: false });
  const dateFormat = new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  const recordDateFormat = new Intl.DateTimeFormat("es-PE", { year: "numeric", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const numeric = (value, decimals = 0) => finite(value) ? new Intl.NumberFormat("es-PE", { maximumFractionDigits: decimals }).format(value) : "—";
  const parseDate = (value) => {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed : null;
  };

  function setText(id, value) {
    const element = $(id);
    if (element && element.textContent !== value) element.textContent = value;
  }

  function loadViewData() {
    if (!state.navigationReady || !state.serviceAvailable) return;
    if (state.activeView === "incidents" && !state.incidentsLoaded) loadIncidents();
    if (state.activeView === "history" && !state.archiveLoaded) loadArchive();
    if (state.activeView === "system" && !state.observationLoaded) loadObservation();
  }

  function activateView(name, { writeHash = false, focus = false } = {}) {
    if (!state.navigationReady) return;
    const selected = Object.hasOwn(views, name) ? name : "overview";
    const previousView = state.activeView;
    state.activeView = selected;
    const view = views[selected];
    document.querySelectorAll("[data-page]").forEach((page) => {
      const active = page.dataset.page === selected;
      page.hidden = !active;
      page.inert = !active;
      page.setAttribute("aria-hidden", String(!active));
    });
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
    document.body.dataset.view = selected;
    setText("view-title", view.title);
    setText("view-description", view.description);
    document.title = `${view.title} · Elipro UPS`;
    if (writeHash && window.location && window.location.hash !== `#${view.hash}`) window.location.hash = view.hash;
    if (selected === "history") renderChart();
    if (selected === "overview") renderOverviewChart();
    hideTooltip();
    loadViewData();
    if (focus || selected !== previousView) window.scrollTo?.({ top: 0, left: 0, behavior: "instant" });
    if (focus) $("page-title")?.focus?.({ preventScroll: true });
  }

  function viewFromHash() {
    const hash = (window.location?.hash || "").slice(1).toLowerCase();
    return Object.keys(views).find((name) => views[name].hash === hash) || "overview";
  }

  function setupNavigation() {
    const buttons = [...document.querySelectorAll("[data-view]")];
    const pages = [...document.querySelectorAll("[data-page]")];
    if (!buttons.length || !pages.length) return;
    state.navigationReady = true;
    buttons.forEach((button, index) => {
      button.addEventListener("click", () => activateView(button.dataset.view, { writeHash: true, focus: true }));
      button.addEventListener("keydown", (event) => {
        let next;
        if (["ArrowDown", "ArrowRight"].includes(event.key)) next = (index + 1) % buttons.length;
        if (["ArrowUp", "ArrowLeft"].includes(event.key)) next = (index - 1 + buttons.length) % buttons.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = buttons.length - 1;
        if (next !== undefined) { event.preventDefault(); buttons[next].focus(); }
      });
    });
    document.querySelectorAll("[data-go-view]").forEach((button) => {
      button.addEventListener("click", () => activateView(button.dataset.goView, { writeHash: true, focus: true }));
    });
    const restore = () => activateView(viewFromHash());
    window.addEventListener?.("hashchange", restore);
    window.addEventListener?.("popstate", restore);
    restore();
    if (!Object.values(views).some((view) => `#${view.hash}` === window.location?.hash)) {
      window.history?.replaceState?.(null, "", "#resumen");
    }
    window.addEventListener?.("resize", () => {
      if (state.activeView === "history") renderChart();
      if (state.activeView === "overview") renderOverviewChart();
    });
  }

  function runtimeLabel(seconds) {
    if (!finite(seconds) || seconds < 0) return "No disponible";
    if (seconds < 60) return `${Math.round(seconds)} s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min${seconds % 60 >= 1 ? ` ${Math.floor(seconds % 60)} s` : ""}`;
    return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
  }

  function toast(message, error = false, warning = false) {
    clearTimeout(state.toastTimer);
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", error);
    el.classList.toggle("warning", warning && !error);
    el.hidden = false;
    state.toastTimer = setTimeout(() => { el.hidden = true; }, error || warning ? 8000 : 4500);
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
      let data;
      try { data = await response.json(); }
      catch {
        const error = new Error("El servicio local devolvió una respuesta no válida.");
        error.communicationFailure = true;
        throw error;
      }
      if (!response.ok) {
        const detail = data.error || data.detail || data.message;
        throw new Error(typeof detail === "string" ? detail : `El servicio local devolvió un error (${response.status}).`);
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError" || error instanceof TypeError) {
        const failure = new Error(error.name === "AbortError" ? "El servicio local no respondió a tiempo." : "No se puede acceder al servicio local. Comprueba que siga en ejecución.");
        failure.communicationFailure = true;
        throw failure;
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  async function post(path, body) {
    if (!state.serviceAvailable || !state.token) throw new Error("Espera a que el servicio local se conecte antes de realizar esta acción.");
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-UPS-Token": state.token },
      body: JSON.stringify(body),
    });
  }

  function updateMutationControls() {
    const unavailable = !state.serviceAvailable || !state.token;
    const busy = !!state.mutation;
    const enabled = !!state.status?.policy?.enabled;
    $("save-policy").disabled = unavailable || busy;
    $("cancel-shutdown").disabled = unavailable || busy;
    $("test-shutdown").disabled = unavailable || busy || enabled;
    $("policy-form").querySelectorAll("input, select").forEach((field) => { field.disabled = busy; });
    const notificationSettingsMissing = !state.status?.notifications?.settings;
    $("notifications-form").querySelectorAll("input").forEach((field) => { field.disabled = unavailable || busy || notificationSettingsMissing; });
    $("notifications-persistent").disabled = unavailable || busy || typeof state.status?.notifications?.settings?.persistent_critical !== "boolean";
    $("save-notifications").disabled = unavailable || busy || notificationSettingsMissing;
    const delivery = state.status?.notifications?.delivery;
    $("test-notifications").disabled = unavailable || busy || notificationSettingsMissing || delivery?.available === false || delivery?.ready === false;
    if (state.mutation !== "test-notifications") setText("test-notifications", state.status?.notifications?.settings?.sound_enabled === false ? "Probar aviso" : "Probar aviso y sonido");
    $("test-notifications").title = unavailable ? "El panel debe recuperar la conexión con el monitor."
      : delivery?.available === false ? "Los avisos de escritorio no están disponibles. Consulta el diagnóstico."
      : delivery?.ready === false ? "Espera a que el canal de avisos esté listo." : "Envía una prueba desde el monitor. No apaga ni hiberna Windows.";
    $("test-shutdown").title = unavailable ? "El panel debe recuperar la conexión con el monitor."
      : enabled ? "Desactiva y guarda la protección automática antes de probar el aviso." : "Simulación sin apagado ni hibernación.";
    setText("test-description", unavailable ? state.stopAcknowledged ? "Elipro está detenido." : "Sin comunicación con el monitor." : enabled ? "Desactiva y guarda la protección antes de probar." : "Simulación: no apaga ni hiberna.");
    for (const id of ["refresh-records", "apply-records", "search-events"]) $(id).disabled = unavailable;
    if (state.navigationReady) for (const id of ["refresh-incidents", "apply-incidents"]) { if ($(id)) $(id).disabled = unavailable || state.incidentsBusy; }
    $("export-events").disabled = unavailable || state.downloadBusy;
    $("more-incidents").disabled = unavailable || state.incidentsBusy;
    $("more-events").disabled = unavailable || state.archiveBusy;
    for (const id of ["download-diagnostics", "download-backup"]) $(id).disabled = unavailable || state.downloadBusy;
    $("stop-monitor").disabled = unavailable || busy;
    $("confirm-stop-monitor").disabled = unavailable || busy;
    $("run-validation").disabled = unavailable || busy || state.validationBusy;
    $("review-physical-tests").disabled = unavailable || state.physicalTestsBusy;
    $("refresh-stability").disabled = unavailable || state.observationBusy;
    $("start-stability").disabled = unavailable || state.observationBusy || state.observation?.state === "running";
    $("stop-stability").disabled = unavailable || state.observationBusy;
    $("download-stability").disabled = unavailable || state.downloadBusy;
  }

  function renderServiceState(error = "") {
    const notice = $("service-notice");
    notice.hidden = state.serviceAvailable;
    if (!state.serviceAvailable) {
      const confirmed = state.lastStatusAt ? ` Último estado confirmado: ${dateFormat.format(state.lastStatusAt)}.` : " Aún no se ha confirmado el estado del monitor.";
      notice.textContent = state.stopAcknowledged ? "Elipro se detuvo por tu solicitud. Ya no lee el UPS, registra eventos ni envía avisos. La PC sigue encendida. La vigilancia y los avisos se reanudarán cuando inicies Elipro de nuevo." : `${error || "Conectando con el monitor local."}${confirmed} Las acciones están deshabilitadas hasta recuperar la comunicación.`;
    }
    updateMutationControls();
  }

  function serviceUnavailable(error) {
    state.serviceAvailable = false;
    renderServiceState(error.message);
    renderConnection(state.status?.connection || {}, null);
    renderSample(null);
    // Losing this panel's API connection does not cancel the monitor's countdown.
    renderProtection(state.status?.protection || {});
    renderNotifications(state.status?.notifications);
    renderHistoryStatus();
    renderHealth(state.status?.health);
    renderObservation();
    renderOverview();
  }

  function renderHistoryStatus() {
    const history = state.status?.history;
    const storageUnavailable = history?.available === false;
    const queryFailed = !!state.historyQueryError;
    const hasDropped = finite(history?.dropped) && history.dropped > 0;
    const configurationWarning = typeof history?.configuration_warning === "string" ? history.configuration_warning : "";
    const notice = $("history-notice");
    notice.hidden = !state.serviceAvailable || (!storageUnavailable && !queryFailed && !hasDropped && !configurationWarning);
    if (!notice.hidden) {
      const recovering = history?.state === "recovering";
      const detail = storageUnavailable
        ? `${recovering ? "El historial se está recuperando" : "El guardado del historial está interrumpido"}.`
        : queryFailed ? "No se puede consultar el historial; se conserva la última consulta disponible."
        : hasDropped ? "El historial está disponible, pero perdió registros durante la falla."
        : "La configuración guardada necesita revisión.";
      const live = state.status?.connection?.state === "connected"
        ? " Las lecturas actuales y los avisos continúan por separado"
        : " Este problema no cambia la conexión USB ni los avisos";
      const protection = state.status?.policy?.enabled
        ? "; la protección conserva sus reglas y necesita lecturas recientes del UPS."
        : "; el apagado automático sigue desactivado.";
      const pending = finite(history?.pending) && history.pending > 0 ? ` Registros pendientes: ${numeric(history.pending)}.` : "";
      const dropped = hasDropped ? ` Registros descartados durante la falla: ${numeric(history.dropped)}.` : "";
      notice.textContent = `${detail}${live}${protection}${pending}${dropped}${configurationWarning ? ` ${configurationWarning}` : ""}`;
      notice.title = storageUnavailable && typeof history.message === "string" ? history.message : "";
    }
    const warning = storageUnavailable || queryFailed || hasDropped;
    $("chart-note").classList.toggle("history-warning", warning);
    $("chart-note").lastChild.textContent = storageUnavailable
      ? ` ${history?.state === "recovering" ? "Recuperando el historial" : "Guardado del historial interrumpido"}. Los datos mostrados pueden estar incompletos.`
      : queryFailed ? " No se pudo actualizar el historial. Se conserva la última consulta disponible."
      : hasDropped ? " El historial está disponible. Puede contener huecos por registros descartados durante la falla."
      : ` Registro cada ${numeric(state.historyInterval / 1000)} s. Los intervalos sin lecturas aparecen como huecos.`;
    if (!state.history.length && (storageUnavailable || queryFailed)) {
      $("chart-empty").querySelector("strong").textContent = "Historial no disponible";
      $("chart-empty").querySelector("p").textContent = storageUnavailable
        ? "Las lecturas actuales se muestran arriba. El historial volverá a actualizarse al recuperar el almacenamiento."
        : state.historyQueryError;
    }
  }

  function renderConnection(connection, sample) {
    if (!state.serviceAvailable) {
      const badge = $("connection-badge");
      badge.className = "connection-badge disconnected";
      badge.lastElementChild.textContent = state.stopAcknowledged ? "Elipro detenido" : "Monitor sin conexión";
      $("connection-notice").hidden = true;
      const card = document.querySelector(".power-card");
      card.classList.remove("is-online", "is-battery", "has-fault");
      setText("power-state", state.stopAcknowledged ? "Monitor detenido" : "Estado sin confirmar");
      setText("power-detail", state.stopAcknowledged ? "Inicia Elipro de nuevo para volver a vigilar el UPS y mostrar sus lecturas." : "El panel necesita comunicarse con el monitor para mostrar lecturas actuales.");
      updateFreshness();
      return;
    }
    const status = ["connected", "stale", "disconnected"].includes(connection.state) ? connection.state : "disconnected";
    const badge = $("connection-badge");
    badge.className = `connection-badge ${status}`;
    badge.lastElementChild.textContent = { connected: "UPS conectado", stale: "Datos desactualizados", disconnected: "Sin conexión al UPS" }[status];
    const notice = $("connection-notice");
    notice.hidden = status === "connected";
    if (status !== "connected") {
      const prefix = status === "stale"
        ? "Las lecturas han dejado de actualizarse. La protección automática necesita datos recientes del UPS."
        : "No se reciben lecturas del UPS. Revisa el cable USB y la conexión del servicio de lectura.";
      notice.textContent = `${prefix}${connection.error ? ` ${String(connection.error)}` : ""}`;
    }
    const card = document.querySelector(".power-card");
    card.classList.toggle("is-online", !!sample && sample.on_battery === false && !sample.fault);
    card.classList.toggle("is-battery", !!sample && sample.on_battery === true && !sample.fault);
    card.classList.toggle("has-fault", !!sample && !!sample.fault);
    if (!sample) {
      setText("power-state", status === "stale" ? "Lecturas en pausa" : "UPS sin conexión");
      setText("power-detail", "Esperando una lectura válida para mostrar el estado.");
    } else if (sample.fault) {
      setText("power-state", "El UPS reporta una falla");
      setText("power-detail", "Revisa el equipo y sus indicadores físicos.");
    } else if (sample.on_battery === true) {
      setText("power-state", "Usando batería");
      setText("power-detail", "El UPS está alimentando tus equipos con su batería.");
    } else if (sample.on_battery === false) {
      setText("power-state", "Red eléctrica activa");
      setText("power-detail", "Tus equipos reciben energía de la red eléctrica.");
    } else {
      setText("power-state", "UPS conectado");
      setText("power-detail", sample.raw_status ? `Estado reportado: ${String(sample.raw_status)}. Suministro sin confirmar.` : "El equipo no reporta el origen del suministro.");
    }
    const source = typeof connection.source === "string" ? connection.source : "Lectura local";
    setText("source-label", state.status?.meta?.source === "native" ? "Conexión USB directa" : state.status?.meta?.bridge_required ? "Lectura mediante ForzaTracker" : "Lectura local");
    $("source-label").title = source;
    updateFreshness();
  }

  function renderSample(sample) {
    const data = sample || {};
    const native = state.status?.meta?.source === "native";
    const bridge = state.status?.meta?.bridge_required === true;
    const voltageMode = native || (!finite(data.battery_percent) && finite(data.battery_voltage));
    document.querySelector(".battery-card").classList.toggle("voltage-mode", voltageMode);
    setText("input-voltage", numeric(data.input_voltage, 1));
    setText("output-voltage", numeric(data.output_voltage, 1));
    setText("battery-percent", voltageMode ? numeric(data.battery_voltage, 2) : numeric(data.battery_percent));
    setText("battery-unit", voltageMode ? "V" : "%");
    $("battery-track").hidden = voltageMode || !finite(data.battery_percent);
    $("battery-note").hidden = !voltageMode && !(bridge && finite(data.battery_percent));
    setText("battery-note", voltageMode ? "El UPS no reporta porcentaje de carga." : "Porcentaje reportado por ForzaTracker.");
    $("detail-battery-voltage").hidden = voltageMode;
    $("detail-temperature").hidden = !finite(data.temperature_c);
    setText("load-percent", numeric(data.load_percent));
    setText("frequency", numeric(data.frequency_hz, 1));
    setText("battery-voltage", numeric(data.battery_voltage, 1));
    setText("temperature", numeric(data.temperature_c, 1));
    setText("runtime", runtimeLabel(data.runtime_seconds));
    if (state.navigationReady && $("battery-runtime")) $("battery-runtime").hidden = !finite(data.runtime_seconds) || data.runtime_seconds <= 0;
    $("battery-bar").style.width = finite(data.battery_percent) ? `${Math.max(0, Math.min(100, data.battery_percent))}%` : "0%";
    $("battery-bar").classList.toggle("low", !!data.low_battery || (finite(data.battery_percent) && data.battery_percent <= 20));
    $("load-bar").style.width = finite(data.load_percent) ? `${Math.max(0, Math.min(100, data.load_percent))}%` : "0%";
    $("load-bar").classList.toggle("high", finite(data.load_percent) && data.load_percent >= 80);
    const batteryTag = $("battery-tag");
    batteryTag.className = "subtle-tag";
    if (!sample) {
      batteryTag.textContent = "Sin lectura";
    } else if (data.low_battery === true || (finite(data.battery_percent) && data.battery_percent <= 20)) {
      batteryTag.textContent = "Batería baja";
      batteryTag.classList.add("warning");
    } else if (voltageMode && data.low_battery === false) {
      batteryTag.textContent = "Sin alerta";
    } else if (data.on_battery) {
      batteryTag.textContent = "En uso";
      batteryTag.classList.add("warning");
    } else if (data.low_battery === false) {
      batteryTag.textContent = "Sin alerta";
    } else {
      batteryTag.textContent = "Sin estado";
    }
    $("load-footer").hidden = !finite(data.estimated_watts);
    setText("watts", `${data.watts_is_estimate && finite(data.estimated_watts) ? "≈ " : ""}${numeric(data.estimated_watts)} W`);
    setText("watts-label", !finite(data.estimated_watts) ? "Potencia no disponible" : data.watts_is_estimate ? "Estimación según carga nominal" : "Potencia reportada por el UPS");
  }

  function updateFreshness() {
    if (!state.status) return;
    const connection = state.status.connection || {};
    const date = parseDate(connection.last_success || state.status.sample?.timestamp);
    if (!date) {
      setText("last-updated", "Aún no hay lecturas");
      return;
    }
    const age = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    const ageText = age < 60 ? `Hace ${age} s` : age < 3600 ? `Hace ${Math.floor(age / 60)} min` : `Hace ${Math.floor(age / 3600)} h`;
    setText("last-updated", `${ageText} · ${clockFormat.format(date)}`);
    $("last-updated").title = date.toLocaleString("es-PE");
  }

  function eventCategory(event) {
    const kind = String(event.kind || "");
    if (/^connection_|^monitor_|^reader_|^supervisor_/.test(kind)) return "connection";
    if (/^policy_|^simulation$/.test(kind)) return "protection";
    return "power";
  }

  function eventLabel(kind) {
    return {
      power_lost: "Corte eléctrico detectado",
      power_restored: "Electricidad restablecida",
      connection_lost: "Comunicación con el UPS perdida",
      connection_restored: "Comunicación con el UPS restablecida",
      monitor_error: "Error del monitor",
      monitor_restarted: "Reinicio del programa Elipro",
      battery_low: "Batería baja del UPS",
      overload: "Sobrecarga del UPS",
      ups_fault: "Falla del UPS",
      policy_countdown: "Cuenta atrás de protección",
      policy_cancelled: "Acción de protección cancelada",
      policy_unknown: "Protección suspendida por falta de lecturas válidas",
      policy_action_started: "Acción de protección iniciada",
      policy_action_failed: "Error al ejecutar la protección",
      policy_changed: "Configuración de protección",
      policy_disabled: "Protección automática desactivada",
      policy_online: "Red eléctrica confirmada",
      policy_on_battery: "Funcionando con batería",
      simulation: "Simulación sin acción real",
    }[kind] || "Registro del monitor";
  }

  function renderEvents(events, notify = true) {
    const list = $("events-list");
    const sorted = Array.isArray(events) ? [...events].sort((a, b) => (parseDate(b.timestamp)?.getTime() || 0) - (parseDate(a.timestamp)?.getTime() || 0)) : [];
    state.events = sorted;
    const eventKey = (event) => String(event.id ?? `${event.timestamp}:${event.kind}:${event.message}`);
    // The initial history is context, not a batch of new alerts.
    if (state.seenEvents === null) {
      state.seenEvents = new Set(sorted.map(eventKey));
    } else if (notify) {
      const fresh = sorted.filter((event) => !state.seenEvents.has(eventKey(event)));
      state.seenEvents = new Set(sorted.map(eventKey));
      const relevant = fresh.filter((event) => /power|connection|battery_low|overload|fault|policy_countdown|policy_unknown|policy_action_failed/.test(String(event.kind || "")));
      if (relevant.length) {
        const newest = relevant[0];
        const severity = String(newest.severity || "");
        const prefix = relevant.length > 1 ? `${relevant.length} eventos nuevos. ` : "";
        toast(`${prefix}${newest.message || "Cambió el estado del UPS."}`, /error|critical|danger/.test(severity), /warning/.test(severity));
      }
    }
    const filtered = state.eventFilter === "all" ? sorted : sorted.filter((event) => eventCategory(event) === state.eventFilter);
    setText("event-count", state.eventFilter === "all" ? String(sorted.length) : `${filtered.length} / ${sorted.length}`);
    $("event-count").title = `${filtered.length} visibles de ${sorted.length} registros recientes recibidos.`;
    const signature = `${state.eventFilter}|${sorted.map((event) => `${event.id}:${event.timestamp}:${event.kind}:${event.severity}:${event.message}`).join("|")}`;
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    if (!filtered.length) {
      list.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "events-empty";
      const icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "✓";
      const title = document.createElement("strong");
      title.textContent = sorted.length ? "Sin eventos de este tipo" : "Sin eventos registrados";
      const description = document.createElement("p");
      description.textContent = sorted.length ? "Selecciona Todos para consultar el resto de los registros recientes." : "Los cortes, retornos de energía y cambios de conexión aparecerán aquí.";
      empty.append(icon, title, description);
      list.append(empty);
      return;
    }
    list.replaceChildren();
    for (const event of filtered) {
      const severity = String(event.severity || "").toLowerCase();
      const kind = String(event.kind || "").toLowerCase();
      const good = /restor|online|connect|return/.test(kind) && !/disconnect|lost|fail/.test(kind);
      const danger = /error|critical|danger/.test(severity) || /fault|execut/.test(kind);
      const warning = /warn/.test(severity) || /outage|battery|disconnect|lost|countdown/.test(kind);
      const item = document.createElement("div");
      item.className = `event${danger ? " danger" : warning ? " warning" : good ? " good" : ""}`;
      const symbol = document.createElement("span");
      symbol.className = "event-symbol";
      symbol.setAttribute("aria-hidden", "true");
      symbol.textContent = danger ? "!" : /disconnect|lost/.test(kind) ? "×" : /outage|battery/.test(kind) ? "ϟ" : good ? "↗" : "·";
      const body = document.createElement("div");
      body.className = "event-body";
      const message = document.createElement("h3");
      message.textContent = event.message || event.kind || "Evento del UPS";
      const date = parseDate(event.timestamp);
      const time = document.createElement("time");
      if (date) {
        time.dateTime = date.toISOString();
        time.textContent = dateFormat.format(date);
        time.title = date.toLocaleString("es-PE");
      } else time.textContent = "Fecha no disponible";
      body.append(message, time);
      item.append(symbol, body);
      list.append(item);
    }
  }

  function renderPolicy(policy) {
    const native = state.status?.meta?.source === "native";
    $("policy-battery-field").hidden = native;
    $("policy-runtime-field").hidden = native;
    $("policy-fields").classList.toggle("native-policy", native);
    setText("policy-trigger-description", native
      ? "Durante un corte, inicia una cuenta atrás al alcanzar el tiempo máximo con batería o cuando el UPS reporte la alerta real de batería baja."
      : "Durante un corte, inicia una cuenta atrás cuando se alcance alguno de estos límites o el UPS avise batería baja.");
    const enabled = !!policy.enabled;
    const summary = $("protection-summary");
    summary.textContent = enabled ? state.status?.meta?.dry_run ? "Simulada · sin acción real" : "Activada" : "Desactivada";
    summary.classList.toggle("enabled", enabled);
    const tag = $("policy-state");
    tag.textContent = enabled ? "Activado" : "Desactivado";
    tag.classList.toggle("good", enabled);
    updateMutationControls();
    if (state.policyInitialized && state.policyDirty) return;
    $("policy-enabled").checked = enabled;
    $("policy-on-battery").value = finite(policy.on_battery_seconds) ? policy.on_battery_seconds / 60 : 5;
    $("policy-low-battery").value = finite(policy.low_battery_percent) ? policy.low_battery_percent : 20;
    $("policy-runtime").value = finite(policy.min_runtime_seconds) ? policy.min_runtime_seconds : 180;
    $("policy-countdown").value = finite(policy.countdown_seconds) ? policy.countdown_seconds : 60;
    $("policy-action").value = policy.action === "hibernate" ? "hibernate" : "shutdown";
    setText("enabled-label", enabled ? "Activada" : "Desactivada");
    state.policyInitialized = true;
  }

  function renderProtection(protection) {
    const alert = $("protection-alert");
    if (state.stopAcknowledged && protection.state === "countdown") {
      alert.hidden = true;
      setText("countdown", "—");
      return;
    }
    const visible = ["countdown", "blocked", "executed"].includes(protection.state);
    alert.hidden = !visible;
    if (!visible) return;
    const simulation = !!protection.simulation;
    const action = state.status?.policy?.action === "hibernate" ? "hibernación" : "apagado";
    setText("protection-title", protection.state === "blocked" ? "La protección requiere atención" : protection.state === "executed" ? simulation ? "Simulación finalizada" : "Acción enviada a Windows" : simulation ? "Prueba del aviso · sin acción real" : `Cuenta atrás para ${action}`);
    const reason = `${protection.reason || (simulation ? "Esta prueba no apagará ni hibernará tu PC." : "Se alcanzó un límite de protección.")}${simulation && protection.reason ? " No se apagará ni hibernará tu PC." : ""}`;
    const unconfirmed = !state.serviceAvailable ? ` Estado sin confirmar desde ${state.lastStatusAt ? clockFormat.format(state.lastStatusAt) : "la pérdida de comunicación"}. ${protection.state === "countdown" ? "La cuenta atrás puede seguir activa; su cancelación no está confirmada." : "No se ha podido consultar su estado actual."}` : "";
    setText("protection-reason", `${reason}${unconfirmed}`);
    $("cancel-shutdown").hidden = protection.state !== "countdown";
    $("cancel-shutdown").textContent = simulation ? "Cancelar prueba" : "Cancelar esta acción";
    updateCountdown();
  }

  function renderNotifications(notifications) {
    const settings = notifications?.settings;
    const delivery = notifications?.delivery || {};
    const tag = $("notifications-state");
    const error = delivery.last_error || notifications?.last_error;
    const ready = state.serviceAvailable && settings && delivery.available !== false && delivery.ready === true;
    tag.textContent = !state.serviceAvailable ? state.stopAcknowledged ? "Detenidos" : "Sin conexión" : !settings || delivery.available === false ? "No disponible" : ready ? "Listo" : "Iniciando";
    tag.classList.toggle("good", !!ready);
    tag.classList.toggle("warning", !ready);
    const lastDelivered = parseDate(delivery.last_delivered_at);
    const lastPopup = parseDate(delivery.last_popup_shown_at);
    setText("notifications-delivery", !state.serviceAvailable
      ? state.stopAcknowledged ? "Elipro está detenido. Los avisos y el sonido automático se reanudarán cuando vuelvas a iniciarlo." : "El panel no puede confirmar el estado de los avisos. Comprueba que el monitor siga activo."
      : !settings ? "El monitor aún no informa un canal de avisos."
      : delivery.available === false ? "Los avisos de escritorio no están disponibles en esta sesión."
      : settings.enabled === false ? "Avisos y sonido automáticos desactivados. La prueba sigue disponible y usa el ajuste de sonido guardado."
      : lastPopup ? `Último aviso de Elipro abierto: ${dateFormat.format(lastPopup)}. Esto no confirma que lo hayas leído.`
      : lastDelivered ? `Último envío a Windows: ${dateFormat.format(lastDelivered)}. Esto no confirma que hayas visto el aviso.`
      : ready ? "Canal de avisos listo. Puedes probarlo sin activar la protección automática."
      : "Iniciando el canal de avisos de escritorio…");
    $("notifications-error").hidden = !error;
    setText("notifications-error", error ? `Último error: ${String(error)}` : "");
    const soundError = delivery.last_sound_error;
    $("notifications-sound-error").hidden = !soundError;
    setText("notifications-sound-error", soundError ? `${ready ? "El canal de avisos visuales sigue disponible; error de sonido" : "Último error de sonido"}: ${String(soundError)}` : "");
    $("notifications-popup-error").hidden = !delivery.popup_error;
    setText("notifications-popup-error", delivery.popup_error ? `Ventana de respaldo de Elipro: ${String(delivery.popup_error)}` : "");
    if (!settings || (state.notificationsInitialized && state.notificationsDirty)) return;
    $("notifications-enabled").checked = settings.enabled === true;
    $("notifications-sound").checked = settings.sound_enabled === true;
    $("notifications-repeat").value = finite(settings.repeat_seconds) ? settings.repeat_seconds : 30;
    const supportsPersistent = typeof settings.persistent_critical === "boolean";
    $("notifications-persistent-option").hidden = !supportsPersistent;
    $("notifications-persistent").checked = settings.persistent_critical === true;
    state.notificationsInitialized = true;
  }

  function formatMoment(value) {
    const date = parseDate(value);
    return date ? recordDateFormat.format(date) : "Sin registro";
  }

  function renderOverview() {
    if (!state.navigationReady) return;
    const data = state.status;
    const confirmed = state.serviceAvailable && data;
    const policy = data?.policy || {};
    const simulated = data?.meta?.dry_run === true;
    const global = $("protection-global-status");
    setText("protection-global-status", !confirmed ? state.stopAcknowledged ? "Elipro detenido" : "Protección sin confirmar"
      : policy.enabled ? simulated ? "Protección simulada" : "Apagado automático activado" : "Apagado automático desactivado");
    global?.classList.toggle("enabled", !!confirmed && policy.enabled === true && !simulated);
    global?.classList.toggle("unconfirmed", !confirmed);
    const globalBar = global?.closest?.(".global-status-bar");
    globalBar?.classList.toggle("is-enabled", !!confirmed && policy.enabled === true && !simulated);
    globalBar?.classList.toggle("is-unknown", !confirmed);
    setText("protection-global-detail", !confirmed ? "El panel necesita recuperar la comunicación con el monitor."
      : simulated ? "Modo de prueba · las acciones reales de Windows están bloqueadas."
      : policy.enabled ? `Acción configurada: ${policy.action === "hibernate" ? "hibernar" : "apagar"} Windows.` : "Elipro vigila y avisa; no iniciará un apagado automático.");
    const connection = !confirmed ? state.stopAcknowledged ? "Monitor detenido" : "Monitor sin conexión"
      : data.connection?.state === "connected" ? "USB conectado" : data.connection?.state === "stale" ? "Lecturas desactualizadas" : "USB sin conexión";
    setText("sidebar-connection-text", connection);
    $("sidebar-connection")?.classList.toggle("warning", !confirmed || data?.connection?.state !== "connected");
    const health = data?.health;
    const healthy = confirmed && health?.reader_alive === true && health?.supervisor_alive === true && ["healthy", "ok", "running"].includes(health.state);
    setText("summary-health", !confirmed ? "Sin confirmar" : healthy ? "Monitor supervisado" : "Revisar monitor");
    const observation = state.observation || health?.observation;
    setText("summary-observation", !confirmed ? "Observación sin confirmar" : observation?.state === "running"
      ? `Observación · ${runtimeLabel(observation.elapsed_seconds)}${finite(observation.target_seconds) ? ` de ${runtimeLabel(observation.target_seconds)}` : ""}`
      : observation?.state === "completed_pending_review" ? "Observación completada · revisar informe"
      : observation?.state === "interrupted" ? "Observación interrumpida" : "Sin observación activa");
    const recent = state.events[0];
    setText("summary-last-incident", recent ? eventLabel(recent.kind) : "Sin eventos recientes");
    setText("summary-incident-time", recent ? formatMoment(recent.timestamp) : "Los cortes y las desconexiones aparecerán aquí.");
  }

  function renderOverviewChart() {
    if (!state.navigationReady) return;
    const chart = $("summary-history-spark");
    if (!chart) return;
    chart.replaceChildren();
    const hours = 1;
    const end = Date.now(), start = end - hours * 3600000;
    const samples = state.overviewHistory.filter((sample) => sample._time >= start && sample._time <= end);
    const values = samples.flatMap((sample) => [sample.input_voltage, sample.output_voltage]).filter(finite);
    chart.setAttribute("viewBox", "0 0 800 170");
    chart.setAttribute("aria-label", values.length ? `Voltaje de entrada y salida, últimos ${hours === 1 ? "60" : "15"} minutos. Los huecos representan falta de lecturas.` : "Sin mediciones de voltaje en este período.");
    if (!values.length) {
      chart.append(svgElement("text", { x: 400, y: 85, class: "chart-axis", "text-anchor": "middle" }, state.overviewHistoryError ? "No se pudo consultar el voltaje de la última hora" : "Esperando mediciones de voltaje"));
      return;
    }
    const minimum = values.reduce((result, value) => Math.min(result, value), Infinity);
    const maximum = values.reduce((result, value) => Math.max(result, value), -Infinity);
    const low = Math.max(0, Math.floor((minimum - 5) / 10) * 10), high = Math.ceil((maximum + 5) / 10) * 10;
    const x = (time) => 42 + (time - start) / (end - start) * 742;
    const y = (value) => 140 - (value - low) / (high - low) * 124;
    for (let index = 0; index <= 2; index++) {
      const value = low + (high - low) * index / 2;
      chart.append(svgElement("line", { x1: 42, x2: 784, y1: y(value), y2: y(value), class: "chart-grid" }));
      chart.append(svgElement("text", { x: 32, y: y(value) + 4, class: "chart-axis", "text-anchor": "end" }, numeric(value)));
    }
    for (const [key, color] of [["input_voltage", "var(--teal)"], ["output_voltage", "var(--blue)"]]) {
      let path = "", previous = null;
      for (const sample of samples) {
        if (!finite(sample[key])) { previous = null; continue; }
        const continuous = previous !== null && (typeof sample.gap_before === "boolean" ? !sample.gap_before : sample._time - previous <= state.historyInterval * 3);
        path += `${continuous ? "L" : "M"}${x(sample._time).toFixed(2)},${y(sample[key]).toFixed(2)} `;
        previous = sample._time;
      }
      if (path) chart.append(svgElement("path", { d: path.trim(), class: "chart-line", stroke: color }));
    }
    chart.append(svgElement("text", { x: 42, y: 164, class: "chart-axis", "text-anchor": "start" }, shortClockFormat.format(new Date(start))));
    chart.append(svgElement("text", { x: 784, y: 164, class: "chart-axis", "text-anchor": "end" }, shortClockFormat.format(new Date(end))));
  }

  function renderHealth(health) {
    const tag = $("health-state");
    const confirmed = state.serviceAvailable && health && typeof health === "object";
    const healthy = confirmed && ["healthy", "ok", "running"].includes(health.state) && health.reader_alive === true && health.supervisor_alive === true;
    tag.textContent = !state.serviceAvailable ? state.stopAcknowledged ? "Detenido" : "Sin confirmar" : !health ? "No disponible" : healthy ? "Supervisado" : "Revisar monitor";
    tag.classList.toggle("good", !!healthy);
    tag.classList.toggle("warning", !healthy);
    setText("health-message", !state.serviceAvailable ? state.stopAcknowledged ? "Detuviste Elipro. La vigilancia está detenida y el lector y su supervisor se están cerrando. Inicia Elipro de nuevo para reanudarla." : "El panel perdió la comunicación. No puede confirmar si el monitor continúa funcionando." : !health ? "Esta versión del monitor no comunica su diagnóstico de funcionamiento." : health.message || (healthy ? "Lector y supervisor funcionando." : "Revisa el diagnóstico del monitor."));
    const data = confirmed ? health : {};
    setText("health-uptime", runtimeLabel(data.uptime_seconds));
    setText("health-workers", confirmed ? `${data.reader_alive === true ? "Activo" : data.reader_alive === false ? "Detenido" : "Sin dato"} / ${data.supervisor_alive === true ? "Activo" : data.supervisor_alive === false ? "Detenido" : "Sin dato"}` : "—");
    setText("health-reads", confirmed ? `${numeric(data.read_successes)} / ${numeric(data.read_failures)}${data.consecutive_failures > 0 ? ` (${numeric(data.consecutive_failures)} seguidas)` : ""}` : "—");
    setText("health-recoveries", confirmed ? `${numeric(data.recoveries)}${finite(data.reader_restarts) ? ` · ${numeric(data.reader_restarts)} reinicios del lector` : ""}` : "—");
    setText("health-last-reading", confirmed ? `${formatMoment(data.last_success)}${finite(data.read_age_seconds) ? ` · hace ${numeric(Math.max(0, data.read_age_seconds))} s` : ""}` : "—");
    setText("health-last-error", confirmed ? data.last_error_at ? `${formatMoment(data.last_error_at)}${data.last_error_kind ? ` · ${String(data.last_error_kind)}` : ""}` : "Ninguno desde el inicio" : "—");
    const process = data.process || {};
    setText("health-resources", confirmed ? `${finite(process.memory_bytes) ? `${numeric(process.memory_bytes / 1048576, 1)} MB` : "Sin dato"} / ${finite(process.cpu_percent) ? `${numeric(process.cpu_percent, 1)} %` : "Midiendo"}` : "—");
    const history = state.status?.history;
    setText("health-storage", !confirmed ? "—" : !history ? "Sin diagnóstico" : `${history.available === false ? "Interrumpido" : "Disponible"} · ${numeric(history.pending ?? 0)} pendientes${history.last_success_at ? ` · ${formatMoment(history.last_success_at)}` : ""}`);
  }

  function renderObservation(data) {
    if (data && typeof data === "object") {
      state.observation = data;
      state.observationLoaded = true;
      sectionError("stability-error", data.last_error ? new Error(String(data.last_error)) : null);
    }
    const observation = state.observation;
    $("stop-stability").hidden = observation?.state !== "running";
    $("download-stability").hidden = observation?.report_available !== true;
    if (!state.serviceAvailable && observation) {
      setText("stability-summary", state.stopAcknowledged ? "Elipro está detenido y la observación dejó de recopilar datos. Revisa el informe cuando vuelvas a iniciarlo." : "El panel no puede confirmar el estado de la observación hasta recuperar la comunicación con Elipro.");
    } else if (observation) {
      const label = { idle: "Sin observación activa", running: "Observación en curso", completed_pending_review: "Período completado; informe pendiente de revisión", interrupted: "Observación interrumpida" }[observation.state] || "Estado de observación sin confirmar";
      const elapsed = finite(observation.elapsed_seconds) ? ` · ${runtimeLabel(observation.elapsed_seconds)}${finite(observation.target_seconds) ? ` de ${runtimeLabel(observation.target_seconds)}` : ""}` : "";
      const samples = finite(observation.samples) ? ` · ${numeric(observation.samples)} comprobaciones` : "";
      const failures = finite(observation.read_failures) ? ` · ${numeric(observation.read_failures)} errores de lectura` : "";
      const dropped = observation.history_dropped > 0 ? ` · ${numeric(observation.history_dropped)} registros descartados` : "";
      const memory = finite(observation.memory_peak_bytes) ? ` · memoria máxima ${numeric(observation.memory_peak_bytes / 1048576, 1)} MB` : "";
      const cpu = finite(observation.cpu_peak_percent) ? ` · CPU máxima ${numeric(observation.cpu_peak_percent, 1)} %` : "";
      const age = finite(observation.max_read_age_seconds) ? ` · mayor antigüedad de lectura ${numeric(observation.max_read_age_seconds, 1)} s` : "";
      setText("stability-summary", `${label}${elapsed}${samples}${failures}${dropped}${memory}${cpu}${age}.`);
    }
    renderOverview();
    updateMutationControls();
  }

  async function loadObservation() {
    if (!state.serviceAvailable || state.observationBusy) return;
    state.observationBusy = true;
    updateMutationControls();
    sectionError("stability-error", null);
    try {
      const data = await request("/api/stability");
      if (!data || typeof data.state !== "string") throw new Error("El monitor no devolvió un estado de observación válido.");
      renderObservation(data);
    } catch (error) { sectionError("stability-error", new Error(`No se pudo consultar la observación. ${error.message}`)); }
    finally { state.observationBusy = false; updateMutationControls(); }
  }

  async function changeObservation(start) {
    if (!state.serviceAvailable || state.observationBusy) return;
    state.observationBusy = true;
    updateMutationControls();
    sectionError("stability-error", null);
    try {
      const data = await post(`/api/stability/${start ? "start" : "stop"}`, start ? { hours: 48 } : {});
      if (!data || typeof data.state !== "string") throw new Error("El monitor no confirmó el cambio de observación.");
      renderObservation(data);
    } catch (error) { sectionError("stability-error", error); }
    finally { state.observationBusy = false; updateMutationControls(); }
  }

  async function runValidation() {
    if (!state.serviceAvailable || state.validationBusy || state.mutation) return;
    state.validationBusy = true;
    updateMutationControls();
    setText("validation-summary", "Comprobando las reglas con datos simulados…");
    $("validation-results").replaceChildren();
    try {
      const data = await post("/api/validation/run", {});
      if (data.source !== "isolated_simulation" || data.os_actions_executed !== 0 || data.live_settings_changed !== false || !Array.isArray(data.scenarios)) throw new Error("El monitor no confirmó las condiciones de seguridad de esta comprobación.");
      const passed = data.passed === true && data.scenarios.length > 0 && data.scenarios.every((scenario) => scenario.passed === true);
      setText("validation-summary", passed ? "Comprobaciones simuladas superadas. No se ejecutaron acciones en Windows ni se cambiaron tus ajustes. Las pruebas físicas se revisan por separado." : "Hay comprobaciones que requieren revisión. No se ejecutaron acciones en Windows ni se cambiaron tus ajustes.");
      for (const [index, scenario] of data.scenarios.entries()) {
        const row = document.createElement("div");
        row.className = `validation-result${scenario.passed === true ? " passed" : " failed"}`;
        const name = document.createElement("strong");
        name.textContent = `${scenario.passed === true ? "✓" : "!"} ${String(scenario.label || `Comprobación ${index + 1}`)}`;
        row.append(name);
        if (scenario.detail) appendDetail(row, String(scenario.detail));
        $("validation-results").append(row);
      }
    } catch (error) { setText("validation-summary", `No se pudo completar la comprobación. ${error.message}`); }
    finally { state.validationBusy = false; updateMutationControls(); }
  }

  async function reviewPhysicalTests() {
    if (!state.serviceAvailable || state.physicalTestsBusy) return;
    state.physicalTestsBusy = true;
    updateMutationControls();
    $("physical-test-summary").hidden = false;
    setText("physical-test-summary", "Consultando las pruebas físicas registradas…");
    try {
      const data = await request("/api/validation");
      const record = data.available === true && data.record;
      if (!record) throw new Error("Aún no hay un registro de pruebas físicas disponible.");
      const cut = record.real_cut;
      const power = cut?.status === "passed" ? `Corte y regreso: registrados${cut.date ? ` el ${String(cut.date)}` : ""}${finite(cut.seconds_detected) ? ` (${numeric(cut.seconds_detected, 1)} s detectados)` : ""}.${cut.session_preserved_user_confirmed === true ? " Sesión conservada, confirmado por ti." : ""}${cut.visual_and_sound_user_confirmed === true ? " Aviso y sonido confirmados por ti." : ""}` : "Corte físico: sin una prueba confirmada en el registro.";
      const usb = record.physical_usb_reconnection?.status === "passed" ? " Reconexión física del USB: comprobada." : " Reconexión física del USB: pendiente de comprobar contigo.";
      const action = record.real_windows_shutdown?.status === "passed" ? " Acción real de Windows: comprobada según el registro." : " Apagado real de Windows: sin probar.";
      const safety = state.status?.policy?.enabled === false ? " El apagado automático está desactivado." : state.status?.meta?.dry_run ? " Las acciones reales están bloqueadas en modo de prueba." : "";
      setText("physical-test-summary", `${power}${usb}${action}${safety}`);
    } catch (error) { setText("physical-test-summary", `No se pudo revisar el registro de pruebas. ${error.message}`); }
    finally { state.physicalTestsBusy = false; updateMutationControls(); }
  }

  function recordFilters(includeEvents = false, incidents = false) {
    const params = new URLSearchParams();
    const prefix = state.navigationReady && incidents && $("incidents-from") ? "incidents" : "records";
    const values = [$(`${prefix}-from`).value, $(`${prefix}-to`).value];
    const dates = values.map((value) => value ? parseDate(value) : null);
    if (values.some((value, index) => value && !dates[index])) throw new Error("Revisa las fechas de la consulta.");
    if (dates[0] && dates[1] && dates[0] > dates[1]) throw new Error("La fecha Desde debe ser anterior o igual a Hasta.");
    if (dates[0]) params.set("from", dates[0].toISOString());
    if (dates[1]) params.set("to", dates[1].toISOString());
    if (includeEvents) {
      if ($("event-kind").value) params.set("kind", $("event-kind").value);
      if ($("event-search").value.trim()) params.set("q", $("event-search").value.trim());
    }
    return params;
  }

  function sectionError(id, error) {
    $(id).hidden = !error;
    setText(id, error ? error.message || String(error) : "");
  }

  function appendDetail(parent, text, className = "") {
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    if (className) paragraph.className = className;
    parent.append(paragraph);
  }

  function renderIncidents() {
    const list = $("incidents-list");
    list.replaceChildren();
    if (!state.incidents.length) return appendDetail(list, "No hay incidentes en este intervalo.", "section-empty");
    for (const incident of state.incidents) {
      const item = document.createElement("article");
      item.className = `incident-item${incident.status === "open" ? " incident-open" : ""}`;
      const heading = document.createElement("h4");
      heading.textContent = `${incident.type === "power" ? "Corte eléctrico" : "Problema de comunicación"} · ${incident.status === "open" ? "Sin recuperación registrada" : "Recuperado"}`;
      item.append(heading);
      appendDetail(item, `${incident.start_known === false ? "Inicio no registrado" : formatMoment(incident.started_at)} → ${incident.ended_at ? formatMoment(incident.ended_at) : "Sin recuperación registrada"}`, "incident-dates");
      const durationLabel = incident.status === "open" ? "Tiempo desde la detección" : `Duración detectada${incident.start_known === false ? " desde el primer registro disponible" : ""}`;
      appendDetail(item, `${durationLabel}: ${finite(incident.duration_seconds) ? runtimeLabel(incident.duration_seconds) : "Sin confirmar"}`);
      if (incident.status === "open") appendDetail(item, incident.type === "power" ? "Un registro sin cierre no confirma que el UPS siga en batería; consulta el estado actual de arriba." : "Un registro sin cierre no confirma que siga habiendo un problema de comunicación; consulta el estado actual de arriba.");
      if (incident.message) appendDetail(item, String(incident.message));
      const actions = Array.isArray(incident.actions) ? incident.actions : [];
      appendDetail(item, actions.length ? `Protección: ${actions.map((event) => event.message || event.kind || "Evento de protección").join(" · ")}` : "Sin acciones de protección registradas durante este incidente.", "incident-actions");
      list.append(item);
    }
  }

  async function loadIncidents(append = false) {
    if (!state.serviceAvailable || state.incidentsBusy) return;
    const version = state.navigationReady ? state.incidentVersion : state.recordVersion;
    const currentVersion = () => state.navigationReady ? state.incidentVersion : state.recordVersion;
    let params;
    try { params = recordFilters(false, true); }
    catch (error) { sectionError("incidents-error", error); return; }
    const query = params.toString();
    if (append && query !== state.incidentQuery) append = false;
    if (append && state.incidentCursor) params.set("cursor", state.incidentCursor);
    params.set("limit", "50");
    state.incidentsBusy = true;
    $("incidents-list").setAttribute("aria-busy", "true");
    updateMutationControls();
    sectionError("incidents-error", null);
    try {
      const data = await request(`/api/incidents?${params}`);
      if (version !== currentVersion()) return;
      if (!Array.isArray(data.incidents)) throw new Error("El monitor no devolvió una lista de incidentes válida.");
      const combined = append ? [...state.incidents, ...data.incidents] : data.incidents;
      state.incidents = combined.filter((item, index) => combined.findIndex((other) => String(other.id) === String(item.id)) === index);
      state.incidentCursor = data.next_cursor || null;
      state.incidentQuery = query;
      state.recordsLoaded = true;
      state.incidentsLoaded = true;
      setText("incidents-count", `${state.incidents.length}${finite(data.total) ? ` / ${numeric(data.total)}` : ""}`);
      $("more-incidents").hidden = !state.incidentCursor;
      renderIncidents();
      setText(state.navigationReady ? "incidents-feedback" : "records-feedback", `Consulta actualizada a las ${clockFormat.format(new Date())}. Las fechas usan la zona horaria de esta PC; puede haber huecos si faltaron lecturas o registros.`);
      renderOverview();
    } catch (error) {
      if (version === currentVersion()) sectionError("incidents-error", new Error(`No se pudieron consultar los incidentes. ${error.message}`));
    } finally {
      state.incidentsBusy = false;
      $("incidents-list").setAttribute("aria-busy", "false");
      updateMutationControls();
      if (version !== currentVersion()) loadIncidents();
    }
  }

  function renderArchive() {
    const list = $("archive-list");
    list.replaceChildren();
    if (!state.archive.length) return appendDetail(list, "No hay eventos que coincidan con la búsqueda.", "section-empty");
    for (const event of state.archive) {
      const row = document.createElement("article");
      row.className = "archive-event";
      const time = document.createElement("time");
      time.textContent = formatMoment(event.timestamp);
      const parsed = parseDate(event.timestamp);
      if (parsed) time.dateTime = parsed.toISOString();
      const body = document.createElement("div");
      appendDetail(body, String(event.message || eventLabel(event.kind)));
      appendDetail(body, eventLabel(event.kind), "archive-kind");
      row.append(time, body);
      list.append(row);
    }
  }

  async function loadArchive(append = false) {
    if (!state.serviceAvailable || state.archiveBusy) return;
    const version = state.recordVersion;
    let params;
    try { params = recordFilters(true); }
    catch (error) { sectionError("archive-error", error); return; }
    const query = params.toString();
    if (append && query !== state.archiveQuery) append = false;
    if (append && state.archiveCursor) params.set("cursor", state.archiveCursor);
    params.set("limit", "50");
    state.archiveBusy = true;
    $("archive-list").setAttribute("aria-busy", "true");
    updateMutationControls();
    sectionError("archive-error", null);
    try {
      const data = await request(`/api/events/search?${params}`);
      if (version !== state.recordVersion) return;
      if (!Array.isArray(data.events)) throw new Error("El monitor no devolvió una lista de eventos válida.");
      const combined = append ? [...state.archive, ...data.events] : data.events;
      state.archive = combined.filter((item, index) => combined.findIndex((other) => String(other.id) === String(item.id)) === index);
      state.archiveQuery = query;
      state.archiveLoaded = true;
      state.archiveCursor = data.next_cursor || null;
      $("more-events").hidden = !state.archiveCursor;
      setText("archive-summary", `${numeric(state.archive.length)} ${state.archive.length === 1 ? "evento mostrado" : "eventos mostrados"}${finite(data.total) ? ` de ${numeric(data.total)}` : ""}. El CSV incluye todos los resultados con estos filtros, no solo esta página.`);
      renderArchive();
      if (state.navigationReady) setText("records-feedback", `Consulta actualizada a las ${clockFormat.format(new Date())}. Las fechas corresponden a los eventos; la gráfica usa el período seleccionado.`);
    } catch (error) {
      if (version === state.recordVersion) sectionError("archive-error", new Error(`No se pudieron consultar los eventos. ${error.message}`));
    } finally {
      state.archiveBusy = false;
      $("archive-list").setAttribute("aria-busy", "false");
      updateMutationControls();
      if (version !== state.recordVersion) loadArchive();
    }
  }

  async function refreshRecords() {
    if (!state.serviceAvailable) return;
    try { recordFilters(true); }
    catch (error) { setText("records-feedback", error.message); return; }
    state.recordVersion += 1;
    setText("records-feedback", "Consultando los registros conservados…");
    await Promise.all(state.navigationReady ? [loadArchive()] : [loadIncidents(), loadArchive()]);
  }

  async function refreshIncidents() {
    if (!state.serviceAvailable) return;
    try { recordFilters(false, true); }
    catch (error) { setText("incidents-feedback", error.message); return; }
    state.incidentVersion += 1;
    setText("incidents-feedback", "Consultando los incidentes conservados…");
    await loadIncidents();
  }

  async function downloadFile(path, filename, feedbackId) {
    if (!state.serviceAvailable || state.downloadBusy) return;
    state.downloadBusy = true;
    updateMutationControls();
    setText(feedbackId, "Preparando la descarga…");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, { cache: "no-store", signal: controller.signal });
      if (!response.ok) {
        let detail;
        try { detail = await response.json(); } catch { /* Preserve a useful HTTP error. */ }
        throw new Error(detail?.error || detail?.message || `No se pudo preparar el archivo (${response.status}).`);
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => window.URL.revokeObjectURL(url), 60000);
      setText(feedbackId, "Archivo entregado al navegador. Revisa sus descargas para guardarlo.");
    } catch (error) {
      setText(feedbackId, error.name === "AbortError" ? "La descarga no respondió a tiempo. El monitor sigue funcionando; puedes intentarlo de nuevo." : `No se pudo descargar el archivo. ${error.message}`);
    } finally {
      clearTimeout(timeout);
      state.downloadBusy = false;
      updateMutationControls();
    }
  }

  function updateCountdown() {
    const protection = state.status?.protection;
    if (state.stopAcknowledged || !protection || protection.state !== "countdown") {
      setText("countdown", "—");
      return;
    }
    const deadline = parseDate(protection.deadline);
    const seconds = deadline ? Math.max(0, Math.ceil((deadline.getTime() - Date.now()) / 1000)) : finite(protection.countdown_remaining) ? Math.max(0, Math.ceil(protection.countdown_remaining)) : null;
    setText("countdown", seconds === null ? "—" : `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`);
    $("countdown").title = !state.serviceAvailable ? "Referencia del último plazo confirmado. La ejecución y la cancelación requieren confirmación del monitor." : "Tiempo restante comunicado por el monitor.";
  }

  function applyStatus(data) {
    if (!data || typeof data !== "object" || !data.connection || !data.policy || !data.protection) throw new Error("El monitor devolvió un estado incompleto.");
    const previousBatteryKey = state.metric === "battery" ? chartSeries()[0].key : null;
    const recovered = !state.serviceAvailable;
    const historyRecovered = state.status?.history?.available === false && data.history?.available === true;
    state.status = data;
    state.serviceAvailable = true;
    state.stopAcknowledged = false;
    state.lastStatusAt = new Date();
    state.token = typeof data.csrf_token === "string" ? data.csrf_token : null;
    if (finite(data.meta?.history_interval_seconds) && data.meta.history_interval_seconds > 0) state.historyInterval = data.meta.history_interval_seconds * 1000;
    $("development-notice").hidden = state.navigationReady || !data.meta?.dry_run;
    $("bridge-policy-note").hidden = data.meta?.bridge_required !== true;
    renderServiceState();
    const connection = data.connection || {};
    const sample = connection.state === "connected" ? data.sample : null;
    renderConnection(connection, sample);
    renderSample(sample);
    renderEvents(data.events);
    renderPolicy(data.policy || {});
    renderProtection(data.protection || {});
    renderNotifications(data.notifications);
    renderHistoryStatus();
    renderHealth(data.health);
    if (data.health?.observation) renderObservation(data.health.observation);
    else renderObservation();
    renderOverview();
    loadViewData();
    // Status and history requests may arrive in either order, especially on first load.
    if (state.metric === "battery" && previousBatteryKey !== chartSeries()[0].key) renderChart();
    if (recovered || historyRecovered) loadHistory();
  }

  async function loadStatus() {
    if (state.statusBusy || state.mutation) return;
    state.statusBusy = true;
    const generation = state.statusGeneration;
    try {
      const data = await request("/api/status");
      if (generation !== state.statusGeneration) return;
      applyStatus(data);
    } catch (error) {
      if (generation !== state.statusGeneration) return;
      serviceUnavailable(error);
    } finally {
      state.statusBusy = false;
    }
  }

  function normalizeHistory(samples) {
    return (Array.isArray(samples) ? samples : [])
      .map((sample) => ({ ...sample, _time: parseDate(sample.timestamp)?.getTime() }))
      .filter((sample) => Number.isFinite(sample._time))
      .sort((a, b) => a._time - b._time);
  }

  async function loadOverviewHistory() {
    if (!state.navigationReady || state.overviewHistoryBusy) return;
    state.overviewHistoryBusy = true;
    const generation = ++state.overviewGeneration;
    try {
      const data = await request("/api/history?hours=1");
      if (generation !== state.overviewGeneration) return;
      state.overviewHistory = normalizeHistory(data.samples);
      state.overviewHistoryError = null;
    } catch (error) {
      if (generation !== state.overviewGeneration) return;
      state.overviewHistory = [];
      state.overviewHistoryError = error.message;
    } finally {
      state.overviewHistoryBusy = false;
      renderOverviewChart();
    }
  }

  async function loadHistory() {
    if (state.historyBusy) return;
    state.historyBusy = true;
    const requestedHours = state.hours;
    const queryHours = state.navigationReady ? Math.max(1, requestedHours) : requestedHours;
    const overviewGeneration = queryHours === 1 ? ++state.overviewGeneration : null;
    try {
      const data = await request(`/api/history?hours=${queryHours}`);
      if (requestedHours !== state.hours) return;
      state.historyQueryError = null;
      state.history = normalizeHistory(data.samples);
      if (queryHours === 1 && overviewGeneration === state.overviewGeneration) {
        state.overviewHistory = state.history;
        state.overviewHistoryError = null;
      }
      renderChart();
      renderOverviewChart();
    } catch (error) {
      if (requestedHours !== state.hours) return;
      state.historyQueryError = error.message;
      if (queryHours === 1 && overviewGeneration === state.overviewGeneration) {
        state.overviewHistory = [];
        state.overviewHistoryError = error.message;
        renderOverviewChart();
      }
      renderHistoryStatus();
    } finally {
      // The history reader has a bounded concurrency limit. Query the full last hour
      // after a long-range response, and finish it before starting another range.
      if (state.navigationReady && queryHours > 1 && requestedHours === state.hours) await loadOverviewHistory();
      state.historyBusy = false;
      if (requestedHours !== state.hours) loadHistory();
    }
  }

  function svgElement(tag, attributes = {}, content) {
    const el = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attributes)) el.setAttribute(key, String(value));
    if (content !== undefined) el.textContent = content;
    return el;
  }

  function chartSeries() {
    if (state.metric === "battery") {
      const voltage = state.status?.meta?.source === "native" || (!state.history.some((sample) => finite(sample.battery_percent)) && state.history.some((sample) => finite(sample.battery_voltage)));
      return [{ key: voltage ? "battery_voltage" : "battery_percent", label: voltage ? "Voltaje de batería" : "Batería", color: "var(--teal)" }];
    }
    if (state.metric === "load") return [{ key: "load_percent", label: "Carga", color: "var(--blue)" }];
    return [{ key: "input_voltage", label: "Entrada", color: "var(--teal)" }, { key: "output_voltage", label: "Salida", color: "var(--blue)" }];
  }

  function renderChart() {
    const chart = $("history-chart");
    chart.replaceChildren();
    const series = chartSeries();
    const unit = state.metric === "voltage" || series[0].key === "battery_voltage" ? "V" : "%";
    const legend = $("chart-legend");
    legend.replaceChildren();
    for (const line of series) {
      const label = document.createElement("span");
      const mark = document.createElement("i");
      mark.style.background = line.color;
      label.append(mark, document.createTextNode(line.label));
      legend.append(label);
    }
    const legendUnit = document.createElement("span");
    legendUnit.className = "chart-unit";
    legendUnit.textContent = unit === "V" ? "VOLTIOS" : "PORCENTAJE";
    legend.append(legendUnit);
    const end = Date.now();
    const start = end - state.hours * 3600000;
    const samples = state.history.filter((sample) => sample._time >= start && sample._time <= end);
    const values = samples.flatMap((sample) => series.map((line) => sample[line.key])).filter(finite);
    const available = values.length > 0;
    let yMin = 0;
    let yMax = 100;
    if (unit === "V") {
      if (available) {
        const smallest = values.reduce((minimum, value) => Math.min(minimum, value), Infinity);
        const largest = values.reduce((maximum, value) => Math.max(maximum, value), -Infinity);
        const batteryVoltage = series[0].key === "battery_voltage";
        const step = batteryVoltage ? 0.5 : 10;
        const padding = Math.max(batteryVoltage ? 0.5 : 5, (largest - smallest) * 0.2);
        yMin = Math.max(0, Math.floor((smallest - padding) / step) * step);
        yMax = Math.ceil((largest + padding) / step) * step;
      } else { yMin = 0; yMax = series[0].key === "battery_voltage" ? 1 : 250; }
    } else if (available) yMax = Math.max(100, Math.ceil(values.reduce((maximum, value) => Math.max(maximum, value), 0) / 20) * 20);
    const left = 38, right = 786, top = 14, bottom = 192;
    const x = (time) => left + (time - start) / (end - start) * (right - left);
    const y = (value) => bottom - (value - yMin) / (yMax - yMin) * (bottom - top);
    for (let index = 0; index <= 4; index++) {
      const value = yMin + (yMax - yMin) * index / 4;
      const py = y(value);
      chart.append(svgElement("line", { x1: left, x2: right, y1: py, y2: py, class: "chart-grid" }));
      chart.append(svgElement("text", { x: left - 10, y: py + 3, class: "chart-axis", "text-anchor": "end" }, numeric(value, series[0].key === "battery_voltage" ? 1 : 0)));
    }
    for (let index = 0; index <= 4; index++) {
      const time = start + (end - start) * index / 4;
      const date = new Date(time);
      const label = state.hours > 24 ? new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "2-digit" }).format(date) : shortClockFormat.format(date);
      chart.append(svgElement("text", { x: x(time), y: 219, class: "chart-axis", "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle" }, label));
    }
    // Never connect samples across a missing measurement or a gap in collection.
    const intervals = samples.slice(1).map((sample, index) => sample._time - samples[index]._time).filter((interval) => interval > 0).sort((a, b) => a - b);
    const expectedInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)] : state.historyInterval;
    const maxGap = Math.max(state.historyInterval * 3, expectedInterval * 3);
    for (const line of series) {
      let path = "";
      let previous = null;
      const dots = [];
      for (const sample of samples) {
        const value = sample[line.key];
        if (!finite(value)) { previous = null; continue; }
        // The backend checks original measurements before decimating long ranges.
        const continuing = previous !== null && (typeof sample.gap_before === "boolean" ? !sample.gap_before : sample._time - previous <= maxGap);
        const px = x(sample._time), py = y(value);
        path += `${continuing ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)} `;
        if (!continuing) dots.push([px, py]);
        previous = sample._time;
      }
      if (path) chart.append(svgElement("path", { d: path.trim(), class: "chart-line", stroke: line.color }));
      for (const [cx, cy] of dots) chart.append(svgElement("circle", { cx, cy, r: 2, fill: line.color }));
    }
    const cursor = svgElement("line", { x1: 0, x2: 0, y1: top, y2: bottom, class: "chart-cursor", visibility: "hidden" });
    chart.append(cursor);
    state.chart = { samples, series, start, end, left, right, x, cursor, unit, maxGap };
    $("chart-empty").hidden = available;
    if (!available) {
      $("chart-empty").querySelector("strong").textContent = state.history.length ? "Sin lecturas para este período" : "El historial empieza aquí";
      $("chart-empty").querySelector("p").textContent = state.history.length ? "Prueba con otro período o espera nuevas lecturas del equipo." : "Las lecturas reales se guardarán mientras el monitor esté en ejecución.";
    }
    const period = { "0.25": "últimos 15 minutos", "1": "última hora", "6": "últimas 6 horas", "24": "últimas 24 horas", "168": "últimos 7 días" }[String(state.hours)];
    chart.setAttribute("aria-label", available ? `Historial de ${state.metric === "voltage" ? "voltaje" : series[0].key === "battery_voltage" ? "voltaje de batería" : state.metric === "battery" ? "batería" : "carga"}, ${period}. ${samples.length} puntos mostrados. Puedes exportar los datos en CSV.` : "Historial sin lecturas disponibles para este período.");
    $("chart-tooltip").hidden = true;
    renderHistoryStatus();
  }

  $("history-chart").addEventListener("pointermove", (event) => {
    const chart = state.chart;
    if (!chart || !chart.samples.length || !$("chart-empty").hidden) return;
    const bounds = $("history-chart").getBoundingClientRect();
    const localX = (event.clientX - bounds.left) / bounds.width * 800;
    if (localX < chart.left || localX > chart.right) return hideTooltip();
    const targetTime = chart.start + (localX - chart.left) / (chart.right - chart.left) * (chart.end - chart.start);
    let closest = chart.samples[0];
    for (const sample of chart.samples) if (Math.abs(sample._time - targetTime) < Math.abs(closest._time - targetTime)) closest = sample;
    const hasValue = chart.series.some((line) => finite(closest[line.key]));
    if (!hasValue || Math.abs(closest._time - targetTime) > Math.max(chart.maxGap, (chart.end - chart.start) * 0.015)) return hideTooltip();
    const px = chart.x(closest._time);
    chart.cursor.setAttribute("x1", px);
    chart.cursor.setAttribute("x2", px);
    chart.cursor.setAttribute("visibility", "visible");
    const tooltip = $("chart-tooltip");
    tooltip.textContent = `${dateFormat.format(new Date(closest._time))}\n${chart.series.map((line) => `${line.label}: ${numeric(closest[line.key], 1)} ${chart.unit}`).join("\n")}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.max(5, Math.min(bounds.width - tooltip.offsetWidth - 5, event.clientX - bounds.left + 14))}px`;
  });

  function hideTooltip() {
    $("chart-tooltip").hidden = true;
    state.chart?.cursor.setAttribute("visibility", "hidden");
  }

  $("history-chart").addEventListener("pointerleave", hideTooltip);
  function savePreferences() {
    try { localStorage.setItem("elipro-history", JSON.stringify({ metric: state.metric, hours: state.hours })); }
    catch { /* Monitoring must work when browser storage is unavailable. */ }
  }

  function selectMetric() {
    document.querySelectorAll("[data-metric]").forEach((tab) => {
      const active = tab.dataset.metric === state.metric;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-pressed", String(active));
    });
  }

  document.querySelectorAll("[data-metric]").forEach((button) => {
    button.addEventListener("click", () => {
      state.metric = button.dataset.metric;
      selectMetric();
      savePreferences();
      renderChart();
    });
  });
  $("history-period").addEventListener("change", () => {
    state.hours = Number($("history-period").value);
    state.overviewGeneration += 1;
    savePreferences();
    state.history = [];
    renderChart();
    loadHistory();
  });
  $("events-filter").addEventListener("change", () => {
    state.eventFilter = $("events-filter").value;
    renderEvents(state.events, false);
  });
  $("settings-toggle").addEventListener("click", () => {
    const button = $("settings-toggle");
    const expanded = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(expanded));
    $("settings-panel").hidden = !expanded;
  });
  $("policy-form").addEventListener("input", () => {
    state.policyDirty = true;
    setText("enabled-label", $("policy-enabled").checked ? "Activada al guardar" : "Desactivada al guardar");
    setText("settings-feedback", "Cambios sin guardar");
    $("settings-feedback").classList.remove("error");
  });
  $("notifications-form").addEventListener("input", () => {
    state.notificationsDirty = true;
    setText("notifications-feedback", "Cambios sin guardar");
    $("notifications-feedback").classList.remove("error");
  });
  $("notifications-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.serviceAvailable || state.mutation || !state.status?.notifications?.settings) return;
    if (!$("notifications-form").reportValidity()) return;
    const settings = {
      enabled: $("notifications-enabled").checked,
      sound_enabled: $("notifications-sound").checked,
      repeat_seconds: Number($("notifications-repeat").value),
    };
    if (typeof state.status.notifications.settings.persistent_critical === "boolean") settings.persistent_critical = $("notifications-persistent").checked;
    state.mutation = "save-notifications";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("save-notifications", "Guardando…");
    try {
      const result = await post("/api/notifications", settings);
      applyStatus(result);
      if (!result.notifications?.settings) throw new Error("El monitor no confirmó la configuración de avisos.");
      state.notificationsDirty = false;
      state.notificationsInitialized = false;
      renderNotifications(result.notifications);
      setText("notifications-feedback", "Avisos guardados");
      $("notifications-feedback").classList.remove("error");
      toast("Configuración de avisos guardada. Se aplica aunque cierres el panel.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("notifications-feedback", "Guardado sin confirmar");
      $("notifications-feedback").classList.add("error");
      toast(error.message, true);
    } finally {
      state.mutation = null;
      setText("save-notifications", "Guardar avisos");
      updateMutationControls();
    }
  });
  $("test-notifications").addEventListener("click", async () => {
    if ($("test-notifications").disabled || state.mutation) return;
    state.mutation = "test-notifications";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("test-notifications", "Enviando prueba…");
    try {
      const result = await post("/api/notifications/test", {});
      applyStatus(result);
      if (result.notifications?.last_notification?.accepted === false) {
        throw new Error(result.notifications.last_error || result.notifications.delivery?.last_error || "El monitor no pudo aceptar la prueba de avisos.");
      }
      setText("notifications-feedback", "Prueba solicitada");
      $("notifications-feedback").classList.remove("error");
      toast("Prueba enviada al monitor. Comprueba el aviso de escritorio y el sonido configurado.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("notifications-feedback", "Prueba sin confirmar");
      $("notifications-feedback").classList.add("error");
      toast(error.message, true);
    } finally {
      state.mutation = null;
      updateMutationControls();
    }
  });
  $("policy-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.serviceAvailable || state.mutation) return;
    if (!$("policy-form").reportValidity()) return;
    const button = $("save-policy");
    state.mutation = "save";
    state.statusGeneration += 1;
    updateMutationControls();
    button.textContent = "Guardando…";
    const policy = {
      enabled: $("policy-enabled").checked,
      on_battery_seconds: Math.round(Number($("policy-on-battery").value) * 60),
      low_battery_percent: state.status?.meta?.source === "native" ? state.status.policy.low_battery_percent : Number($("policy-low-battery").value),
      min_runtime_seconds: state.status?.meta?.source === "native" ? state.status.policy.min_runtime_seconds : Number($("policy-runtime").value),
      countdown_seconds: Number($("policy-countdown").value),
      action: $("policy-action").value,
    };
    try {
      const result = await post("/api/policy", policy);
      applyStatus(result);
      state.policyDirty = false;
      state.policyInitialized = false;
      renderPolicy(result.policy);
      setText("settings-feedback", "Configuración guardada");
      $("settings-feedback").classList.remove("error");
      toast(policy.enabled ? "Protección automática activada en esta PC." : "Configuración guardada. La protección está desactivada.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("settings-feedback", "Guardado sin confirmar");
      $("settings-feedback").classList.add("error");
      toast(error.message, true);
    } finally {
      state.mutation = null;
      updateMutationControls();
      button.textContent = "Guardar configuración";
    }
  });
  $("test-shutdown").addEventListener("click", async () => {
    if (!state.serviceAvailable || state.mutation || state.status?.policy?.enabled) return;
    state.mutation = "test";
    state.statusGeneration += 1;
    updateMutationControls();
    try {
      const result = await post("/api/protection/test", {});
      applyStatus(result);
      if (!result.protection?.simulation || result.protection.state !== "countdown") throw new Error("El monitor no confirmó el inicio de la prueba.");
      toast("Prueba iniciada. Windows no se apagará ni hibernará.");
      $("protection-alert").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      toast(error.message, true);
    } finally { state.mutation = null; updateMutationControls(); }
  });
  $("cancel-shutdown").addEventListener("click", async () => {
    if (!state.serviceAvailable || state.mutation) return;
    state.mutation = "cancel";
    state.statusGeneration += 1;
    updateMutationControls();
    try {
      const simulation = !!state.status?.protection?.simulation;
      const result = await post("/api/protection/cancel", {});
      applyStatus(result);
      if (["countdown", "executed"].includes(result.protection.state)) throw new Error("El monitor aún reporta una cuenta atrás o una acción enviada a Windows.");
      toast(simulation ? "El monitor confirmó la cancelación de la prueba." : result.protection.state === "cancelled" ? "Cuenta atrás cancelada. La protección volverá a evaluarse en el próximo corte." : "El monitor confirma que no hay una cuenta atrás activa.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      toast(`No se confirmó la cancelación. ${error.message}`, true);
    } finally { state.mutation = null; updateMutationControls(); }
  });

  $("refresh-records").addEventListener("click", refreshRecords);
  if (document.querySelectorAll("[data-page]").length) {
    $("refresh-incidents")?.addEventListener("click", refreshIncidents);
    $("incidents-form")?.addEventListener("submit", (event) => { event.preventDefault(); return refreshIncidents(); });
    $("clear-incidents")?.addEventListener("click", () => {
      $("incidents-from").value = "";
      $("incidents-to").value = "";
      if (state.incidentsLoaded) return refreshIncidents();
      setText("incidents-feedback", "Fechas vacías: la consulta incluirá todos los incidentes conservados.");
    });
  }
  $("run-validation").addEventListener("click", runValidation);
  $("review-physical-tests").addEventListener("click", reviewPhysicalTests);
  $("refresh-stability").addEventListener("click", loadObservation);
  $("start-stability").addEventListener("click", () => changeObservation(true));
  $("stop-stability").addEventListener("click", () => changeObservation(false));
  $("download-stability").addEventListener("click", () => downloadFile("/api/stability/report", "elipro-observacion.json", "stability-summary"));
  $("records-form").addEventListener("submit", (event) => { event.preventDefault(); return refreshRecords(); });
  $("event-search-form").addEventListener("submit", (event) => { event.preventDefault(); return refreshRecords(); });
  $("clear-records").addEventListener("click", () => {
    $("records-from").value = "";
    $("records-to").value = "";
    if (state.navigationReady ? state.archiveLoaded : state.recordsLoaded) return refreshRecords();
    setText("records-feedback", "Fechas vacías: la consulta incluirá todo el historial conservado.");
  });
  $("more-incidents").addEventListener("click", () => loadIncidents(true));
  $("more-events").addEventListener("click", () => loadArchive(true));
  $("export-events").addEventListener("click", () => {
    try { return downloadFile(`/api/events/export.csv?${recordFilters(true)}`, "elipro-eventos.csv", "archive-summary"); }
    catch (error) { sectionError("archive-error", error); }
  });
  $("download-diagnostics").addEventListener("click", () => downloadFile("/api/diagnostics", "elipro-diagnostico.json", "maintenance-feedback"));
  $("download-backup").addEventListener("click", () => downloadFile("/api/backup.zip", "elipro-respaldo.zip", "maintenance-feedback"));
  $("stop-monitor").addEventListener("click", () => { $("stop-confirmation").hidden = false; });
  $("cancel-stop-monitor").addEventListener("click", () => { $("stop-confirmation").hidden = true; });
  $("confirm-stop-monitor").addEventListener("click", async () => {
    if (!state.serviceAvailable || state.mutation) return;
    state.mutation = "stop";
    state.statusGeneration += 1;
    updateMutationControls();
    try {
      const result = await post("/api/service/stop", {});
      if (result.stopped !== true && result.stopping !== true) throw new Error("El monitor no confirmó que se está deteniendo.");
      state.stopAcknowledged = true;
      $("stop-confirmation").hidden = true;
      serviceUnavailable(new Error("El monitor se está deteniendo."));
      toast("Elipro se está cerrando. La PC sigue encendida.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      toast(`No se pudo confirmar la salida de Elipro. ${error.message}`, true);
    } finally { state.mutation = null; updateMutationControls(); }
  });

  try {
    const preferences = JSON.parse(localStorage.getItem("elipro-history") || "null");
    if (["voltage", "battery", "load"].includes(preferences?.metric)) state.metric = preferences.metric;
    if ([0.25, 1, 6, 24, 168].includes(preferences?.hours)) state.hours = preferences.hours;
  } catch { /* Ignore old or invalid preferences. */ }
  selectMetric();
  applyTheme(selectedTheme);
  $("theme-toggle")?.addEventListener("click", () => {
    applyTheme(selectedTheme === "light" ? "dark" : "light", true);
  });
  $("history-period").value = String(state.hours);
  setupNavigation();
  updateMutationControls();
  renderChart();
  loadStatus();
  loadHistory();
  setInterval(loadStatus, 2000);
  setInterval(loadHistory, 30000);
  setInterval(() => { if (state.observationLoaded) loadObservation(); }, 60000);
  setInterval(() => { updateFreshness(); updateCountdown(); }, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initializeApp, { once: true });
  else initializeApp();
})();
