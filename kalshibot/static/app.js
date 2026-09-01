const board = document.getElementById("board");
const updated = document.getElementById("clock");
const disclaimer = document.getElementById("disclaimer");
const filterInput = document.getElementById("filter");
const edgesOnly = document.getElementById("edges-only");
const refreshBtn = document.getElementById("refresh");
const blotter = document.getElementById("blotter");

let snapshot = null;
let campaign = null;
let section = "crypto";
let view = "markets";
let blot = "positions";
let selected = null;
let chartHours = 4;
let chartTimer = null;
let chartData = null;

function money(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `$${Number(value).toFixed(2)}`;
}

function cents(value, signed = false) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const n = 100 * Number(value);
  const sign = signed ? (n >= 0 ? "+" : "") : "";
  return `${sign}${n.toFixed(0)}¢`;
}

function pct(value) {
  if (value == null) return "—";
  return `${(100 * value).toFixed(0)}%`;
}

function volumeLabel(value) {
  if (value == null || value <= 0) return "";
  if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M vol`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(1)}k vol`;
  return `$${value.toFixed(0)} vol`;
}

function closeLabel(iso) {
  if (!iso) return "";
  const ms = Date.parse(iso) - Date.now();
  if (Number.isNaN(ms)) return "";
  if (ms < 0) return "closed";
  const hours = ms / 36e5;
  if (hours < 1) return `${Math.max(1, Math.round(ms / 6e4))}m left`;
  if (hours < 48) return `${hours.toFixed(1)}h left`;
  return `${(hours / 24).toFixed(1)}d left`;
}

function etClock() {
  try {
    return new Date().toLocaleString("en-US", {
      timeZone: "America/New_York",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    }) + " ET";
  } catch {
    return new Date().toLocaleTimeString();
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatSpot(value) {
  if (value == null) return "";
  if (value >= 100) return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  if (value >= 1) return `$${value.toFixed(3)}`;
  return `$${value.toPrecision(3)}`;
}

function yesAsk(row) {
  return row.yes_ask;
}

function noAsk(row) {
  return row.yes_bid == null ? null : Math.max(0, 1 - row.yes_bid);
}

function renderMarkets() {
  if (!snapshot) return;
  const block = snapshot.sections[section];
  const q = filterInput.value.trim().toLowerCase();
  let rows = [...(block.predictions || [])];
  if (edgesOnly.checked) rows = rows.filter((r) => r.edge >= 0.02);
  if (q) {
    rows = rows.filter((r) =>
      `${r.event_title} ${r.market_title} ${r.subtitle} ${r.market_ticker} ${r.asset || ""}`
        .toLowerCase()
        .includes(q)
    );
  }
  rows.sort((a, b) => Math.abs(b.edge || 0) - Math.abs(a.edge || 0));

  if (!rows.length) {
    board.innerHTML = `<div class="empty">${
      edgesOnly.checked
        ? "No ≥ 2¢ edges in this tab. Uncheck the filter to browse every live contract."
        : "No contracts in this view. Try another tab or refresh."
    }</div>`;
    return;
  }

  board.innerHTML = rows
    .map((row) => {
      const lean = row.side === "NO" ? "NO" : row.side === "YES" ? "YES" : "—";
      const leanClass = lean === "NO" ? "no" : lean === "YES" ? "yes" : "";
      const edgeClass = row.edge >= 0.02 ? "pos" : row.edge < 0 ? "neg" : "";
      const meta = [closeLabel(row.close_time), row.spot != null ? `spot ${formatSpot(row.spot)}` : ""]
        .filter(Boolean)
        .join(" · ");
      return `
        <button class="scan-row" type="button" data-ticker="${escapeHtml(row.market_ticker)}">
          <span class="scan-lean ${leanClass}">${lean}</span>
          <span class="scan-body">
            <span class="scan-title">${escapeHtml(row.event_title)}</span>
            <span class="scan-sub">${escapeHtml(row.subtitle || row.market_title || "")}</span>
            <span class="scan-stats">
              <span>Fair ${pct(row.model_prob)}</span>
              <span>Yes ${cents(yesAsk(row))}</span>
              <span class="edge ${edgeClass}">${cents(row.edge, true)}</span>
              <span>${escapeHtml(meta)}</span>
            </span>
          </span>
          <span class="scan-go">Chart</span>
        </button>`;
    })
    .join("");
  board.querySelectorAll(".scan-row").forEach((card) => {
    card.addEventListener("click", () => {
      const row = rows.find((r) => r.market_ticker === card.dataset.ticker);
      if (row) openMarket(row, true);
    });
  });
}

function updateStats() {
  if (!snapshot) return;
  for (const key of ["crypto", "commodities", "sports"]) {
    const stats = snapshot.sections[key].stats;
    document.getElementById(`stat-${key}`).textContent = stats.opportunities;
  }
}

function setView(next) {
  view = next;
  document.getElementById("view-markets").classList.toggle("hidden", view !== "markets");
  document.getElementById("view-portfolio").classList.toggle("hidden", view !== "portfolio");
  document.getElementById("view-trade").classList.toggle("hidden", view !== "trade");
  document.querySelectorAll(".nav-link").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  if (view !== "trade") stopChart();
}

function renderCampaign() {
  if (!campaign) return;
  const cash = campaign.kalshi_cash;
  const equity = campaign.equity ?? campaign.bankroll;
  const pnl = Number(campaign.realized || 0);
  document.getElementById("folio-cash").textContent = money(cash);
  document.getElementById("folio-equity").textContent = money(equity);
  const pnlEl = document.getElementById("folio-pnl");
  pnlEl.textContent = `${pnl >= 0 ? "+" : ""}${money(pnl).slice(1)}`;
  if (pnl > 0) pnlEl.className = "num pos";
  else if (pnl < 0) pnlEl.className = "num neg";
  else pnlEl.className = "num";
  document.getElementById("folio-room").textContent = money(campaign.room);
  document.getElementById("folio-idea").textContent = money(campaign.typical_idea);
  document.getElementById("nav-cash").textContent = money(cash ?? equity);

  const pill = document.getElementById("live-pill");
  pill.className = "live-pill";
  if (campaign.live) {
    pill.classList.add("live");
    pill.textContent = "LIVE";
  } else {
    pill.classList.add("dry");
    pill.textContent = "DRY";
  }
  pill.title = campaign.can_trade
    ? (campaign.live ? "Tap to switch to DRY (no real orders)" : "Tap to go LIVE (real Kalshi orders)")
    : "Need Kalshi API key and private key on this host before going LIVE";
  pill.setAttribute("aria-pressed", campaign.live ? "true" : "false");

  const chips = [];
  chips.push(`<span class="chip ${campaign.auto ? "on" : ""}">${campaign.auto ? "Auto" : "Manual"}</span>`);
  chips.push(`<span class="chip ${campaign.maker_auto === false ? "" : "on"}">Maker ${campaign.maker_auto === false ? "off" : "on"}</span>`);
  chips.push(`<span class="chip ${campaign.follow_kalshi_cash ? "on" : ""}">${campaign.follow_kalshi_cash ? "Follows cash" : "Fixed book"}</span>`);
  if (campaign.fifteen_look) chips.push(`<span class="chip on">15m look</span>`);
  if (campaign.fifteen_stopped) chips.push(`<span class="chip warn">15m stopped</span>`);
  if (campaign.fifteen_revenge) chips.push(`<span class="chip warn">15m revenge</span>`);
  if (campaign.halted) chips.push(`<span class="chip warn">Halted</span>`);
  document.getElementById("status-chips").innerHTML = chips.join("");

  const haltBtn = document.getElementById("halt-btn");
  haltBtn.textContent = campaign.halted ? "Resume" : "Halt";
  haltBtn.classList.toggle("resume", Boolean(campaign.halted));

  const follow = campaign.follow_kalshi_cash ? "follows Kalshi cash" : "fixed book";
  const cadence = campaign.auto
    ? "auto · 15m at :02–:04 · maker last 3 min · hourly every 5 min"
    : "manual fire";
  const book = campaign.rests_source === "kalshi" || campaign.positions_source === "kalshi"
    ? "Kalshi book"
    : "local book";
  document.getElementById("book-meta").textContent =
    `${book} · ${follow} · ${cadence} · ${campaign.open_tickets?.length || 0} open · ${campaign.rests?.length || 0} resting`;

  renderBlotter();
}

function emptyBlotter(kind) {
  if (kind === "orders") {
    if (String(campaign.blotter_error || "").includes("orders")) {
      return "Could not load Kalshi orders. Showing the local book.";
    }
    if (!campaign.can_trade) {
      return "No resting orders in the local book. Load Kalshi keys on this host to see live orders.";
    }
    if (campaign.rests_source === "kalshi") {
      return "No resting orders on Kalshi. Filled contracts show under Positions.";
    }
    return "No resting orders.";
  }
  if (String(campaign.blotter_error || "").includes("positions")) {
    return "Could not load Kalshi positions. Showing the local book.";
  }
  if (!campaign.can_trade) {
    return "No open positions in the local book. Load Kalshi keys on this host to see live holdings.";
  }
  if (campaign.positions_source === "kalshi") {
    return "No open positions on Kalshi.";
  }
  return "No open positions.";
}

function renderBlotter() {
  if (!campaign) {
    blotter.innerHTML = `<div class="empty">Loading campaign…</div>`;
    return;
  }
  if (blot === "activity") {
    const lines = (campaign.log || []).map((row) => `${(row.ts || "").slice(11, 19)}  ${(row.loop || "").padEnd(8)}  ${row.message}`);
    blotter.innerHTML = `<pre class="activity">${escapeHtml(lines.join("\n") || "No campaign fires yet.")}</pre>`;
    return;
  }
  const rows = blot === "orders" ? campaign.rests || [] : campaign.open_tickets || [];
  if (!rows.length) {
    blotter.innerHTML = `<div class="empty">${emptyBlotter(blot)}</div>`;
    return;
  }
  const body = rows
    .map((row) => {
      const side = String(row.side || "").toLowerCase();
      const px = row.fill ?? row.price;
      return `<div class="row">
        <div>
          <div>${escapeHtml(row.title || row.ticker || "—")}</div>
          <div class="ticker">${escapeHtml(row.ticker || "")} · ${escapeHtml(row.loop || "")}</div>
        </div>
        <div><span class="side-tag ${side}">${escapeHtml(side.toUpperCase() || "—")}</span></div>
        <div class="num hide-sm">${cents(px)}</div>
        <div class="num hide-sm">${row.count != null ? Number(row.count).toFixed(2) : "—"}</div>
        <div class="num">${money(row.cost ?? (px != null && row.count != null ? px * row.count : null))}</div>
      </div>`;
    })
    .join("");
  blotter.innerHTML = `
    <div class="row head">
      <div>Contract</div>
      <div>Side</div>
      <div class="hide-sm">Price</div>
      <div class="hide-sm">Qty</div>
      <div>Cost</div>
    </div>
    ${body}`;
}

async function load(force = false) {
  if (updated) updated.textContent = etClock();
  if (!snapshot) board.innerHTML = `<div class="status">Scanning live Kalshi contracts…</div>`;
  const response = await fetch(`/api/predictions${force ? "?force=true" : ""}`);
  if (!response.ok) {
    board.innerHTML = `<div class="empty">Could not load markets (${response.status}).</div>`;
    return;
  }
  snapshot = await response.json();
  if (snapshot.disclaimer) disclaimer.textContent = snapshot.disclaimer;
  updateStats();
  renderMarkets();
}

async function loadCampaign() {
  const response = await fetch("/api/campaign");
  if (!response.ok) return;
  campaign = await response.json();
  renderCampaign();
}

document.querySelectorAll(".cat").forEach((btn) => {
  btn.addEventListener("click", () => {
    section = btn.dataset.section;
    document.querySelectorAll(".cat").forEach((el) => el.classList.toggle("active", el === btn));
    renderMarkets();
  });
});

document.querySelectorAll(".nav-link").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    const next = link.dataset.view;
    history.pushState({ view: next }, "", next === "portfolio" ? "/portfolio" : "/");
    setView(next);
  });
});

document.querySelector(".brand").addEventListener("click", (event) => {
  event.preventDefault();
  history.pushState({ view: "markets" }, "", "/");
  setView("markets");
});

document.getElementById("trade-back").addEventListener("click", () => {
  history.pushState({ view: "markets" }, "", "/");
  setView("markets");
});

document.getElementById("chart-ranges").addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-hours]");
  if (!btn) return;
  event.preventDefault();
  chartHours = Number(btn.dataset.hours);
  document.querySelectorAll("#chart-ranges button").forEach((el) => el.classList.toggle("on", el === btn));
  if (selected) loadChart();
});

window.addEventListener("popstate", route);
route();

document.querySelectorAll(".blot-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    blot = btn.dataset.blot;
    document.querySelectorAll(".blot-tab").forEach((el) => el.classList.toggle("active", el === btn));
    renderBlotter();
  });
});

filterInput.addEventListener("input", renderMarkets);
edgesOnly.addEventListener("change", renderMarkets);
refreshBtn.addEventListener("click", () => {
  load(true);
  loadCampaign();
});

document.querySelectorAll("[data-fire]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    setView("portfolio");
    blot = "activity";
    document.querySelectorAll(".blot-tab").forEach((el) => el.classList.toggle("active", el.dataset.blot === "activity"));
    blotter.innerHTML = `<pre class="activity">Firing ${btn.dataset.fire}…</pre>`;
    try {
      const response = await fetch(`/api/campaign/fire/${btn.dataset.fire}`, { method: "POST" });
      const data = await response.json();
      blotter.innerHTML = `<pre class="activity">${escapeHtml((data.actions || []).join("\n") || "Done.")}</pre>`;
      await loadCampaign();
    } finally {
      btn.disabled = false;
    }
  });
});

document.getElementById("live-pill").addEventListener("click", async () => {
  if (!campaign) await loadCampaign();
  if (!campaign) return;
  const next = !campaign.live;
  if (next) {
    if (!campaign.can_trade) {
      window.alert("This host does not have Kalshi keys loaded. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH, restart serve, then tap LIVE.");
      return;
    }
    if (!window.confirm("Go LIVE? Fire 15m / hourly / maker will send real post-only orders to Kalshi from this process.")) {
      return;
    }
  }
  const pill = document.getElementById("live-pill");
  pill.disabled = true;
  try {
    const response = await fetch("/api/campaign/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ live: next }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      window.alert(data.detail || "Could not change DRY / LIVE.");
      return;
    }
    campaign = data.status || campaign;
    renderCampaign();
  } finally {
    pill.disabled = false;
  }
});

document.getElementById("halt-btn").addEventListener("click", async () => {
  if (!campaign) return;
  const next = !campaign.halted;
  const btn = document.getElementById("halt-btn");
  btn.disabled = true;
  try {
    const response = await fetch("/api/campaign/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ halted: next }),
    });
    if (response.ok) {
      const data = await response.json();
      campaign = data.status || campaign;
      renderCampaign();
    }
  } finally {
    btn.disabled = false;
  }
});

setInterval(() => {
  if (updated) updated.textContent = etClock();
}, 1000);

function findRow(ticker) {
  if (!snapshot || !ticker) return null;
  for (const key of ["crypto", "commodities", "sports"]) {
    const hit = (snapshot.sections[key].predictions || []).find((row) => row.market_ticker === ticker);
    if (hit) return hit;
  }
  return null;
}

function openMarket(row, push = false) {
  selected = row;
  if (push) history.pushState({ view: "trade", ticker: row.market_ticker }, "", `/market/${encodeURIComponent(row.market_ticker)}`);
  setView("trade");
  document.getElementById("trade-title").textContent = row.event_title;
  document.getElementById("trade-sub").textContent = row.subtitle || row.market_title;
  document.getElementById("trade-why").textContent = row.rationale || "";
  document.getElementById("trade-kalshi").href = `https://kalshi.com/markets/${encodeURIComponent((row.series_ticker || "").toLowerCase())}`;
  renderTradePair(row);
  loadChart();
  stopChart();
  chartTimer = setInterval(loadChart, 15000);
}

function renderTradePair(row) {
  const pickYes = row.side === "YES";
  const pickNo = row.side === "NO";
  document.getElementById("trade-pair").innerHTML = `
    <span class="leg yes ${pickYes ? "pick" : ""}"><span class="leg-name">Yes quote</span><span class="leg-px">${cents(yesAsk(row))}</span></span>
    <span class="leg no ${pickNo ? "pick" : ""}"><span class="leg-name">No quote</span><span class="leg-px">${cents(noAsk(row))}</span></span>`;
}

function stopChart() {
  if (chartTimer) clearInterval(chartTimer);
  chartTimer = null;
}

function drawChart(points) {
  const svg = document.getElementById("chart");
  const empty = document.getElementById("chart-empty");
  if (!points.length) {
    svg.innerHTML = "";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  const w = 640;
  const h = 240;
  const pad = { l: 8, r: 48, t: 16, b: 20 };
  const ys = points.map((p) => p.yes);
  let min = Math.min(...ys);
  let max = Math.max(...ys);
  if (max - min < 0.02) {
    min = Math.max(0, min - 0.02);
    max = Math.min(1, max + 0.02);
  }
  const xs = points.map((p) => p.ts);
  const x0 = xs[0];
  const x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
  const x = (ts) => pad.l + ((ts - x0) / (x1 - x0)) * (w - pad.l - pad.r);
  const y = (val) => pad.t + (1 - (val - min) / (max - min)) * (h - pad.t - pad.b);
  const coords = points.map((p) => `${x(p.ts).toFixed(1)},${y(p.yes).toFixed(1)}`);
  const line = coords.join(" ");
  const area = `${pad.l},${h - pad.b} ${coords.join(" ")} ${x(points[points.length - 1].ts).toFixed(1)},${h - pad.b}`;
  const last = points[points.length - 1];
  const first = points[0];
  const up = last.yes >= first.yes;
  const stroke = up ? "#1ecb73" : "#ff4d6d";
  const fill = up ? "rgba(30,203,115,0.16)" : "rgba(255,77,109,0.16)";
  svg.innerHTML = `
    <polyline points="${pad.l},${y(min).toFixed(1)} ${w - pad.r},${y(min).toFixed(1)}" fill="none" stroke="#2a2a2e" />
    <polyline points="${pad.l},${y((min + max) / 2).toFixed(1)} ${w - pad.r},${y((min + max) / 2).toFixed(1)}" fill="none" stroke="#222226" />
    <polygon points="${area}" fill="${fill}" />
    <polyline points="${line}" fill="none" stroke="${stroke}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
    <circle cx="${x(last.ts).toFixed(1)}" cy="${y(last.yes).toFixed(1)}" r="4" fill="${stroke}" />
    <text x="${w - 6}" y="${y(last.yes).toFixed(1)}" text-anchor="end" dominant-baseline="middle" fill="${stroke}" font-size="12" font-family="DM Mono, monospace">${(100 * last.yes).toFixed(0)}¢</text>`;
}

async function loadChart() {
  if (!selected) return;
  const response = await fetch(`/api/chart/${encodeURIComponent(selected.series_ticker)}/${encodeURIComponent(selected.market_ticker)}?hours=${chartHours}`);
  if (!response.ok) {
    document.getElementById("chart-empty").classList.remove("hidden");
    document.getElementById("chart-empty").textContent = "Chart unavailable";
    return;
  }
  chartData = await response.json();
  const live = chartData.live || {};
  if (live.yes != null) {
    selected = { ...selected, yes_bid: live.yes_bid, yes_ask: live.yes_ask, market_prob: live.yes };
    renderTradePair(selected);
    document.getElementById("trade-last").textContent = cents(live.yes);
  }
  const change = chartData.change;
  const ch = document.getElementById("trade-change");
  if (change == null) {
    ch.textContent = "";
    ch.className = "edge";
  } else {
    ch.textContent = `${change >= 0 ? "+" : ""}${(100 * change).toFixed(1)}¢`;
    ch.className = `edge ${change >= 0 ? "pos" : "neg"}`;
  }
  document.getElementById("chart-meta").innerHTML = `<span>Live · ${chartHours}H · ${chartData.points.length} prints · ${chartData.interval}m bars</span><span>${closeLabel(live.close_time || selected.close_time)}</span>`;
  drawChart(chartData.points || []);
}

function route() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  if (path === "/portfolio") {
    setView("portfolio");
    return;
  }
  if (path.startsWith("/market/")) {
    const ticker = decodeURIComponent(path.slice("/market/".length));
    const row = findRow(ticker) || selected || { market_ticker: ticker, series_ticker: ticker.split("-")[0], event_title: ticker, subtitle: "", side: "" };
    openMarket(row, false);
    return;
  }
  setView("markets");
}

load(false).then(() => route());
loadCampaign();
setInterval(() => load(false), 60000);
setInterval(loadCampaign, 30000);
