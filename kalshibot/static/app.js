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
  let rows = block.predictions || [];
  if (edgesOnly.checked) rows = rows.filter((r) => r.edge >= 0.02);
  if (q) {
    rows = rows.filter((r) =>
      `${r.event_title} ${r.market_title} ${r.subtitle} ${r.market_ticker} ${r.asset || ""}`
        .toLowerCase()
        .includes(q)
    );
  }

  if (!rows.length) {
    board.innerHTML = `<div class="empty">No markets in this view. Try clearing filters or refreshing.</div>`;
    return;
  }

  board.innerHTML = rows
    .map((row) => {
      const pickYes = row.side === "YES";
      const pickNo = row.side === "NO";
      const edgeClass = row.edge >= 0.02 ? "pos" : row.edge < 0 ? "neg" : "";
      const href = `https://kalshi.com/markets/${encodeURIComponent((row.series_ticker || "").toLowerCase())}`;
      return `
        <article class="card">
          <div class="card-top">
            <div class="event">${escapeHtml(row.event_title)}</div>
            <div class="ticker">${escapeHtml(row.asset || "")}</div>
          </div>
          <div class="sub">${escapeHtml(row.subtitle || row.market_title)}</div>
          <div class="pair">
            <a class="leg yes ${pickYes ? "pick" : ""}" href="${href}" target="_blank" rel="noreferrer">
              <span class="leg-name">Yes</span>
              <span class="leg-px">${cents(yesAsk(row))}</span>
            </a>
            <a class="leg no ${pickNo ? "pick" : ""}" href="${href}" target="_blank" rel="noreferrer">
              <span class="leg-name">No</span>
              <span class="leg-px">${cents(noAsk(row))}</span>
            </a>
          </div>
          <div class="meta-row">
            <span>${[closeLabel(row.close_time), volumeLabel(row.volume_24h || row.volume), row.spot != null ? `spot ${formatSpot(row.spot)}` : ""].filter(Boolean).join(" · ")}</span>
            <span class="edge ${edgeClass}">fair ${pct(row.model_prob)} · ${cents(row.edge, true)}</span>
          </div>
          <div class="why">${escapeHtml(row.rationale || "")}</div>
        </article>`;
    })
    .join("");
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
  document.querySelectorAll(".nav-link").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
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
  if (campaign.halted) {
    pill.classList.add("halted");
    pill.textContent = "HALTED";
  } else if (campaign.live) {
    pill.classList.add("live");
    pill.textContent = "LIVE";
  } else {
    pill.classList.add("dry");
    pill.textContent = "DRY";
  }

  const chips = [];
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
  document.getElementById("book-meta").textContent =
    `${follow} · ${campaign.open_tickets?.length || 0} open · ${campaign.rests?.length || 0} resting`;

  renderBlotter();
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
    blotter.innerHTML = `<div class="empty">${blot === "orders" ? "No resting orders." : "No open positions."}</div>`;
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
  if (!snapshot) board.innerHTML = `<div class="status">Pulling live Kalshi markets…</div>`;
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

document.querySelectorAll(".nav-link").forEach((btn) => {
  btn.addEventListener("click", () => setView(btn.dataset.view));
});

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

load(false);
loadCampaign();
setInterval(() => load(false), 60000);
setInterval(loadCampaign, 30000);
