/* ConciliaBT — Frontend (Vanilla JS, no frameworks) — Two-phase reconciliation UI */

"use strict";

/* ============ STATE ============ */
const state = {
  accounts: [],
  periods: [],
  accountId: null,
  periodId: null,
  overview: null,
  view: "resumen",
  importSource: "BANCO",
  importPreview: null,
  importRows: [],
  importFileName: "",
  selectedMovIds: new Set(),
  mesaTab: "pendientes",
  alertasTab: "duplicados",
  hojaTab: "hoja",
  settings: {},
  mesaFilter: "",
  importNumberFormat: "auto",
  importDefaultYear: 2026,
  importReplace: false,
  importUpdateBalances: true,
  importAutoReconcile: true,
  importText: "",
  showImportText: false,
  importPasteText: "",
};

/* ============ HELPERS ============ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtVE = (cents) => {
  if (cents === null || cents === undefined || isNaN(cents)) return "—";
  const neg = cents < 0;
  const a = Math.abs(Math.round(cents));
  const whole = Math.floor(a / 100).toLocaleString("es-VE").replace(/,/g, ".");
  return `${neg ? "-" : ""}${whole},${String(a % 100).padStart(2, "0")}`;
};
const fmtSigned = (cents) => {
  if (cents === null || cents === undefined) return "—";
  if (cents === 0) return "0,00";
  return cents > 0 ? `+${fmtVE(cents)}` : fmtVE(cents);
};
const fmtDate = (iso) => {
  if (!iso) return "—";
  const p = String(iso).slice(0, 10).split("-");
  return p.length === 3 ? `${p[2]}/${p[1]}/${p[0]}` : iso;
};
const amtClass = (cents) => (cents < 0 ? "amt-neg" : cents > 0 ? "amt-in" : "amt-muted");
const statusLabel = {
  PENDIENTE: "Pendiente", CONCILIADO: "Conciliado", CONCILIADO_DIF: "Conciliado c/ Diferencia",
  EN_TRANSITO: "En Tránsito", AJUSTE_BANCO: "Cargo/Abono Bancario", DUPLICADO: "Duplicado",
  REVERSO: "Reverso / Anulación", FALLA_SISTEMA: "Falla de Sistema", ERROR_BANCO: "Error Bancario",
};
const statusBadge = (st) => {
  const map = {
    PENDIENTE: "badge-neutral", CONCILIADO: "badge-ok", CONCILIADO_DIF: "badge-warn",
    EN_TRANSITO: "badge-warn", AJUSTE_BANCO: "badge-warn", DUPLICADO: "badge-err",
    REVERSO: "badge-neutral", FALLA_SISTEMA: "badge-sys", ERROR_BANCO: "badge-err",
  };
  return `<span class="badge ${map[st] || "badge-neutral"}">${esc(statusLabel[st] || st)}</span>`;
};
const srcLabel = { BANCO: "Banco del Tesoro", LIBROS: "Según Libros", MAYOR: "Mayor Analítico" };
const srcShort = { BANCO: "Banco", LIBROS: "Libros", MAYOR: "Mayor" };
const targetLabel = {
  LIBROS_Y_MAYOR: "Tesorería y Contabilidad", SOLO_MAYOR: "Solo Mayor Analítico",
  SOLO_LIBROS: "Solo Según Libros", RECLAMO_BANCO: "Reclamo Banco del Tesoro",
  AUDITORIA_SISTEMA: "Soporte / Falla de Sistema",
};
const matchTypeLabel = {
  EXACTO_TRIPLE: "Exacto Triple", EXACTO_DOBLE: "Exacto Doble", CON_DIFERENCIA: "Con Diferencia",
  REVERSO: "Reverso", LOTE: "Lote Agrupado",
};
const kindLabel = {
  FALTA_EN_MAYOR: "Falta en Mayor", FALTA_EN_LIBROS: "Falta en Libros",
  DIFERENCIA_MONTO: "Diferencia de monto", FALLA_SISTEMA: "Falla del sistema",
  FALLA_HISTORICA: "Falla histórica", EN_TRANSITO: "En tránsito",
  NO_REGISTRADO_EN_MAYOR: "No registrado en Mayor", DUPLICADO_LIBROS: "Duplicado en Libros",
  DUPLICADO_MAYOR: "Duplicado en Mayor", POSIBLE_DUPLICADO_LIBROS: "Posible duplicado (Libros)",
  POSIBLE_DUPLICADO_MAYOR: "Posible duplicado (Mayor)", POSIBLE_DUPLICADO_BANCO: "Posible duplicado (Banco)",
  ERROR_BANCO: "Error bancario",
};

/* ============ PIN GATE ============ */
const getPin = () => sessionStorage.getItem("conciliabt_pin") || "";
function promptPin() {
  return new Promise((resolve) => {
    openModal("Acceso protegido", `
      <p class="form-hint">Esta aplicación está protegida con un PIN. Ingrésalo para continuar.</p>
      <div class="form-group"><label>PIN de acceso</label>
        <input class="input" id="pinInput" type="password" inputmode="numeric" autocomplete="current-password" placeholder="••••••">
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" id="pinCancel">Cancelar</button>
        <button class="btn btn-primary" id="pinOk">Entrar</button>
      </div>`);
    const submit = () => {
      const v = $("#pinInput").value.trim();
      if (!v) return;
      sessionStorage.setItem("conciliabt_pin", v);
      closeModal();
      resolve(true);
    };
    $("#pinOk").addEventListener("click", submit);
    $("#pinCancel").addEventListener("click", () => { closeModal(); resolve(false); });
    $("#pinInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    $("#pinInput").focus();
  });
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const pin = getPin();
  if (pin) headers["X-App-Pin"] = pin;
  const res = await fetch(path, { headers, ...opts });
  if (res.status === 401) {
    const ok = await promptPin();
    if (!ok) throw new Error("Acceso denegado: PIN requerido.");
    return api(path, opts);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `Error ${res.status}`;
    throw new Error(msg);
  }
  return data;
}
const apiGet = (p) => api(p);
const apiPost = (p, body) => api(p, { method: "POST", body: JSON.stringify(body ?? {}) });
const apiPut = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body ?? {}) });
const apiDelete = (p) => api(p, { method: "DELETE" });

async function downloadBlob(path, filename) {
  const headers = {};
  const pin = getPin();
  if (pin) headers["X-App-Pin"] = pin;
  let res = await fetch(path, { headers });
  if (res.status === 401) {
    const ok = await promptPin();
    if (!ok) throw new Error("Acceso denegado: PIN requerido.");
    headers["X-App-Pin"] = getPin();
    res = await fetch(path, { headers });
  }
  if (!res.ok) throw new Error(`Error ${res.status} al descargar`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

function toast(msg, isErr = false) {
  const t = document.createElement("div");
  t.className = "toast" + (isErr ? " err" : "");
  t.textContent = msg;
  $("#toastStack").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

function openModal(title, bodyHtml) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalBackdrop").hidden = false;
}
function closeModal() { $("#modalBackdrop").hidden = true; }
$("#btnCloseModal").addEventListener("click", closeModal);
$("#modalBackdrop").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeModal(); });

/* ============ INIT / CONTEXT ============ */
async function loadContext() {
  const data = await apiGet("/api/accounts");
  state.accounts = data.accounts || [];
  state.periods = data.periods || [];
  state.settings = await apiGet("/api/settings").catch(() => ({}));

  const accSel = $("#accountSelect");
  accSel.innerHTML = state.accounts.map((a) => `<option value="${a.id}">${esc(a.name)} — ${esc(a.account_number)}</option>`).join("");

  if (state.accountId === null || !state.accounts.some((a) => a.id === state.accountId)) {
    state.accountId = state.accounts[0]?.id ?? null;
  }
  accSel.value = state.accountId ?? "";

  const acc = state.accounts.find((a) => a.id === state.accountId);
  $("#accountMeta").textContent = acc ? `${acc.bank_name} · ${acc.account_type} · ${acc.ledger_code} · ${acc.currency}` : "Sin cuentas";

  const pers = state.periods.filter((p) => p.account_id === state.accountId);
  const perSel = $("#periodSelect");
  perSel.innerHTML = pers.map((p) => `<option value="${p.id}">${esc(p.label)}${p.status === "CERRADO" ? " (Cerrado)" : ""}</option>`).join("");
  if (!pers.some((p) => p.id === state.periodId)) {
    state.periodId = pers[0]?.id ?? null;
  }
  perSel.value = state.periodId ?? "";

  await loadOverview();
  render();
}

async function loadOverview() {
  if (!state.periodId) { state.overview = null; return; }
  state.overview = await apiGet(`/api/periods/${state.periodId}/overview`);
  state.selectedMovIds.clear();
  updateBadges();
  updateTopbar();
}

function updateBadges() {
  const ov = state.overview;
  const pend = ov ? ov.movements.filter((m) => m.status === "PENDIENTE").length : 0;
  const alerts = ov ? (ov.suspected_duplicates?.length || 0) + (ov.system_issues?.filter((i) => i.status !== "RESUELTA").length || 0) : 0;
  const adj = ov ? ov.adjustments.filter((a) => !a.applied).length : 0;
  const set = (id, n) => { const el = $(id); el.hidden = !n; el.textContent = n; };
  set("#badgePending", pend);
  set("#badgeAlerts", alerts);
  set("#badgeAdj", adj);
}

function updateTopbar() {
  const ov = state.overview;
  if (!ov) {
    $("#topbarTitle").textContent = "ConciliaBT";
    $("#topbarSub").textContent = "Crea una cuenta y un período para comenzar";
    $("#periodStatusPill").textContent = "—";
    $("#balanceStatusPill").textContent = "—";
    return;
  }
  const p = ov.sheet.period;
  $("#topbarTitle").textContent = `Conciliación — ${p.label}`;
  $("#topbarSub").textContent = `${p.account_name} · ${p.account_number} · ${p.ledger_code} · ${p.currency}`;
  const pill = $("#periodStatusPill");
  pill.textContent = p.status === "CERRADO" ? "CERRADO" : "ABIERTO";
  pill.classList.toggle("closed", p.status === "CERRADO");
  const bal = $("#balanceStatusPill");
  const s = ov.sheet.summary;
  if (s.is_balanced) {
    bal.textContent = "✓ Conciliado sin diferencias";
    bal.className = "balance-pill ok";
  } else {
    const d = s.diff_banco_libros_cents;
    bal.textContent = `Diferencia vs Banco: Bs. ${fmtVE(d)}`;
    bal.className = "balance-pill warn";
  }
}

/* ============ RENDER DISPATCH ============ */
function render() {
  const v = state.view;
  if (v === "resumen") renderResumen();
  else if (v === "importar") renderImportar();
  else if (v === "mesa") renderMesa();
  else if (v === "alertas") renderAlertas();
  else if (v === "hoja") renderHoja();
  else if (v === "config") renderConfig();
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $("#sidebar").classList.remove("open");
}

/* ============ VIEW: RESUMEN (Two phases) ============ */
function phaseCard(num, title, subtitle, f, leftLabel, rightLabel, leftClose, rightClose, concLeft, concRight) {
  const ok = f.unexplained_diff_cents === 0;
  const items = f.items || [];
  const sysCount = items.filter((i) => i.kind === "FALLA_SISTEMA" || i.kind === "FALLA_HISTORICA").length;
  return `
  <div class="card phase-card">
    <div class="card-header">
      <div>
        <h3 class="card-title"><span class="phase-num">FASE ${num}</span> ${title}</h3>
        <p class="card-subtitle">${subtitle}</p>
      </div>
      ${ok ? '<span class="badge badge-ok">✓ Diferencias identificadas</span>' : `<span class="badge badge-warn">⚠ Sin explicar: Bs. ${fmtVE(f.unexplained_diff_cents)}</span>`}
    </div>
    <div class="card-body">
      <div class="phase-stats">
        <div><div class="kpi-label">${leftLabel} (cierre)</div><div class="num phase-val">${fmtVE(leftClose)}</div></div>
        <div><div class="kpi-label">${rightLabel} (cierre)</div><div class="num phase-val">${fmtVE(rightClose)}</div></div>
        <div><div class="kpi-label">Diferencia bruta</div><div class="num phase-val ${amtClass(f.raw_diff_cents)}">${fmtVE(f.raw_diff_cents)}</div></div>
        <div><div class="kpi-label">Explicada</div><div class="num phase-val amt-in">${fmtVE(f.explained_diff_cents)}</div></div>
        <div><div class="kpi-label">Saldo conciliado</div><div class="num phase-val" style="color:var(--emerald-700)">${fmtVE(concLeft)}${concRight !== null && concRight !== undefined && concRight !== concLeft ? ` / ${fmtVE(concRight)}` : ""}</div></div>
        <div><div class="kpi-label">Partidas con diferencia</div><div class="num phase-val">${items.length}${sysCount ? ` <span class="badge badge-sys">${sysCount} falla(s)</span>` : ""}</div></div>
      </div>
    </div>
  </div>`;
}

function renderResumen() {
  const c = $("#viewContainer");
  const ov = state.overview;
  if (!ov) {
    c.innerHTML = `<div class="card"><div class="empty-state">No hay período seleccionado.<br/>Usa «+ Cuenta» y «+ Período» en el panel izquierdo para crear el primero.</div></div>`;
    return;
  }
  const sh = ov.sheet, p = sh.period, s = sh.summary;
  const integ = sh.integrity;
  const f1 = sh.fase1, f2 = sh.fase2;

  const kpi = (cls, label, val, sub1, sub2) => `
    <div class="kpi-card ${cls}">
      <div class="kpi-label">${label}</div>
      <div class="kpi-value num">${fmtVE(val)}</div>
      <div class="kpi-sub"><span>${sub1}</span><span>${sub2}</span></div>
    </div>`;

  const schedRows = sh.rows.map((row) => {
    if (row.kind === "fase") {
      return `<tr class="row-fase"><td colspan="4">${esc(row.label)}</td></tr>`;
    }
    const cls = row.kind === "saldo" ? "row-subtotal" : row.kind === "subtotal" ? "row-subtotal" : row.kind === "total" ? "row-total" : "";
    const cell = (v) => v === null || v === undefined ? `<td class="right num amt-muted">—</td>` : `<td class="right num ${amtClass(v)}">${fmtVE(v)}</td>`;
    return `<tr class="${cls}"><td>${esc(row.label)}</td>${cell(row.banco_cents)}${cell(row.libros_cents)}${cell(row.mayor_cents)}</tr>`;
  }).join("");

  const bridge1Rows = (f1.items || []).map((bi) => `
    <tr>
      <td class="mono">${fmtDate(bi.date)}<br/>${esc(bi.reference)}</td>
      <td><span class="badge ${bi.kind === "FALLA_SISTEMA" || bi.kind === "FALLA_HISTORICA" ? "badge-sys" : "badge-warn"}">${kindLabel[bi.kind] || bi.kind}</span> ${esc(bi.description)}</td>
      <td class="right num">${fmtVE(bi.libros_cents)}</td>
      <td class="right num">${fmtVE(bi.mayor_cents)}</td>
      <td class="right num ${amtClass(bi.diff_libros_minus_mayor_cents)}">${fmtSigned(bi.diff_libros_minus_mayor_cents)}</td>
    </tr>`).join("");

  const bridge2Rows = (f2.items || []).map((bi) => `
    <tr>
      <td class="mono">${fmtDate(bi.date)}<br/>${esc(bi.reference)}</td>
      <td><span class="badge badge-warn">${kindLabel[bi.kind] || bi.kind}</span> ${esc(bi.description)}</td>
      <td class="right num">${fmtVE(bi.mayor_cents)}</td>
      <td class="right num">${fmtVE(bi.banco_cents)}</td>
      <td class="right num ${amtClass(bi.diff_mayor_minus_banco_cents)}">${fmtSigned(bi.diff_mayor_minus_banco_cents)}</td>
    </tr>`).join("");

  const groups = ov.groups || [];
  const concGroups = groups.filter((g) => g.match_type !== "REVERSO").length;
  const revGroups = groups.filter((g) => g.match_type === "REVERSO").length;
  const diffGroups = groups.filter((g) => g.match_type === "CON_DIFERENCIA").length;

  const integRow = (src, label) => {
    const i = integ[src];
    const ok = i.internal_diff_cents === 0;
    return `<tr>
      <td><strong>${label}</strong></td>
      <td class="right num">${i.count}</td>
      <td class="right num">${i.reconciled_count}</td>
      <td class="right num">${i.pending_count}</td>
      <td class="right num">${fmtVE(i.open_cents)}</td>
      <td class="right num amt-in">+${fmtVE(i.total_in_cents)}</td>
      <td class="right num amt-out">−${fmtVE(i.total_out_cents)}</td>
      <td class="right num">${fmtVE(i.calculated_close_cents)}</td>
      <td class="right num">${fmtVE(i.declared_close_cents)}</td>
      <td class="right num ${ok ? "amt-in" : "amt-neg"}">${ok ? "✓ Cuadra" : fmtVE(i.internal_diff_cents)}</td>
    </tr>`;
  };

  c.innerHTML = `
    <div class="kpi-grid">
      ${kpi("", "Banco del Tesoro — Saldo Final", sh.banco.close_cents, `Inicial: ${fmtVE(sh.banco.open_cents)}`, `Conciliado: ${fmtVE(sh.banco.conciliated_cents)}`)}
      ${kpi("libros", "Según Libros (Tesorería)", sh.libros.close_cents, `Inicial: ${fmtVE(sh.libros.open_cents)}`, `Conciliado: ${fmtVE(sh.libros.conciliated_cents)}`)}
      ${kpi("mayor", "Mayor Analítico (Contabilidad)", sh.use_mayor ? sh.mayor.close_cents : 0, sh.use_mayor ? `Inicial: ${fmtVE(sh.mayor.open_cents)}` : "No cargado", sh.use_mayor ? `Conciliado: ${fmtVE(sh.mayor.conciliated_cents)}` : "")}
      ${kpi(s.is_balanced ? "conciliated" : "conciliated warn", "Saldo Conciliado (Real)", sh.banco.conciliated_cents, s.is_balanced ? "✓ Sin diferencias" : `Dif. Banco−Libros: ${fmtVE(s.diff_banco_libros_cents)}`, s.unreviewed_duplicates_count ? `⚠ ${s.unreviewed_duplicates_count} posible(s) duplicado(s)` : `${concGroups} grupos conciliados`)}
    </div>

    ${s.is_balanced ? `<div class="notice ok">✓ <strong>Conciliación cuadrada en ambas fases.</strong> Los tres saldos conciliados coinciden en Bs. ${fmtVE(sh.banco.conciliated_cents)}. Puedes exportar el Acta PDF y el Excel para firmar.</div>`
      : `<div class="notice warn">⚠ <strong>Quedan diferencias por conciliar.</strong> Banco−Libros: <strong>Bs. ${fmtVE(s.diff_banco_libros_cents)}</strong>${sh.use_mayor ? ` · Banco−Mayor: <strong>Bs. ${fmtVE(s.diff_banco_mayor_cents)}</strong>` : ""}. Revisa las fases y la Mesa de Conciliación.</div>`}

    <div class="grid-2">
      ${phaseCard(1, "Según Libros ↔ Mayor Analítico", "Primero se concilian los dos registros internos de la empresa", f1, "Según Libros", "Mayor Analítico", f1.close_libros_cents, f1.close_mayor_cents, f1.conciliado_libros_cents, f1.conciliado_mayor_cents)}
      ${phaseCard(2, "Mayor Analítico ↔ Banco del Tesoro", "Luego se concilia Contabilidad contra el Estado de Cuenta", f2, "Mayor (conciliado F1)", "Banco del Tesoro", f2.close_mayor_cents, f2.close_banco_cents, f2.conciliado_mayor_cents, f2.conciliado_banco_cents)}
    </div>

    <div class="card">
      <div class="card-header"><h3 class="card-title">Hoja de Conciliación (Procedimiento en Dos Fases)</h3></div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Concepto</th><th class="right">Banco del Tesoro</th><th class="right">Según Libros</th><th class="right">Mayor Analítico</th></tr></thead>
        <tbody>${schedRows}</tbody>
      </table></div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-header"><h3 class="card-title">Fase 1 — Diferencias Exactas Libros vs. Mayor</h3></div>
        <div class="card-body">
          <p class="form-hint" style="margin-top:0">Diferencia bruta: <strong>Bs. ${fmtVE(f1.raw_diff_cents)}</strong> · Explicada: <strong>Bs. ${fmtVE(f1.explained_diff_cents)}</strong> · Sin explicar: <strong>Bs. ${fmtVE(f1.unexplained_diff_cents)}</strong></p>
          ${f1.items?.length ? `<div class="table-wrap"><table class="data-table">
            <thead><tr><th>Fecha / Ref.</th><th>Detalle</th><th class="right">Libros</th><th class="right">Mayor</th><th class="right">Dif. L−M</th></tr></thead>
            <tbody>${bridge1Rows}</tbody>
          </table></div>` : `<div class="empty-state">✓ Sin diferencias entre Según Libros y Mayor Analítico.</div>`}
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h3 class="card-title">Fase 2 — Diferencias Exactas Mayor vs. Banco</h3></div>
        <div class="card-body">
          <p class="form-hint" style="margin-top:0">Diferencia bruta: <strong>Bs. ${fmtVE(f2.raw_diff_cents)}</strong> · Explicada: <strong>Bs. ${fmtVE(f2.explained_diff_cents)}</strong> · Sin explicar: <strong>Bs. ${fmtVE(f2.unexplained_diff_cents)}</strong></p>
          ${f2.items?.length ? `<div class="table-wrap"><table class="data-table">
            <thead><tr><th>Fecha / Ref.</th><th>Detalle</th><th class="right">Mayor</th><th class="right">Banco</th><th class="right">Dif. M−B</th></tr></thead>
            <tbody>${bridge2Rows}</tbody>
          </table></div>` : `<div class="empty-state">✓ Sin diferencias entre Mayor Analítico y Banco del Tesoro.</div>`}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <h3 class="card-title">Integridad de Fuentes (Saldo Inicial + Movimientos = Saldo Final)</h3>
        <button class="btn btn-secondary btn-sm" id="btnRecalc">Recalcular cierres desde movimientos</button>
      </div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Fuente</th><th class="right">Movs.</th><th class="right">Concil.</th><th class="right">Pend.</th><th class="right">Saldo Inicial</th><th class="right">Entradas</th><th class="right">Salidas</th><th class="right">Cierre Calculado</th><th class="right">Cierre Declarado</th><th class="right">Chequeo</th></tr></thead>
        <tbody>
          ${integRow("BANCO", "Banco del Tesoro")}
          ${integRow("LIBROS", "Según Libros (Tesorería)")}
          ${integRow("MAYOR", "Mayor Analítico (Contabilidad)")}
        </tbody>
      </table></div>
    </div>

    <div class="card">
      <div class="card-header"><h3 class="card-title">Resumen del Cruce</h3></div>
      <div class="card-body" style="display:grid;grid-template-columns:1fr 1fr;gap:10px 18px">
        <div><span class="badge badge-ok">Conciliados</span><div class="num" style="font-size:20px;font-weight:700">${concGroups}</div></div>
        <div><span class="badge badge-warn">Con diferencia</span><div class="num" style="font-size:20px;font-weight:700">${diffGroups}</div></div>
        <div><span class="badge badge-neutral">Reversos</span><div class="num" style="font-size:20px;font-weight:700">${revGroups}</div></div>
        <div><span class="badge badge-err">Posibles duplicados</span><div class="num" style="font-size:20px;font-weight:700">${ov.suspected_duplicates?.length || 0}</div></div>
        <div><span class="badge badge-sys">Fallas de sistema</span><div class="num" style="font-size:20px;font-weight:700">${ov.system_issues?.filter((i) => i.status !== "RESUELTA").length || 0}</div></div>
        <div><span class="badge badge-neutral">Pendientes</span><div class="num" style="font-size:20px;font-weight:700">${ov.movements.filter((m) => m.status === "PENDIENTE").length}</div></div>
      </div>
    </div>

    ${p.notes ? `<div class="card"><div class="card-header"><h3 class="card-title">Notas del Período</h3></div><div class="card-body">${esc(p.notes)}</div></div>` : ""}

    <div class="card">
      <div class="card-header"><h3 class="card-title">Acciones del Período</h3></div>
      <div class="card-body" style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" id="btnEditBalances">Editar saldos y notas</button>
        <button class="btn btn-secondary btn-sm" id="btnRollover">Cerrar y abrir mes siguiente</button>
        <button class="btn btn-danger btn-sm" id="btnDeletePeriod">Eliminar período</button>
      </div>
    </div>`;

  $("#btnRecalc")?.addEventListener("click", async () => {
    await apiPost(`/api/periods/${state.periodId}/recalc-balances`);
    toast("Saldos de cierre recalculados desde los movimientos.");
    await loadOverview(); render();
  });
  $("#btnEditBalances")?.addEventListener("click", () => openBalancesModal());
  $("#btnRollover")?.addEventListener("click", async () => {
    if (!confirm("¿Cerrar este período y abrir el mes siguiente arrastrando tránsito y fallas del sistema?")) return;
    const r = await apiPost(`/api/periods/${state.periodId}/rollover`);
    toast(`Mes siguiente abierto. Arrastre: ${r.carried.transit_items} tránsito(s), ${r.carried.system_issues} falla(s).`);
    await loadContext();
  });
  $("#btnDeletePeriod")?.addEventListener("click", async () => {
    if (!confirm("¿Eliminar este período y TODOS sus movimientos? Esta acción no se puede deshacer.")) return;
    await apiDelete(`/api/periods/${state.periodId}`);
    toast("Período eliminado.");
    state.periodId = null;
    await loadContext();
  });
}

/* ============ VIEW: IMPORTAR ============ */
function renderImportar() {
  const c = $("#viewContainer");
  const ov = state.overview;
  const counts = { BANCO: 0, LIBROS: 0, MAYOR: 0 };
  if (ov) ov.movements.forEach((m) => counts[m.source]++);
  const card = (src, name, sub) => `
    <button class="source-card ${state.importSource === src ? "active" : ""}" data-src="${src}">
      <div class="source-card-top"><span class="source-card-name">${name}</span>${state.importSource === src ? '<span class="badge badge-ok">Seleccionado</span>' : ""}</div>
      <div class="source-card-sub">${sub}</div>
      <div class="source-card-stat"><span>Movimientos cargados</span><strong class="num">${counts[src]}</strong></div>
    </button>`;

  const pv = state.importPreview;
  const rows = state.importRows;
  const pvHtml = pv ? `
    <div class="card">
      <div class="card-header">
        <div>
          <h3 class="card-title">Vista Previa de Extracción — ${esc(srcLabel[state.importSource])}</h3>
          <p class="card-subtitle">${esc(state.importFileName)} · Método: ${esc(pv.method || "—")} · Formato numérico: ${esc(pv.detected_format || "—")}</p>
        </div>
        <span class="badge ${rows.length ? "badge-ok" : "badge-warn"}">${rows.length} fila(s) lista(s)</span>
      </div>
      <div class="card-body">
        ${(pv.warnings || []).map((w) => `<div class="notice warn">⚠ ${esc(w)}</div>`).join("")}
        <div class="grid-3" style="margin-bottom:12px">
          <div><div class="kpi-label">Entradas</div><div class="num" style="font-weight:700;color:var(--emerald-700)">+${fmtVE(pv.total_in_cents)}</div></div>
          <div><div class="kpi-label">Salidas</div><div class="num" style="font-weight:700">−${fmtVE(pv.total_out_cents)}</div></div>
          <div><div class="kpi-label">Neto</div><div class="num" style="font-weight:700">${fmtSigned(pv.net_cents)}</div></div>
        </div>
        <div class="form-grid cols-3" style="margin-bottom:12px">
          <div class="form-group"><label>Saldo Inicial detectado (Bs.)</label><input class="input num" id="pvOpen" value="${pv.opening_balance_cents !== null && pv.opening_balance_cents !== undefined ? fmtVE(pv.opening_balance_cents) : ""}" placeholder="0,00"></div>
          <div class="form-group"><label>Saldo Final detectado (Bs.)</label><input class="input num" id="pvClose" value="${pv.closing_balance_cents !== null && pv.closing_balance_cents !== undefined ? fmtVE(pv.closing_balance_cents) : ""}" placeholder="0,00"></div>
          <div class="form-group"><label>Cuenta detectada</label><input class="input" id="pvAcct" value="${esc(pv.detected_account || "")}" placeholder="—"></div>
        </div>
        <div class="table-wrap" style="max-height:340px;overflow-y:auto"><table class="data-table">
          <thead><tr><th>Fecha</th><th>Referencia</th><th>Descripción</th><th>Categoría</th><th class="right">Monto (Bs.)</th><th>Dir.</th><th></th></tr></thead>
          <tbody>
            ${rows.map((r, i) => `<tr>
              <td class="mono">${fmtDate(r.tx_date)}</td>
              <td class="mono">${esc(r.reference)}</td>
              <td>${esc(r.description)}</td>
              <td>${esc(r.category || "")}</td>
              <td class="right num ${r.direction === "IN" ? "amt-in" : "amt-out"}">${fmtVE(r.amount_cents)}</td>
              <td>${r.direction === "IN" ? "Entrada" : "Salida"}</td>
              <td><button class="wb-mini-btn" data-delrow="${i}">Quitar</button></td>
            </tr>`).join("")}
          </tbody>
        </table></div>
        <div class="form-grid" style="margin-top:14px">
          <label class="form-hint"><input type="checkbox" id="optReplace" ${state.importReplace ? "checked" : ""}> Reemplazar movimientos existentes de esta fuente</label>
          <label class="form-hint"><input type="checkbox" id="optBalances" ${state.importUpdateBalances ? "checked" : ""}> Actualizar saldos inicial/final del período</label>
          <label class="form-hint"><input type="checkbox" id="optAuto" ${state.importAutoReconcile ? "checked" : ""}> Ejecutar autoconciliación al importar</label>
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="btn btn-primary" id="btnCommitImport">Importar ${rows.length} movimiento(s) a ${esc(srcLabel[state.importSource])}</button>
          <button class="btn btn-secondary" id="btnDiscardImport">Descartar vista previa</button>
        </div>
      </div>
    </div>` : "";

  c.innerHTML = `
    <div class="notice info">Sube el <strong>PDF digital</strong> (no escaneado) del Estado de Cuenta del Banco del Tesoro, del reporte <strong>Según Libros (Tesorería)</strong> y del <strong>Mayor Analítico (Contabilidad)</strong>. La app extrae fecha, referencia, descripción, montos y saldos automáticamente. Revisa la vista previa antes de importar.</div>

    <div class="source-cards">
      ${card("BANCO", "Banco del Tesoro", "Estado de Cuenta (Débito / Crédito)")}
      ${card("LIBROS", "Según Libros — Tesorería", "Reporte de Tesorería (Debe / Haber)")}
      ${card("MAYOR", "Mayor Analítico — Contabilidad", "Mayor de cuenta (Debe / Haber)")}
    </div>

    <div class="card">
      <div class="card-header"><h3 class="card-title">Cargar ${esc(srcLabel[state.importSource])}</h3></div>
      <div class="card-body">
        <div class="form-grid cols-3" style="margin-bottom:12px">
          <div class="form-group"><label>Formato numérico</label>
            <select class="select" id="impFmt">
              <option value="auto" ${state.importNumberFormat === "auto" ? "selected" : ""}>Automático (recomendado)</option>
              <option value="ve" ${state.importNumberFormat === "ve" ? "selected" : ""}>Venezolano 1.234,56</option>
              <option value="us" ${state.importNumberFormat === "us" ? "selected" : ""}>Sistema 1,234.56</option>
            </select></div>
          <div class="form-group"><label>Año por defecto (si el PDF no trae año)</label><input class="input num" id="impYear" type="number" value="${state.importDefaultYear}"></div>
          <div class="form-group"><label>Archivo</label><input class="input" type="file" id="impFile" accept=".pdf,.xlsx,.xlsm,.csv,.txt,.tsv"></div>
        </div>
        <div class="dropzone" id="dropzone">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="1.6"><path d="M12 16V5m0 0l-4 4m4-4l4 4" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round"/></svg>
          <div class="dropzone-title">Arrastra aquí el PDF o haz clic para elegirlo</div>
          <div class="dropzone-sub">PDF digital, Excel (.xlsx) o CSV/TXT · Máx. 25 MB</div>
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="btn btn-secondary btn-sm" id="btnToggleText">${state.showImportText ? "Ocultar carga por texto" : "¿El PDF es escaneado? Pegar texto copiado"}</button>
          <button class="btn btn-secondary btn-sm" id="btnAddManual">+ Movimiento manual</button>
          <button class="btn btn-danger btn-sm" id="btnClearSource">Vaciar fuente ${esc(srcLabel[state.importSource])}</button>
        </div>
        ${state.showImportText ? `
        <div style="margin-top:14px">
          <label class="form-hint">Pega el texto copiado del PDF (una fila por movimiento: fecha, referencia, descripción, montos y saldo). También acepta filas separadas por ; o tab.</label>
          <textarea class="textarea" id="pasteArea" rows="8" placeholder="06/10/2026  009812445502  ND PAGO DIRECTO PROVEEDOR TECNOSISTEMAS  200.300,00  649.700,00">${esc(state.importPasteText)}</textarea>
          <button class="btn btn-primary btn-sm" id="btnParseText" style="margin-top:8px">Analizar texto pegado</button>
        </div>` : ""}
      </div>
    </div>

    ${pvHtml}

    <div class="card">
      <div class="card-header"><h3 class="card-title">Archivos PDF de Ejemplo (Octubre 2026)</h3><span class="badge badge-neutral">Listos para descargar y re-subir</span></div>
      <div class="card-body" style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" data-sample="01_Estado_Cuenta_Banco_del_Tesoro_Oct2026.pdf">Descargar Estado de Cuenta BT</button>
        <button class="btn btn-secondary btn-sm" data-sample="02_Segun_Libros_Tesoreria_Oct2026.pdf">Descargar Según Libros</button>
        <button class="btn btn-secondary btn-sm" data-sample="03_Mayor_Analitico_Contabilidad_Oct2026.pdf">Descargar Mayor Analítico</button>
      </div>
    </div>`;

  $$(".source-card", c).forEach((el) => el.addEventListener("click", () => {
    state.importSource = el.dataset.src;
    state.importPreview = null; state.importRows = [];
    render();
  }));

  const dz = $("#dropzone");
  const fileInput = $("#impFile");
  dz.addEventListener("click", () => fileInput.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
    const f = e.dataTransfer.files?.[0];
    if (f) uploadImportFile(f);
  });
  fileInput.addEventListener("change", () => { const f = fileInput.files?.[0]; if (f) uploadImportFile(f); });

  $("#impFmt")?.addEventListener("change", (e) => { state.importNumberFormat = e.target.value; });
  $("#impYear")?.addEventListener("change", (e) => { state.importDefaultYear = parseInt(e.target.value, 10) || 2026; });
  $("#btnToggleText")?.addEventListener("click", () => { state.showImportText = !state.showImportText; render(); });
  $("#btnAddManual")?.addEventListener("click", () => openManualMovementModal());
  $("#btnClearSource")?.addEventListener("click", async () => {
    if (!confirm(`¿Vaciar TODOS los movimientos de ${srcLabel[state.importSource]} en este período?`)) return;
    await apiDelete(`/api/periods/${state.periodId}/sources/${state.importSource}`);
    toast("Fuente vaciada.");
    await loadOverview(); render();
  });
  $("#btnParseText")?.addEventListener("click", async () => {
    const txt = $("#pasteArea").value;
    state.importPasteText = txt;
    if (!txt.trim()) return toast("Pega primero el texto del PDF.", true);
    try {
      const res = await apiPost("/api/import/preview-text", { text: txt, source: state.importSource, number_format: state.importNumberFormat, default_year: state.importDefaultYear });
      applyPreview(res, "Texto pegado desde PDF");
    } catch (e) { toast(e.message, true); }
  });

  $$("[data-sample]", c).forEach((b) => b.addEventListener("click", async () => {
    try { await downloadBlob(`/api/samples/${b.dataset.sample}`, b.dataset.sample); }
    catch (e) { toast(e.message, true); }
  }));

  $$("[data-delrow]", c).forEach((b) => b.addEventListener("click", () => {
    state.importRows.splice(parseInt(b.dataset.delrow, 10), 1);
    render();
  }));
  $("#btnDiscardImport")?.addEventListener("click", () => { state.importPreview = null; state.importRows = []; render(); });
  $("#btnCommitImport")?.addEventListener("click", commitImport);
}

async function uploadImportFile(file) {
  if (!state.periodId) return toast("Selecciona un período primero.", true);
  const fd = new FormData();
  fd.append("file", file);
  fd.append("source", state.importSource);
  fd.append("number_format", state.importNumberFormat);
  fd.append("default_year", String(state.importDefaultYear));
  toast("Extrayendo movimientos del archivo…");
  try {
    const headers = {};
    const pin = getPin();
    if (pin) headers["X-App-Pin"] = pin;
    let res = await fetch("/api/import/preview-file", { method: "POST", body: fd, headers });
    if (res.status === 401) {
      const ok = await promptPin();
      if (!ok) throw new Error("Acceso denegado: PIN requerido.");
      headers["X-App-Pin"] = getPin();
      res = await fetch("/api/import/preview-file", { method: "POST", body: fd, headers });
    }
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Error al leer el archivo");
    applyPreview(data, file.name);
  } catch (e) { toast(e.message, true); }
}

function applyPreview(res, filename) {
  state.importPreview = res;
  state.importFileName = filename;
  state.importRows = (res.rows || []).map((r) => ({ ...r }));
  if (!state.importRows.length) toast("No se detectaron filas. Revisa las advertencias y el formato.", true);
  else toast(`${state.importRows.length} movimiento(s) detectado(s). Revisa y confirma.`);
  render();
}

async function commitImport() {
  const pv = state.importPreview;
  const rows = state.importRows;
  if (!rows.length) return toast("No hay filas para importar.", true);
  const parseVE = (s) => {
    if (s === null || s === undefined || String(s).trim() === "") return null;
    const t = String(s).replace(/\./g, "").replace(",", ".");
    const n = parseFloat(t);
    return isNaN(n) ? null : Math.round(n * 100);
  };
  const payload = {
    source: state.importSource,
    origin_file: state.importFileName,
    replace_source: $("#optReplace")?.checked || false,
    update_balances: $("#optBalances")?.checked ?? true,
    auto_reconcile: $("#optAuto")?.checked ?? true,
    opening_balance_cents: parseVE($("#pvOpen")?.value),
    closing_balance_cents: parseVE($("#pvClose")?.value),
    rows: rows.map((r) => ({
      tx_date: r.tx_date, reference: r.reference || "", description: r.description,
      category: r.category || null, direction: r.direction, amount_cents: r.amount_cents,
      running_balance_cents: r.running_balance_cents ?? null,
    })),
  };
  try {
    const res = await apiPost(`/api/periods/${state.periodId}/import-commit`, payload);
    toast(`Importados ${res.inserted} movimiento(s). Autoconciliación ejecutada.`);
    state.importPreview = null; state.importRows = [];
    await loadOverview();
    state.view = "mesa";
    render();
  } catch (e) { toast(e.message, true); }
}

/* ============ VIEW: MESA ============ */
function renderMesa() {
  const c = $("#viewContainer");
  const ov = state.overview;
  if (!ov) { c.innerHTML = `<div class="card"><div class="empty-state">Sin período seleccionado.</div></div>`; return; }
  const tab = state.mesaTab;
  const tabBtn = (id, label) => `<button class="subtab ${tab === id ? "active" : ""}" data-mesatab="${id}">${label}</button>`;

  let body = "";
  if (tab === "pendientes") body = mesaPendientesHtml(ov);
  else if (tab === "grupos") body = mesaGruposHtml(ov);
  else if (tab === "todos") body = mesaTodosHtml(ov);

  c.innerHTML = `
    <div class="toolbar">
      <div class="subtabs">${tabBtn("pendientes", "Pendientes por conciliar")}${tabBtn("grupos", "Grupos conciliados")}${tabBtn("todos", "Todos los movimientos")}</div>
      <div class="toolbar-group">
        <input class="input input-sm" id="mesaFilter" placeholder="Filtrar por descripción o referencia…" value="${esc(state.mesaFilter)}" style="width:230px">
        <button class="btn btn-primary btn-sm" id="btnAuto2">Autoconciliar (Fase 1 + Fase 2)</button>
        <button class="btn btn-secondary btn-sm" id="btnAutoReset">Reiniciar + Autoconciliar</button>
        <button class="btn btn-secondary btn-sm" id="btnAddMov2">+ Movimiento manual</button>
      </div>
    </div>
    ${body}`;

  $$("[data-mesatab]", c).forEach((b) => b.addEventListener("click", () => { state.mesaTab = b.dataset.mesatab; render(); }));
  $("#mesaFilter")?.addEventListener("input", (e) => { state.mesaFilter = e.target.value; renderMesaKeepFocus(); });
  $("#btnAuto2")?.addEventListener("click", () => doAutoReconcile(false));
  $("#btnAutoReset")?.addEventListener("click", () => { if (confirm("¿Reiniciar cruces automáticos y volver a conciliar? (Se conservan duplicados, reversos y fallas confirmadas)")) doAutoReconcile(true); });
  $("#btnAddMov2")?.addEventListener("click", () => openManualMovementModal());
  bindMesaEvents(c);
  updateMatchBar();
}

function renderMesaKeepFocus() {
  const v = state.mesaFilter;
  renderMesa();
  const el = $("#mesaFilter");
  if (el) { el.focus(); el.setSelectionRange(v.length, v.length); }
}

async function doAutoReconcile(reset) {
  toast("Ejecutando conciliación en dos fases…");
  try {
    const r = await apiPost(`/api/periods/${state.periodId}/auto-reconcile`, { reset_existing: reset });
    const st = r.stats || {};
    toast(`Fase 1 (Libros↔Mayor): ${st.fase1_exact || 0} exacto(s), ${st.fase1_diffs || 0} con diferencia · Fase 2 (Mayor↔Banco): ${st.fase2_exact || 0} exacto(s), ${st.fase2_diffs || 0} con diferencia · Reversos: ${st.reversals || 0}.`);
    await loadOverview(); render();
  } catch (e) { toast(e.message, true); }
}

function movRowHtml(m, suspectIds) {
  const sel = state.selectedMovIds.has(m.id);
  const susp = suspectIds.has(m.id);
  return `<div class="wb-row ${sel ? "selected" : ""} ${susp ? "suspect-dup" : ""}" data-movid="${m.id}">
    <input type="checkbox" data-sel="${m.id}" ${sel ? "checked" : ""} aria-label="Seleccionar">
    <div class="wb-row-body">
      <div class="wb-row-top">
        <span class="wb-date-ref">${fmtDate(m.tx_date)} · ${esc(m.reference || "s/ref")}</span>
        <span class="wb-amt num ${m.direction === "IN" ? "amt-in" : "amt-out"}">${m.direction === "IN" ? "+" : "−"}${fmtVE(m.amount_cents)}</span>
      </div>
      <div class="wb-desc">${esc(m.description)}</div>
      <div class="wb-row-foot">
        ${statusBadge(m.status)}
        <span style="display:flex;gap:4px">
          <button class="wb-mini-btn" data-editmov="${m.id}">Editar</button>
          <button class="wb-mini-btn" data-delmov="${m.id}">Eliminar</button>
        </span>
      </div>
    </div>
  </div>`;
}

function mesaPendientesHtml(ov) {
  const suspectIds = new Set((ov.suspected_duplicates || []).map((d) => d.suspect.id));
  const f = state.mesaFilter.toLowerCase();
  const match = (m) => !f || (m.description || "").toLowerCase().includes(f) || (m.reference || "").toLowerCase().includes(f);
  const cols = ["BANCO", "LIBROS", "MAYOR"].filter((s) => s !== "MAYOR" || ov.sheet.use_mayor);
  const colHtml = (src) => {
    const list = ov.movements.filter((m) => m.source === src && m.status === "PENDIENTE" && match(m));
    const sum = list.reduce((a, m) => a + m.signed_cents, 0);
    return `<div class="wb-col ${src.toLowerCase()}">
      <div class="wb-col-hdr"><span class="wb-col-title">${srcLabel[src]}</span><span class="wb-col-count">${list.length} pendiente(s) · Neto ${fmtSigned(sum)}</span></div>
      <div class="wb-list">${list.length ? list.map((m) => movRowHtml(m, suspectIds)).join("") : `<div class="empty-state">✓ Sin pendientes en esta fuente</div>`}</div>
    </div>`;
  };
  return `<div class="workbench-grid ${cols.length === 2 ? "two-cols" : ""}">${cols.map(colHtml).join("")}</div>
    <p class="form-hint" style="margin-top:10px">Marca con ✓ los movimientos que pertenecen a la misma operación (en las 3 columnas) y pulsa «Conciliar Seleccionados». Si los montos difieren, la app los concilia con diferencia y clasifica la causa (céntimos, error de dígito, transposición…). Las filas rosadas son posibles duplicados.</p>`;
}

function mesaGruposHtml(ov) {
  const groups = ov.groups || [];
  if (!groups.length) return `<div class="card"><div class="empty-state">Aún no hay grupos conciliados. Ejecuta «Autoconciliar» o concilia manualmente desde Pendientes.</div></div>`;
  const rows = groups.map((g) => {
    const mem = (src) => g.members.filter((m) => m.source === src);
    const cell = (src) => {
      const ms = mem(src);
      if (!ms.length) return `<span class="amt-muted">—</span>`;
      const sum = ms.reduce((a, m) => a + m.signed_cents, 0);
      return `<span class="num ${amtClass(sum)}">${fmtVE(sum)}</span><div class="mono" style="color:var(--slate-500)">${ms.map((m) => esc(m.reference)).join(" · ")}</div>`;
    };
    const diffBL = g.diff_banco_libros_cents;
    const diffLM = g.diff_libros_mayor_cents;
    const f1done = g.has_libros && g.has_mayor;
    const f2done = g.has_mayor && g.has_banco;
    const phaseBadge = (done, label) => done
      ? `<span class="badge badge-ok">${label} ✓</span>`
      : `<span class="badge badge-neutral">${label} —</span>`;
    return `<tr>
      <td class="mono">#${g.id}</td>
      <td><span class="badge ${g.match_type === "CON_DIFERENCIA" ? "badge-warn" : g.match_type === "REVERSO" ? "badge-neutral" : "badge-ok"}">${matchTypeLabel[g.match_type] || g.match_type}</span></td>
      <td style="white-space:nowrap">${phaseBadge(f1done, "F1")}<br/>${phaseBadge(f2done, "F2")}</td>
      <td>${esc(g.members[0]?.description || "")}<div class="form-hint">${esc(g.note || "")}</div></td>
      <td class="right">${cell("BANCO")}</td>
      <td class="right">${cell("LIBROS")}</td>
      <td class="right">${cell("MAYOR")}</td>
      <td class="right num ${diffBL === null ? "amt-muted" : amtClass(diffBL)}">${diffBL === null ? "—" : fmtVE(diffBL)}</td>
      <td class="right num ${diffLM === null ? "amt-muted" : amtClass(diffLM)}">${diffLM === null ? "—" : fmtVE(diffLM)}</td>
      <td style="white-space:nowrap">
        <button class="wb-mini-btn" data-editgroup="${g.id}">Editar</button>
        <button class="wb-mini-btn" data-delgroup="${g.id}">Deshacer</button>
      </td>
    </tr>`;
  }).join("");
  return `<div class="card"><div class="table-wrap"><table class="data-table">
    <thead><tr><th>#</th><th>Tipo</th><th>Fases</th><th>Concepto / Nota</th><th class="right">Banco</th><th class="right">Libros</th><th class="right">Mayor</th><th class="right">Dif. B−L</th><th class="right">Dif. L−M</th><th>Acciones</th></tr></thead>
    <tbody>${rows}</tbody></table></div></div>`;
}

function mesaTodosHtml(ov) {
  const f = state.mesaFilter.toLowerCase();
  const list = ov.movements.filter((m) => !f || (m.description || "").toLowerCase().includes(f) || (m.reference || "").toLowerCase().includes(f) || m.source.toLowerCase().includes(f));
  const rows = list.map((m) => `<tr>
    <td class="mono">${m.id}</td>
    <td><span class="badge badge-neutral">${srcShort[m.source]}</span></td>
    <td class="mono">${fmtDate(m.tx_date)}</td>
    <td class="mono">${esc(m.reference)}</td>
    <td>${esc(m.description)}<div class="form-hint">${esc(m.category || "")}${m.is_carryover ? " · [Tránsito mes ant.]" : ""}</div></td>
    <td class="right num ${m.direction === "IN" ? "amt-in" : "amt-out"}">${m.direction === "IN" ? "+" : "−"}${fmtVE(m.amount_cents)}</td>
    <td>${statusBadge(m.status)}</td>
    <td class="mono">${m.match_group_id ? "#" + m.match_group_id : "—"}</td>
    <td style="white-space:nowrap"><button class="wb-mini-btn" data-editmov="${m.id}">Editar</button> <button class="wb-mini-btn" data-delmov="${m.id}">Eliminar</button></td>
  </tr>`).join("");
  return `<div class="card"><div class="table-wrap"><table class="data-table">
    <thead><tr><th>ID</th><th>Fuente</th><th>Fecha</th><th>Referencia</th><th>Descripción / Categoría</th><th class="right">Monto (Bs.)</th><th>Estado</th><th>Grupo</th><th>Acciones</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="9"><div class="empty-state">Sin movimientos.</div></td></tr>`}</tbody></table></div></div>`;
}

function bindMesaEvents(root) {
  $$(".wb-row", root).forEach((row) => {
    row.addEventListener("click", (e) => {
      if (e.target.closest("button") || e.target.closest("input")) return;
      const id = parseInt(row.dataset.movid, 10);
      state.selectedMovIds.has(id) ? state.selectedMovIds.delete(id) : state.selectedMovIds.add(id);
      row.classList.toggle("selected", state.selectedMovIds.has(id));
      const cb = row.querySelector("input[data-sel]");
      if (cb) cb.checked = state.selectedMovIds.has(id);
      updateMatchBar();
    });
  });
  $$("[data-sel]", root).forEach((cb) => cb.addEventListener("change", () => {
    const id = parseInt(cb.dataset.sel, 10);
    cb.checked ? state.selectedMovIds.add(id) : state.selectedMovIds.delete(id);
    cb.closest(".wb-row")?.classList.toggle("selected", cb.checked);
    updateMatchBar();
  }));
  $$("[data-editmov]", root).forEach((b) => b.addEventListener("click", () => openEditMovementModal(parseInt(b.dataset.editmov, 10))));
  $$("[data-delmov]", root).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Eliminar este movimiento?")) return;
    await apiDelete(`/api/movements/${b.dataset.delmov}`);
    toast("Movimiento eliminado.");
    await loadOverview(); render();
  }));
  $$("[data-editgroup]", root).forEach((b) => b.addEventListener("click", () => openEditGroupModal(parseInt(b.dataset.editgroup, 10))));
  $$("[data-delgroup]", root).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Deshacer este grupo de conciliación? Los movimientos volverán a Pendiente.")) return;
    await apiDelete(`/api/match-groups/${b.dataset.delgroup}`);
    toast("Grupo deshecho.");
    await loadOverview(); render();
  }));
}

function updateMatchBar() {
  const bar = $("#matchBar");
  const ov = state.overview;
  if (!ov || !state.selectedMovIds.size) { bar.hidden = true; return; }
  const sel = ov.movements.filter((m) => state.selectedMovIds.has(m.id));
  const bySrc = { BANCO: 0, LIBROS: 0, MAYOR: 0 };
  const cnt = { BANCO: 0, LIBROS: 0, MAYOR: 0 };
  sel.forEach((m) => { bySrc[m.source] += m.signed_cents; cnt[m.source]++; });
  const vals = Object.values(bySrc).filter((v) => v !== 0);
  const exact = vals.length > 1 && vals.every((v) => v === vals[0]);
  const pill = (src) => cnt[src] ? `<span class="mb-pill ${exact ? "exact" : "diff"}">${srcShort[src]}: ${fmtSigned(bySrc[src])} (${cnt[src]})</span>` : "";
  $("#matchBarSums").innerHTML = `<strong>${sel.length} seleccionado(s)</strong>${pill("BANCO")}${pill("LIBROS")}${pill("MAYOR")}${exact ? '<span class="mb-pill exact">✓ Montos iguales</span>' : '<span class="mb-pill diff">Montos difieren → se conciliará con diferencia</span>'}`;
  bar.hidden = false;
}

$("#btnClearSelection").addEventListener("click", () => { state.selectedMovIds.clear(); render(); });
$("#btnConfirmMatch").addEventListener("click", async () => {
  const ids = [...state.selectedMovIds];
  if (ids.length < 2) return toast("Selecciona al menos 2 movimientos.", true);
  const ov = state.overview;
  const sel = ov.movements.filter((m) => state.selectedMovIds.has(m.id));
  const sources = [...new Set(sel.map((m) => m.source))];
  const sameSource = sources.length === 1;
  const net = sel.reduce((a, m) => a + m.signed_cents, 0);
  const isReversal = sameSource && net === 0;
  openModal("Confirmar conciliación manual", `
    <p class="form-hint">Se conciliarán <strong>${sel.length} movimiento(s)</strong> de ${sources.map((s) => srcLabel[s]).join(", ")}.</p>
    <div class="table-wrap"><table class="data-table"><tbody>
      ${sel.map((m) => `<tr><td><span class="badge badge-neutral">${srcShort[m.source]}</span></td><td class="mono">${fmtDate(m.tx_date)} ${esc(m.reference)}</td><td>${esc(m.description)}</td><td class="right num">${fmtSigned(m.signed_cents)}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="form-grid" style="margin-top:12px">
      <div class="form-group"><label>Tipo de conciliación</label>
        <select class="select" id="mmType">
          <option value="normal" ${isReversal ? "" : "selected"}>Normal (entre fuentes)</option>
          <option value="reversal" ${isReversal ? "selected" : ""}>Reverso / Anulación (misma fuente, neto 0)</option>
        </select></div>
      <div class="form-group"><label>Fuente canónica (monto de referencia)</label>
        <select class="select" id="mmCanon">${sources.map((s) => `<option value="${s}">${srcLabel[s]}</option>`).join("")}</select></div>
      <div class="form-group"><label>Si hay diferencia, tratar como</label>
        <select class="select" id="mmRes">
          <option value="AJUSTE">Ajuste contable (céntimos / diferencia menor)</option>
          <option value="FALLA_SISTEMA">Falla del sistema (error de dígito / transposición)</option>
          <option value="ERROR_BANCO">Error del banco</option>
        </select></div>
      <div class="form-group"><label>Nota</label><input class="input" id="mmNote" placeholder="Ej. Conciliado manualmente por OP-312"></div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" id="mmCancel">Cancelar</button>
      <button class="btn btn-primary" id="mmOk">Conciliar</button>
    </div>`);
  $("#mmCancel").addEventListener("click", closeModal);
  $("#mmOk").addEventListener("click", async () => {
    const payload = {
      movement_ids: ids,
      match_as_reversal: $("#mmType").value === "reversal",
      canonical_source: $("#mmCanon").value,
      resolution_type: $("#mmRes").value,
      note: $("#mmNote").value,
    };
    try {
      const r = await apiPost(`/api/periods/${state.periodId}/manual-match`, payload);
      toast(`Conciliado (grupo #${r.group_id}, ${r.match_type}).`);
      closeModal();
      await loadOverview(); render();
    } catch (e) { toast(e.message, true); }
  });
});

/* ============ VIEW: ALERTAS ============ */
function renderAlertas() {
  const c = $("#viewContainer");
  const ov = state.overview;
  if (!ov) { c.innerHTML = `<div class="card"><div class="empty-state">Sin período seleccionado.</div></div>`; return; }
  const tab = state.alertasTab;
  const tabBtn = (id, label) => `<button class="subtab ${tab === id ? "active" : ""}" data-alertatab="${id}">${label}</button>`;

  let body = "";
  if (tab === "duplicados") {
    const dups = ov.suspected_duplicates || [];
    body = dups.length ? `<div class="finding-list">${dups.map((d) => `
      <div class="finding-item dup">
        <div class="finding-main">
          <div class="finding-title"><span class="badge badge-err">Posible duplicado</span> ${esc(srcLabel[d.source])} · Bs. ${fmtVE(d.suspect.amount_cents)}</div>
          <div class="finding-desc"><strong>Sospechoso:</strong> ${fmtDate(d.suspect.tx_date)} · ${esc(d.suspect.reference)} · ${esc(d.suspect.description)}<br/>
          <strong>Original:</strong> ${fmtDate(d.original.tx_date)} · ${esc(d.original.reference)} · ${esc(d.original.description)}<br/>
          ${esc(d.reason)} · ${d.days_apart} día(s) de diferencia</div>
        </div>
        <div class="finding-actions">
          <button class="btn btn-danger btn-sm" data-confirmdup="${d.suspect.id}">Confirmar duplicado</button>
          <button class="btn btn-secondary btn-sm" data-keepdup="${d.suspect.id}">No es duplicado (dejar pendiente)</button>
        </div>
      </div>`).join("")}</div>`
      : `<div class="card"><div class="empty-state">✓ No se detectaron posibles duplicados en este período.</div></div>`;
  } else if (tab === "fallas") {
    const issues = ov.system_issues || [];
    const rows = issues.map((i) => `<tr class="row-sys">
      <td class="mono">${esc(i.code)}</td>
      <td class="mono">${fmtDate(i.issue_date)}</td>
      <td><span class="badge badge-sys">${esc(i.affected_module)}</span></td>
      <td>${esc(i.title)}<div class="form-hint">${esc(i.audit_note || "")}</div></td>
      <td class="right num">${i.banco_cents !== null && i.banco_cents !== undefined ? fmtVE(i.banco_cents) : "—"}</td>
      <td class="right num">${i.libros_cents !== null && i.libros_cents !== undefined ? fmtVE(i.libros_cents) : "—"}</td>
      <td class="right num">${i.mayor_cents !== null && i.mayor_cents !== undefined ? fmtVE(i.mayor_cents) : "—"}</td>
      <td class="right num ${amtClass(i.adj_libros_cents)}">${fmtSigned(i.adj_libros_cents)}</td>
      <td class="right num ${amtClass(i.adj_mayor_cents)}">${fmtSigned(i.adj_mayor_cents)}</td>
      <td>${i.carry_forward ? "Sí" : "No"}</td>
      <td>${statusBadge(i.status === "RESUELTA" ? "CONCILIADO" : "FALLA_SISTEMA")}</td>
      <td style="white-space:nowrap"><button class="wb-mini-btn" data-editissue="${i.id}">Editar</button> <button class="wb-mini-btn" data-delissue="${i.id}">Eliminar</button></td>
    </tr>`).join("");
    body = `<div class="card">
      <div class="card-header"><h3 class="card-title">Fallas del Sistema Documentadas</h3><button class="btn btn-indigo btn-sm" id="btnNewIssue">+ Registrar falla histórica</button></div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Código</th><th>Fecha</th><th>Módulo</th><th>Descripción / Soporte</th><th class="right">Banco</th><th class="right">Libros</th><th class="right">Mayor</th><th class="right">Ajuste Libros</th><th class="right">Ajuste Mayor</th><th>Arrastra</th><th>Estado</th><th>Acciones</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="12"><div class="empty-state">Sin fallas registradas. Las diferencias Libros↔Mayor por error de dígito se registran aquí automáticamente.</div></td></tr>`}</tbody>
      </table></div></div>`;
  } else if (tab === "diferencias") {
    const f1 = ov.sheet.fase1, f2 = ov.sheet.fase2;
    const itemRow = (bi, phase, diffKey, leftKey, rightKey) => `<tr>
      <td><span class="badge ${phase === 1 ? "badge-neutral" : "badge-warn"}">Fase ${phase}</span></td>
      <td class="mono">${fmtDate(bi.date)}<br/>${esc(bi.reference)}</td>
      <td><span class="badge ${bi.kind === "FALLA_SISTEMA" || bi.kind === "FALLA_HISTORICA" ? "badge-sys" : "badge-warn"}">${kindLabel[bi.kind] || bi.kind}</span> ${esc(bi.description)}</td>
      <td class="right num">${fmtVE(bi[leftKey])}</td>
      <td class="right num">${fmtVE(bi[rightKey])}</td>
      <td class="right num ${amtClass(bi[diffKey])}">${fmtSigned(bi[diffKey])}</td>
    </tr>`;
    const rows1 = (f1.items || []).map((bi) => itemRow(bi, 1, "diff_libros_minus_mayor_cents", "libros_cents", "mayor_cents")).join("");
    const rows2 = (f2.items || []).map((bi) => itemRow(bi, 2, "diff_mayor_minus_banco_cents", "mayor_cents", "banco_cents")).join("");
    body = `<div class="card"><div class="table-wrap"><table class="data-table">
      <thead><tr><th>Fase</th><th>Fecha / Ref.</th><th>Diferencia exacta encontrada</th><th class="right">Izq.</th><th class="right">Der.</th><th class="right">Impacto</th></tr></thead>
      <tbody>${rows1}${rows2 || `<tr><td colspan="6"><div class="empty-state">✓ Sin diferencias en Fase 2.</div></td></tr>`}</tbody>
    </table></div></div>
    <p class="form-hint">Fase 1: Libros vs. Mayor (izq. = Según Libros, der. = Mayor Analítico). Fase 2: Mayor vs. Banco (izq. = Mayor Analítico, der. = Banco del Tesoro).</p>`;
  } else {
    const diffs = (ov.groups || []).filter((g) => g.match_type === "CON_DIFERENCIA");
    body = diffs.length ? `<div class="finding-list">${diffs.map((g) => `
      <div class="finding-item warn">
        <div class="finding-main">
          <div class="finding-title"><span class="badge badge-warn">Con diferencia</span> Grupo #${g.id} · ${esc(g.discrepancy_kind || "")} · ${esc(g.resolution_type || "")}</div>
          <div class="finding-desc">${esc(g.members[0]?.description || "")}<br/>${esc(g.note || "")}<br/>
          Banco: <strong class="num">${g.sum_banco_cents !== null ? fmtVE(g.sum_banco_cents) : "—"}</strong> · Libros: <strong class="num">${g.sum_libros_cents !== null ? fmtVE(g.sum_libros_cents) : "—"}</strong> · Mayor: <strong class="num">${g.sum_mayor_cents !== null ? fmtVE(g.sum_mayor_cents) : "—"}</strong></div>
        </div>
        <div class="finding-actions"><button class="btn btn-secondary btn-sm" data-editgroup="${g.id}">Revisar / editar</button></div>
      </div>`).join("")}</div>`
      : `<div class="card"><div class="empty-state">✓ No hay grupos con diferencia de monto.</div></div>`;
  }

  c.innerHTML = `
    <div class="toolbar"><div class="subtabs">
      ${tabBtn("diferencias", `Diferencias exactas (${(ov.sheet.fase1.items?.length || 0) + (ov.sheet.fase2.items?.length || 0)})`)}
      ${tabBtn("duplicados", `Posibles duplicados (${ov.suspected_duplicates?.length || 0})`)}
      ${tabBtn("fallas", `Fallas del sistema (${ov.system_issues?.filter((i) => i.status !== "RESUELTA").length || 0})`)}
      ${tabBtn("grupos", `Grupos con diferencia (${(ov.groups || []).filter((g) => g.match_type === "CON_DIFERENCIA").length})`)}
    </div></div>
    ${body}`;

  $$("[data-alertatab]", c).forEach((b) => b.addEventListener("click", () => { state.alertasTab = b.dataset.alertatab; render(); }));
  $$("[data-confirmdup]", c).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Confirmar como DUPLICADO? Se generará el ajuste de reverso correspondiente.")) return;
    await apiPut(`/api/movements/${b.dataset.confirmdup}`, { status: "DUPLICADO" });
    toast("Marcado como duplicado. Ajuste de reverso generado.");
    await loadOverview(); render();
  }));
  $$("[data-keepdup]", c).forEach((b) => b.addEventListener("click", () => toast("Se mantiene pendiente: la diferencia seguirá visible hasta que lo concilies o lo marques.")));
  $$("[data-editgroup]", c).forEach((b) => b.addEventListener("click", () => openEditGroupModal(parseInt(b.dataset.editgroup, 10))));
  $("#btnNewIssue")?.addEventListener("click", () => openSystemIssueModal());
  $$("[data-editissue]", c).forEach((b) => b.addEventListener("click", () => openSystemIssueModal(parseInt(b.dataset.editissue, 10))));
  $$("[data-delissue]", c).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Eliminar este registro de falla del sistema?")) return;
    await apiDelete(`/api/system-issues/${b.dataset.delissue}`);
    toast("Falla eliminada.");
    await loadOverview(); render();
  }));
}

/* ============ VIEW: HOJA Y AJUSTES ============ */
function renderHoja() {
  const c = $("#viewContainer");
  const ov = state.overview;
  if (!ov) { c.innerHTML = `<div class="card"><div class="empty-state">Sin período seleccionado.</div></div>`; return; }
  const tab = state.hojaTab;
  const tabBtn = (id, label) => `<button class="subtab ${tab === id ? "active" : ""}" data-hojatab="${id}">${label}</button>`;
  const sh = ov.sheet;

  let body = "";
  if (tab === "hoja") {
    const schedRows = sh.rows.map((row) => {
      if (row.kind === "fase") return `<tr class="row-fase"><td colspan="4">${esc(row.label)}</td></tr>`;
      const cls = row.kind === "saldo" ? "row-subtotal" : row.kind === "subtotal" ? "row-subtotal" : row.kind === "total" ? "row-total" : "";
      const cell = (v) => v === null || v === undefined ? `<td class="right num amt-muted">—</td>` : `<td class="right num ${amtClass(v)}">${fmtVE(v)}</td>`;
      return `<tr class="${cls}"><td>${esc(row.label)}</td>${cell(row.banco_cents)}${cell(row.libros_cents)}${cell(row.mayor_cents)}</tr>`;
    }).join("");
    body = `<div class="card"><div class="table-wrap"><table class="data-table">
      <thead><tr><th>Concepto de Conciliación (Dos Fases)</th><th class="right">Banco del Tesoro</th><th class="right">Según Libros</th><th class="right">Mayor Analítico</th></tr></thead>
      <tbody>${schedRows}</tbody></table></div></div>`;
  } else if (tab === "ajustes") {
    const adjs = ov.adjustments || [];
    const rows = adjs.map((a) => `<tr>
      <td class="mono">${fmtDate(a.adj_date)}</td>
      <td><span class="badge ${a.target === "AUDITORIA_SISTEMA" ? "badge-sys" : a.target === "RECLAMO_BANCO" ? "badge-err" : "badge-neutral"}">${targetLabel[a.target] || a.target}</span></td>
      <td class="mono">${esc(a.reference)}</td>
      <td>${esc(a.concept)}<div class="form-hint">${esc(a.note || "")}</div></td>
      <td>${esc(a.debit_account)}</td>
      <td>${esc(a.credit_account)}</td>
      <td class="right num">${fmtVE(a.amount_cents)}</td>
      <td>${a.applied ? '<span class="badge badge-ok">Aplicado</span>' : '<span class="badge badge-warn">Pendiente</span>'}</td>
      <td style="white-space:nowrap">
        <button class="wb-mini-btn" data-toggleadj="${a.id}">${a.applied ? "Desmarcar" : "Marcar aplicado"}</button>
        ${a.is_manual ? `<button class="wb-mini-btn" data-deladj="${a.id}">Eliminar</button>` : ""}
      </td>
    </tr>`).join("");
    const total = adjs.reduce((a, x) => a + x.amount_cents, 0);
    body = `<div class="card">
      <div class="card-header"><h3 class="card-title">Ajustes Contables y de Tesorería Sugeridos</h3><button class="btn btn-primary btn-sm" id="btnNewAdj">+ Ajuste manual</button></div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Fecha</th><th>Aplicar En</th><th>Referencia</th><th>Concepto</th><th>Cuenta DEBE</th><th>Cuenta HABER</th><th class="right">Monto (Bs.)</th><th>Estado</th><th>Acciones</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="9"><div class="empty-state">Sin ajustes. Se generan automáticamente desde los cargos bancarios, duplicados y diferencias.</div></td></tr>`}
        ${adjs.length ? `<tr class="row-total"><td colspan="6">TOTAL AJUSTES</td><td class="right num">${fmtVE(total)}</td><td colspan="2"></td></tr>` : ""}
        </tbody></table></div></div>`;
  } else {
    const pend = ov.movements.filter((m) => m.match_group_id === null);
    const rows = pend.map((m) => `<tr>
      <td><span class="badge badge-neutral">${srcShort[m.source]}</span></td>
      <td class="mono">${fmtDate(m.tx_date)}</td>
      <td class="mono">${esc(m.reference)}</td>
      <td>${esc(m.description)}</td>
      <td class="right num ${amtClass(m.signed_cents)}">${fmtSigned(m.signed_cents)}</td>
      <td>${statusBadge(m.status)}</td>
    </tr>`).join("");
    body = `<div class="card"><div class="table-wrap"><table class="data-table">
      <thead><tr><th>Fuente</th><th>Fecha</th><th>Referencia</th><th>Descripción</th><th class="right">Monto (Bs.)</th><th>Estado</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="6"><div class="empty-state">✓ Sin partidas pendientes: todo está conciliado o clasificado.</div></td></tr>`}</tbody></table></div></div>`;
  }

  c.innerHTML = `
    <div class="toolbar">
      <div class="subtabs">${tabBtn("hoja", "Hoja de Conciliación")}${tabBtn("ajustes", `Ajustes (${ov.adjustments?.filter((a) => !a.applied).length || 0} pendientes)`)}${tabBtn("pendientes", "Partidas Pendientes")}</div>
      <div class="toolbar-group">
        <button class="btn btn-secondary btn-sm" id="btnExcel2">Descargar Excel</button>
        <button class="btn btn-primary btn-sm" id="btnPdf2">Descargar Acta PDF</button>
      </div>
    </div>
    ${body}`;

  $$("[data-hojatab]", c).forEach((b) => b.addEventListener("click", () => { state.hojaTab = b.dataset.hojatab; render(); }));
  $("#btnExcel2")?.addEventListener("click", () => downloadExport("excel"));
  $("#btnPdf2")?.addEventListener("click", () => downloadExport("pdf"));
  $("#btnNewAdj")?.addEventListener("click", () => openAdjustmentModal());
  $$("[data-toggleadj]", c).forEach((b) => b.addEventListener("click", async () => {
    const a = ov.adjustments.find((x) => x.id === parseInt(b.dataset.toggleadj, 10));
    await apiPut(`/api/adjustments/${a.id}`, { applied: !a.applied });
    toast(a.applied ? "Ajuste desmarcado." : "Ajuste marcado como aplicado.");
    await loadOverview(); render();
  }));
  $$("[data-deladj]", c).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Eliminar este ajuste manual?")) return;
    await apiDelete(`/api/adjustments/${b.dataset.deladj}`);
    toast("Ajuste eliminado.");
    await loadOverview(); render();
  }));
}

async function downloadExport(kind) {
  if (!state.periodId) return;
  try {
    await downloadBlob(
      `/api/periods/${state.periodId}/export/${kind}`,
      kind === "excel" ? "Conciliacion_BT.xlsx" : "Acta_Conciliacion_BT.pdf"
    );
  } catch (e) { toast(e.message, true); }
}

/* ============ VIEW: CONFIG ============ */
function renderConfig() {
  const c = $("#viewContainer");
  const s = state.settings || {};
  const accRows = state.accounts.map((a) => `<tr>
    <td><strong>${esc(a.name)}</strong><div class="form-hint">${esc(a.bank_name)} · ${esc(a.account_type)} · ${esc(a.currency)}</div></td>
    <td class="mono">${esc(a.account_number)}</td>
    <td class="mono">${esc(a.ledger_code)}</td>
    <td class="right num">${a.period_count ?? ""}</td>
    <td style="white-space:nowrap"><button class="wb-mini-btn" data-editacc="${a.id}">Editar</button> <button class="wb-mini-btn" data-delacc="${a.id}">Eliminar</button></td>
  </tr>`).join("");
  const perRows = state.periods.map((p) => `<tr>
    <td>${esc(p.label)}</td>
    <td>${esc(state.accounts.find((a) => a.id === p.account_id)?.name || "")}</td>
    <td>${statusBadge(p.status === "CERRADO" ? "CONCILIADO" : "PENDIENTE")}</td>
    <td class="right num">${fmtVE(p.close_banco_cents)}</td>
    <td style="white-space:nowrap"><button class="wb-mini-btn" data-goperiod="${p.id}">Abrir</button> <button class="wb-mini-btn" data-delperiod="${p.id}">Eliminar</button></td>
  </tr>`).join("");

  c.innerHTML = `
    <div class="grid-2">
      <div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">Cuentas Bancarias</h3><button class="btn btn-primary btn-sm" id="btnNewAcc2">+ Nueva cuenta</button></div>
          <div class="table-wrap"><table class="data-table">
            <thead><tr><th>Cuenta</th><th>Número</th><th>Código Contable</th><th class="right">Períodos</th><th>Acciones</th></tr></thead>
            <tbody>${accRows}</tbody></table></div>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">Períodos</h3></div>
          <div class="table-wrap"><table class="data-table">
            <thead><tr><th>Período</th><th>Cuenta</th><th>Estado</th><th class="right">Cierre Banco</th><th>Acciones</th></tr></thead>
            <tbody>${perRows}</tbody></table></div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">Datos de la Empresa y Firmas (Acta PDF)</h3></div>
          <div class="card-body">
            <div class="form-grid">
              <div class="form-group full"><label>Nombre de la empresa</label><input class="input" id="setCompany" value="${esc(s.company_name || "")}"></div>
              <div class="form-group"><label>RIF</label><input class="input" id="setRif" value="${esc(s.company_rif || "")}"></div>
              <div class="form-group"><label>Ventana de fechas para cruzar (días)</label><input class="input num" id="setWindow" type="number" value="${esc(s.date_window_days || "5")}"></div>
              <div class="form-group"><label>Elaborado por</label><input class="input" id="setPrep" value="${esc(s.prepared_by || "")}"></div>
              <div class="form-group"><label>Revisado por</label><input class="input" id="setRev" value="${esc(s.reviewed_by || "")}"></div>
              <div class="form-group"><label>Aprobado por</label><input class="input" id="setAppr" value="${esc(s.approved_by || "")}"></div>
            </div>
            <button class="btn btn-primary btn-sm" id="btnSaveSettings" style="margin-top:12px">Guardar datos</button>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">Cuentas Contables de Ajuste</h3></div>
          <div class="card-body">
            <div class="form-grid">
              <div class="form-group full"><label>Comisiones y gastos bancarios</label><input class="input" id="setAcctComm" value="${esc(s.acct_commissions || "")}"></div>
              <div class="form-group full"><label>IGTF</label><input class="input" id="setAcctIgtf" value="${esc(s.acct_igtf || "")}"></div>
              <div class="form-group full"><label>Intereses / ingresos financieros</label><input class="input" id="setAcctInt" value="${esc(s.acct_interest || "")}"></div>
              <div class="form-group full"><label>Diferencias y ajustes en conciliación</label><input class="input" id="setAcctRound" value="${esc(s.acct_rounding || "")}"></div>
              <div class="form-group full"><label>Partidas por aclarar / diferencias de sistema</label><input class="input" id="setAcctSusp" value="${esc(s.acct_suspense || "")}"></div>
            </div>
            <button class="btn btn-primary btn-sm" id="btnSaveSettings2" style="margin-top:12px">Guardar cuentas</button>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><h3 class="card-title">Datos de Demostración</h3></div>
          <div class="card-body">
            <p class="form-hint" style="margin-top:0">Restaura el período de ejemplo de Octubre 2026 con la diferencia de Bs. 1.000,00 (201.300 vs 200.300), céntimos, duplicado, reverso, comisiones y tránsito.</p>
            <button class="btn btn-secondary btn-sm" id="btnResetDemo">Restablecer datos de ejemplo</button>
          </div>
        </div>
      </div>
    </div>`;

  const save = async () => {
    const payload = {
      company_name: $("#setCompany").value, company_rif: $("#setRif").value,
      date_window_days: $("#setWindow").value, prepared_by: $("#setPrep").value,
      reviewed_by: $("#setRev").value, approved_by: $("#setAppr").value,
      acct_commissions: $("#setAcctComm").value, acct_igtf: $("#setAcctIgtf").value,
      acct_interest: $("#setAcctInt").value, acct_rounding: $("#setAcctRound").value,
      acct_suspense: $("#setAcctSusp").value,
    };
    state.settings = await apiPut("/api/settings", payload);
    toast("Datos guardados.");
  };
  $("#btnSaveSettings").addEventListener("click", save);
  $("#btnSaveSettings2").addEventListener("click", save);
  $("#btnNewAcc2").addEventListener("click", () => openAccountModal());
  $$("[data-editacc]", c).forEach((b) => b.addEventListener("click", () => openAccountModal(parseInt(b.dataset.editacc, 10))));
  $$("[data-delacc]", c).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Eliminar esta cuenta y todos sus períodos?")) return;
    await apiDelete(`/api/accounts/${b.dataset.delacc}`);
    toast("Cuenta eliminada.");
    state.accountId = null; state.periodId = null;
    await loadContext();
  }));
  $$("[data-goperiod]", c).forEach((b) => b.addEventListener("click", async () => {
    const p = state.periods.find((x) => x.id === parseInt(b.dataset.goperiod, 10));
    state.accountId = p.account_id; state.periodId = p.id;
    await loadContext();
  }));
  $$("[data-delperiod]", c).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("¿Eliminar este período y todos sus movimientos?")) return;
    await apiDelete(`/api/periods/${b.dataset.delperiod}`);
    toast("Período eliminado.");
    state.periodId = null;
    await loadContext();
  }));
  $("#btnResetDemo").addEventListener("click", async () => {
    if (!confirm("¿Restablecer los datos de ejemplo? Se recreará la cuenta demo de Octubre 2026.")) return;
    await apiPost("/api/demo/reset");
    toast("Datos de ejemplo restablecidos.");
    state.accountId = null; state.periodId = null;
    await loadContext();
  });
}

/* ============ MODALS ============ */
function openAccountModal(id) {
  const a = id ? state.accounts.find((x) => x.id === id) : null;
  openModal(a ? "Editar cuenta" : "Nueva cuenta bancaria", `
    <div class="form-grid">
      <div class="form-group full"><label>Nombre de la cuenta</label><input class="input" id="accName" value="${esc(a?.name || "")}" placeholder="Cuenta Operativa Principal BT"></div>
      <div class="form-group"><label>Banco</label><input class="input" id="accBank" value="${esc(a?.bank_name || "Banco del Tesoro")}"></div>
      <div class="form-group"><label>Tipo</label><input class="input" id="accType" value="${esc(a?.account_type || "Corriente")}"></div>
      <div class="form-group full"><label>Número de cuenta</label><input class="input mono" id="accNum" value="${esc(a?.account_number || "")}" placeholder="0163-0900-12-0010458921"></div>
      <div class="form-group"><label>Código contable</label><input class="input mono" id="accLedger" value="${esc(a?.ledger_code || "1.1.01.02.001")}"></div>
      <div class="form-group"><label>Moneda</label><input class="input" id="accCur" value="${esc(a?.currency || "Bs.")}"></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="accCancel">Cancelar</button><button class="btn btn-primary" id="accOk">Guardar</button></div>`);
  $("#accCancel").addEventListener("click", closeModal);
  $("#accOk").addEventListener("click", async () => {
    const payload = { name: $("#accName").value, bank_name: $("#accBank").value, account_type: $("#accType").value, account_number: $("#accNum").value, ledger_code: $("#accLedger").value, currency: $("#accCur").value };
    if (!payload.name.trim() || !payload.account_number.trim()) return toast("Nombre y número de cuenta son obligatorios.", true);
    try {
      if (a) await apiPut(`/api/accounts/${a.id}`, payload);
      else { const n = await apiPost("/api/accounts", payload); state.accountId = n.id; }
      toast("Cuenta guardada.");
      closeModal();
      await loadContext();
    } catch (e) { toast(e.message, true); }
  });
}

function openPeriodModal() {
  if (!state.accountId) return toast("Crea primero una cuenta.", true);
  const now = new Date();
  openModal("Nuevo período mensual", `
    <div class="form-grid">
      <div class="form-group"><label>Mes</label><select class="select" id="perMonth">${["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"].map((m, i) => `<option value="${i + 1}" ${i + 1 === now.getMonth() + 1 ? "selected" : ""}>${m}</option>`).join("")}</select></div>
      <div class="form-group"><label>Año</label><input class="input num" id="perYear" type="number" value="${now.getFullYear()}"></div>
      <div class="form-group full"><label>Saldo inicial Banco del Tesoro (Bs.)</label><input class="input num" id="perOB" placeholder="0,00"></div>
      <div class="form-group"><label>Saldo inicial Según Libros (Bs.)</label><input class="input num" id="perOL" placeholder="0,00"></div>
      <div class="form-group"><label>Saldo inicial Mayor Analítico (Bs.)</label><input class="input num" id="perOM" placeholder="0,00"></div>
      <label class="form-hint full"><input type="checkbox" id="perCarry" checked> Arrastrar saldos de cierre del período anterior + tránsito y fallas del sistema</label>
      <label class="form-hint full"><input type="checkbox" id="perMayor" checked> Usar Mayor Analítico (triple conciliación)</label>
      <div class="form-group full"><label>Notas</label><textarea class="textarea" id="perNotes" rows="2"></textarea></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="perCancel">Cancelar</button><button class="btn btn-primary" id="perOk">Crear período</button></div>`);
  $("#perCancel").addEventListener("click", closeModal);
  $("#perOk").addEventListener("click", async () => {
    const payload = {
      account_id: state.accountId, month: parseInt($("#perMonth").value, 10), year: parseInt($("#perYear").value, 10),
      use_mayor: $("#perMayor").checked, carry_from_previous: $("#perCarry").checked,
      open_banco: $("#perOB").value || "0,00", open_libros: $("#perOL").value || "0,00", open_mayor: $("#perOM").value || "0,00",
      notes: $("#perNotes").value,
    };
    try {
      const r = await apiPost("/api/periods", payload);
      state.periodId = r.period.id;
      toast(`Período ${r.period.label} creado. Arrastre: ${r.carried.transit_items} tránsito(s), ${r.carried.system_issues} falla(s).`);
      closeModal();
      await loadContext();
    } catch (e) { toast(e.message, true); }
  });
}

function openBalancesModal() {
  const p = state.overview.sheet.period;
  openModal("Editar saldos y notas del período", `
    <div class="form-grid">
      <div class="form-group"><label>Saldo inicial Banco (Bs.)</label><input class="input num" id="balOB" value="${fmtVE(p.open_banco_cents)}"></div>
      <div class="form-group"><label>Saldo final Banco (Bs.)</label><input class="input num" id="balCB" value="${fmtVE(p.close_banco_cents)}"></div>
      <div class="form-group"><label>Saldo inicial Libros (Bs.)</label><input class="input num" id="balOL" value="${fmtVE(p.open_libros_cents)}"></div>
      <div class="form-group"><label>Saldo final Libros (Bs.)</label><input class="input num" id="balCL" value="${fmtVE(p.close_libros_cents)}"></div>
      <div class="form-group"><label>Saldo inicial Mayor (Bs.)</label><input class="input num" id="balOM" value="${fmtVE(p.open_mayor_cents)}"></div>
      <div class="form-group"><label>Saldo final Mayor (Bs.)</label><input class="input num" id="balCM" value="${fmtVE(p.close_mayor_cents)}"></div>
      <div class="form-group full"><label>Notas del período</label><textarea class="textarea" id="balNotes" rows="3">${esc(p.notes || "")}</textarea></div>
    </div>
    <p class="form-hint">Escribe los montos en formato venezolano (1.234,56). Los negativos con signo −.</p>
    <div class="modal-footer"><button class="btn btn-secondary" id="balCancel">Cancelar</button><button class="btn btn-primary" id="balOk">Guardar</button></div>`);
  const parseVE = (s) => {
    const t = String(s).replace(/\./g, "").replace(",", ".");
    const n = parseFloat(t);
    return isNaN(n) ? 0 : Math.round(n * 100);
  };
  $("#balCancel").addEventListener("click", closeModal);
  $("#balOk").addEventListener("click", async () => {
    await apiPut(`/api/periods/${state.periodId}`, {
      open_banco_cents: parseVE($("#balOB").value), close_banco_cents: parseVE($("#balCB").value),
      open_libros_cents: parseVE($("#balOL").value), close_libros_cents: parseVE($("#balCL").value),
      open_mayor_cents: parseVE($("#balOM").value), close_mayor_cents: parseVE($("#balCM").value),
      notes: $("#balNotes").value,
    });
    toast("Saldos actualizados.");
    closeModal();
    await loadOverview(); render();
  });
}

function openManualMovementModal() {
  if (!state.periodId) return toast("Selecciona un período primero.", true);
  openModal(`Movimiento manual — ${srcLabel[state.importSource]}`, `
    <div class="form-grid">
      <div class="form-group"><label>Fecha (DD/MM/AAAA)</label><input class="input" id="mvDate" placeholder="15/10/2026"></div>
      <div class="form-group"><label>Referencia</label><input class="input" id="mvRef" placeholder="OP-370"></div>
      <div class="form-group full"><label>Descripción</label><input class="input" id="mvDesc" placeholder="Pago proveedor…"></div>
      <div class="form-group"><label>Dirección</label><select class="select" id="mvDir"><option value="OUT">Salida (pago / débito)</option><option value="IN">Entrada (cobro / abono)</option></select></div>
      <div class="form-group"><label>Monto (Bs.)</label><input class="input num" id="mvAmt" placeholder="1.234,56"></div>
      <div class="form-group full"><label>Nota</label><input class="input" id="mvNote"></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="mvCancel">Cancelar</button><button class="btn btn-primary" id="mvOk">Agregar</button></div>`);
  $("#mvCancel").addEventListener("click", closeModal);
  $("#mvOk").addEventListener("click", async () => {
    const t = String($("#mvAmt").value).replace(/\./g, "").replace(",", ".");
    const amt = Math.round(parseFloat(t) * 100);
    if (!$("#mvDesc").value.trim() || isNaN(amt) || amt <= 0) return toast("Descripción y monto válido son obligatorios.", true);
    try {
      await apiPost(`/api/periods/${state.periodId}/movements`, {
        source: state.importSource, tx_date: $("#mvDate").value || "01/01/2026",
        reference: $("#mvRef").value, description: $("#mvDesc").value,
        direction: $("#mvDir").value, amount_cents: amt, note: $("#mvNote").value,
      });
      toast("Movimiento agregado.");
      closeModal();
      await loadOverview(); render();
    } catch (e) { toast(e.message, true); }
  });
}

function openEditMovementModal(id) {
  const m = state.overview.movements.find((x) => x.id === id);
  if (!m) return;
  openModal(`Editar movimiento #${m.id} — ${srcLabel[m.source]}`, `
    <div class="form-grid">
      <div class="form-group"><label>Fecha (AAAA-MM-DD)</label><input class="input" id="emDate" value="${esc(m.tx_date)}"></div>
      <div class="form-group"><label>Referencia</label><input class="input" id="emRef" value="${esc(m.reference)}"></div>
      <div class="form-group full"><label>Descripción</label><input class="input" id="emDesc" value="${esc(m.description)}"></div>
      <div class="form-group"><label>Dirección</label><select class="select" id="emDir"><option value="OUT" ${m.direction === "OUT" ? "selected" : ""}>Salida</option><option value="IN" ${m.direction === "IN" ? "selected" : ""}>Entrada</option></select></div>
      <div class="form-group"><label>Monto (Bs.)</label><input class="input num" id="emAmt" value="${fmtVE(m.amount_cents)}"></div>
      <div class="form-group"><label>Estado</label><select class="select" id="emStatus">${Object.keys(statusLabel).map((k) => `<option value="${k}" ${m.status === k ? "selected" : ""}>${statusLabel[k]}</option>`).join("")}</select></div>
      <div class="form-group full"><label>Nota</label><input class="input" id="emNote" value="${esc(m.note || "")}"></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="emCancel">Cancelar</button><button class="btn btn-primary" id="emOk">Guardar</button></div>`);
  $("#emCancel").addEventListener("click", closeModal);
  $("#emOk").addEventListener("click", async () => {
    const t = String($("#emAmt").value).replace(/\./g, "").replace(",", ".");
    const amt = Math.round(parseFloat(t) * 100);
    try {
      await apiPut(`/api/movements/${m.id}`, {
        tx_date: $("#emDate").value, reference: $("#emRef").value, description: $("#emDesc").value,
        direction: $("#emDir").value, amount_cents: amt, status: $("#emStatus").value, note: $("#emNote").value,
      });
      toast("Movimiento actualizado.");
      closeModal();
      await loadOverview(); render();
    } catch (e) { toast(e.message, true); }
  });
}

function openEditGroupModal(id) {
  const g = (state.overview.groups || []).find((x) => x.id === id);
  if (!g) return;
  const sources = [...new Set(g.members.map((m) => m.source))];
  openModal(`Editar grupo #${g.id}`, `
    <p class="form-hint">Tipo actual: <strong>${matchTypeLabel[g.match_type] || g.match_type}</strong> · Canónica: ${esc(g.canonical_source)} · Bs. ${fmtVE(g.canonical_cents)}</p>
    <div class="form-grid">
      <div class="form-group"><label>Fuente canónica</label><select class="select" id="egCanon">${sources.map((s) => `<option value="${s}" ${g.canonical_source === s ? "selected" : ""}>${srcLabel[s]}</option>`).join("")}</select></div>
      <div class="form-group"><label>Tratamiento de la diferencia</label><select class="select" id="egRes">
        <option value="NINGUNA" ${g.resolution_type === "NINGUNA" ? "selected" : ""}>Ninguna (exacto)</option>
        <option value="AJUSTE" ${g.resolution_type === "AJUSTE" ? "selected" : ""}>Ajuste contable</option>
        <option value="FALLA_SISTEMA" ${g.resolution_type === "FALLA_SISTEMA" ? "selected" : ""}>Falla del sistema</option>
        <option value="ERROR_BANCO" ${g.resolution_type === "ERROR_BANCO" ? "selected" : ""}>Error del banco</option>
      </select></div>
      <div class="form-group full"><label>Nota</label><textarea class="textarea" id="egNote" rows="2">${esc(g.note || "")}</textarea></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="egCancel">Cancelar</button><button class="btn btn-primary" id="egOk">Guardar</button></div>`);
  $("#egCancel").addEventListener("click", closeModal);
  $("#egOk").addEventListener("click", async () => {
    await apiPut(`/api/match-groups/${g.id}`, { canonical_source: $("#egCanon").value, resolution_type: $("#egRes").value, note: $("#egNote").value });
    toast("Grupo actualizado.");
    closeModal();
    await loadOverview(); render();
  });
}

function openSystemIssueModal(id) {
  const i = id ? state.overview.system_issues.find((x) => x.id === id) : null;
  openModal(i ? "Editar falla del sistema" : "Registrar falla del sistema", `
    <div class="form-grid">
      <div class="form-group"><label>Código</label><input class="input" id="siCode" value="${esc(i?.code || "SIS-HIST")}" placeholder="OP-312"></div>
      <div class="form-group"><label>Fecha origen (AAAA-MM-DD)</label><input class="input" id="siDate" value="${esc(i?.issue_date || "")}" placeholder="2026-10-06"></div>
      <div class="form-group full"><label>Título / descripción</label><input class="input" id="siTitle" value="${esc(i?.title || "")}" placeholder="Diferencia histórica entre Según Libros y Mayor Analítico"></div>
      <div class="form-group"><label>Módulo afectado</label><select class="select" id="siMod">
        ${["LIBROS", "MAYOR", "AMBOS", "BANCO"].map((x) => `<option value="${x}" ${i?.affected_module === x ? "selected" : ""}>${x === "LIBROS" ? "Según Libros" : x === "MAYOR" ? "Mayor Analítico" : x === "AMBOS" ? "Ambos" : "Banco"}</option>`).join("")}
      </select></div>
      <div class="form-group"><label>Estado</label><select class="select" id="siStatus">
        <option value="ACTIVA" ${i?.status === "ACTIVA" ? "selected" : ""}>Activa</option>
        <option value="RESUELTA" ${i?.status === "RESUELTA" ? "selected" : ""}>Resuelta</option>
      </select></div>
      <div class="form-group"><label>Monto Banco (Bs.)</label><input class="input num" id="siB" value="${i?.banco_cents != null ? fmtVE(i.banco_cents) : ""}" placeholder="vacío si no aplica"></div>
      <div class="form-group"><label>Monto Libros (Bs.)</label><input class="input num" id="siL" value="${i?.libros_cents != null ? fmtVE(i.libros_cents) : ""}"></div>
      <div class="form-group"><label>Monto Mayor (Bs.)</label><input class="input num" id="siM" value="${i?.mayor_cents != null ? fmtVE(i.mayor_cents) : ""}"></div>
      <div class="form-group"><label>Ajuste Libros (Bs.)</label><input class="input num" id="siAL" value="${fmtVE(i?.adj_libros_cents || 0)}"></div>
      <div class="form-group"><label>Ajuste Mayor (Bs.)</label><input class="input num" id="siAM" value="${fmtVE(i?.adj_mayor_cents || 0)}"></div>
      <label class="form-hint"><input type="checkbox" id="siOpen" ${i?.affects_opening ? "checked" : ""}> Afecta saldo inicial (histórica)</label>
      <label class="form-hint"><input type="checkbox" id="siCarry" ${i ? (i.carry_forward ? "checked" : "") : "checked"}> Arrastrar al mes siguiente</label>
      <div class="form-group full"><label>Nota técnica / soporte de auditoría</label><textarea class="textarea" id="siNote" rows="2">${esc(i?.audit_note || "")}</textarea></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="siCancel">Cancelar</button><button class="btn btn-primary" id="siOk">Guardar</button></div>`);
  const parseVE = (s) => {
    if (s === null || s === undefined || String(s).trim() === "") return null;
    const t = String(s).replace(/\./g, "").replace(",", ".");
    const n = parseFloat(t);
    return isNaN(n) ? null : Math.round(n * 100);
  };
  $("#siCancel").addEventListener("click", closeModal);
  $("#siOk").addEventListener("click", async () => {
    const payload = {
      code: $("#siCode").value, issue_date: $("#siDate").value || "2026-10-01", title: $("#siTitle").value,
      affected_module: $("#siMod").value, status: $("#siStatus").value,
      banco_cents: parseVE($("#siB").value), libros_cents: parseVE($("#siL").value), mayor_cents: parseVE($("#siM").value),
      adj_libros_cents: parseVE($("#siAL").value) || 0, adj_mayor_cents: parseVE($("#siAM").value) || 0,
      affects_opening: $("#siOpen").checked, carry_forward: $("#siCarry").checked, audit_note: $("#siNote").value,
    };
    if (!payload.title.trim()) return toast("El título es obligatorio.", true);
    try {
      if (i) await apiPut(`/api/system-issues/${i.id}`, payload);
      else await apiPost(`/api/periods/${state.periodId}/system-issues`, payload);
      toast("Falla guardada.");
      closeModal();
      await loadOverview(); render();
    } catch (e) { toast(e.message, true); }
  });
}

function openAdjustmentModal() {
  const s = state.settings || {};
  openModal("Nuevo ajuste manual", `
    <div class="form-grid">
      <div class="form-group"><label>Fecha (AAAA-MM-DD)</label><input class="input" id="ajDate" placeholder="2026-10-31"></div>
      <div class="form-group"><label>Aplicar en</label><select class="select" id="ajTarget">
        ${Object.entries(targetLabel).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
      </select></div>
      <div class="form-group"><label>Referencia</label><input class="input" id="ajRef" placeholder="OP-370"></div>
      <div class="form-group"><label>Monto (Bs.)</label><input class="input num" id="ajAmt" placeholder="1.234,56"></div>
      <div class="form-group full"><label>Concepto</label><input class="input" id="ajConcept" placeholder="Ajuste por…"></div>
      <div class="form-group full"><label>Cuenta DEBE</label><input class="input" id="ajDebit" value="${esc(s.acct_rounding || "")}"></div>
      <div class="form-group full"><label>Cuenta HABER</label><input class="input" id="ajCredit" value="${esc(s.acct_suspense || "")}"></div>
      <div class="form-group full"><label>Nota</label><input class="input" id="ajNote"></div>
    </div>
    <div class="modal-footer"><button class="btn btn-secondary" id="ajCancel">Cancelar</button><button class="btn btn-primary" id="ajOk">Guardar ajuste</button></div>`);
  $("#ajCancel").addEventListener("click", closeModal);
  $("#ajOk").addEventListener("click", async () => {
    const t = String($("#ajAmt").value).replace(/\./g, "").replace(",", ".");
    const amt = Math.round(parseFloat(t) * 100);
    if (!$("#ajConcept").value.trim() || isNaN(amt) || amt <= 0) return toast("Concepto y monto válido son obligatorios.", true);
    try {
      await apiPost(`/api/periods/${state.periodId}/adjustments`, {
        adj_date: $("#ajDate").value || "2026-10-31", target: $("#ajTarget").value, reference: $("#ajRef").value,
        concept: $("#ajConcept").value, debit_account: $("#ajDebit").value, credit_account: $("#ajCredit").value,
        amount_cents: amt, note: $("#ajNote").value,
      });
      toast("Ajuste creado.");
      closeModal();
      await loadOverview(); render();
    } catch (e) { toast(e.message, true); }
  });
}

/* ============ GLOBAL EVENTS ============ */
$$(".nav-item").forEach((b) => b.addEventListener("click", () => { state.view = b.dataset.view; render(); }));
$("#accountSelect").addEventListener("change", async (e) => {
  state.accountId = parseInt(e.target.value, 10) || null;
  state.periodId = null;
  await loadContext();
});
$("#periodSelect").addEventListener("change", async (e) => {
  state.periodId = parseInt(e.target.value, 10) || null;
  await loadOverview(); render();
});
$("#btnNewAccount").addEventListener("click", () => openAccountModal());
$("#btnNewPeriod").addEventListener("click", () => openPeriodModal());
$("#btnMobileMenu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#btnAutoReconcile").addEventListener("click", () => state.periodId && doAutoReconcile(false));
$("#btnExportExcel").addEventListener("click", () => downloadExport("excel"));
$("#btnExportPdf").addEventListener("click", () => downloadExport("pdf"));

/* ============ BOOT ============ */
if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

loadContext().catch((e) => {
  $("#viewContainer").innerHTML = `<div class="card"><div class="empty-state">Error al cargar: ${esc(e.message)}<br/>Recarga la página.</div></div>`;
});
