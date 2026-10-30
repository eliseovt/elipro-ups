"use strict";

(() => {
  const themeStorageKey = "elipro-theme";
  let selectedTheme = "dark";
  let themeTransitionTimer = null;
  try {
    const savedTheme = localStorage.getItem(themeStorageKey);
    if (savedTheme === "light" || savedTheme === "dark") selectedTheme = savedTheme;
  } catch { /* Theme storage is optional; retain the established dark appearance. */ }

  function applyTheme(theme, persist = false) {
    if (persist) {
      clearTimeout(themeTransitionTimer);
      const animate = !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      document.documentElement.classList.toggle("theme-transitioning", animate);
      if (animate) themeTransitionTimer = setTimeout(() => document.documentElement.classList.remove("theme-transitioning"), 350);
    }
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
    metric: "voltage",
    hours: 1,
    token: null,
    policyInitialized: false,
    policyDirty: false,
    notificationsInitialized: false,
    notificationsDirty: false,
    webhooksInitialized: false,
    webhooksDirty: false,
    webhookTestSignature: null,
    webhookDeliveriesSignature: null,
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
    recordFilters: "",
    records: [],
    recordCursor: null,
    recordQuery: "",
    recordsBusy: false,
    recordsLoaded: false,
    downloadBusy: false,
    stopAcknowledged: false,
    activeView: "overview",
    navigationReady: false,
    criticalKnown: new Map(),
    criticalAcknowledged: new Set(),
    criticalItems: new Map(),
    pendingNotifications: new Map(),
    criticalOpen: false,
    criticalDominantKey: null,
    countdownView: null,
    criticalFocusTarget: null,
    criticalBatterySources: new Set(),
    criticalCountdownVisible: false,
    criticalCountdownAcknowledged: null,
    criticalResultAcknowledged: null,
    upsPowerReviewNeedsStatus: false,
    upsPowerReviewOperation: null,
  };
  const views = {
    overview: { hash: "resumen", title: "Resumen", description: "" },
    protection: { hash: "proteccion", title: "Protección", description: "Elige qué hacer cuando se corte la luz." },
    history: { hash: "historial", title: "Historial", description: "" },
    system: { hash: "sistema", title: "Sistema", description: "" },
    webhooks: { hash: "webhooks", title: "Webhooks", description: "Configura y gestiona tus integraciones con sistemas externos." },
  };
  const webhookEventIds = ["power_lost", "power_restored", "battery_low", "overload", "ups_fault"];
  const clockFormat = new Intl.DateTimeFormat("es-PE", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const shortClockFormat = new Intl.DateTimeFormat("es-PE", { hour: "2-digit", minute: "2-digit", hour12: false });
  const dateFormat = new Intl.DateTimeFormat("es-PE", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
  const recordDateFormat = new Intl.DateTimeFormat("es-PE", { year: "numeric", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const monotonicNow = () => typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();
  const percentage = (value) => finite(value) && value >= 0 && value <= 100;
  const estimatedBattery = (sample) => percentage(sample?.battery_percent_estimate) && typeof sample?.battery_estimate_method === "string" && sample.battery_estimate_method.trim().length > 0;
  const batteryCharge = (sample) => percentage(sample?.battery_percent) ? { value: sample.battery_percent, source: "reported" }
    : estimatedBattery(sample) && sample.battery_estimate_method === "nt2402pro-pmv-qs-v1" ? { value: sample.battery_percent_estimate, source: "estimated" } : null;
  const lowBatteryThreshold = () => percentage(state.status?.policy?.low_battery_percent) ? state.status.policy.low_battery_percent : 20;
  const numeric = (value, decimals = 0) => finite(value) ? new Intl.NumberFormat("es-PE", { maximumFractionDigits: decimals }).format(value) : "—";
  const parseDate = (value) => {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isFinite(parsed.getTime()) ? parsed : null;
  };

  const criticalDefinitions = [
    { kind: "ups_fault", title: "Falla del UPS", active: (sample) => sample.fault === true ? true : sample.fault === false || ["Line mode", "AVR mode", "Battery mode", "Battery test mode", "Shutdown mode"].includes(sample.raw_status) ? false : null,
      message: () => "El UPS reporta una falla. Revisa sus indicadores físicos." },
    { kind: "overload", title: "Sobrecarga del UPS", active: (sample) => finite(sample.load_percent) ? sample.load_percent >= 100 : null,
      message: (sample) => `Carga reportada: ${numeric(sample.load_percent)} %. La capacidad del UPS está al límite o se ha superado.` },
    { kind: "battery_low", title: "Batería baja", active: (sample) => {
      const charge = batteryCharge(sample);
      const threshold = lowBatteryThreshold();
      const percentageLow = threshold > 0 && charge && charge.value <= threshold
        && (charge.source === "reported" || sample.on_battery === true);
      if (sample.low_battery === true) state.criticalBatterySources.add("signal");
      if (percentageLow) state.criticalBatterySources.add("percent");
      if (sample.low_battery === true || percentageLow) return true;
      if (state.criticalBatterySources.size) {
        return [...state.criticalBatterySources].every((source) => source === "signal" ? sample.low_battery === false
          : threshold === 0 || charge && (charge.value > threshold || charge.source === "estimated" && sample.on_battery === false)) ? false : null;
      }
      return sample.low_battery === false || charge && charge.value > threshold ? false : null;
    },
      message: (sample) => sample.low_battery === true ? "El UPS ha enviado la señal de batería baja."
        : `Carga ${batteryCharge(sample)?.source === "estimated" ? "estimada" : "reportada"} de batería: ${numeric(batteryCharge(sample)?.value)} %. Umbral configurado: ${numeric(lowBatteryThreshold())} %.` },
  ];
  const criticalKinds = new Set(criticalDefinitions.map((definition) => definition.kind));

  function powerTransition() {
    const power = state.status?.system_power?.state;
    const connection = state.status?.connection?.state;
    return ["suspended", "resuming"].includes(power) ? power
      : ["suspended", "resuming"].includes(connection) ? connection : null;
  }

  function telemetryContext() {
    if (!state.serviceAvailable) return { sample: null, quality: state.stopAcknowledged ? "stopped" : state.lastStatusAt ? "disconnected" : "starting", mode: "unknown" };
    const transition = powerTransition();
    if (transition) return { sample: null, quality: transition, mode: "unknown" };
    const connection = state.status?.connection || {};
    if (connection.state !== "connected") return { sample: null, quality: connection.state === "stale" ? "stale" : "disconnected", mode: "unknown" };
    const sample = state.status?.sample;
    const stamp = parseDate(sample?.timestamp);
    const age = stamp ? (Date.now() - stamp.getTime()) / 1000 : null;
    if (!sample || sample.connected === false || !stamp || age < -5 || age > 15) return { sample: null, quality: sample ? "stale" : "starting", mode: "unknown" };
    return { sample, quality: "live", mode: sample.fault === true ? "fault" : sample.on_battery === true ? "battery" : sample.on_battery === false ? "line" : "unknown" };
  }

  function captureCriticalAcknowledgment() {
    const protection = state.status?.protection;
    return {
      kinds: [...state.criticalItems.keys()],
      resultKey: protectionResultKey(protection),
      countdown: protection?.state === "countdown" ? {
        id: protection.countdown_id || null,
        deadline: parseDate(protection.deadline)?.getTime() ?? null,
        remaining: finite(protection.countdown_remaining) ? protection.countdown_remaining : null,
        simulation: !!protection.simulation,
        action: state.status?.policy?.action,
        phase: protection.phase || "windows_countdown",
      } : null,
    };
  }

  function finishCriticalAcknowledgment(acknowledgment) {
    if (!state.criticalOpen) return;
    const protection = state.status?.protection;
    if (acknowledgment.resultKey === protectionResultKey(protection)) state.criticalResultAcknowledged = acknowledgment.resultKey;
    const currentCountdown = captureCriticalAcknowledgment().countdown;
    const seenCountdown = acknowledgment.countdown;
    const sameCountdown = currentCountdown && seenCountdown && currentCountdown.simulation === seenCountdown.simulation
      && currentCountdown.action === seenCountdown.action && currentCountdown.phase === seenCountdown.phase
      && (currentCountdown.id && seenCountdown.id ? currentCountdown.id === seenCountdown.id : currentCountdown.deadline !== null && seenCountdown.deadline !== null
        ? Math.abs(currentCountdown.deadline - seenCountdown.deadline) <= 2000
        : currentCountdown.deadline === seenCountdown.deadline && currentCountdown.remaining <= seenCountdown.remaining + 2);
    if (sameCountdown) state.criticalCountdownAcknowledged = seenCountdown;
    for (const kind of acknowledgment.kinds) {
      if (state.criticalKnown.get(kind) === true) state.criticalAcknowledged.add(kind);
      state.criticalItems.delete(kind);
    }
    // A new persisted notice or physical condition arriving with the response still needs its own acknowledgment.
    const currentResult = protectionResultKey(protection);
    if (state.pendingNotifications.size || state.criticalItems.size || currentCountdown && !sameCountdown
        || currentResult !== null && state.criticalResultAcknowledged !== currentResult) { refreshTelemetry(); return; }
    closeCriticalDialog();
  }

  function closeCriticalDialog() {
    if (!state.criticalOpen) return;
    state.criticalOpen = false;
    state.criticalDominantKey = null;
    const dialog = $("critical-dialog");
    if (dialog.open) dialog.close();
    const previous = state.criticalFocusTarget;
    state.criticalFocusTarget = null;
    const focusable = previous && previous !== document.body && previous.isConnected !== false && !previous.disabled && !previous.closest?.("[hidden], [inert]");
    (focusable ? previous : $("page-title"))?.focus?.({ preventScroll: true });
  }

  async function acknowledgeCritical() {
    if (!state.criticalOpen || state.mutation) return;
    const acknowledgment = captureCriticalAcknowledgment();
    const ids = [...state.pendingNotifications.keys()];
    if (!ids.length) { finishCriticalAcknowledgment(acknowledgment); return; }
    if (!state.serviceAvailable || !state.token) {
      setText("critical-feedback", "Recupera la conexión con el monitor para confirmar Entendido. Los avisos siguen pendientes.");
      $("critical-feedback").hidden = false;
      return;
    }
    state.mutation = "ack-notifications";
    state.statusGeneration += 1;
    $("critical-feedback").hidden = true;
    updateMutationControls();
    try {
      // The API bounds each commit to 200 IDs. Snapshot the displayed IDs once
      // so notices arriving during a large acknowledgment remain unread.
      for (let offset = 0; offset < ids.length; offset += 200) {
        const batch = ids.slice(offset, offset + 200);
        const result = await post("/api/notifications/ack", { ids: batch });
        if (!Array.isArray(result.notifications?.pending) || result.notifications.pending.some((item) => batch.includes(item.id))) {
          throw new Error("El monitor no confirmó Entendido para los avisos seleccionados.");
        }
        applyStatus(result);
      }
      finishCriticalAcknowledgment(acknowledgment);
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("critical-feedback", `No se confirmó Entendido. ${error.message} Los avisos siguen pendientes.`);
      $("critical-feedback").hidden = false;
    } finally {
      state.mutation = null;
      updateMutationControls();
    }
  }

  function syncPendingNotifications() {
    const pending = state.status?.notifications?.pending;
    if (state.serviceAvailable && Array.isArray(pending)) {
      const next = new Map();
      for (const notice of pending) {
        if (!Number.isSafeInteger(notice?.id) || notice.id <= 0) continue;
        // Recovered incidents stay unread in the monitor's durable inbox, but
        // they no longer require an interrupting dialog. Older monitors do not
        // publish requires_attention, so retain the same recovery rules here.
        const requiresAttention = typeof notice.requires_attention === "boolean" ? notice.requires_attention
          : !notice.resolved_at && !["power_restored", "connection_restored", "monitor_restarted", "policy_cancelled", "ups_poweroff_reviewed"].includes(notice.kind);
        if (!requiresAttention) continue;
        const item = state.pendingNotifications.get(notice.id) || { notificationId: notice.id };
        item.definition = { kind: String(notice.kind || "notification"), title: String(notice.title || eventLabel(notice.kind)) };
        item.status = notice.confirmed === false ? "unconfirmed" : "active";
        const timestamp = parseDate(notice.timestamp);
        item.priority = Number.isInteger(notice.priority) && notice.priority >= 0 && notice.priority <= 3 ? notice.priority : 0;
        item.timestamp = timestamp?.getTime() ?? 0;
        const resolved = parseDate(notice.resolved_at);
        item.message = `${String(notice.message || eventLabel(notice.kind))}${timestamp ? ` Registrado: ${recordDateFormat.format(timestamp)}.` : ""}${resolved ? ` Recuperado: ${recordDateFormat.format(resolved)}.` : ""}`;
        next.set(notice.id, item);
      }
      state.pendingNotifications = next;
    }
    if (!state.serviceAvailable) {
      for (const item of state.pendingNotifications.values()) if (item.status !== "resolved") item.status = "unconfirmed";
    }
  }

  function syncCountdownView(protection, elapsed = 0) {
    const id = protection?.countdown_id;
    if (protection?.state !== "countdown" || protection.simulation || typeof id !== "string" || !/^[a-f0-9]{32}$/.test(id)) {
      state.countdownView = null;
      return;
    }
    const now = monotonicNow();
    const seconds = finite(protection.countdown_remaining) ? protection.countdown_remaining
      : Math.max(0, ((parseDate(protection.deadline)?.getTime() ?? Date.now()) - Date.now()) / 1000);
    const deadline = now + Math.max(0, seconds * 1000 - elapsed);
    state.countdownView = { id, deadline: state.countdownView?.id === id ? Math.min(state.countdownView.deadline, deadline) : deadline,
      freshUntil: now + Math.max(0, Math.min(3000, finite(protection.valid_for_ms) ? protection.valid_for_ms : 0) - elapsed) };
  }

  function canCancelCountdown() {
    const protection = state.status?.protection;
    if (!state.serviceAvailable || !state.token || state.mutation || protection?.state !== "countdown"
        || protection.cancelable === false || simulationPhase() === "ups_delay") return false;
    if (protection.simulation) return true;
    const view = state.countdownView;
    return !!view && view.id === protection.countdown_id && monotonicNow() < Math.min(view.deadline, view.freshUntil);
  }

  function renderCriticalCountdown() {
    const protection = state.status?.protection;
    const phase = simulationPhase();
    const sequenceNote = protection?.simulation && phase === "ups_delay" ? `La solicitud a Windows solo se representa. ${simulationDelayDescription()}. No se confirma que Windows haya terminado ni se corta la energía.`
      : protection?.simulation && phase === "complete" ? "Secuencia simulada finalizada. No se ejecutaron acciones de Windows ni se cortó la energía del UPS."
      : protection?.simulation && phase === "windows_countdown" ? state.status?.ups_poweroff?.simulation
        ? "Simulación de la cuenta atrás de Windows. Después se representará la espera del UPS; no se ejecutan acciones reales."
        : "Simulación de la cuenta atrás de Windows. No se ejecutan acciones reales."
      : protection?.state === "executed" && !protection.simulation ? "La solicitud enviada a Windows no confirma que el apagado o la hibernación hayan terminado." : protection?.state === "countdown" && !protection.simulation ? "Cancelar protección anula Windows y UPS hasta el próximo corte. Todavía no se ha programado el corte del UPS." : "";
    $("critical-sequence-note").hidden = !state.criticalOpen || !sequenceNote;
    setText("critical-sequence-note", sequenceNote);
    const visible = state.criticalOpen && !state.stopAcknowledged && protection?.state === "countdown";
    const cancelable = protection?.cancelable !== false && phase !== "ups_delay";
    $("critical-countdown").hidden = !visible;
    $("critical-cancel").hidden = !visible || !cancelable;
    if (!visible) { state.criticalCountdownVisible = false; return; }
    if (!state.criticalCountdownVisible) $("critical-feedback").hidden = true;
    state.criticalCountdownVisible = true;
    const label = protection.simulation ? phase === "ups_delay" ? "Espera simulada del UPS" : "Cuenta atrás de prueba" : state.status?.policy?.action === "hibernate" ? "Hibernación programada" : "Apagado programado";
    setText("critical-countdown-label", `${label}. ${phase === "ups_delay" ? "Esta fase solo representa la espera del UPS." : "Cerrar el aviso no cancela la protección."}${!state.serviceAvailable ? " Estado sin confirmar." : ""}`);
    setText("critical-countdown-time", $("countdown").textContent);
    $("critical-cancel").textContent = "Cancelar protección";
    $("critical-cancel").disabled = !cancelable || !canCancelCountdown();
  }

  function protectionResultKey(protection) {
    return ["blocked", "executed"].includes(protection?.state)
      ? JSON.stringify([protection.state, !!protection.simulation, state.status?.policy?.action, protection.reason || ""])
      : null;
  }

  function renderCriticalAlerts(sample, live) {
    const previousPendingNotifications = [...state.pendingNotifications.values()];
    const hadPendingNotifications = previousPendingNotifications.length > 0;
    syncPendingNotifications();
    const rawPending = state.status?.notifications?.pending;
    const acknowledgedCountdownElsewhere = state.serviceAvailable && Array.isArray(rawPending)
      && previousPendingNotifications.some((item) => item.definition.kind === "policy_countdown"
        && !rawPending.some((notice) => notice.id === item.notificationId));
    // The monitor owns incident episodes and durable acknowledgments. Older
    // versions without an inbox retain the existing telemetry-only fallback.
    const hasInbox = Array.isArray(state.status?.notifications?.pending);
    if (hasInbox) {
      state.criticalItems.clear();
    } else if (live && sample) {
      for (const definition of criticalDefinitions) {
        const active = definition.active(sample);
        if (active === null) {
          if (state.criticalItems.has(definition.kind)) state.criticalItems.get(definition.kind).status = "unconfirmed";
          continue;
        }
        state.criticalKnown.set(definition.kind, active);
        if (!active) state.criticalAcknowledged.delete(definition.kind);
        if (!active && definition.kind === "battery_low") state.criticalBatterySources.clear();
        if (active && !state.criticalAcknowledged.has(definition.kind)) {
          const item = state.criticalItems.get(definition.kind) || { definition };
          item.status = "active";
          item.message = definition.message(sample);
          state.criticalItems.set(definition.kind, item);
        } else if (!active) {
          state.criticalItems.delete(definition.kind);
        }
      }
    } else {
      for (const item of state.criticalItems.values()) item.status = "unconfirmed";
    }
    const protection = state.status?.protection || {};
    const countdown = !state.stopAcknowledged && protection.state === "countdown";
    const resultKey = !state.stopAcknowledged ? protectionResultKey(protection) : null;
    if (!resultKey) state.criticalResultAcknowledged = null;
    const resultAttention = resultKey !== null && state.criticalResultAcknowledged !== resultKey;
    const simulationResultVisible = state.criticalOpen && protection.simulation === true && simulationPhase() === "complete";
    if (!countdown) state.criticalCountdownAcknowledged = null;
    const acknowledged = state.criticalCountdownAcknowledged;
    const deadline = parseDate(protection.deadline)?.getTime() ?? null;
    if (acknowledged?.simulation && protection.simulation && acknowledged.phase === "windows_countdown" && simulationPhase() === "ups_delay") {
      acknowledged.phase = "ups_delay";
      acknowledged.deadline = deadline;
      acknowledged.remaining = protection.countdown_remaining;
    }
    // The service reconstructs its wall-clock deadline from rounded seconds on every poll.
    const newDeadline = deadline !== null && finite(acknowledged?.deadline) && Math.abs(deadline - acknowledged.deadline) > 2000;
    const increasedRemaining = deadline === null && finite(protection.countdown_remaining) && finite(acknowledged?.remaining) && protection.countdown_remaining > acknowledged.remaining + 2;
    const changedEpisode = protection.countdown_id && acknowledged?.id ? protection.countdown_id !== acknowledged.id : newDeadline || increasedRemaining;
    if (countdown && state.serviceAvailable && acknowledged && (changedEpisode || acknowledged.simulation !== !!protection.simulation || acknowledged.action !== state.status?.policy?.action)) state.criticalCountdownAcknowledged = null;
    if (hadPendingNotifications && !state.pendingNotifications.size && state.mutation !== "ack-notifications"
        && !state.criticalItems.size && countdown && acknowledgedCountdownElsewhere && !resultAttention) {
      // Entendido in the native window is the same explicit action as in this panel.
      finishCriticalAcknowledgment(captureCriticalAcknowledgment());
    }
    if (!state.criticalItems.size && !state.pendingNotifications.size && (!countdown || state.criticalCountdownAcknowledged) && !resultAttention && !simulationResultVisible) {
      // Automatic recovery only closes the view. It never acknowledges notices,
      // suppresses a new incident, or changes an active protection action.
      closeCriticalDialog();
      renderCriticalCountdown();
      return;
    }
    const pending = [...state.pendingNotifications.values()];
    const pendingKinds = new Set(pending.map((item) => item.definition.kind));
    const items = [...criticalDefinitions.filter((definition) => !pendingKinds.has(definition.kind)).map((definition) => state.criticalItems.get(definition.kind)).filter(Boolean), ...pending];
    const itemKey = (item) => item.notificationId ?? item.definition.kind;
    const itemPriority = (item) => item.priority ?? 2;
    items.sort((left, right) => itemPriority(right) - itemPriority(left)
      || Number(itemKey(right) === state.criticalDominantKey) - Number(itemKey(left) === state.criticalDominantKey)
      || (left.timestamp ?? 0) - (right.timestamp ?? 0) || (left.notificationId ?? 0) - (right.notificationId ?? 0));
    state.criticalDominantKey = items.length ? itemKey(items[0]) : null;
    const active = items.filter((item) => item.status === "active");
    const unconfirmed = items.some((item) => item.status === "unconfirmed");
    const dominant = items[0];
    let title = dominant?.status === "unconfirmed" ? "Estado sin confirmar" : dominant?.definition.title || "Alerta resuelta";
    let description = active.length ? "El UPS requiere tu atención. Revisa las condiciones detectadas." : unconfirmed ? "No hay información suficiente para confirmar las condiciones detectadas. Revisa el estado y la conexión con el monitor." : "El UPS ya no reporta estas condiciones. El incidente permanece en el registro.";
    if (pending.length) {
      title = dominant?.status === "unconfirmed" ? "Estado sin confirmar" : dominant?.definition.title || "Avisos pendientes de Entendido";
      description = "El aviso se cierra automáticamente cuando se confirma la recuperación. El incidente se conserva en el historial. Entendido confirma su lectura sin cancelar la protección.";
    }
    if (!items.length) {
      const simulation = protection.simulation === true;
      if (countdown) {
        title = !state.serviceAvailable ? "Cuenta atrás sin confirmar" : simulation ? simulationPhase() === "ups_delay" ? "Simulación: espera para apagar el UPS" : "Cuenta atrás de prueba" : state.status?.policy?.action === "hibernate" ? "Hibernación programada" : "Apagado programado";
        description = `${protection.reason || "Se alcanzó un límite de protección."} ${simulation ? "Esta prueba no apagará ni hibernará tu PC." : "Guarda tu trabajo. Puedes cancelar esta acción antes de que termine la cuenta atrás."}${!state.serviceAvailable ? " El monitor no está disponible; la cuenta atrás puede seguir activa y su cancelación no está confirmada." : ""}`;
      } else {
        title = state.stopAcknowledged ? "Elipro detenido" : protection.state === "cancelled" ? "Cuenta atrás cancelada" : simulationPhase() === "complete" ? "Simulación finalizada" : protection.state === "executed" ? simulation ? "Simulación finalizada" : "Solicitud enviada a Windows" : protection.state === "blocked" ? "La protección requiere atención" : "Cuenta atrás finalizada";
        description = state.stopAcknowledged ? "Detuviste el monitor. La vigilancia se reanudará cuando vuelvas a iniciar Elipro." : simulationPhase() === "complete" ? "Windows y el UPS permanecen encendidos. La secuencia solo se ha representado." : protection.reason || "El monitor ya no reporta una cuenta atrás activa.";
      }
    }
    const dominantCountdown = countdown && !protection.simulation && !state.criticalCountdownAcknowledged
      && (!items.length || items[0].definition.kind === "policy_countdown" || itemPriority(items[0]) < 3);
    if (dominantCountdown) {
      const confirmed = state.serviceAvailable && !!state.countdownView
        && monotonicNow() < Math.min(state.countdownView.deadline, state.countdownView.freshUntil);
      title = !confirmed ? "Cuenta atrás sin confirmar" : state.status?.policy?.action === "hibernate" ? "Hibernación programada" : "Apagado programado";
      description = `${protection.reason || "Se alcanzó un límite de protección."} Guarda tu trabajo. ${confirmed ? "Puedes cancelar esta cuenta mientras siga vigente." : "La cuenta atrás puede seguir activa; su cancelación no está confirmada."}`;
    }
    $("critical-dialog").dataset.priority = String(dominantCountdown ? 3 : items.length ? itemPriority(items[0]) : 0);
    $("critical-urgency").hidden = !dominantCountdown;
    setText("critical-title", title);
    setText("critical-description", description);
    const list = $("critical-list");
    const signature = items.map((item) => `${item.notificationId ?? item.definition.kind}:${item.status}:${item.message}`).join("|");
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature;
      list.replaceChildren(...items.map((item) => {
        if (!item.element) {
          item.element = document.createElement("li");
          item.titleElement = document.createElement("strong");
          item.messageElement = document.createElement("p");
          item.stateElement = document.createElement("span");
          item.stateElement.className = "critical-condition-state";
          item.element.append(item.titleElement, item.messageElement, item.stateElement);
        }
        const row = item.element;
        row.dataset.kind = item.definition.kind;
        if (item.notificationId) row.dataset.notificationId = String(item.notificationId);
        row.dataset.state = item.status;
        item.titleElement.textContent = item.definition.title;
        item.messageElement.textContent = item.message;
        item.stateElement.textContent = `${{ active: "Activa", resolved: "Resuelta", unconfirmed: "Sin confirmar" }[item.status]}${item.notificationId ? " · Pendiente de Entendido" : ""}`;
        return row;
      }));
    }
    const dialog = $("critical-dialog");
    dialog.dataset.state = active.length || countdown && state.serviceAvailable ? "active" : unconfirmed || countdown ? "unconfirmed" : "resolved";
    if (!state.criticalOpen) {
      state.criticalFocusTarget = document.activeElement;
      state.criticalOpen = true;
      $("critical-feedback").hidden = true;
      dialog.showModal();
      $("critical-title").focus({ preventScroll: true });
    }
    renderCriticalCountdown();
  }

  function refreshTelemetry() {
    const context = telemetryContext();
    renderConnection(state.status?.connection || {}, context.sample, context);
    renderSample(context.sample);
    renderCriticalAlerts(context.sample, context.quality === "live");
  }

  function setText(id, value) {
    const element = $(id);
    if (element && element.textContent !== value) element.textContent = value;
  }

  function loadViewData() {
    if (!state.navigationReady || !state.serviceAvailable) return;
    if (state.activeView === "history" && !state.recordsLoaded) loadRecords();
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
    const sharedHistory = $("shared-history");
    const showHistory = selected === "overview" || selected === "history";
    sharedHistory.hidden = !showHistory;
    sharedHistory.inert = !showHistory;
    sharedHistory.setAttribute("aria-hidden", String(!showHistory));
    const recentEvents = $("recent-events-card");
    recentEvents.hidden = selected !== "overview";
    recentEvents.inert = selected !== "overview";
    document.querySelectorAll(".sidebar-nav [data-view]").forEach((button) => {
      const active = button.dataset.view === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
    document.body.dataset.view = selected;
    $("protection-master").hidden = selected !== "protection";
    $("protection-master").inert = selected !== "protection";
    setText("view-title", view.title);
    setText("view-description", view.description);
    $("view-description").hidden = !view.description;
    document.title = `${view.title} · Elipro UPS`;
    if (writeHash && window.location && window.location.hash !== `#${view.hash}`) window.location.hash = view.hash;
    if (showHistory) renderChart();
    hideTooltip();
    loadViewData();
    if (focus || selected !== previousView) window.scrollTo?.({ top: 0, left: 0, behavior: "instant" });
    if (focus) $("page-title")?.focus?.({ preventScroll: true });
  }

  function viewFromHash() {
    const hash = (window.location?.hash || "").slice(1).toLowerCase();
    if (hash === "avisos") return "system";
    if (hash === "incidentes") return "history";
    return Object.keys(views).find((name) => views[name].hash === hash) || "overview";
  }

  function setupNavigation() {
    const buttons = [...document.querySelectorAll(".sidebar-nav [data-view]")];
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
      window.history?.replaceState?.(null, "", `#${views[state.activeView].hash}`);
    }
    window.addEventListener?.("resize", () => {
      if (state.activeView === "history" || state.activeView === "overview") renderChart();
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
    const started = monotonicNow();
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
        const error = new Error(typeof detail === "string" ? detail : `El servicio local devolvió un error (${response.status}).`);
        if (typeof data.code === "string") error.code = data.code;
        throw error;
      }
      if (data && typeof data === "object") Object.defineProperty(data, "_requestElapsedMs", { value: Math.max(0, monotonicNow() - started), configurable: true });
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
    const transition = powerTransition();
    const physicalTest = upsPhysicalTestActive();
    $("save-policy").disabled = unavailable || busy || physicalTest;
    $("critical-cancel").disabled = !canCancelCountdown();
    $("critical-ack").disabled = busy;
    setText("critical-ack", state.mutation === "ack-notifications" ? "Confirmando…" : "Entendido");
    $("test-shutdown").disabled = unavailable || busy || enabled || state.policyDirty || !!transition || physicalTest;
    $("policy-form").querySelectorAll("input, select").forEach((field) => { field.disabled = busy || physicalTest; });
    $("policy-enabled").disabled = busy || physicalTest;
    const canConfigureUps = typeof state.status?.ups_poweroff?.can_configure === "boolean" ? state.status.ups_poweroff.can_configure : state.status?.ups_poweroff?.available === true;
    $("policy-ups-poweroff").disabled = unavailable || busy || physicalTest || (!canConfigureUps && !$("policy-ups-poweroff").checked);
    $("ups-power-review-ack").disabled = !upsPowerCanReview();
    $("ups-power-review-submit").disabled = !upsPowerCanReview() || !$("ups-power-review-ack").checked;
    const notificationSettingsMissing = !state.status?.notifications?.settings;
    $("notifications-form").querySelectorAll("input, select").forEach((field) => { field.disabled = unavailable || busy || notificationSettingsMissing; });
    $("notification-theme").disabled = unavailable || busy || notificationSettingsMissing
      || !["auto", "light", "dark"].includes(state.status?.notifications?.settings?.theme);
    $("notifications-persistent").disabled = unavailable || busy || Array.isArray(state.status?.notifications?.pending)
      || typeof state.status?.notifications?.settings?.persistent_critical !== "boolean";
    $("save-notifications").disabled = unavailable || busy || notificationSettingsMissing;
    const delivery = state.status?.notifications?.delivery;
    $("test-notifications").disabled = unavailable || busy || notificationSettingsMissing || delivery?.available === false || delivery?.ready === false;
    if (state.mutation !== "test-notifications") setText("test-notifications-label", state.status?.notifications?.settings?.sound_enabled === false ? "Probar aviso" : "Probar aviso y sonido");
    $("test-notifications").title = unavailable ? "El panel debe recuperar la conexión con el monitor."
      : delivery?.available === false ? "Los avisos de escritorio no están disponibles. Consulta el diagnóstico."
      : delivery?.ready === false ? "Espera a que el canal de avisos esté listo." : "Envía una prueba desde el monitor. No apaga ni hiberna Windows.";
    const webhookSettingsMissing = !state.status?.webhooks?.settings;
    $("webhooks-form").querySelectorAll("input").forEach((field) => { field.disabled = unavailable || busy || webhookSettingsMissing; });
    $("save-webhooks").disabled = unavailable || busy || webhookSettingsMissing;
    const webhookTestPending = ["queued", "sending", "retrying"].includes(state.status?.webhooks?.test?.state);
    $("test-webhook").disabled = unavailable || busy || webhookSettingsMissing || webhookTestPending
      || !$("webhook-enabled").checked || !$("webhook-url").value.trim();
    $("test-webhook").title = unavailable ? "El panel debe recuperar la conexión con el monitor."
      : webhookSettingsMissing ? "La configuración de webhooks no está disponible."
      : !$("webhook-enabled").checked ? "Activa y guarda los webhooks antes de probar."
      : !$("webhook-url").value.trim() ? "Configura y guarda una URL antes de probar."
      : webhookTestPending ? "Hay una prueba de webhook en curso."
      : state.webhooksDirty ? "Guarda los cambios antes de probar el webhook."
      : state.status?.webhooks?.dry_run ? "Simula la prueba sin enviar un POST (--dry-run)."
      : "Envía un evento webhook_test a la URL guardada.";
    if (state.mutation !== "test-webhook") setText("test-webhook-label", state.status?.webhooks?.test?.state === "retrying" ? "Esperando reintento…" : webhookTestPending ? "Enviando prueba…" : "Probar webhook");
    $("test-shutdown").title = unavailable ? "El panel debe recuperar la conexión con el monitor."
      : transition ? "Espera a que Windows reanude las lecturas antes de simular."
      : enabled ? "Desactiva y guarda la protección automática antes de probar el aviso."
      : state.policyDirty ? "Guarda la configuración para simular el tiempo que elegiste."
      : !simulationIncludesUps() ? "Simula la cuenta atrás de Windows. El apagado adicional del UPS está desactivado o no disponible."
      : "Simula la cuenta atrás guardada y la espera del UPS. No ejecuta acciones en Windows ni corta la energía.";
    setText("test-description", unavailable ? state.stopAcknowledged ? "Elipro está detenido." : "Sin comunicación con el monitor."
      : transition ? "Espera a que Windows reanude las lecturas antes de simular."
      : enabled ? "Desactiva y guarda la protección antes de probar."
      : state.policyDirty ? "Guarda los cambios antes de simular."
      : !simulationIncludesUps() ? `Simulación de ${finite(state.status?.policy?.countdown_seconds) ? state.status.policy.countdown_seconds : 15} s para Windows. Sin apagado adicional del UPS. No ejecuta acciones reales.`
      : `Simulación de ${finite(state.status?.policy?.countdown_seconds) ? state.status.policy.countdown_seconds : "—"} s guardados para Windows y ${simulationDelayDescription().toLocaleLowerCase("es")}. No apaga ni hiberna Windows ni corta la energía.`);
    for (const id of ["apply-records", "clear-records"]) $(id).disabled = unavailable;
    $("export-records").disabled = unavailable || state.downloadBusy;
    $("more-records").disabled = unavailable || state.recordsBusy;
    $("stop-monitor").disabled = unavailable || busy || physicalTest;
    $("confirm-stop-monitor").disabled = unavailable || busy || physicalTest;
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
    refreshTelemetry();
    // Losing this panel's API connection does not cancel the monitor's countdown.
    renderProtection(state.status?.protection || {});
    renderNotifications(state.status?.notifications);
    renderWebhooks(state.status?.webhooks);
    renderHistoryStatus();
    renderHealth(state.status?.health);
    renderUpsPoweroff();
    renderManualUpsTestNotices();
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

  function renderConnection(connection, sample, context = telemetryContext()) {
    const diagram = $("energy-diagram");
    diagram.dataset.mode = context.mode;
    diagram.dataset.quality = context.quality;
    const liveSource = context.quality === "live" && ["line", "battery"].includes(context.mode);
    const inputActive = liveSource && context.mode === "line" && finite(sample?.input_voltage) && sample.input_voltage > 0;
    const outputPowered = liveSource && finite(sample?.output_voltage) && sample.output_voltage > 0;
    diagram.dataset.inputActive = String(inputActive);
    diagram.dataset.outputPowered = String(outputPowered);
    const missingMains = context.quality === "live" && context.mode === "battery" && sample?.input_voltage === 0;
    $("input-supply-note").hidden = !missingMains;
    setText("input-supply-note", missingMains ? "Sin red" : "");
    const batterySupplying = context.mode === "battery" && outputPowered;
    $("battery-flow-note").hidden = !batterySupplying;
    setText("battery-flow-note", batterySupplying ? "En uso · " : "");
    const badge = $("connection-badge");
    const showConnection = (status, label) => {
      badge.className = `connection-indicator ups-power-indicator ${status}`;
      badge.lastElementChild.textContent = label;
      badge.title = label;
    };
    if (!state.serviceAvailable) {
      showConnection("disconnected", state.stopAcknowledged ? "Elipro detenido" : "Monitor sin conexión");
      $("connection-notice").hidden = true;
      setText("power-state", state.stopAcknowledged ? "Monitor detenido" : "Estado sin confirmar");
      setText("power-detail", state.stopAcknowledged ? "Inicia Elipro de nuevo para volver a vigilar el UPS y mostrar sus lecturas." : "El panel necesita comunicarse con el monitor para mostrar lecturas actuales.");
      updateFreshness();
      return;
    }
    const transition = ["suspended", "resuming"].includes(context.quality) ? context.quality : null;
    const status = transition || (context.quality === "live" ? "connected" : context.quality === "stale" || context.quality === "starting" ? "stale" : "disconnected");
    showConnection(status, { connected: "UPS conectado", stale: "Datos desactualizados", disconnected: "Sin conexión al UPS",
      suspended: "Lecturas en pausa por Windows", resuming: "Reanudando lectura del UPS" }[status]);
    const notice = $("connection-notice");
    notice.hidden = status === "connected";
    if (transition) {
      notice.textContent = transition === "suspended"
        ? "Lecturas en pausa por suspensión o hibernación de Windows. Elipro las retomará al reanudar."
        : "Windows se está reanudando. Elipro está recuperando la lectura del UPS; la protección automática espera datos recientes.";
    } else if (status !== "connected") {
      const prefix = status === "stale"
        ? "Las lecturas han dejado de actualizarse. La protección automática necesita datos recientes del UPS."
        : "No se reciben lecturas del UPS. Revisa el cable USB y la conexión del servicio de lectura.";
      notice.textContent = `${prefix}${connection.error ? ` ${String(connection.error)}` : ""}`;
    }
    if (!sample) {
      setText("power-state", transition === "suspended" ? "Lecturas en pausa" : transition === "resuming" ? "Reanudando lecturas"
        : context.quality === "starting" ? "Esperando al UPS" : status === "stale" ? "Lecturas en pausa" : "UPS sin conexión");
      setText("power-detail", transition === "suspended" ? "Elipro retomará la vigilancia cuando Windows se reanude y llegue una lectura válida."
        : transition === "resuming" ? "Esperando la primera lectura válida después de reanudar Windows."
        : "Esperando una lectura válida para mostrar el estado.");
    } else if (sample.fault) {
      setText("power-state", "El UPS reporta una falla");
      setText("power-detail", "Revisa el equipo y sus indicadores físicos.");
    } else if (sample.on_battery === true) {
      setText("power-state", "Funcionando con batería");
      setText("power-detail", outputPowered ? "El UPS está alimentando tus equipos con su batería."
        : "El UPS reporta batería, pero no se confirma tensión en la salida a tus equipos.");
    } else if (sample.on_battery === false) {
      setText("power-state", !inputActive ? sample.input_voltage === 0 ? "Entrada de red sin tensión" : "Entrada de red sin confirmar"
        : sample.raw_status === "AVR mode" ? "Red eléctrica · Regulación AVR" : "Red eléctrica activa");
      setText("power-detail", !inputActive ? "El UPS reporta modo de red, pero la tensión de entrada no está confirmada."
        : outputPowered ? "Tus equipos reciben energía de la red eléctrica." : "Hay tensión de entrada, pero no se confirma tensión en la salida a tus equipos.");
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
    const reported = percentage(data.battery_percent);
    const estimated = !reported && estimatedBattery(data);
    const battery = reported ? data.battery_percent : estimated ? data.battery_percent_estimate : null;
    setText("input-voltage", numeric(data.input_voltage, 1));
    setText("output-voltage", numeric(data.output_voltage, 1));
    setText("battery-percent", numeric(battery));
    setText("battery-unit", "%");
    $("battery-track").dataset.available = String(finite(battery));
    $("battery-note").hidden = false;
    setText("battery-note", reported ? "Carga reportada" : estimated ? "Carga estimada" : "No disponible");
    const protectionCharge = batteryCharge(data);
    const informationalEstimate = estimated && !protectionCharge;
    $("battery-note").title = estimated ? informationalEstimate
      ? `Estimación informativa · ${data.battery_estimate_method}. Este método no se usa en la protección por porcentaje.`
      : `Carga estimada · ${data.battery_estimate_method}. Se usa para la protección por porcentaje; el UPS también puede enviar su alarma de batería baja.` : "";
    setText("load-percent", numeric(data.load_percent));
    setText("frequency", numeric(data.frequency_hz, 1));
    setText("battery-voltage", numeric(data.battery_voltage, 2));
    setText("runtime", runtimeLabel(data.runtime_seconds));
    $("battery-runtime").hidden = !finite(data.runtime_seconds) || data.runtime_seconds <= 0;
    $("battery-bar").style.height = finite(battery) ? `${battery}%` : "0%";
    $("battery-bar").classList.toggle("low", data.low_battery === true || lowBatteryThreshold() > 0 && protectionCharge && protectionCharge.value <= lowBatteryThreshold());
    setText("policy-battery-source", finite(battery)
      ? `Actual: ${numeric(battery)} % ${estimated ? "estimado" : "reportado por el UPS"}${informationalEstimate ? ". Estimación informativa: este método no se usa en la protección por porcentaje." : ""}`
      : "Porcentaje de batería no disponible. La alarma de batería baja del UPS sigue atendida.");
    $("policy-battery-source").title = estimated ? "El UPS no mide directamente este porcentaje. También se atiende su alarma de batería baja."
      : "También se atiende la alarma de batería baja del UPS.";
    renderBatteryProtectionNote();
    $("load-bar").style.height = finite(data.load_percent) ? `${Math.max(0, Math.min(100, data.load_percent))}%` : "0%";
    $("load-bar").classList.toggle("high", finite(data.load_percent) && data.load_percent >= 80);
    const meter = $("load-meter");
    meter.dataset.available = String(finite(data.load_percent));
    if (finite(data.load_percent)) meter.setAttribute("aria-valuenow", String(Math.max(0, Math.min(100, data.load_percent))));
    else meter.removeAttribute("aria-valuenow");
    meter.setAttribute("aria-valuetext", finite(data.load_percent) ? `${numeric(data.load_percent)} % de carga del UPS${data.load_percent >= 100 ? "; capacidad al límite o sobrecarga" : ""}` : "Carga del UPS no disponible");
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
    if (/^policy_|^ups_poweroff_|^ups_battery_test|^ups_cancel_test|^windows_action_|^simulation$/.test(kind)) return "protection";
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
      ups_poweroff_submitted: "Corte del UPS solicitado",
      ups_poweroff_unconfirmed: "Corte del UPS sin confirmar",
      ups_poweroff_cancel_requested: "Cancelación del corte del UPS solicitada",
      ups_poweroff_blocked: "Corte del UPS bloqueado",
      ups_poweroff_reviewed: "Revisión del corte del UPS cerrada",
      ups_battery_test_reviewed: "Observación física del UPS registrada",
      ups_battery_test_windows_unconfirmed: "Solicitud de hibernación sin confirmar",
      windows_action_unconfirmed: "Solicitud a Windows sin confirmar",
      simulation: "Simulación sin acción real",
    }[kind] || "Registro del monitor";
  }

  function eventMessage(event) {
    const message = String(event.message || "");
    if (event.kind === "ups_battery_test_reviewed") {
      const result = event.data?.result;
      if (result === "automatic_return_observed") return "Se observó que las salidas del UPS volvieron solas al regresar la electricidad. Este comportamiento está aceptado; quedan otras condiciones de habilitación por comprobar.";
      if (result === "battery_cut_manual_restart_observed") return "Observación física favorable registrada; el perfil automático del UPS sigue sin validar.";
      if (result === "physical_test_incomplete") return "Prueba física incompleta registrada; el apagado automático del UPS permanece bloqueado.";
    }
    if (event.kind === "policy_unknown" && (!message.trim() || message.trim() === "unknown")) return eventLabel(event.kind);
    return message || eventLabel(event.kind);
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
      const persistentKinds = new Set([...state.pendingNotifications.values()].map((item) => item.definition.kind));
      const relevant = fresh.filter((event) => !criticalKinds.has(event.kind) && !persistentKinds.has(event.kind) && /power|connection|policy_countdown|policy_unknown|policy_action_failed/.test(String(event.kind || "")));
      if (relevant.length) {
        const newest = relevant[0];
        const severity = String(newest.severity || "");
        const prefix = relevant.length > 1 ? `${relevant.length} eventos nuevos. ` : "";
        toast(`${prefix}${eventMessage(newest)}`, /error|critical|danger/.test(severity), /warning/.test(severity));
      }
    }
    // Notifications still use the raw event stream. Only this summary uses the
    // complete, server-grouped records so a truncated event window cannot invent
    // a start, recovery or duration for an incident.
    const error = typeof state.status?.recent_records_error === "string" ? state.status.recent_records_error : "";
    const grouped = Array.isArray(state.status?.recent_records);
    const records = (grouped ? state.status.recent_records : error ? [] : sorted)
      .filter(record => record && typeof record === "object")
      .slice().sort((a, b) => (parseDate(b.ended_at || b.timestamp || b.started_at)?.getTime() || 0)
        - (parseDate(a.ended_at || a.timestamp || a.started_at)?.getTime() || 0));
    const filtered = state.eventFilter === "all" ? records : records.filter(record => {
      if (Array.isArray(record.categories) && record.categories.includes(state.eventFilter)) return true;
      const category = ["power", "connection", "protection"].includes(record.category)
        ? record.category : eventCategory({ kind: record.kind || record.type });
      return category === state.eventFilter;
    });
    sectionError("recent-events-error", error);
    setText("recent-events-caption", filtered.length && filtered.every(record => record.record_type === "incident")
      ? "Últimos incidentes" : "Actividad reciente");
    const signature = JSON.stringify([state.eventFilter, error, filtered.map(record => ({
      id: record.id, record_type: record.record_type, type: record.type, kind: record.kind,
      category: record.category, timestamp: record.timestamp, severity: record.severity,
      message: eventMessage({ ...record, kind: record.kind || record.type, message: record.description || record.message }),
      status: record.status, start_known: record.start_known, started_at: record.started_at, ended_at: record.ended_at,
      duration: record.status === "recovered" ? recentDuration(record) : null,
    }))]);
    if (list.dataset.signature === signature) return;
    list.dataset.signature = signature;
    if (!filtered.length) {
      list.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "events-empty";
      const icon = document.createElement("span");
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = error ? "!" : "✓";
      const title = document.createElement("strong");
      title.textContent = error ? "No se pudo cargar la actividad" : records.length ? "Sin eventos de este tipo" : "Sin eventos registrados";
      const description = document.createElement("p");
      description.textContent = error ? "El resumen se volverá a consultar automáticamente."
        : records.length ? "Selecciona Todos para consultar el resto de los registros recientes."
        : "Los cortes, retornos de energía y cambios de conexión aparecerán aquí.";
      empty.append(icon, title, description);
      list.append(empty);
      return;
    }
    list.replaceChildren();
    for (const record of filtered) {
      if (record.record_type === "incident") {
        list.append(renderRecentIncident(record));
        continue;
      }
      const event = { ...record, kind: record.kind || record.type, message: record.description || record.message };
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
      message.textContent = eventMessage(event);
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

  function recentDuration(record) {
    const start = parseDate(record.started_at), end = parseDate(record.ended_at);
    if (record.status !== "recovered" || record.start_known === false || !start || !end || end < start
        || !finite(record.duration_seconds) || record.duration_seconds < 0) return null;
    const seconds = Math.floor(record.duration_seconds);
    return seconds < 3600 ? runtimeLabel(seconds)
      : `${Math.floor(seconds / 3600)} h ${String(Math.floor(seconds % 3600 / 60)).padStart(2, "0")} min`;
  }

  function renderRecentIncident(record) {
    const power = record.type === "power";
    const recovered = record.status === "recovered" && !!parseDate(record.ended_at);
    const item = document.createElement("article");
    item.className = "event recent-incident";
    const symbol = svgElement("svg", { viewBox: "0 0 24 24", class: "event-symbol", "aria-hidden": "true" });
    symbol.append(svgElement("path", { d: power ? "m13 2-9 12h7l-1 8 10-13h-7l1-7Z"
      : "M10 13a5 5 0 0 0 7 .1l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7-.1l-3 3a5 5 0 0 0 7 7l2-2" }));
    const body = document.createElement("div");
    body.className = "event-body";
    const title = document.createElement("h3");
    title.textContent = power ? "Corte eléctrico" : "Interrupción de comunicación";
    const status = document.createElement("p");
    status.className = `incident-status ${recovered ? "recovered" : "open"}`;
    const statusIcon = svgElement("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" });
    statusIcon.append(svgElement("circle", { cx: 12, cy: 12, r: 9 }));
    statusIcon.append(svgElement("path", { d: recovered ? "m8 12 3 3 5-6" : "M12 7v6m0 4h.01" }));
    status.append(statusIcon, document.createTextNode(recovered ? "Restablecida" : "Sin cierre registrado"));
    if (!recovered) status.title = "El registro no confirma cuándo terminó. Consulta el estado actual del UPS.";
    const duration = document.createElement("p");
    duration.className = "incident-duration";
    const durationText = recentDuration(record);
    if (durationText !== null) {
      const value = document.createElement("strong"), description = document.createElement("span");
      value.textContent = durationText;
      description.textContent = power ? "sin red eléctrica" : "sin lecturas";
      duration.append(value, document.createTextNode(" "), description);
    } else {
      const unknown = document.createElement("span");
      unknown.textContent = "Duración sin confirmar";
      duration.append(unknown);
    }
    const times = document.createElement("dl");
    times.className = "incident-times";
    for (const [label, value, missing] of [
      ["Inicio", record.start_known === false ? null : record.started_at, "Inicio no registrado"],
      ["Recuperación", recovered ? record.ended_at : null, "No registrada"],
    ]) {
      const row = document.createElement("div"), term = document.createElement("dt"), detail = document.createElement("dd");
      term.textContent = label;
      const date = parseDate(value);
      if (date) {
        const time = document.createElement("time");
        time.dateTime = date.toISOString();
        time.textContent = dateFormat.format(date);
        time.title = recordDateFormat.format(date);
        detail.append(time);
      } else detail.textContent = missing;
      row.append(term, detail);
      times.append(row);
    }
    body.append(title, status, duration, times);
    item.append(symbol, body);
    return item;
  }

  function renderPolicy(policy) {
    const native = state.status?.meta?.source === "native";
    $("policy-battery-field").hidden = false;
    $("policy-runtime-field").hidden = native;
    $("policy-fields").classList.toggle("native-policy", native);
    const enabled = !!policy.enabled;
    const blocked = state.status?.meta?.dry_run === true || state.status?.meta?.simulation_source === true;
    renderBatteryProtectionNote();
    renderUpsPoweroff();
    updateMutationControls();
    if (state.policyInitialized && state.policyDirty) return;
    $("policy-enabled").checked = enabled;
    $("policy-low-battery").value = finite(policy.low_battery_percent) ? policy.low_battery_percent : 20;
    $("policy-runtime").value = finite(policy.min_runtime_seconds) ? policy.min_runtime_seconds : 180;
    $("policy-countdown").value = finite(policy.countdown_seconds) ? policy.countdown_seconds : 15;
    $("policy-ups-delay").value = configuredUpsDelay(policy) / 60;
    $("policy-action").value = policy.action === "shutdown" ? "shutdown" : "hibernate";
    $("policy-ups-poweroff").checked = policy.ups_poweroff === true;
    setText("enabled-label", enabled ? blocked ? "Modo de prueba" : "Activada" : "Desactivada");
    state.policyInitialized = true;
    updateMutationControls();
  }

  function renderUpsPoweroff() {
    const capability = state.status?.ups_poweroff;
    const automaticRestartObserved = capability?.restart_behavior === "automatic_on_mains" && capability?.restart_behavior_observed === true;
    const observedSequence = capability?.reserve_mode === "observed_sequence";
    const reason = upsReason(capability?.reason);
    const fallback = !capability ? "Este monitor no informa si puede apagar el UPS."
      : observedSequence && capability.can_configure === true ? "Disponible para configurar; se comprobarán las condiciones antes de actuar."
      : capability.available === true ? "Disponible para esta configuración."
      : capability.validation === "incompatible" ? "Este equipo no es compatible con el apagado del UPS."
      : capability.validation === "pending" ? "Pendiente de validación física. Esta opción todavía no puede activarse."
      : "El apagado del UPS no está disponible en esta configuración.";
    setText("ups-power-capability", !state.serviceAvailable ? "Sin comunicación con el monitor. La capacidad del UPS no está confirmada." : reason || fallback);
    $("ups-power-capability").hidden = state.serviceAvailable && capability?.available === true
      && capability?.sequence_admission?.available !== false;
    $("ups-power-capability").classList.toggle("ups-power-unavailable", (capability?.can_configure ?? capability?.available) !== true || !state.serviceAvailable);
    setText("ups-power-restart", automaticRestartObserved ? "El UPS enciende sus salidas automáticamente cuando vuelve la luz."
      : capability?.manual_restart_verified === true && capability?.manual_restart === true ? "En esta unidad se verificó el encendido manual de las salidas."
      : "El comportamiento de las salidas al regresar la electricidad todavía no está confirmado para esta unidad.");
    setText("ups-power-restart-summary", automaticRestartObserved ? "Retorno automático al volver la luz."
      : capability?.manual_restart_verified === true && capability?.manual_restart === true ? "Retorno manual de las salidas."
      : "Retorno de las salidas pendiente de confirmar.");
    const policy = protectionDraft();
    setText("ups-power-enabled-label", policy.ups_poweroff === true ? "Activado" : "Desactivado");
    setText("ups-power-delay", `De 1 a 9 min, desde la solicitud de ${policy.action === "shutdown" ? "apagado" : "hibernación"}.`);
    const admissionReason = capability?.sequence_admission?.available === false ? upsReason(capability.sequence_admission.reason) : "";
    $("ups-power-sequence-warning").hidden = !admissionReason || admissionReason === reason;
    setText("ups-power-sequence-warning", admissionReason && admissionReason !== reason ? admissionReason : "");
    renderUpsPowerConditions(capability);
    renderProtectionPreview();
    const states = {
      blocked: observedSequence && capability.can_configure === true ? "La opción del UPS puede guardarse. Antes de programarlo se comprobarán las condiciones recientes." : "El apagado del UPS está bloqueado; se conserva la protección de Windows.",
      pending: "Hay una solicitud de apagado del UPS pendiente. La finalización de Windows no está confirmada.",
      submitted: "Comando enviado al UPS. El corte físico no está confirmado.",
      unconfirmed: "No se pudo confirmar el apagado del UPS.",
      cancelled: "Apagado del UPS cancelado.",
    };
    $("ups-power-state").hidden = !states[capability?.state] || capability?.state === "blocked" && !!reason;
    setText("ups-power-state", states[capability?.state] || "");
    const operation = capability?.operation;
    const canDisplayReview = state.serviceAvailable && capability?.can_review === true
      && ["review_required", "submitted", "armed", "uncertain", "cancel_submitted"].includes(operation?.state)
      && typeof operation?.operation_id === "string" && operation.operation_id.length > 0;
    $("ups-power-review").hidden = !canDisplayReview;
    if (!canDisplayReview || state.upsPowerReviewOperation !== operation.operation_id || !upsPowerCanReview()) $("ups-power-review-ack").checked = false;
    state.upsPowerReviewOperation = canDisplayReview ? operation.operation_id : null;
    $("ups-power-review-ack").disabled = !upsPowerCanReview();
    $("ups-power-review-submit").disabled = !upsPowerCanReview() || !$("ups-power-review-ack").checked;
  }

  function upsPowerCanReview() {
    const capability = state.status?.ups_poweroff;
    const operation = capability?.operation;
    const telemetry = telemetryContext();
    return state.serviceAvailable && !!state.token && !state.mutation && !state.upsPowerReviewNeedsStatus
      && !upsPhysicalTestActive() && !powerTransition() && capability?.can_review === true
      && ["review_required", "submitted", "armed", "uncertain", "cancel_submitted"].includes(operation?.state)
      && typeof operation?.operation_id === "string" && operation.operation_id.length > 0
      && telemetry.quality === "live" && telemetry.sample?.on_battery === false
      && telemetry.sample.low_battery === false && telemetry.sample.fault === false
      && finite(telemetry.sample.output_voltage) && telemetry.sample.output_voltage > 0;
  }

  function upsReason(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function renderUpsPowerConditions(capability) {
    const admission = capability?.sequence_admission;
    const available = state.serviceAvailable && capability?.available === true && admission?.available !== false;
    const confirmed = state.serviceAvailable && typeof capability?.available === "boolean";
    const status = $("ups-power-availability");
    status.dataset.state = !confirmed ? "unknown" : available ? "available" : "blocked";
    status.textContent = !confirmed ? "Sin confirmar" : available ? "Disponible ahora" : "Bloqueado ahora";
    setText("ups-power-conditions-note", "Solo estas cuatro condiciones pueden bloquear la orden. El voltaje y la carga no bloquean el apagado; el porcentaje bajo de batería adelanta la protección.");
    const categories = [
      { key: "connection", label: "UPS conectado y compatible", description: "Comprueba que Elipro pueda comunicarse con el UPS por USB y apagarlo.", checks: ["native_source", "source", "identity", "usb_identity", "profile", "connection", "communication", "monitor_running", "operational_profile", "sample_fresh", "qs_status", "power_state"], action: "Comprueba la conexión USB y el estado del monitor." },
      { key: "windows", label: protectionDraft().action === "shutdown" ? "Windows preparado para apagar" : "Windows preparado para hibernar", description: protectionDraft().action === "shutdown" ? "Comprueba que Elipro pueda apagar este equipo automáticamente." : "Comprueba que Elipro pueda hibernar este equipo automáticamente.", checks: ["real_actions", "windows", "windows_action", "windows_ready"], action: "Revisa la acción de Windows y los permisos del servicio." },
      { key: "operation", label: "Sin pruebas ni apagados pendientes", description: "Si hay una prueba o un apagado pendiente, primero debe terminar o revisarse.", checks: ["manual_tests", "operation", "previous_operation", "battery_test", "shutdown_pending"], action: "Espera a que termine la operación o revisa su estado antes de iniciar otra." },
      { key: "controlled_shutdown", label: "Estado de la batería y del UPS", description: "Si el UPS avisa de batería crítica o una falla, podría apagarse antes del tiempo elegido.", checks: ["low_battery", "fault"], action: "Revisa la alarma indicada por el UPS. Se mantiene la protección de Windows." },
    ];
    const checks = Array.isArray(capability?.checks) ? capability.checks : Array.isArray(admission?.checks) ? admission.checks : [];
    const blockers = Array.isArray(capability?.blockers) ? capability.blockers : [];
    const list = $("ups-power-checks");
    const rows = categories.map((category) => {
      const blocker = blockers.find((item) => item?.key === category.key);
      const legacyFailures = checks.filter((check) => check && category.checks.includes(check.key || check.code)
        && check.passed === false && check.blocking !== false && check.severity !== "info");
      const blocked = blocker ? blocker.blocked === true : legacyFailures.length > 0;
      const confirmedCheck = state.serviceAvailable && (typeof blocker?.blocked === "boolean" || available || blocked);
      const row = document.createElement("li");
      row.dataset.state = !confirmedCheck ? "unknown" : blocked ? "blocked" : "passed";
      row.dataset.check = category.key;
      const label = document.createElement("span");
      label.textContent = category.label;
      const result = document.createElement("strong");
      result.textContent = !confirmedCheck ? "Sin confirmar" : blocked ? "Bloqueado" : "Listo";
      row.append(label, result);
      const description = document.createElement("small");
      description.className = "ups-power-check-description";
      description.textContent = category.description;
      row.append(description);
      const reasons = blocker ? (Array.isArray(blocker.reasons) ? blocker.reasons : []) : legacyFailures.map((check) => check.reason);
      const details = [...new Set(reasons.map(upsReason).filter(Boolean))];
      const summary = upsReason(blocker?.reason);
      if (summary && !details.includes(summary) && summary !== details.join(" ")) details.push(summary);
      const reason = details.join(" ");
      if (confirmedCheck && blocked && reason) {
        const detail = document.createElement("small");
        detail.textContent = reason;
        row.append(detail);
        const action = document.createElement("small");
        action.className = "ups-power-resolution";
        action.textContent = category.action;
        row.append(action);
      }
      return row;
    });
    list.replaceChildren(...rows);
    list.hidden = false;
  }

  function upsCancelTestActive() {
    return ["arming", "submitted", "armed", "cancelling", "cancel_submitted", "cancelled", "observing", "uncertain", "review_required"].includes(state.status?.ups_cancel_test?.state);
  }

  function upsBatteryTestActive() {
    return ["arming", "submitted", "uncertain", "windows_requested", "windows_failed", "cancelling", "cancel_submitted", "review_required"].includes(state.status?.ups_battery_test?.state);
  }

  function upsPhysicalTestActive() {
    return upsCancelTestActive() || upsBatteryTestActive();
  }

  function renderManualUpsTestNotices() {
    for (const [diagnostic, active, prefix] of [
      [state.status?.ups_cancel_test, upsCancelTestActive(), "ups-cancel-test"],
      [state.status?.ups_battery_test, upsBatteryTestActive(), "ups-battery-test"],
    ]) {
      $(prefix + "-urgent").hidden = !active;
      if (!active) { setText(prefix + "-urgent-message", ""); continue; }
      const review = diagnostic?.state === "review_required";
      const rejected = diagnostic?.windows_request === "failed" || diagnostic?.state === "windows_failed";
      const unconfirmed = diagnostic?.windows_request === "unconfirmed";
      const pendingBitMissing = diagnostic?.result === "pending_bit_not_reported" || diagnostic?.reason_code === "pending_bit_not_reported";
      const reportedMessage = typeof diagnostic?.message === "string" ? diagnostic.message.trim() : "";
      let message = review ? "Una prueba manual anterior del UPS requiere revisión desde el diagnóstico. No se repetirán órdenes automáticamente."
        : rejected ? "Windows rechazó la hibernación. El corte del UPS puede seguir pendiente y cualquier cancelación intentada no está confirmada."
        : unconfirmed ? "La solicitud de hibernación quedó sin confirmar; esto no demuestra que Windows haya terminado. El corte del UPS puede seguir pendiente."
        : pendingBitMissing && reportedMessage ? reportedMessage
        : "Hay una prueba manual del UPS pendiente. El resultado del corte o de su cancelación no está confirmado. No repitas órdenes; la continuación requiere el diagnóstico.";
      const remaining = finite(diagnostic?.remaining) ? Math.max(0, Math.ceil(diagnostic.remaining)) : null;
      if (remaining !== null && !review) message += ` Tiempo calculado desde el envío: ${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}.`;
      if (!state.serviceAvailable) message += " Sin comunicación con Elipro: este es el último estado recibido; la operación no se considera cancelada.";
      setText(prefix + "-urgent-message", message);
    }
  }

  function simulationPhase() {
    if (state.status?.protection?.simulation !== true) return null;
    return state.status.protection.phase || state.status?.ups_poweroff?.simulation?.phase || null;
  }

  function configuredUpsDelay(policy = state.status?.policy) {
    return finite(policy?.ups_poweroff_delay_seconds) ? policy.ups_poweroff_delay_seconds : 180;
  }

  function protectionDraft() {
    const policy = state.status?.policy || {};
    if (!state.policyDirty) return policy;
    return { ...policy, action: $("policy-action").value, countdown_seconds: Number($("policy-countdown").value), low_battery_percent: Number($("policy-low-battery").value),
      ups_poweroff: $("policy-ups-poweroff").checked,
      ups_poweroff_delay_seconds: Number($("policy-ups-delay").value) * 60 };
  }

  function simulationIncludesUps() {
    const capability = state.status?.ups_poweroff;
    const canConfigure = typeof capability?.can_configure === "boolean" ? capability.can_configure : capability?.available === true;
    return state.status?.policy?.ups_poweroff === true && canConfigure;
  }

  function simulationDelayDescription() {
    const simulated = state.status?.ups_poweroff?.simulation;
    const delay = finite(simulated?.delay_seconds) ? simulated.delay_seconds : configuredUpsDelay();
    return `Retardo simulado del UPS de ${numeric(delay)} s desde el envío`;
  }

  function renderProtectionPreview() {
    const policy = protectionDraft();
    const countdown = finite(policy.countdown_seconds) ? policy.countdown_seconds : 15;
    const delay = configuredUpsDelay(policy);
    const hibernate = policy.action !== "shutdown";
    renderBatteryProtectionNote();
    setText("protection-preview-countdown", "Corte detectado");
    setText("protection-preview-wait", "Aviso inmediato.");
    setText("protection-preview-action", `A los ${numeric(countdown)} s`);
    setText("protection-preview-action-detail", hibernate ? "Se solicita hibernar Windows" : "Se solicita apagar Windows");
    setText("policy-countdown-label", hibernate ? "Tiempo antes de hibernar" : "Tiempo antes de apagar");
    setText("policy-countdown-note", `Puedes cancelar durante estos ${numeric(countdown)} segundos.`);
    setText("protection-preview-ups", policy.ups_poweroff === true ? `+${runtimeLabel(delay)}` : "El UPS permanece encendido");
    const capability = state.status?.ups_poweroff;
    const currentlyBlocked = state.serviceAvailable && (capability?.available === false || capability?.sequence_admission?.available === false);
    setText("protection-preview-ups-detail", policy.ups_poweroff !== true ? "No se programa un corte adicional."
      : currentlyBlocked ? "Ahora está bloqueado. Revisa las condiciones del UPS."
      : "Corte programado del UPS.");
  }

  function renderBatteryProtectionNote() {
    const policy = protectionDraft();
    const informational = estimatedBattery(state.status?.sample) && !batteryCharge(state.status?.sample);
    setText("battery-protection-subtitle", policy.action === "shutdown" ? "Adelantar el apagado de Windows" : "Adelantar la hibernación");
    setText("policy-battery-help", policy.low_battery_percent === 0
      ? "La prioridad por porcentaje está desactivada. La alarma crítica del UPS mantiene la protección inmediata de Windows."
      : informational ? "La prioridad por porcentaje requiere una medición reportada o una estimación validada. La estimación actual es solo informativa. 0 desactiva la prioridad por porcentaje."
        : "Si estás usando batería y se alcanza el umbral, la espera restante se reduce a 10 s como máximo. El retardo del UPS no cambia.");
    const emergency = state.status?.protection?.emergency;
    $("policy-battery-emergency").hidden = emergency?.active !== true;
    setText("policy-battery-emergency", emergency?.active !== true ? ""
      : emergency.source === "ups_critical_alarm"
        ? "El UPS reporta batería crítica: Windows tiene prioridad inmediata. No se programa el corte del UPS porque esa orden podría cortar la energía antes de completar la hibernación."
        : `Protección prioritaria por carga ${emergency.source === "reported_percentage" ? "reportada" : "estimada"}: ${numeric(emergency.percent)} %, umbral de ${numeric(emergency.threshold_percent)} %. La cuenta de Windows se limita a ${numeric(emergency.seconds_limit || 10)} s; se conserva el retardo configurado del UPS.`);
  }

  function renderProtection(protection) {
    const alert = $("protection-alert");
    if (state.stopAcknowledged && protection.state === "countdown") {
      alert.hidden = true;
      setText("countdown", "—");
      return;
    }
    const visible = protection.state === "countdown";
    alert.hidden = !visible;
    if (!visible) return;
    const simulation = !!protection.simulation;
    const action = state.status?.policy?.action === "hibernate" ? "hibernación" : "apagado";
    const phase = simulationPhase();
    setText("protection-title", protection.state === "blocked" ? "La protección requiere atención" : phase === "complete" ? "Simulación finalizada" : protection.state === "executed" ? simulation ? "Simulación finalizada" : "Solicitud enviada a Windows" : simulation ? phase === "ups_delay" ? "Simulación: espera para apagar el UPS" : "Prueba del aviso · sin acción real" : `Cuenta atrás para ${action}`);
    const reason = phase === "ups_delay" ? `La solicitud a Windows solo se representa. ${simulationDelayDescription()}. No se confirma que Windows haya terminado ni se corta la energía.`
      : phase === "complete" ? "No se ejecutaron acciones en Windows ni se cortó la energía del UPS."
      : `${protection.reason || (simulation ? "Esta prueba no apagará ni hibernará tu PC." : "Se alcanzó un límite de protección.")}${simulation && protection.reason ? " No se apagará ni hibernará tu PC." : ""}${protection.state === "executed" && !simulation ? " La solicitud no confirma que Windows haya terminado." : ""}`;
    const unconfirmed = !state.serviceAvailable ? ` Estado sin confirmar desde ${state.lastStatusAt ? clockFormat.format(state.lastStatusAt) : "la pérdida de comunicación"}. ${protection.state === "countdown" ? "La cuenta atrás puede seguir activa; su cancelación no está confirmada." : "No se ha podido consultar su estado actual."}` : "";
    setText("protection-reason", `${reason}${unconfirmed}`);
    updateCountdown();
  }

  function renderNotifications(notifications) {
    const settings = notifications?.settings;
    const delivery = notifications?.delivery || {};
    const tag = $("notifications-state");
    const error = delivery.last_error || notifications?.last_error;
    const hasInbox = Array.isArray(notifications?.pending);
    $("notifications-persistent-option").hidden = hasInbox;
    $("notifications-inbox-note").hidden = !hasInbox;
    const ready = state.serviceAvailable && settings && delivery.available !== false && delivery.ready === true;
    setText("notifications-state-label", !state.serviceAvailable ? state.stopAcknowledged ? "Detenidos" : "Sin conexión" : !settings || delivery.available === false ? "No disponible" : ready ? "Listos" : "Iniciando");
    tag.classList.toggle("good", !!ready);
    tag.classList.toggle("warning", !ready);
    const lastDelivered = parseDate(delivery.last_delivered_at);
    const lastPopup = parseDate(delivery.last_popup_shown_at);
    $("notifications-delivery").hidden = !!ready && settings.enabled !== false && !lastPopup && !lastDelivered;
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
    $("notification-theme").value = ["auto", "light", "dark"].includes(settings.theme) ? settings.theme : "auto";
    const supportsPersistent = typeof settings.persistent_critical === "boolean";
    setText("notifications-persistent-note", supportsPersistent ? "Hasta cerrarlos o resolverse la alerta." : "No disponible en esta versión del monitor.");
    $("notifications-persistent").checked = settings.persistent_critical === true;
    state.notificationsInitialized = true;
  }

  function webhookDraft() {
    return {
      webhook_enabled: $("webhook-enabled").checked,
      webhook_url: $("webhook-url").value.trim(),
      webhook_events: webhookEventIds.filter((id) => $("webhook-event-" + id).checked),
    };
  }

  function webhookSettingsMatch(draft, saved) {
    return !!saved && draft.webhook_enabled === saved.webhook_enabled
      && draft.webhook_url === saved.webhook_url
      && webhookEventIds.every((id) => draft.webhook_events.includes(id) === (saved.webhook_events || []).includes(id));
  }

  function renderWebhookSelection() {
    const count = webhookEventIds.filter((id) => $("webhook-event-" + id).checked).length;
    setText("webhook-event-count", `${count} de 5 seleccionados`);
  }

  function renderWebhooks(webhooks) {
    const settings = webhooks?.settings;
    $("webhooks-dry-run").hidden = webhooks?.dry_run !== true;
    if (settings && !(state.webhooksInitialized && state.webhooksDirty)) {
      $("webhook-enabled").checked = settings.webhook_enabled === true;
      $("webhook-url").value = typeof settings.webhook_url === "string" ? settings.webhook_url : "";
      const events = Array.isArray(settings.webhook_events) ? settings.webhook_events : ["power_lost", "battery_low"];
      webhookEventIds.forEach((id) => { $("webhook-event-" + id).checked = events.includes(id); });
      state.webhooksInitialized = true;
    }
    renderWebhookSelection();
    const test = webhooks?.test;
    if (test && test.id != null) {
      const signature = JSON.stringify([test.id, test.state, test.message, test.http_status, test.attempts, test.next_retry_at]);
      if (signature !== state.webhookTestSignature) {
        state.webhookTestSignature = signature;
        const feedback = $("webhooks-test-feedback");
        const fallback = {
          queued: "Prueba en cola. Esperando el envío…",
          sending: "Enviando prueba al webhook…",
          retrying: "El primer intento falló. Reintento en 5 s (2/2).",
          success: "Prueba enviada correctamente.",
          error: "No se pudo enviar la prueba del webhook.",
          simulated: "Prueba simulada (--dry-run). No se envió ningún webhook.",
          cancelled: "La prueba se canceló al cambiar la configuración.",
        };
        // A queued request is not a successful delivery; the monitor confirms it in later polls.
        const message = test.state === "simulated" ? fallback.simulated : test.state === "retrying" ? fallback.retrying : test.message || fallback[test.state] || "Estado de prueba no disponible.";
        setText("webhooks-test-feedback", test.state === "success" && Number.isInteger(test.http_status) ? `${message} HTTP ${test.http_status}.` : message);
        feedback.hidden = false;
        feedback.classList.toggle("error", test.state === "error");
        feedback.classList.toggle("simulated", test.state === "simulated" || test.state === "retrying");
      }
    }
    renderWebhookDeliveries(webhooks?.deliveries);
    updateMutationControls();
  }

  function renderWebhookDeliveries(deliveries) {
    const events = {
      power_lost: "Corte de energía", power_restored: "Suministro recuperado", battery_low: "Batería baja",
      overload: "Sobrecarga", ups_fault: "Falla del UPS", webhook_test: "Prueba de webhook",
    };
    const labels = { queued: "En cola", sending: "Enviando", success: "Entregado", error: "Fallido", cancelled: "Cancelado", simulated: "Simulado" };
    const rows = (Array.isArray(deliveries) ? deliveries : []).filter((delivery) => delivery && typeof delivery === "object")
      .slice().sort((left, right) => (parseDate(right.timestamp)?.getTime() || 0) - (parseDate(left.timestamp)?.getTime() || 0)).slice(0, 50)
      .map((delivery) => {
        const knownState = delivery.state === "retrying" || Object.hasOwn(labels, delivery.state);
        const kind = knownState ? delivery.state : "unknown";
        const retryAt = parseDate(delivery.next_retry_at);
        const seconds = retryAt ? Math.max(0, Math.ceil((retryAt.getTime() - Date.now()) / 1000)) : 5;
        const status = kind === "retrying" ? seconds > 0 ? `Reintento en ${seconds} s` : "Reintento pendiente" : labels[kind] || "No disponible";
        const attempts = Number.isInteger(delivery.attempts) ? Math.max(0, Math.min(2, delivery.attempts)) : 0;
        const message = typeof delivery.message === "string" ? delivery.message : "";
        const http = Number.isInteger(delivery.http_status) ? `HTTP ${delivery.http_status}` : "";
        const result = message && http && !message.includes(http) ? `${message} (${http})` : message || http || "—";
        return { id: delivery.id, timestamp: delivery.timestamp, event: String(delivery.event || "—"), kind, status, attempts, result };
      });
    const signature = JSON.stringify(rows);
    if (signature === state.webhookDeliveriesSignature) return;
    state.webhookDeliveriesSignature = signature;
    setText("webhook-deliveries-count", `${rows.length} ${rows.length === 1 ? "entrega" : "entregas"}`);
    $("webhook-deliveries-empty").hidden = rows.length !== 0;
    $("webhook-deliveries-scroll").hidden = rows.length === 0;
    const body = $("webhook-deliveries-body");
    const nodes = rows.map((delivery) => {
      const row = document.createElement("tr");
      row.dataset.deliveryId = String(delivery.id || "");
      const cell = (value) => {
        const node = document.createElement("td");
        node.textContent = value;
        row.append(node);
        return node;
      };
      cell(formatMoment(delivery.timestamp));
      const event = cell("");
      const name = document.createElement("strong");
      name.textContent = Object.hasOwn(events, delivery.event) ? events[delivery.event] : delivery.event;
      const code = document.createElement("code");
      code.textContent = delivery.event;
      event.append(name, code);
      const status = cell("");
      const badge = document.createElement("span");
      badge.classList.add("webhook-delivery-state", `webhook-delivery-${delivery.kind}`);
      badge.textContent = delivery.status;
      status.append(badge);
      cell(`${delivery.attempts}/2`);
      cell(delivery.result);
      return row;
    });
    body.replaceChildren(...nodes);
  }

  function formatMoment(value) {
    const date = parseDate(value);
    return date ? recordDateFormat.format(date) : "Sin registro";
  }

  function renderHealth(health) {
    const tag = $("health-state");
    const confirmed = state.serviceAvailable && health && typeof health === "object";
    const history = health?.history || state.status?.history;
    const storageProblem = history?.available === false || history?.dropped > 0;
    const stale = finite(health?.read_age_seconds) && health.read_age_seconds > 15;
    const healthy = confirmed && ["healthy", "ok", "running"].includes(health.state) && health.reader_alive === true && health.supervisor_alive === true && health.processor_alive !== false && !storageProblem && !stale;
    const transition = confirmed && ["suspended", "resuming"].includes(health.state) ? health.state : null;
    setText("health-state-label", !state.serviceAvailable ? state.stopAcknowledged ? "Detenido" : "Sin confirmar" : !health ? "No disponible"
      : transition === "suspended" ? "En pausa" : transition === "resuming" ? "Reanudando" : healthy ? "Operativo" : "Revisar monitor");
    tag.classList.toggle("good", !!healthy);
    tag.classList.toggle("warning", !healthy && !transition);
    const message = !state.serviceAvailable ? state.stopAcknowledged ? "Detuviste Elipro. La vigilancia está detenida. Inicia Elipro de nuevo para reanudarla." : "El panel perdió la comunicación. No puede confirmar si el monitor continúa funcionando." : !health ? "El monitor aún no comunica su estado." : health.message || (transition === "suspended" ? "Las lecturas están en pausa por suspensión o hibernación de Windows."
      : transition === "resuming" ? "Elipro está recuperando las lecturas después de reanudar Windows."
      : history?.available === false ? "No se puede guardar el historial. La vigilancia continúa."
      : history?.dropped > 0 ? `Se perdieron ${numeric(history.dropped)} registros del historial durante esta sesión.`
      : stale ? "La última lectura tiene más de 15 segundos. Revisa la conexión con el UPS."
      : healthy ? "" : "La supervisión del monitor requiere atención.");
    setText("health-message", message);
    $("health-message").hidden = !message;
    const data = confirmed ? health : {};
    setText("health-uptime", runtimeLabel(data.uptime_seconds));
    setText("health-last-reading", confirmed && finite(data.read_age_seconds) ? `Hace ${runtimeLabel(Math.max(0, data.read_age_seconds))}` : "—");
    $("health-last-reading").title = confirmed ? formatMoment(data.last_success) : "";
    const process = data.process || {};
    setText("health-resources", confirmed && finite(process.memory_bytes) ? `${numeric(process.memory_bytes / 1048576, 1)} MB RAM` : "—");
    setText("health-cpu", confirmed ? `CPU ${finite(process.cpu_percent) ? `${numeric(process.cpu_percent, 1)} %` : "midiendo"}` : "—");
  }

  function readRecordFilters() {
    const params = new URLSearchParams();
    const values = [$("records-from").value, $("records-to").value];
    const dates = values.map(value => value ? parseDate(value) : null);
    if (values.some((value, index) => value && !dates[index])) throw new Error("Revisa las fechas de la consulta.");
    if (dates[0] && dates[1] && dates[0] > dates[1]) throw new Error("La fecha Desde debe ser anterior o igual a Hasta.");
    if (dates[0]) params.set("from", dates[0].toISOString());
    if (dates[1]) params.set("to", dates[1].toISOString());
    if ($("event-kind").value) params.set("kind", $("event-kind").value);
    if ($("event-search").value.trim()) params.set("q", $("event-search").value.trim());
    return params;
  }

  function recordFilters() {
    return new URLSearchParams(state.recordFilters);
  }

  function renderRecordFilters() {
    const filters = recordFilters();
    const start = filters.get("from"), end = filters.get("to");
    const range = start && end ? `${formatMoment(start)} – ${formatMoment(end)}`
      : start ? `Desde ${formatMoment(start)}` : end ? `Hasta ${formatMoment(end)}` : "Todas las fechas";
    setText("records-feedback", `${range}. La gráfica usa su propio período.`);
  }

  function sectionError(id, error) {
    $(id).hidden = !error;
    setText(id, error ? error.message || String(error) : "");
  }

  function appendDetail(parent, text, className = "") {
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    paragraph.className = className;
    parent.append(paragraph);
  }

  function renderRecords(emptyMessage = "No hay registros que coincidan con los filtros.") {
    const body = $("records-body");
    body.replaceChildren();
    if (!state.records.length) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "section-empty";
      cell.textContent = emptyMessage;
      row.append(cell);
      body.append(row);
      return;
    }
    const actionLabels = { failed: "Fallida", started: "Iniciada", cancelled: "Cancelada", simulated: "Simulada", none: "Sin acciones" };
    const categoryLabels = { power: "Energía", connection: "Comunicación", protection: "Protección", alerts: "Alerta del UPS", settings: "Configuración", other: "Evento" };
    const alertLabels = { battery_low: "Batería baja", overload: "Sobrecarga", ups_fault: "Falla del UPS" };
    for (const [index, record] of state.records.entries()) {
      const incident = record.record_type === "incident";
      const type = incident ? record.type === "power" ? "Corte eléctrico" : "Comunicación" : alertLabels[record.kind || record.type] || categoryLabels[record.category] || eventLabel(record.kind || record.type) || "Evento";
      const date = incident && record.start_known === false ? "Inicio no registrado" : formatMoment(record.timestamp || record.started_at);
      const description = String(record.description || record.message || type);
      const duration = incident && finite(record.duration_seconds) ? runtimeLabel(record.duration_seconds) : "—";
      const row = document.createElement("tr");
      row.className = "record-row";
      const dateCell = document.createElement("td"), typeCell = document.createElement("td"), descriptionCell = document.createElement("td");
      dateCell.textContent = date;
      typeCell.textContent = type;
      appendDetail(descriptionCell, description);
      if (incident) {
        const notes = [];
        if (record.ended_at) notes.push(`Recuperación: ${formatMoment(record.ended_at)}`);
        if (record.action_result && record.action_result !== "none") notes.push(`Protección: ${actionLabels[record.action_result] || record.action_result}`);
        if (notes.length) appendDetail(descriptionCell, notes.join(" · "), "record-meta");
      }
      const statusCell = document.createElement("td"), durationCell = document.createElement("td");
      statusCell.textContent = incident ? record.status === "open" ? "Sin cierre registrado" : "Recuperado" : "—";
      if (incident) statusCell.className = record.status === "open" ? "records-status-active" : "records-status-recovered";
      durationCell.textContent = duration;
      row.append(dateCell, typeCell, descriptionCell, statusCell, durationCell);

      const detailRow = document.createElement("tr"), detailCell = document.createElement("td"), details = document.createElement("div");
      detailRow.className = "incident-detail-row";
      detailRow.id = `record-detail-${index}`;
      detailRow.hidden = true;
      detailCell.colSpan = 6;
      details.className = "incident-details";
      if (incident) {
        appendDetail(details, `Inicio: ${date}`);
        appendDetail(details, record.ended_at ? `Recuperación: ${formatMoment(record.ended_at)}` : "Sin recuperación registrada.");
        appendDetail(details, `${record.status === "open" ? "Tiempo desde la detección" : "Duración detectada"}: ${duration}`);
        if (record.message) appendDetail(details, String(record.message));
        if (record.recovery_message) appendDetail(details, String(record.recovery_message));
        appendDetail(details, `Protección: ${actionLabels[record.action_result] || "Sin acciones"}`);
        if (record.status === "open") appendDetail(details, "Un registro sin cierre no confirma que el problema continúe. Consulta Resumen para comprobar el estado actual.", "incident-warning");
      }
      const events = Array.isArray(record.events) && record.events.length ? record.events : Array.isArray(record.actions) ? record.actions : [];
      if (events.length) {
        const sequence = document.createElement("ol");
        sequence.className = "incident-action-sequence";
        for (const event of events) {
          const item = document.createElement("li");
          item.textContent = `${formatMoment(event.timestamp)} · ${eventLabel(event.kind) || "Evento"} · ${eventMessage(event) || ""}`;
          sequence.append(item);
        }
        details.append(sequence);
      } else if (!incident) appendDetail(details, `${date} · ${type} · ${description}`);
      if (!incident && record.data && Object.keys(record.data).length) {
        const data = document.createElement("pre");
        data.className = "record-data";
        data.textContent = JSON.stringify(record.data, null, 2);
        details.append(data);
      }
      detailCell.append(details);
      detailRow.append(detailCell);
      const buttonCell = document.createElement("td"), button = document.createElement("button");
      button.type = "button";
      button.className = "button button-quiet incident-details-toggle";
      button.textContent = "Ver detalles";
      button.setAttribute("aria-controls", detailRow.id);
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-label", `Ver detalles: ${type}, ${date}`);
      button.addEventListener("click", () => {
        detailRow.hidden = !detailRow.hidden;
        button.textContent = detailRow.hidden ? "Ver detalles" : "Ocultar detalles";
        button.setAttribute("aria-expanded", String(!detailRow.hidden));
        button.setAttribute("aria-label", `${detailRow.hidden ? "Ver" : "Ocultar"} detalles: ${type}, ${date}`);
      });
      buttonCell.append(button);
      row.append(buttonCell);
      body.append(row, detailRow);
    }
  }

  async function loadRecords(append = false) {
    if (!state.serviceAvailable || state.recordsBusy || append && !state.recordCursor) return;
    const version = state.recordVersion;
    const params = recordFilters();
    const query = params.toString();
    if (append && query !== state.recordQuery) append = false;
    if (append) params.set("cursor", state.recordCursor);
    params.set("limit", "50");
    state.recordsBusy = true;
    $("records-list").setAttribute("aria-busy", "true");
    updateMutationControls();
    sectionError("records-error", null);
    try {
      const data = await request(`/api/records?${params}`);
      if (version !== state.recordVersion) return;
      if (!Array.isArray(data.records)) throw new Error("El monitor no devolvió un registro válido.");
      const combined = append ? [...state.records, ...data.records] : data.records;
      state.records = combined.filter((record, index) => combined.findIndex(other => String(other.id) === String(record.id)) === index);
      state.recordQuery = query;
      state.recordCursor = data.next_cursor || null;
      state.recordsLoaded = true;
      const total = finite(data.total) ? data.total : state.records.length;
      setText("records-count", `${numeric(total)} ${total === 1 ? "registro" : "registros"}`);
      setText("records-summary", `${numeric(state.records.length)} ${state.records.length === 1 ? "mostrado" : "mostrados"} de ${numeric(total)}. El CSV incluye todos los resultados filtrados.`);
      $("more-records").hidden = !state.recordCursor;
      renderRecords();
    } catch (error) {
      if (version === state.recordVersion) {
        if (error.code === "record_cursor_expired") {
          state.recordCursor = null;
          $("more-records").hidden = true;
        }
        sectionError("records-error", new Error(`No se pudo consultar el registro. ${error.message}`));
        if (!state.records.length) renderRecords("No se pudo cargar el registro.");
      }
    } finally {
      state.recordsBusy = false;
      $("records-list").setAttribute("aria-busy", "false");
      updateMutationControls();
      if (version !== state.recordVersion) loadRecords();
    }
  }

  async function refreshRecords() {
    if (!state.serviceAvailable) return;
    let filters;
    try { filters = readRecordFilters().toString(); }
    catch (error) { setText("records-feedback", error.message); return; }
    const changed = filters !== state.recordFilters;
    state.recordFilters = filters;
    state.recordVersion += 1;
    state.recordCursor = null;
    $("more-records").hidden = true;
    if (changed) {
      state.records = [];
      setText("records-count", "—");
      setText("records-summary", "Consultando el registro…");
      renderRecords("Consultando el registro…");
    }
    renderRecordFilters();
    await loadRecords();
  }

  async function downloadFile(path, filename, feedbackId) {
    if (!state.serviceAvailable || state.downloadBusy) return;
    state.downloadBusy = true;
    updateMutationControls();
    $(feedbackId).hidden = false;
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
      renderCriticalCountdown();
      return;
    }
    const deadline = parseDate(protection.deadline);
    const seconds = state.countdownView ? Math.max(0, Math.ceil((state.countdownView.deadline - monotonicNow()) / 1000))
      : deadline ? Math.max(0, Math.ceil((deadline.getTime() - Date.now()) / 1000)) : finite(protection.countdown_remaining) ? Math.max(0, Math.ceil(protection.countdown_remaining)) : null;
    setText("countdown", seconds === null ? "—" : `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`);
    $("countdown").title = !state.serviceAvailable ? "Referencia del último plazo confirmado. La ejecución y la cancelación requieren confirmación del monitor." : "Tiempo restante comunicado por el monitor.";
    renderCriticalCountdown();
  }

  function applyStatus(data) {
    if (!data || typeof data !== "object" || !data.connection || !data.policy || !data.protection) throw new Error("El monitor devolvió un estado incompleto.");
    const recovered = !state.serviceAvailable;
    const historyRecovered = state.status?.history?.available === false && data.history?.available === true;
    syncCountdownView(data.protection, data._requestElapsedMs || 0);
    state.status = data;
    state.upsPowerReviewNeedsStatus = false;
    state.serviceAvailable = true;
    state.stopAcknowledged = false;
    state.lastStatusAt = new Date();
    state.token = typeof data.csrf_token === "string" ? data.csrf_token : null;
    if (finite(data.meta?.history_interval_seconds) && data.meta.history_interval_seconds > 0) state.historyInterval = data.meta.history_interval_seconds * 1000;
    $("development-notice").hidden = !(data.meta?.dry_run || data.meta?.simulation_source);
    $("bridge-policy-note").hidden = data.meta?.bridge_required !== true;
    renderServiceState();
    refreshTelemetry();
    renderEvents(data.events);
    renderPolicy(data.policy || {});
    renderProtection(data.protection || {});
    renderManualUpsTestNotices();
    renderNotifications(data.notifications);
    renderWebhooks(data.webhooks);
    renderHistoryStatus();
    renderHealth(data.health);
    loadViewData();
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
      .map((sample) => ({ ...sample,
        battery_percent: percentage(sample.battery_percent) ? sample.battery_percent : null,
        battery_percent_estimate: estimatedBattery(sample) ? sample.battery_percent_estimate : null,
        _time: parseDate(sample.timestamp)?.getTime() }))
      .filter((sample) => Number.isFinite(sample._time))
      .sort((a, b) => a._time - b._time);
  }

  async function loadHistory() {
    if (state.historyBusy) return;
    state.historyBusy = true;
    const requestedHours = state.hours;
    try {
      const data = await request(`/api/history?hours=${requestedHours}`);
      if (requestedHours !== state.hours) return;
      state.historyQueryError = null;
      state.history = normalizeHistory(data.samples);
      renderChart();
    } catch (error) {
      if (requestedHours !== state.hours) return;
      state.historyQueryError = error.message;
      renderHistoryStatus();
    } finally {
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

  function chartSeries(samples) {
    if (state.metric === "battery") {
      const reported = samples.some((sample) => percentage(sample.battery_percent));
      const estimated = samples.some(estimatedBattery);
      if (!reported && !estimated && samples.some((sample) => finite(sample.battery_voltage))) {
        return [{ key: "battery_voltage", label: "Voltaje de batería", color: "var(--teal)" }];
      }
      const series = [];
      if (reported || !estimated) series.push({ key: "battery_percent", label: "Carga reportada", color: "var(--teal)" });
      if (estimated) series.push({ key: "battery_percent_estimate", label: "Carga estimada", color: "var(--blue)", estimated: true });
      return series;
    }
    if (state.metric === "load") return [{ key: "load_percent", label: "Carga", color: "var(--blue)" }];
    return [{ key: "input_voltage", label: "Entrada", color: "var(--teal)" }, { key: "output_voltage", label: "Salida", color: "var(--blue)" }];
  }

  function renderChart() {
    const chart = $("history-chart");
    chart.replaceChildren();
    const width = Math.max(280, chart.clientWidth || 800);
    const height = Math.max(120, chart.clientHeight || 230);
    chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const end = Date.now();
    const start = end - state.hours * 3600000;
    const samples = state.history.filter((sample) => sample._time >= start && sample._time <= end);
    const series = chartSeries(samples);
    const unit = state.metric === "voltage" || series[0].key === "battery_voltage" ? "V" : "%";
    const legend = $("chart-legend");
    legend.replaceChildren();
    for (const line of series) {
      const label = document.createElement("span");
      const mark = document.createElement("i");
      mark.style.background = line.color;
      if (line.estimated) mark.className = "legend-estimated";
      label.append(mark, document.createTextNode(line.label));
      legend.append(label);
    }
    const legendUnit = document.createElement("span");
    legendUnit.className = "chart-unit";
    legendUnit.textContent = unit === "V" ? "VOLTIOS" : "PORCENTAJE";
    legend.append(legendUnit);
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
    const left = 38, right = width - 14, top = 14, bottom = height - 38;
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
      chart.append(svgElement("text", { x: x(time), y: height - 11, class: "chart-axis", "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle" }, label));
    }
    // Never connect samples across a missing measurement or a gap in collection.
    const intervals = samples.slice(1).map((sample, index) => sample._time - samples[index]._time).filter((interval) => interval > 0).sort((a, b) => a - b);
    const expectedInterval = intervals.length ? intervals[Math.floor(intervals.length / 2)] : state.historyInterval;
    const maxGap = Math.max(state.historyInterval * 3, expectedInterval * 3);
    for (const line of series) {
      let path = "";
      let previous = null;
      let previousMethod = null;
      const dots = [];
      for (const sample of samples) {
        const value = sample[line.key];
        if (!finite(value)) { previous = null; continue; }
        // The backend checks original measurements before decimating long ranges.
        const continuing = previous !== null && (!line.estimated || previousMethod === sample.battery_estimate_method) && (typeof sample.gap_before === "boolean" ? !sample.gap_before : sample._time - previous <= maxGap);
        const px = x(sample._time), py = y(value);
        path += `${continuing ? "L" : "M"}${px.toFixed(2)},${py.toFixed(2)} `;
        if (!continuing) dots.push([px, py]);
        previous = sample._time;
        previousMethod = sample.battery_estimate_method;
      }
      if (path) chart.append(svgElement("path", { d: path.trim(), class: "chart-line", stroke: line.color, ...(line.estimated ? { "stroke-dasharray": "6 4" } : {}) }));
      for (const [cx, cy] of dots) chart.append(svgElement("circle", { cx, cy, r: 2, fill: line.color }));
    }
    const cursor = svgElement("line", { x1: 0, x2: 0, y1: top, y2: bottom, class: "chart-cursor", visibility: "hidden" });
    chart.append(cursor);
    state.chart = { samples, series, start, end, left, right, width, x, cursor, unit, maxGap };
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
    const localX = (event.clientX - bounds.left) / bounds.width * chart.width;
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
    savePreferences();
    state.history = [];
    renderChart();
    loadHistory();
  });
  $("events-filter").addEventListener("change", () => {
    state.eventFilter = $("events-filter").value;
    renderEvents(state.events, false);
  });
  function policyInput(event) {
    if (event.target === $("ups-power-review-ack")) return;
    state.policyDirty = true;
    setText("enabled-label", $("policy-enabled").checked ? state.status?.meta?.dry_run ? "Modo de prueba" : "Activada" : "Desactivada");
    setText("settings-feedback", "Cambios sin guardar");
    $("settings-feedback").classList.remove("error");
    updateMutationControls();
    renderManualUpsTestNotices();
    renderUpsPoweroff();
  }
  $("policy-form").addEventListener("input", policyInput);
  $("policy-enabled").addEventListener("input", policyInput);
  $("webhooks-form").addEventListener("input", () => {
    state.webhooksDirty = !webhookSettingsMatch(webhookDraft(), state.status?.webhooks?.settings);
    setText("webhooks-feedback", state.webhooksDirty ? "Cambios sin guardar" : "");
    $("webhooks-feedback").classList.remove("error");
    renderWebhookSelection();
    updateMutationControls();
  });
  $("webhooks-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if ($("save-webhooks").disabled || state.mutation) return;
    if (!$("webhooks-form").reportValidity()) return;
    const settings = webhookDraft();
    if (settings.webhook_enabled && !settings.webhook_url) {
      setText("webhooks-feedback", "Introduce una URL para activar los webhooks.");
      $("webhooks-feedback").classList.add("error");
      $("webhook-url").focus();
      return;
    }
    state.mutation = "save-webhooks";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("save-webhooks-label", "Guardando…");
    try {
      const result = await post("/api/webhooks", settings);
      applyStatus(result);
      if (!result.webhooks?.settings || !webhookSettingsMatch(settings, result.webhooks.settings)) throw new Error("El monitor no confirmó la configuración de webhooks.");
      state.webhooksDirty = false;
      state.webhooksInitialized = false;
      renderWebhooks(result.webhooks);
      setText("webhooks-feedback", settings.webhook_enabled ? "Webhooks guardados" : "Envíos desactivados. Configuración conservada.");
      $("webhooks-feedback").classList.remove("error");
      toast(settings.webhook_enabled ? "Configuración de webhooks guardada." : "Webhooks desactivados. Se conservaron la URL y los eventos.");
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("webhooks-feedback", `Guardado sin confirmar. ${error.message}`);
      $("webhooks-feedback").classList.add("error");
    } finally {
      state.mutation = null;
      setText("save-webhooks-label", "Guardar webhooks");
      updateMutationControls();
    }
  });
  $("test-webhook").addEventListener("click", async () => {
    if ($("test-webhook").disabled || state.mutation) return;
    if (state.webhooksDirty) {
      setText("webhooks-feedback", "Guarda los cambios antes de probar el webhook.");
      $("webhooks-feedback").classList.add("error");
      return;
    }
    state.mutation = "test-webhook";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("test-webhook-label", "Solicitando prueba…");
    try {
      const result = await post("/api/webhooks/test", {});
      if (result.webhooks?.test?.id == null) throw new Error("El monitor no confirmó la solicitud de prueba.");
      applyStatus(result);
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      setText("webhooks-test-feedback", `Prueba sin confirmar. ${error.message}`);
      $("webhooks-test-feedback").hidden = false;
      $("webhooks-test-feedback").classList.add("error");
      $("webhooks-test-feedback").classList.remove("simulated");
    } finally {
      state.mutation = null;
      updateMutationControls();
    }
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
    if (["auto", "light", "dark"].includes(state.status.notifications.settings.theme)) settings.theme = $("notification-theme").value;
    if (typeof state.status.notifications.settings.persistent_critical === "boolean") settings.persistent_critical = Array.isArray(state.status.notifications.pending)
      ? state.status.notifications.settings.persistent_critical : $("notifications-persistent").checked;
    state.mutation = "save-notifications";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("save-notifications-label", "Guardando…");
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
      setText("save-notifications-label", "Guardar avisos");
      updateMutationControls();
    }
  });
  $("test-notifications").addEventListener("click", async () => {
    if ($("test-notifications").disabled || state.mutation) return;
    state.mutation = "test-notifications";
    state.statusGeneration += 1;
    updateMutationControls();
    setText("test-notifications-label", "Enviando prueba…");
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
    if (!state.serviceAvailable || state.mutation || upsPhysicalTestActive()) return;
    if (!$("policy-form").reportValidity()) return;
    const countdownSeconds = Number($("policy-countdown").value);
    if (!Number.isInteger(countdownSeconds) || countdownSeconds < 10 || countdownSeconds > 120) {
      setText("settings-feedback", "El tiempo antes de actuar debe ser un entero de 10 a 120 segundos.");
      $("settings-feedback").classList.add("error");
      return;
    }
    const upsDelayMinutes = Number($("policy-ups-delay").value);
    if (!Number.isInteger(upsDelayMinutes) || upsDelayMinutes < 1 || upsDelayMinutes > 9) {
      setText("settings-feedback", "El retardo del UPS debe ser un entero de 1 a 9 minutos.");
      $("settings-feedback").classList.add("error");
      return;
    }
    const lowBatteryPercent = Number($("policy-low-battery").value);
    if (!Number.isInteger(lowBatteryPercent) || lowBatteryPercent < 0 || lowBatteryPercent > 100) {
      setText("settings-feedback", "El umbral de batería baja debe ser un porcentaje entero de 0 a 100.");
      $("settings-feedback").classList.add("error");
      return;
    }
    const button = $("save-policy");
    state.mutation = "save";
    state.statusGeneration += 1;
    updateMutationControls();
    button.textContent = "Guardando…";
    const policy = {
      enabled: $("policy-enabled").checked,
      low_battery_percent: lowBatteryPercent,
      min_runtime_seconds: state.status?.meta?.source === "native" ? state.status.policy.min_runtime_seconds : Number($("policy-runtime").value),
      countdown_seconds: countdownSeconds,
      action: $("policy-action").value,
      ups_poweroff: $("policy-ups-poweroff").checked,
      ups_poweroff_delay_seconds: upsDelayMinutes * 60,
    };
    try {
      const result = await post("/api/policy", policy);
      applyStatus(result);
      state.policyDirty = false;
      state.policyInitialized = false;
      renderPolicy(result.policy);
      setText("settings-feedback", "Configuración guardada");
      $("settings-feedback").classList.remove("error");
      toast(policy.enabled ? result.meta?.dry_run
        ? "Regla guardada en modo de prueba. El apagado y la hibernación siguen bloqueados."
        : "Protección automática activada en esta PC."
        : "Configuración guardada. La protección está desactivada.", false, policy.enabled && result.meta?.dry_run === true);
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
    if (!state.serviceAvailable || state.mutation || state.status?.policy?.enabled || state.policyDirty || powerTransition() || upsPhysicalTestActive()) return;
    state.mutation = "test";
    state.statusGeneration += 1;
    updateMutationControls();
    try {
      const includeUps = simulationIncludesUps();
      const result = await post("/api/protection/test", { include_ups: includeUps });
      applyStatus(result);
      if (!result.protection?.simulation || result.protection.state !== "countdown") throw new Error("El monitor no confirmó el inicio de la prueba.");
      toast(includeUps ? "Simulación iniciada. Windows y UPS no ejecutarán acciones reales." : "Simulación de Windows iniciada. El apagado adicional del UPS está desactivado o no disponible.");
      $("protection-alert").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "center" });
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      toast(error.message, true);
    } finally { state.mutation = null; updateMutationControls(); }
  });
  $("ups-power-review-ack").addEventListener("change", updateMutationControls);
  $("ups-power-review-submit").addEventListener("click", async () => {
    if (!upsPowerCanReview() || !$("ups-power-review-ack").checked) return;
    const operationId = state.status.ups_poweroff.operation.operation_id;
    state.mutation = "ups-power-review";
    state.upsPowerReviewNeedsStatus = true;
    state.statusGeneration += 1;
    $("ups-power-review-feedback").hidden = true;
    updateMutationControls();
    try {
      const result = await post("/api/ups/poweroff/review", { operation_id: operationId, acknowledge_review: true });
      applyStatus(result);
      if (result.ups_poweroff?.operation?.state !== "completed") throw new Error("El monitor no confirmó el cierre de la revisión.");
      toast("Revisión cerrada; próxima protección preparada. El corte anterior no se confirma automáticamente.", false, true);
    } catch (error) {
      state.upsPowerReviewNeedsStatus = true;
      if (error.communicationFailure) serviceUnavailable(error);
      setText("ups-power-review-feedback", `Cierre de revisión sin confirmar. ${error.message} No se ha confirmado automáticamente el corte anterior. Esta acción solo actualiza el registro.`);
      $("ups-power-review-feedback").hidden = false;
      toast("No se confirmó el cierre de la revisión del UPS. El corte anterior sigue sin confirmación física automática.", true);
    } finally {
      state.mutation = null;
      renderUpsPoweroff();
      updateMutationControls();
    }
  });
  async function cancelProtection() {
    if (!canCancelCountdown()) return;
    const simulation = !!state.status.protection.simulation;
    const countdownId = simulation ? null : state.status.protection.countdown_id;
    state.mutation = "cancel";
    state.statusGeneration += 1;
    updateMutationControls();
    $("critical-feedback").hidden = true;
    try {
      const result = await post(simulation ? "/api/protection/cancel" : "/api/protection/countdown/cancel",
        simulation ? {} : { countdown_id: countdownId });
      if (!simulation && result.cancelled_countdown_id !== countdownId) throw new Error("El monitor no confirmó la cancelación de esta cuenta.");
      if (!simulation && state.status?.protection?.state === "countdown" && state.status.protection.countdown_id !== countdownId) {
        throw new Error("El monitor ya informa otra cuenta. Se conserva su estado actual.");
      }
      applyStatus(result);
      if (["countdown", "executed"].includes(result.protection.state)) throw new Error("El monitor aún reporta una cuenta atrás o una acción enviada a Windows.");
      if (state.criticalOpen && !state.criticalItems.size && !state.pendingNotifications.size) finishCriticalAcknowledgment(captureCriticalAcknowledgment());
      toast(simulation ? "El monitor confirmó la cancelación de la prueba." : result.protection.state === "cancelled" ? "Cuenta atrás cancelada. La protección volverá a evaluarse en el próximo corte." : "El monitor confirma que no hay una cuenta atrás activa.");
      if (state.criticalOpen) {
        setText("critical-feedback", "El monitor confirmó que no hay una cuenta atrás activa.");
        $("critical-feedback").hidden = false;
      }
    } catch (error) {
      if (error.communicationFailure) serviceUnavailable(error);
      toast(`No se confirmó la cancelación. ${error.message}`, true);
      if (state.criticalOpen) {
        setText("critical-feedback", `No se confirmó la cancelación. ${error.message}`);
        $("critical-feedback").hidden = false;
      }
    } finally { state.mutation = null; updateMutationControls(); }
  }
  $("open-protection-countdown").addEventListener("click", (event) => {
    event.preventDefault();
    if (state.stopAcknowledged || state.status?.protection?.state !== "countdown") return;
    state.criticalCountdownAcknowledged = null;
    activateView("protection", { writeHash: true });
    refreshTelemetry();
  });
  $("critical-cancel").addEventListener("click", cancelProtection);
  $("critical-ack").addEventListener("click", acknowledgeCritical);
  $("critical-dialog").addEventListener("cancel", (event) => { event.preventDefault(); $("critical-ack").focus(); });
  $("critical-dialog").addEventListener("close", () => {
    if (state.criticalOpen) $("critical-dialog").showModal();
  });
  $("critical-dialog").addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || !state.criticalOpen) return;
    const buttons = [!$("critical-countdown").hidden && !$("critical-cancel").disabled ? $("critical-cancel") : null, $("critical-ack")].filter(Boolean);
    const first = buttons[0], last = buttons.at(-1);
    if (event.shiftKey && (document.activeElement === first || document.activeElement === $("critical-title"))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  });

  $("records-form").addEventListener("submit", (event) => { event.preventDefault(); return refreshRecords(); });
  $("records-form").addEventListener("input", () => setText("records-feedback", "Cambios sin aplicar. Pulsa «Aplicar filtros»."));
  $("clear-records").addEventListener("click", () => {
    for (const id of ["records-from", "records-to", "event-search", "event-kind"]) $(id).value = "";
    return refreshRecords();
  });
  $("more-records").addEventListener("click", () => loadRecords(true));
  $("export-records").addEventListener("click", () => downloadFile(
    `/api/records/export.csv?${recordFilters()}`, "elipro-registro.csv", "records-summary"));
  $("stop-monitor").addEventListener("click", () => { if (!upsPhysicalTestActive()) $("stop-confirmation").hidden = false; });
  $("cancel-stop-monitor").addEventListener("click", () => { $("stop-confirmation").hidden = true; });
  $("confirm-stop-monitor").addEventListener("click", async () => {
    if (upsPhysicalTestActive()) return;
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
  renderRecordFilters();
  updateMutationControls();
  renderChart();
  loadStatus();
  loadHistory();
  setInterval(loadStatus, 2000);
  setInterval(loadHistory, 30000);
  setInterval(() => { if (state.status) refreshTelemetry(); updateCountdown(); }, 1000);
  window.addEventListener?.("focus", () => { if (state.status) refreshTelemetry(); });
  document.addEventListener?.("visibilitychange", () => { if (state.status) refreshTelemetry(); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initializeApp, { once: true });
  else initializeApp();
})();
