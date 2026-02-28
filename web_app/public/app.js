const els = {
  liveDot: document.getElementById("liveDot"),
  liveText: document.getElementById("liveText"),
  lastUpdated: document.getElementById("lastUpdated"),
  eventsMtime: document.getElementById("eventsMtime"),
  metricsMtime: document.getElementById("metricsMtime"),

  refreshBtn: document.getElementById("refreshBtn"),
  pollSelect: document.getElementById("pollSelect"),

  kpiTotal: document.getElementById("kpiTotal"),
  kpiTickets: document.getElementById("kpiTickets"),
  kpiTicketsSub: document.getElementById("kpiTicketsSub"),
  kpiConf: document.getElementById("kpiConf"),
  kpiFps: document.getElementById("kpiFps"),
  kpiSys: document.getElementById("kpiSys"),

  fpsPoints: document.getElementById("fpsPoints"),
  rowCount: document.getElementById("rowCount"),
  tbody: document.getElementById("tbody"),
  emptyState: document.getElementById("emptyState"),

  modal: document.getElementById("modal"),
  modalBackdrop: document.getElementById("modalBackdrop"),
  modalClose: document.getElementById("modalClose"),
  modalTitle: document.getElementById("modalTitle"),
  modalSub: document.getElementById("modalSub"),
  modalImg: document.getElementById("modalImg"),
};

let pollTimer = null;
let lastSuccessfulFetchMs = 0;

function safeStr(x) { return (x ?? "").toString(); }

function parseUkDatetimeToMs(s) {
  // Expect: DD/MM/YYYY HH:MM:SS
  const t = safeStr(s).trim();
  if (!t) return null;

  const isoTry = Date.parse(t);
  if (!Number.isNaN(isoTry)) return isoTry;

  const m = t.match(/^(\d{2})\/(\d{2})\/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})$/);
  if (!m) return null;

  const dd = Number(m[1]);
  const mm = Number(m[2]);
  const yyyy = Number(m[3]);
  const HH = Number(m[4]);
  const MM = Number(m[5]);
  const SS = Number(m[6]);

  // Create as local time on the laptop
  const d = new Date(yyyy, mm - 1, dd, HH, MM, SS);
  const ms = d.getTime();
  return Number.isFinite(ms) ? ms : null;
}

function formatLocal(s) {
  const ms = parseUkDatetimeToMs(s);
  if (ms == null) return s || "—";
  return new Date(ms).toLocaleString();
}

function fmtPctFromConfidence(c) {
  if (c == null) return "—";
  const n = Number(c);
  if (!Number.isFinite(n)) return "—";
  // If looks like 0..1 => percent
  const pct = n <= 1.0 ? (n * 100) : n;
  return `${pct.toFixed(1)}%`;
}

function setLiveState(isLive) {
  if (isLive) {
    els.liveDot.className = "h-2 w-2 animate-pulse rounded-full bg-emerald-400";
    els.liveText.textContent = "Live";
  } else {
    els.liveDot.className = "h-2 w-2 rounded-full bg-amber-400";
    els.liveText.textContent = "Stale";
  }
}

function openModal(eventRow) {
  els.modalTitle.textContent = safeStr(eventRow.plate) || "Plate";
  els.modalSub.textContent = formatLocal(eventRow.timestamp);

  const url = eventRow.imageUrl;
  if (url) {
    els.modalImg.src = url;
  } else {
    // No imagePath present: show a “broken” state gracefully
    els.modalImg.src = "";
  }

  els.modal.classList.remove("hidden");
}

function closeModal() {
  els.modal.classList.add("hidden");
  // Stop loading / free up
  els.modalImg.src = "";
}

els.modalBackdrop.addEventListener("click", closeModal);
els.modalClose.addEventListener("click", closeModal);

let fpsChart = null;

function initChart() {
  const ctx = document.getElementById("fpsChart");
  fpsChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: "FPS",
        data: [],
        tension: 0.25,
        pointRadius: 0
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: { enabled: true }
      },
      scales: {
        x: {
          ticks: { color: "#cbd5e1", maxTicksLimit: 10 },
          grid: { color: "rgba(255,255,255,0.06)" }
        },
        y: {
          ticks: { color: "#cbd5e1" },
          grid: { color: "rgba(255,255,255,0.06)" }
        }
      }
    }
  });
}

function updateChart(metrics) {
  if (!fpsChart) initChart();

  // Keep last N points
  const N = 120;

  const cleaned = metrics
    .filter(m => m.fps != null)
    .map(m => ({
      t: m.timestamp,
      ms: parseUkDatetimeToMs(m.timestamp),
      fps: Number(m.fps)
    }))
    .sort((a, b) => (a.ms ?? 0) - (b.ms ?? 0));

  const sliced = cleaned.slice(-N);

  fpsChart.data.labels = sliced.map(p => {
    const ms = p.ms;
    if (ms == null) return safeStr(p.t);
    const d = new Date(ms);
    return d.toLocaleTimeString();
  });
  fpsChart.data.datasets[0].data = sliced.map(p => p.fps);
  fpsChart.update();

  els.fpsPoints.textContent = `${sliced.length}`;
}

function updateKpis(events, metrics) {
  const total = events.length;
  els.kpiTotal.textContent = `${total}`;

  const ticketCount = events.filter(e => e.ticketOwned === true).length;
  els.kpiTickets.textContent = `${ticketCount}`;
  els.kpiTicketsSub.textContent = total ? `${((ticketCount / total) * 100).toFixed(1)}% of detections` : "—";

  const confs = events.map(e => e.confidence).filter(c => c != null && Number.isFinite(Number(c)));
  if (confs.length) {
    const avg = confs.reduce((a, b) => a + Number(b), 0) / confs.length;
    els.kpiConf.textContent = fmtPctFromConfidence(avg);
  } else {
    els.kpiConf.textContent = "—";
  }

  const latestMetric = metrics
    .slice()
    .map(m => ({ ...m, ms: parseUkDatetimeToMs(m.timestamp) }))
    .sort((a, b) => (b.ms ?? -1) - (a.ms ?? -1))[0];

  if (latestMetric?.fps != null) els.kpiFps.textContent = `${Number(latestMetric.fps).toFixed(1)}`;
  else els.kpiFps.textContent = "—";

  const cpu = latestMetric?.cpu_percent;
  const temp = latestMetric?.temp_c;
  const ram = latestMetric?.ram_mb;

  const bits = [];
  if (cpu != null) bits.push(`CPU ${Number(cpu).toFixed(0)}%`);
  if (temp != null) bits.push(`Temp ${Number(temp).toFixed(1)}°C`);
  if (ram != null) bits.push(`RAM ${Number(ram).toFixed(0)} MB`);
  els.kpiSys.textContent = bits.length ? bits.join(" • ") : "—";
}

function renderEventsTable(events) {
  // Sort newest first using UK datetime
  const sorted = events
    .slice()
    .map(e => ({ ...e, ms: parseUkDatetimeToMs(e.timestamp) }))
    .sort((a, b) => (b.ms ?? -1) - (a.ms ?? -1));

  els.rowCount.textContent = `${sorted.length} rows`;
  els.tbody.innerHTML = "";

  if (sorted.length === 0) {
    els.emptyState.classList.remove("hidden");
    return;
  }
  els.emptyState.classList.add("hidden");

  const MAX = 200;

  for (const e of sorted.slice(0, MAX)) {
    const tr = document.createElement("tr");
    tr.className = "hover:bg-white/5";

    const ticketBadge = e.ticketOwned
      ? `<span class="rounded-full bg-emerald-500/15 text-emerald-200 border border-emerald-400/20 px-2 py-0.5 text-xs">Owned</span>`
      : `<span class="rounded-full bg-rose-500/15 text-rose-200 border border-rose-400/20 px-2 py-0.5 text-xs">Not owned</span>`;

    tr.innerHTML = `
      <td class="px-4 py-3">
        <button class="plateBtn text-left">
          <div class="font-semibold tracking-wide text-slate-100 underline decoration-white/20 hover:decoration-white/60">
            ${safeStr(e.plate) || "—"}
          </div>
          <div class="mt-1 text-xs text-slate-400">
            Click to view image
          </div>
        </button>
      </td>
      <td class="px-4 py-3 text-slate-200">${formatLocal(e.timestamp)}</td>
      <td class="px-4 py-3">${ticketBadge}</td>
      <td class="px-4 py-3 text-slate-300">${fmtPctFromConfidence(e.confidence)}</td>
    `;

    tr.querySelector(".plateBtn").addEventListener("click", () => openModal(e));
    els.tbody.appendChild(tr);
  }
}

async function fetchDashboard() {
  try {
    const res = await fetch("/api/dashboard", { cache: "no-store" });
    const data = await res.json();
    if (data?.error) throw new Error(data.error);

    els.lastUpdated.textContent = formatLocal(data.updatedAt);
    els.eventsMtime.textContent = data.files?.eventsMtime ? formatLocal(data.files.eventsMtime) : "—";
    els.metricsMtime.textContent = data.files?.metricsMtime ? formatLocal(data.files.metricsMtime) : "—";

    const events = (data.events || []).map(e => ({
      ...e,
      ticketOwned: !!e.ticketOwned
    }));
    const metrics = data.metrics || [];

    updateKpis(events, metrics);
    updateChart(metrics);
    renderEventsTable(events);

    lastSuccessfulFetchMs = Date.now();
    setLiveState(true);
  } catch (e) {
    setLiveState(false);
    console.error(e);
  }
}

function setPolling(ms) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  if (ms > 0) pollTimer = setInterval(fetchDashboard, ms);
}

els.refreshBtn.addEventListener("click", fetchDashboard);
els.pollSelect.addEventListener("change", (e) => setPolling(Number(e.target.value)));

// Stale detection: if no successful fetch for >10s, mark stale
setInterval(() => {
  const age = Date.now() - lastSuccessfulFetchMs;
  if (lastSuccessfulFetchMs && age > 10000) setLiveState(false);
}, 1000);

initChart();
fetchDashboard();
setPolling(Number(els.pollSelect.value));
