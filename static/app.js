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
    if (delta < 0) return "red";         // fecha ya pasada y sin pagar
    if (delta <= 14) return "red";       // queda menos de 2 semanas
    if (delta <= 45) return "yellow";
  }
  if (tripStart) {
    const delta = daysBetween(today, tripStart);
    if (delta <= 14) return "red";
  }
  if (item.amount_eur == null) return "yellow";  // falta comprobante
  return "gray";
}

function itineraryLevel(day) {
  const today = todayIso();
  const delta = daysBetween(today, day.date);
  const hasReal = day.real_expense != null && day.real_expense > 0;
  if (hasReal) return "green";
  if (delta < 0) return "red";       // día pasado sin gasto registrado
  if (delta === 0) return "red";     // es hoy
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
    { label: "Presupuesto total", eur: b.total_eur, usd: b.total_usd, color: "text-blue-400" },
    { label: "Ya pagado", eur: b.paid_eur, usd: b.paid_usd, color: "text-emerald-400" },
    { label: "Pendiente", eur: b.pending_eur, usd: b.pending_usd, color: "text-amber-400" },
    { label: "Gastado en viaje", eur: b.spent_eur, usd: b.spent_usd, color: "text-sky-400" },
  ];
  document.getElementById("summary-cards").innerHTML = cards
    .map(
      (c) => `
      <div class="card">
        <div class="text-xs uppercase tracking-wider text-slate-400">${c.label}</div>
        <div class="text-xl md:text-2xl font-bold mt-1 ${c.color}">${fmtEur(c.eur)}</div>
        <div class="text-xs text-slate-400">${fmtUsd(c.usd)}</div>
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
    if (level === "red")
      urgent.push({ label: `Día ${d.day_number} · ${d.activity}`, kind: "día" });
    else if (level === "yellow")
      soon.push({ label: `Día ${d.day_number} · ${d.activity}`, kind: "día" });
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
    headline = `<div class="flex items-center gap-2 text-emerald-400 font-semibold">${dotHTML("green")} Todo al día · sin alertas</div>`;
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
      <h3 class="font-semibold text-slate-200">🚦 Alertas</h3>
      ${headline}
    </div>
    ${body}`;
}

function renderItinerary() {
  const current = state.summary?.trip?.current_day;
  const tbody = document.getElementById("itinerary-body");
  tbody.innerHTML = state.itinerary
    .map((d) => {
      const isCurrent = d.day_number === current;
      const hasReal = d.real_expense != null && d.real_expense > 0;
      const status = d.status || (hasReal ? "completed" : "empty");
      const badge =
        status === "completed"
          ? '<span class="badge badge-done">Completado</span>'
          : status === "in_progress"
          ? '<span class="badge badge-inprog">En curso</span>'
          : '<span class="badge badge-empty">—</span>';
      const level = itineraryLevel(d);

      return `
        <tr class="${isCurrent ? "current-day" : ""} ${hasReal ? "has-real" : "only-estimated"}">
          <td class="px-3 py-3 font-semibold">
            <span class="inline-flex items-center gap-2">${dotHTML(level)}${d.day_number}</span>
          </td>
          <td class="px-3 py-3 whitespace-nowrap">${d.date}</td>
          <td class="px-3 py-3">${d.city}</td>
          <td class="px-3 py-3"><div class="activity-cell">${d.activity || ""}</div></td>
          <td class="px-3 py-3 text-right tabular-nums">${fmtEur(d.estimated_expense)}</td>
          <td class="px-3 py-3 text-right tabular-nums">
            <input
              class="real-input"
              type="number"
              step="0.01"
              min="0"
              data-day="${d.day_number}"
              value="${d.real_expense ?? ""}"
              placeholder="—"
            />
          </td>
          <td class="px-3 py-3 text-center">${badge}</td>
        </tr>`;
    })
    .join("");

  tbody.querySelectorAll("input.real-input").forEach((inp) => {
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
        ? `<span class="ml-2 text-xs text-slate-500">· ${item.reservation_code}</span>`
        : "";
      const level = checklistLevel(item, tripStart);
      return `
        <div class="card flex items-center justify-between gap-3" data-id="${item.id}">
          <div class="flex items-start gap-3 flex-1 min-w-0">
            <input type="checkbox" ${paid ? "checked" : ""}
              class="mt-1 w-5 h-5 accent-emerald-500 cursor-pointer"
              data-id="${item.id}" />
            <div class="min-w-0 flex-1">
              <div class="font-semibold truncate flex items-center gap-2">
                ${dotHTML(level)}<span class="truncate">${item.concept}</span>${code}
              </div>
              ${item.detail ? `<div class="text-sm text-slate-400">${item.detail}</div>` : ""}
              <div class="text-xs text-slate-500 mt-1">${badge} · ${amount}</div>
            </div>
          </div>
        </div>`;
    })
    .join("");

  list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", async (e) => {
      const id = Number(e.target.dataset.id);
      const status = e.target.checked ? "paid" : "pending";
      const card = list.querySelector(`.card[data-id="${id}"]`);
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
    .map(
      (d) => {
        const pending = d.confirmed === false;
        return `
      <div class="card flex flex-col md:flex-row md:items-center gap-3 justify-between ${pending ? "ring-1 ring-amber-500/40" : ""}">
        <div class="flex-1 min-w-0">
          <div class="font-semibold">
            ${d.description || d.filename}
            ${pending ? '<span class="ml-2 text-xs badge badge-pending">Sin confirmar</span>' : ""}
          </div>
          <div class="text-xs text-slate-400 mt-1">
            ${d.doc_type || "otro"}
            ${d.date ? ` · ${d.date}` : ""}
            ${d.provider ? ` · ${d.provider}` : ""}
            ${d.day_number ? ` · día ${d.day_number}` : ""}
            ${d.amount_eur != null ? ` · ${fmtEur(d.amount_eur)}` : ""}
            ${d.amount_usd != null ? ` · ${fmtUsd(d.amount_usd)}` : ""}
          </div>
        </div>
        <div class="flex gap-2">
          <a href="/api/documents/${d.id}/file" target="_blank" class="btn-secondary text-sm">Ver</a>
          <button class="btn-secondary text-sm text-red-300" data-del="${d.id}">Eliminar</button>
        </div>
      </div>`;
      }
    )
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
