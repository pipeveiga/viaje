// TripDesk frontend — vanilla JS single-page app.

const API = "/api";
const POLL_MS = 30_000;

const state = {
  summary: null,
  itinerary: [],
  checklist: [],
  documents: [],
};

// -- utils ------------------------------------------------------------------

function fmtEur(v) {
  if (v == null) return "—";
  return `€${Number(v).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtUsd(v) {
  if (v == null) return "—";
  return `US$${Number(v).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// -- country mapping --------------------------------------------------------

const COUNTRY_BY_CITY = {
  "Barcelona":   { code: "ES", flag: "🇪🇸", name: "España",    cssVar: "es" },
  "Madrid":      { code: "ES", flag: "🇪🇸", name: "España",    cssVar: "es" },
  "Roma":        { code: "IT", flag: "🇮🇹", name: "Italia",    cssVar: "it" },
  "En vuelo":    { code: "AR", flag: "🇦🇷", name: "En tránsito", cssVar: "ar" },
  "Escala NYC":  { code: "US", flag: "🇺🇸", name: "Escala NYC", cssVar: "us" },
};

function countryFor(city) {
  return (
    COUNTRY_BY_CITY[city] || {
      code: "--", flag: "🌍", name: city || "Desconocido", cssVar: "neutral",
    }
  );
}

// -- traffic lights ---------------------------------------------------------

const MONTHS_ES = {
  ene: "01", feb: "02", mar: "03", abr: "04", may: "05", jun: "06",
  jul: "07", ago: "08", sep: "09", oct: "10", nov: "11", dic: "12",
};

function parseSpanishDate(text) {
  if (!text) return null;
  const m = String(text).toLowerCase().match(/(\d{1,2})[\s-]?(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)/);
  if (!m) return null;
  const day = String(m[1]).padStart(2, "0");
  const month = MONTHS_ES[m[2]];
  return `2026-${month}-${day}`;
}

function daysBetween(isoA, isoB) {
  const a = new Date(isoA + "T00:00:00Z");
  const b = new Date(isoB + "T00:00:00Z");
  return Math.round((b - a) / 86400000);
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function checklistLevel(item, tripStart) {
  if (item.status === "paid") return "green";
  const relatedDate = parseSpanishDate(item.detail);
  const today = todayIso();
  if (relatedDate) {
    const delta = daysBetween(today, relatedDate);
    if (delta < 0) return "red";
    if (delta <= 14) return "red";
    if (delta <= 45) return "yellow";
  }
  if (tripStart) {
    const delta = daysBetween(today, tripStart);
    if (delta <= 14) return "red";
  }
  if (item.amount_eur == null) return "yellow";
  return "gray";
}

function itineraryLevel(day) {
  const today = todayIso();
  const delta = daysBetween(today, day.date);
  const hasReal = day.real_expense != null && day.real_expense > 0;
  if (hasReal) return "green";
  if (delta < 0) return "red";
  if (delta === 0) return "red";
  if (delta <= 7) return "yellow";
  return "gray";
}

function dotHTML(level) {
  return `<span class="dot dot-${level}" title="${level}"></span>`;
}

// -- render -----------------------------------------------------------------

function renderHeader() {
  const s = state.summary;
  if (!s) return;
  document.getElementById("trip-meta").textContent =
    `${s.trip.start_date} → ${s.trip.end_date} · ${s.trip.total_days} días`;

  const cd = document.getElementById("countdown");
  if (s.trip.in_trip) {
    cd.textContent = `Día ${s.trip.current_day} de ${s.trip.total_days}`;
    if (s.trip.current_city) {
      document.getElementById("trip-meta").textContent += ` · 📍 ${s.trip.current_city}`;
    }
  } else if (s.trip.days_until > 0) {
    cd.textContent = `Faltan ${s.trip.days_until} días`;
  } else {
    cd.textContent = "Viaje finalizado";
  }
}

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  const b = s.budget;
  const cards = [
    { label: "Presupuesto total", eur: b.total_eur, usd: b.total_usd, color: "var(--sky)" },
    { label: "Ya pagado",         eur: b.paid_eur,  usd: b.paid_usd,  color: "var(--emerald)" },
    { label: "Pendiente",         eur: b.pending_eur, usd: b.pending_usd, color: "var(--amber)" },
    { label: "Gastado en viaje",  eur: b.spent_eur, usd: b.spent_usd, color: "var(--accent)" },
  ];
  document.getElementById("summary-cards").innerHTML = cards
    .map(
      (c) => `
      <div class="summary-card" style="--card-color:${c.color}">
        <div class="label">${c.label}</div>
        <div class="primary">${fmtEur(c.eur)}</div>
        <div class="secondary">${fmtUsd(c.usd)}</div>
      </div>`
    )
    .join("");
}

function renderAlerts() {
  const tripStart = state.summary?.trip?.start_date;
  const urgent = [];
  const soon = [];

  state.checklist.forEach((item) => {
    const level = checklistLevel(item, tripStart);
    if (level === "red") urgent.push({ label: item.concept, kind: "pago" });
    else if (level === "yellow")
      soon.push({
        label: `${item.concept}${item.amount_eur == null ? " (sin comprobante)" : ""}`,
        kind: "pago",
      });
  });

  state.itinerary.forEach((d) => {
    const level = itineraryLevel(d);
    const label = d.activity ? `Día ${d.day_number} · ${d.city}` : `Día ${d.day_number} · ${d.city}`;
    if (level === "red") urgent.push({ label, kind: "día" });
    else if (level === "yellow") soon.push({ label, kind: "día" });
  });

  const card = document.getElementById("alerts-card");
  card.classList.remove("has-red", "has-yellow", "all-green");
  if (urgent.length) card.classList.add("has-red");
  else if (soon.length) card.classList.add("has-yellow");
  else card.classList.add("all-green");

  const renderList = (items, level) =>
    items
      .slice(0, 6)
      .map(
        (i) => `
        <div class="alert-list-item">
          ${dotHTML(level)}
          <span class="flex-1 truncate">${i.label}</span>
          <span class="text-xs text-slate-500">${i.kind}</span>
        </div>`
      )
      .join("");

  let headline = "";
  if (urgent.length === 0 && soon.length === 0) {
    headline = `<div class="flex items-center gap-2 text-emerald-700 font-semibold">${dotHTML("green")} Todo al día</div>`;
  } else {
    headline = `
      <div class="flex items-center gap-4 flex-wrap">
        <div class="flex items-center gap-2">${dotHTML("red")} <span class="font-semibold">${urgent.length}</span> urgente${urgent.length === 1 ? "" : "s"}</div>
        <div class="flex items-center gap-2">${dotHTML("yellow")} <span class="font-semibold">${soon.length}</span> próximo${soon.length === 1 ? "" : "s"}</div>
      </div>`;
  }

  const body = urgent.length
    ? `<div class="mt-3 grid gap-1.5">${renderList(urgent, "red")}</div>`
    : soon.length
    ? `<div class="mt-3 grid gap-1.5">${renderList(soon, "yellow")}</div>`
    : "";

  card.innerHTML = `
    <div class="flex items-center justify-between gap-3 flex-wrap">
      <h3 class="font-serif-display text-base">Alertas</h3>
      ${headline}
    </div>
    ${body}`;
}

// -- itinerary (grouped by country segment) ---------------------------------

function groupByCountry(itinerary) {
  const groups = [];
  let current = null;
  for (const d of itinerary) {
    const country = countryFor(d.city);
    if (!current || current.code !== country.code) {
      current = { ...country, days: [] };
      groups.push(current);
    }
    current.days.push(d);
  }
  return groups;
}

function renderItinerary() {
  const currentDay = state.summary?.trip?.current_day;
  const container = document.getElementById("itinerary-groups");
  const groups = groupByCountry(state.itinerary);

  container.innerHTML = groups
    .map((g) => {
      const firstDate = g.days[0].date;
      const lastDate = g.days[g.days.length - 1].date;
      const dayRange =
        g.days.length === 1
          ? `Día ${g.days[0].day_number}`
          : `Días ${g.days[0].day_number}–${g.days[g.days.length - 1].day_number}`;

      const rows = g.days
        .map((d) => {
          const isCurrent = d.day_number === currentDay;
          const hasReal = d.real_expense != null && d.real_expense > 0;
          const level = itineraryLevel(d);
          return `
            <div class="day-row ${isCurrent ? "is-current" : ""}"
                 style="--country-color:var(--${g.cssVar})">
              <div>
                <div class="day-number">${d.day_number}</div>
                <div class="day-date">${formatShortDate(d.date)}</div>
              </div>
              <div>
                <div class="day-city">${dotHTML(level)} ${d.city}</div>
                <div class="day-activity"
                     contenteditable="true"
                     data-day="${d.day_number}"
                     data-original="${escapeAttr(d.activity || "")}">${escapeHtml(d.activity || "")}</div>
              </div>
              <div class="day-amount">
                <span class="lbl">Estimado</span>
                ${fmtEur(d.estimated_expense)}
              </div>
              <div class="day-amount">
                <span class="lbl">Real</span>
                <input class="real-input"
                       type="number" step="0.01" min="0"
                       data-day="${d.day_number}"
                       value="${d.real_expense ?? ""}"
                       placeholder="—" />
              </div>
              <div class="day-state">
                ${
                  hasReal
                    ? '<span class="badge badge-done">Ok</span>'
                    : '<span class="badge badge-empty">—</span>'
                }
              </div>
            </div>`;
        })
        .join("");

      return `
        <div class="country-group">
          <div class="country-header" style="--country-wash:var(--${g.cssVar}-wash)">
            <span class="flag">${g.flag}</span>
            <span class="title">${g.name}</span>
            <span class="sub">${dayRange} · ${firstDate}${firstDate !== lastDate ? " → " + lastDate : ""}</span>
          </div>
          ${rows}
        </div>`;
    })
    .join("");

  container.querySelectorAll("input.real-input").forEach((inp) => {
    inp.addEventListener("change", async (e) => {
      const day = Number(e.target.dataset.day);
      const value = e.target.value === "" ? null : Number(e.target.value);
      try {
        await api(`/itinerary/${day}`, {
          method: "PUT",
          body: JSON.stringify({
            real_expense: value,
            status: value != null && value > 0 ? "completed" : "empty",
          }),
        });
        await loadAll();
      } catch (err) {
        alert("Error actualizando el día: " + err.message);
      }
    });
  });

  container.querySelectorAll(".day-activity").forEach((el) => {
    el.addEventListener("blur", async () => {
      const day = Number(el.dataset.day);
      const newValue = el.innerText.trim();
      const original = (el.dataset.original || "").trim();
      if (newValue === original) return;
      try {
        await api(`/itinerary/${day}`, {
          method: "PUT",
          body: JSON.stringify({ activity: newValue }),
        });
        el.dataset.original = newValue;
      } catch (err) {
        alert("No pude guardar la actividad: " + err.message);
        el.innerText = original;
      }
    });
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        el.blur();
      }
    });
  });
}

function formatShortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("es-AR", { day: "2-digit", month: "short" }).replace(".", "");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
function escapeAttr(s) {
  return escapeHtml(s).replaceAll('"', "&quot;");
}

// -- checklist --------------------------------------------------------------

function checklistIcon(concept) {
  const c = concept.toLowerCase();
  if (c.includes("hotel")) return { icon: "🏨", cls: "hotel" };
  if (c.includes("vuelo")) return { icon: "✈︎", cls: "flight" };
  if (c.includes("tren")) return { icon: "🚆", cls: "train" };
  if (c.includes("bus")) return { icon: "🚌", cls: "bus" };
  if (c.includes("t-jove") || c.includes("metro")) return { icon: "🚇", cls: "transit" };
  if (c.includes("esim") || c.includes("datos")) return { icon: "📶", cls: "data" };
  return { icon: "📄", cls: "" };
}

function renderChecklist() {
  const list = document.getElementById("checklist-list");
  const tripStart = state.summary?.trip?.start_date;
  list.innerHTML = state.checklist
    .map((item) => {
      const paid = item.status === "paid";
      const badge = paid
        ? '<span class="badge badge-paid">Pagado</span>'
        : '<span class="badge badge-pending">Pendiente</span>';
      const amount = item.amount_eur != null ? fmtEur(item.amount_eur) : "—";
      const code = item.reservation_code
        ? `<span class="ml-2 text-xs text-slate-400">· ${item.reservation_code}</span>`
        : "";
      const level = checklistLevel(item, tripStart);
      const ico = checklistIcon(item.concept);
      return `
        <div class="checklist-card ${paid ? "is-paid" : ""}" data-id="${item.id}">
          <span class="cat-icon ${ico.cls}">${ico.icon}</span>
          <input type="checkbox" ${paid ? "checked" : ""} data-id="${item.id}" />
          <div class="min-w-0 flex-1">
            <div class="font-semibold truncate flex items-center gap-2">
              ${dotHTML(level)}<span class="truncate">${item.concept}</span>${code}
            </div>
            ${item.detail ? `<div class="text-sm text-slate-500 mt-0.5">${item.detail}</div>` : ""}
            <div class="text-xs text-slate-500 mt-1">${badge} · ${amount}</div>
          </div>
        </div>`;
    })
    .join("");

  list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", async (e) => {
      const id = Number(e.target.dataset.id);
      const status = e.target.checked ? "paid" : "pending";
      const card = list.querySelector(`.checklist-card[data-id="${id}"]`);
      try {
        await api(`/checklist/${id}`, {
          method: "PUT",
          body: JSON.stringify({ status }),
        });
        card?.classList.add("fade-flash");
        setTimeout(() => card?.classList.remove("fade-flash"), 700);
        await loadAll();
      } catch (err) {
        alert("Error: " + err.message);
        e.target.checked = !e.target.checked;
      }
    });
  });
}

function renderDocuments() {
  const list = document.getElementById("documents-list");
  if (!state.documents.length) {
    list.innerHTML = `<div class="text-slate-500 text-sm text-center py-6">No hay documentos cargados aún.</div>`;
    return;
  }
  list.innerHTML = state.documents
    .map((d) => {
      const pending = d.confirmed === false;
      return `
      <div class="doc-card ${pending ? "is-pending" : ""}">
        <div class="flex-1 min-w-0">
          <div class="font-semibold flex items-center gap-2 flex-wrap">
            <span>${d.description || d.filename}</span>
            ${d.doc_type ? `<span class="doc-chip">${d.doc_type}</span>` : ""}
            ${pending ? '<span class="badge badge-pending">Sin confirmar</span>' : ""}
          </div>
          <div class="text-xs text-slate-500 mt-1">
            ${d.date ? d.date : ""}
            ${d.provider ? ` · ${d.provider}` : ""}
            ${d.day_number ? ` · día ${d.day_number}` : ""}
            ${d.amount_eur != null ? ` · ${fmtEur(d.amount_eur)}` : ""}
            ${d.amount_usd != null ? ` · ${fmtUsd(d.amount_usd)}` : ""}
          </div>
        </div>
        <div class="flex gap-2 shrink-0">
          <a href="/api/documents/${d.id}/file" target="_blank" class="btn-secondary text-sm">Ver</a>
          <button class="btn-secondary text-sm" style="color:var(--rose)" data-del="${d.id}">Eliminar</button>
        </div>
      </div>`;
    })
    .join("");

  list.querySelectorAll("button[data-del]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      const id = e.target.dataset.del;
      if (!confirm("¿Eliminar este documento?")) return;
      await api(`/documents/${id}`, { method: "DELETE" });
      await loadAll();
    });
  });
}

// -- loading ----------------------------------------------------------------

async function loadAll() {
  try {
    const [summary, itinerary, checklist, documents] = await Promise.all([
      api("/summary"),
      api("/itinerary"),
      api("/checklist"),
      api("/documents"),
    ]);
    state.summary = summary;
    state.itinerary = itinerary;
    state.checklist = checklist;
    state.documents = documents;
    renderHeader();
    renderSummary();
    renderAlerts();
    renderItinerary();
    renderChecklist();
    renderDocuments();
  } catch (err) {
    console.error("loadAll failed", err);
  }
}

// -- tabs -------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.dataset.tab;
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.add("hidden"));
    document.getElementById(`tab-${tab}`).classList.remove("hidden");
  });
});

// -- upload -----------------------------------------------------------------

document.getElementById("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  for (const [k, v] of [...fd.entries()]) {
    if (v === "" || v === null) fd.delete(k);
  }
  try {
    const res = await fetch(`${API}/documents`, { method: "POST", body: fd });
    if (!res.ok) throw new Error(await res.text());
    form.reset();
    await loadAll();
  } catch (err) {
    alert("Error subiendo archivo: " + err.message);
  }
});

// -- config modal -----------------------------------------------------------

const modal = document.getElementById("config-modal");
document.getElementById("btn-config").addEventListener("click", async () => {
  const cfg = await api("/config");
  document.getElementById("config-rate").value = cfg.eur_usd_rate || "1.172";
  document.getElementById("config-ars").value = cfg.usd_ars_rate || "1450";
  modal.classList.remove("hidden");
});
document.getElementById("config-cancel").addEventListener("click", () => {
  modal.classList.add("hidden");
});
document.getElementById("config-save").addEventListener("click", async () => {
  const rate = parseFloat(document.getElementById("config-rate").value);
  const ars = parseFloat(document.getElementById("config-ars").value);
  if (!rate || rate <= 0) return alert("Tipo de cambio EUR/USD inválido");
  if (!ars || ars <= 0) return alert("Tipo de cambio USD/ARS inválido");
  await api("/config", {
    method: "PUT",
    body: JSON.stringify({ eur_usd_rate: rate, usd_ars_rate: ars }),
  });
  modal.classList.add("hidden");
  await loadAll();
});

document.getElementById("config-reset").addEventListener("click", async () => {
  const confirm1 = confirm(
    "¿Seguro? Esto borra TODOS los documentos, los montos del checklist y las actividades del itinerario."
  );
  if (!confirm1) return;
  const confirm2 = prompt('Escribí "EMPEZAR" para confirmar:');
  if (confirm2 !== "EMPEZAR") return;
  try {
    const res = await api("/checklist/reset-all", { method: "POST" });
    alert(
      `Listo. Borré ${res.documents_deleted} documentos, ${res.files_removed} archivos, ` +
      `reseteé ${res.checklist_reset} items del checklist y ${res.itinerary_reset} días del itinerario.`
    );
    modal.classList.add("hidden");
    await loadAll();
  } catch (err) {
    alert("Error: " + err.message);
  }
});

// -- boot -------------------------------------------------------------------

loadAll();
setInterval(loadAll, POLL_MS);
