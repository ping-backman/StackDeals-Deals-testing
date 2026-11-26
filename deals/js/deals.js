(() => {
  const DEALS_API = "https://deals-api.getstackdeals.xyz";
  const PUBLIC_FALLBACK_JSON = "data/public-deals.json";

  let publicDeals = [];
  let vipDeals = [];
  let activeCategory = "ALL";
  let fbShareMode = false;

  const urlParams = new URLSearchParams(window.location.search);
  if (urlParams.get("session")) {
    localStorage.setItem("sd_session", urlParams.get("session"));
  }
  const sdSession = localStorage.getItem("sd_session") || "";
  const spotlightDealId = urlParams.get("deal") || "";

  if (urlParams.get("fb") === "1") {
    fbShareMode = true;
    document.getElementById("fb-mode-state").textContent = "ON";
    document.getElementById("fb-mode-btn").classList.add("active");
  }

  function showToast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.remove("hidden");
    setTimeout(() => el.classList.add("hidden"), 2400);
  }

  async function init() {
    await Promise.all([loadPublicDeals(), checkVipSession()]);
  }

  async function loadPublicDeals() {
    try {
      const res = await fetch(`${DEALS_API}/api/deals/public`);
      const data = await res.json();
      publicDeals = data.deals || [];
      document.getElementById("last-updated").textContent = `Updated: ${new Date(data.updated_at).toLocaleString()}`;
    } catch (_) {
      const res = await fetch(PUBLIC_FALLBACK_JSON);
      const data = await res.json();
      publicDeals = data.deals || [];
    }

    if (spotlightDealId) {
      const match = publicDeals.find(d => d.id === spotlightDealId);
      if (match) renderSpotlight(match);
    }
    renderPublicGrid();
  }

  async function checkVipSession() {
    const authPill = document.getElementById("auth-pill");
    if (!sdSession) {
      authPill.textContent = "Sign In via Giveaway";
      document.getElementById("vip-locked-card").classList.remove("hidden");
      return;
    }

    try {
      const res = await fetch(`${DEALS_API}/api/deals/vip?session=${encodeURIComponent(sdSession)}`);
      const data = await res.json();

      if (data.unlocked) {
        authPill.textContent = `👑 VIP Unlocked (${data.progress.qualified_referrals}/5 Refs)`;
        document.getElementById("vip-locked-card").classList.add("hidden");
        document.getElementById("vip-unlocked-wrap").classList.remove("hidden");
        document.getElementById("vip-points-badge").textContent = `${data.progress.total_points} Draw Points`;
        vipDeals = data.deals || [];
        renderGrid(vipDeals, "vip-grid");
      } else if (data.authenticated && data.progress) {
        const p = data.progress;
        authPill.textContent = `Signed In (${p.qualified_referrals}/5 VIP Refs)`;
        document.getElementById("vip-locked-card").classList.remove("hidden");
        document.getElementById("chk-auth").textContent = p.self_verified
          ? "✅ Email verified on StackDeals"
          : "⏳ Verify your email on StackDeals";
        document.getElementById("chk-self-task").textContent = p.self_task_done
          ? "✅ Completed at least 1 offerwall task"
          : "⏳ Complete at least 1 quick offerwall task yourself";
        document.getElementById("ref-counter").textContent = `${p.qualified_referrals}/5`;
        if (p.qualified_referrals >= 5) {
          document.getElementById("chk-refs").innerHTML = "✅ 5/5 qualified referrals completed!";
        }
      } else {
        authPill.textContent = "Sign In via Giveaway";
        document.getElementById("vip-locked-card").classList.remove("hidden");
      }
    } catch (_) {
      authPill.textContent = "Giveaway Portal";
      document.getElementById("vip-locked-card").classList.remove("hidden");
    }
  }

  function renderSpotlight(deal) {
    const wrap = document.getElementById("spotlight-banner");
    const box = document.getElementById("spotlight-content");
    wrap.classList.remove("hidden");
    box.innerHTML = `
      <div style="flex:1; min-width:240px;">
        <span style="background:#f59e0b;color:#0b0f19;font-weight:800;font-size:11px;padding:3px 8px;border-radius:6px;">
          FEATURED FIND • ${escapeHtml(deal.badge)}
        </span>
        <h2 style="font-size:18px;margin:8px 0;">${escapeHtml(deal.title)}</h2>
        <div class="price-row">
          ${deal.deal_price ? `<span class="price-now">$${Number(deal.deal_price).toFixed(2)}</span>` : ""}
          ${deal.retail_price ? `<span class="price-was">$${Number(deal.retail_price).toFixed(2)}</span>` : ""}
          ${deal.promo_code ? `<span class="code-val">Code: ${escapeHtml(deal.promo_code)}</span>` : ""}
        </div>
      </div>
      <div style="min-width:210px;">
        <button class="btn-grab" data-grab="${deal.id}" data-url="${escapeHtml(deal.affiliate_url)}" data-code="${escapeHtml(deal.promo_code || "")}">
          ${deal.promo_code ? "Copy Code &amp; Claim on Amazon →" : "Claim Deal on Amazon →"}
        </button>
      </div>
    `;
  }

  function renderPublicGrid() {
    const q = document.getElementById("search-input").value.trim().toLowerCase();
    const sortBy = document.getElementById("sort-select").value;

    let filtered = publicDeals.filter(d => {
      const catOk = activeCategory === "ALL" || d.category === activeCategory;
      const qOk = !q || d.title.toLowerCase().includes(q) || (d.promo_code || "").toLowerCase().includes(q);
      return catOk && qOk;
    });

    if (sortBy === "discount") {
      filtered.sort((a, b) => (b.discount_pct || 0) - (a.discount_pct || 0));
    } else if (sortBy === "likes") {
      filtered.sort((a, b) => (b.likes || 0) - (a.likes || 0));
    } else if (sortBy === "price_asc") {
      filtered.sort((a, b) => (a.deal_price || 9999) - (b.deal_price || 9999));
    }

    document.getElementById("deal-count").textContent = `Showing ${filtered.length} verified Amazon deals`;
    renderGrid(filtered, "public-grid");
  }

  function renderGrid(list, containerId) {
    const grid = document.getElementById(containerId);
    if (!list.length) {
      grid.innerHTML = `<p style="color:#94a3b8;grid-column:1/-1;text-align:center;padding:30px;">No deals match this filter right now.</p>`;
      return;
    }

    grid.innerHTML = list.map(d => {
      const shareBridgeUrl = `${DEALS_API}/s/${encodeURIComponent(d.id)}`;
      const fbCaption = d.fb_caption || `🔥 ${d.badge}: ${d.title}\n👇 Link in first comment!`;
      const fbComment = `👉 Grab it here: ${shareBridgeUrl}`;

      return `
        <article class="deal-card">
          <div class="card-media">
            <span class="badge-discount">${escapeHtml(d.badge)}</span>
            ${d.image_url
              ? `<img src="${escapeHtml(d.image_url)}" alt="${escapeHtml(d.title)}" loading="lazy" />`
              : `<div class="placeholder-icon">🛍️</div>`}
            <span class="badge-cat">${escapeHtml(d.category)}</span>
          </div>
          <div class="card-body">
            <div>
              <h3 class="deal-title">${escapeHtml(d.title)}</h3>
              <div class="price-row">
                ${d.deal_price ? `<span class="price-now">$${Number(d.deal_price).toFixed(2)}</span>` : `<span class="price-now" style="font-size:15px;">See Deal Price</span>`}
                ${d.retail_price ? `<span class="price-was">$${Number(d.retail_price).toFixed(2)}</span>` : ""}
              </div>
              ${d.promo_code ? `
                <div class="code-box">
                  <span>Code:</span>
                  <span class="code-val">${escapeHtml(d.promo_code)}</span>
                </div>` : ""}
            </div>

            <div>
              <button class="btn-grab" data-grab="${d.id}" data-url="${escapeHtml(d.affiliate_url)}" data-code="${escapeHtml(d.promo_code || "")}">
                ${d.promo_code ? "Copy Code &amp; Get Deal →" : "Get Deal on Amazon →"}
              </button>

              <div class="card-actions">
                <button class="act-btn" data-like="${d.id}">❤️ <span>${d.likes || 12}</span></button>
                <button class="act-btn" data-share="${escapeHtml(shareBridgeUrl)}">🔗 Share</button>
                <button class="act-btn" data-report="${d.id}" title="Report expired code or price jump">🚩 Dead?</button>
              </div>

              ${fbShareMode ? `
                <div class="fb-tools">
                  <button class="fb-copy-btn" data-copy-text="${encodeURIComponent(fbCaption)}">1️⃣ Copy FB Post Caption (No Link)</button>
                  <button class="fb-copy-btn secondary" data-copy-text="${encodeURIComponent(fbComment)}">2️⃣ Copy 1st Comment Link (deals.getstackdeals.xyz)</button>
                </div>` : ""}
            </div>
          </div>
        </article>
      `;
    }).join("");
  }

  document.addEventListener("click", async (e) => {
    const grabBtn = e.target.closest("[data-grab]");
    if (grabBtn) {
      const id = grabBtn.dataset.grab;
      const url = grabBtn.dataset.url;
      const code = grabBtn.dataset.code;
      if (code && navigator.clipboard) {
        navigator.clipboard.writeText(code).catch(() => {});
        showToast(`Copied code ${code} — opening Amazon...`);
      }
      fetch(`${DEALS_API}/api/deals/claim`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deal_id: id })
      }).catch(() => {});
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }

    const likeBtn = e.target.closest("[data-like]");
    if (likeBtn) {
      const id = likeBtn.dataset.like;
      const span = likeBtn.querySelector("span");
      span.textContent = Number(span.textContent) + 1;
      fetch(`${DEALS_API}/api/deals/like`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deal_id: id, session: sdSession })
      }).catch(() => {});
      return;
    }

    const shareBtn = e.target.closest("[data-share]");
    if (shareBtn) {
      navigator.clipboard.writeText(shareBtn.dataset.share);
      showToast("✅ Copied StackDeals share link!");
      return;
    }

    const reportBtn = e.target.closest("[data-report]");
    if (reportBtn) {
      const id = reportBtn.dataset.report;
      fetch(`${DEALS_API}/api/deals/report`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deal_id: id })
      }).catch(() => {});
      showToast("🚩 Flagged! Auto-hides at 3 reports.");
      return;
    }

    const copyBtn = e.target.closest("[data-copy-text]");
    if (copyBtn) {
      const txt = decodeURIComponent(copyBtn.dataset.copyText);
      navigator.clipboard.writeText(txt);
      showToast("✅ Copied for Facebook!");
    }
  });

  document.querySelectorAll(".nav-tab").forEach(tab => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".nav-tab").forEach(t => t.classList.remove("active"));
      tab.classList.add("active");
      const view = tab.dataset.view;
      document.getElementById("view-public").classList.toggle("hidden", view !== "public");
      document.getElementById("view-vip").classList.toggle("hidden", view !== "vip");
    });
  });

  document.getElementById("category-pills").addEventListener("click", (e) => {
    const pill = e.target.closest(".pill");
    if (!pill) return;
    document.querySelectorAll(".pill").forEach(p => p.classList.remove("active"));
    pill.classList.add("active");
    activeCategory = pill.dataset.cat;
    renderPublicGrid();
  });

  document.getElementById("search-input").addEventListener("input", renderPublicGrid);
  document.getElementById("sort-select").addEventListener("change", renderPublicGrid);

  document.getElementById("fb-mode-btn").addEventListener("click", () => {
    fbShareMode = !fbShareMode;
    document.getElementById("fb-mode-state").textContent = fbShareMode ? "ON" : "OFF";
    document.getElementById("fb-mode-btn").classList.toggle("active", fbShareMode);
    renderPublicGrid();
    if (vipDeals.length) renderGrid(vipDeals, "vip-grid");
  });

  function escapeHtml(str) {
    return String(str || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  init();
})();
