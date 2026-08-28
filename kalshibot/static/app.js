const board = document.getElementById("board");
const updated = document.getElementById("updated");
const disclaimer = document.getElementById("disclaimer");
const filterInput = document.getElementById("filter");
const edgesOnly = document.getElementById("edges-only");
const refreshBtn = document.getElementById("refresh");

let snapshot = null;
let section = "crypto";

function pct(value) {
  if (value == null) return "—";
  return `${(100 * value).toFixed(1)}%`;
}

function cents(value) {
  const n = 100 * value;
  const sign = n >= 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}¢`;
}

function closeLabel(iso) {
  if (!iso) return "n/a";
  const ms = Date.parse(iso) - Date.now();
  if (Number.isNaN(ms)) return iso;
  if (ms < 0) return "closed";
  const hours = ms / 36e5;
  if (hours < 1) return `${Math.max(1, Math.round(ms / 6e4))}m`;
  if (hours < 48) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function render() {
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
      const edgeClass = row.edge >= 0.02 ? "pos" : row.edge < 0 ? "neg" : "";
      return `
        <article class="card">
          <div>
            <div class="event">${escapeHtml(row.event_title)}</div>
            <div class="sub">${escapeHtml(row.subtitle || row.market_title)} · ${escapeHtml(row.market_ticker)}</div>
            <div class="muted">Closes ${closeLabel(row.close_time)}${row.spot != null ? ` · spot ${formatSpot(row.spot)}` : ""}</div>
          </div>
          <div>
            <div class="label">Market</div>
            <div class="num">${pct(row.market_prob)}</div>
          </div>
          <div>
            <div class="label">Bot</div>
            <div class="num">${pct(row.model_prob)}</div>
          </div>
          <div>
            <div class="label">Edge</div>
            <div class="num ${edgeClass}">${cents(row.edge)}<span class="side">${escapeHtml(row.side)}</span></div>
          </div>
          <div class="why">${escapeHtml(row.rationale)} · vol24 ${Number(row.volume_24h).toLocaleString()}</div>
        </article>`;
    })
    .join("");
}

function formatSpot(value) {
  if (value >= 100) return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
  if (value >= 1) return `$${value.toFixed(3)}`;
  return `$${value.toPrecision(3)}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function updateStats() {
  for (const key of ["crypto", "commodities", "sports"]) {
    const stats = snapshot.sections[key].stats;
    document.getElementById(`stat-${key}`).textContent =
      `${stats.opportunities} edges · ${stats.markets} markets`;
  }
}

async function load(force = false) {
  updated.textContent = "Scanning Kalshi…";
  board.innerHTML = `<div class="status">Pulling Crypto, Commodities, and Sports Bets…</div>`;
  const response = await fetch(`/api/predictions${force ? "?force=true" : ""}`);
  if (!response.ok) {
    board.innerHTML = `<div class="empty">Could not load predictions (${response.status}).</div>`;
    return;
  }
  snapshot = await response.json();
  disclaimer.textContent = snapshot.disclaimer;
  const when = new Date(snapshot.generated_at);
  updated.textContent = `Updated ${when.toLocaleTimeString()}`;
  updateStats();
  render();
}

document.querySelectorAll(".sec").forEach((btn) => {
  btn.addEventListener("click", () => {
    section = btn.dataset.section;
    document.querySelectorAll(".sec").forEach((el) => el.classList.toggle("active", el === btn));
    render();
  });
});

filterInput.addEventListener("input", render);
edgesOnly.addEventListener("change", render);
refreshBtn.addEventListener("click", () => {
  load(true);
  loadCampaign();
});

load(false);
loadCampaign();
setInterval(() => load(false), 60000);
setInterval(loadCampaign, 30000);

async function loadCampaign() {
  const response = await fetch("/api/campaign");
  if (!response.ok) return;
  const data = await response.json();
  document.getElementById("book-room").textContent = `room $${Number(data.room).toFixed(2)}`;
  const cash = data.kalshi_cash != null ? ` · Kalshi $${Number(data.kalshi_cash).toFixed(2)}` : "";
  const cap = data.bankroll_cap != null ? ` · cap $${Number(data.bankroll_cap).toFixed(2)}` : "";
  const idea = data.typical_idea != null ? ` · ~$${Number(data.typical_idea).toFixed(2)}/idea` : "";
  document.getElementById("book-meta").textContent =
    `realized $${Number(data.realized).toFixed(2)} · open $${Number(data.open_cost).toFixed(2)} · book $${Number(data.equity ?? data.bankroll).toFixed(2)}${cash}${cap}${idea}`;
  document.getElementById("maker-mode").textContent = data.live ? "LIVE" : "DRY";
  const pb = data.playbook || {};
  const follow = data.follow_kalshi_cash ? "follows Kalshi cash" : "fixed book";
  if (pb.risk_cap) {
    document.getElementById("playbook-meta").textContent =
      `${follow} · ${(100 * pb.typical_risk_min).toFixed(0)}–${(100 * pb.typical_risk_max).toFixed(0)}% · cap ${(100 * pb.risk_cap).toFixed(0)}%`;
  }
  const lines = (data.log || []).map((row) => `${row.ts.slice(11, 19)}  ${row.loop}  ${row.message}`);
  document.getElementById("campaign-log").textContent = lines.join("\n") || "No campaign fires yet.";
}

document.querySelectorAll("[data-fire]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    document.getElementById("campaign-log").textContent = `Firing ${btn.dataset.fire}…`;
    try {
      const response = await fetch(`/api/campaign/fire/${btn.dataset.fire}`, { method: "POST" });
      const data = await response.json();
      document.getElementById("campaign-log").textContent = (data.actions || []).join("\n");
      await loadCampaign();
    } finally {
      btn.disabled = false;
    }
  });
});
