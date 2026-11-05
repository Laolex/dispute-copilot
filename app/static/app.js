/* Dispute Copilot: AG Grid views for proposals and the audit log.
 * Security: every value from the server is rendered with textContent / DOM nodes, never written as HTML. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function pill(text, kind) { return el("span", "pill pill-" + kind, text); }

  // ---- theme: AG Grid's Quartz, tuned to the dashboard palette ------------------------------
  var theme = agGrid.themeQuartz.withParams({
    accentColor: "#315efb", backgroundColor: "#ffffff", foregroundColor: "#172b4d",
    headerBackgroundColor: "#f5f7fb", borderColor: "#e8edf3", rowHoverColor: "#f5f8ff",
    fontFamily: "system-ui, sans-serif", headerFontWeight: 700, wrapperBorderRadius: 0, borderRadius: 8
  });

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error(url + " -> HTTP " + r.status);
      return r.json();
    });
  }

  // ---- proposals ----------------------------------------------------------------------------
  var ACTIONS = { contest: ["Contest", "ok"], accept: ["Accept", "warn"], review: ["Needs review", "neutral"], wait: ["Wait", "neutral"] };
  var STATES = { PENDING: "accent", APPROVED: "accent", EXECUTED: "ok", FAILED: "bad", REJECTED: "neutral", SUPERSEDED: "neutral", UNCERTAIN: "warn" };

  function idCell(p) {
    var a = el("a", null, "#" + p.data.id);
    a.href = "/proposals/" + p.data.id;
    a.addEventListener("click", function (ev) { ev.stopPropagation(); });
    return a;
  }
  function actionCell(p) {
    var r = p.data, a = ACTIONS[r.action] || [r.action, "neutral"], w = el("span");
    w.appendChild(pill(a[0], a[1]));
    if (r.link_rel) w.appendChild(el("span", "muted", " via " + r.link_rel.replace(/_/g, " ")));
    if (!r.executable && r.state === "PENDING" && (r.action === "contest" || r.action === "accept")) w.appendChild(el("span", "muted", " (not executable)"));
    return w;
  }
  function dueCell(p) {
    if (p.value == null || p.data.state !== "PENDING") return el("span", "muted", "—");
    var days = (p.value - Date.now()) / 86400000, text, kind;
    if (days < 0) { text = "overdue"; kind = "bad"; }
    else if (days < 1) { text = "due today"; kind = "bad"; }
    else if (days < 3) { text = Math.floor(days) + "d left"; kind = "warn"; }
    else { text = Math.floor(days) + "d left"; kind = "neutral"; }
    var w = el("span"); w.appendChild(pill(text, kind)); return w;
  }
  function claimCell(p) {
    if (p.data.ai_disagrees) return pill("AI disagrees", "bad");
    if (p.data.mismatch) return pill("claim ≠ reason", "bad");
    return el("span", "muted", (p.data.claim_type || "").replace(/_/g, " "));
  }
  function stateCell(p) { return pill(p.value, STATES[p.value] || "neutral"); }
  function money(p) {
    if (p.value == null) return "";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: p.data.currency || "USD" }).format(p.value); }
    catch (e) { return String(p.value); }
  }

  function proposalColumns() {
    return [
      { headerName: "#", field: "id", width: 80, minWidth: 70, sort: "desc", cellRenderer: idCell, floatingFilter: false, filter: "agNumberColumnFilter" },
      { headerName: "Dispute", field: "dispute_id", width: 180, minWidth: 150, cellClass: "mono" },
      { headerName: "Proposed action", field: "action", width: 280, minWidth: 210, cellRenderer: actionCell },
      { headerName: "Amount", field: "amount", width: 110, minWidth: 90, type: "rightAligned", valueFormatter: money, floatingFilter: false, filter: "agNumberColumnFilter" },
      { headerName: "Respond by", colId: "due", width: 130, minWidth: 110, floatingFilter: false, filter: "agNumberColumnFilter",
        valueGetter: function (p) { return p.data.due ? Date.parse(p.data.due) : null; }, cellRenderer: dueCell },
      { headerName: "Stage", field: "stage", width: 110, minWidth: 90, hide: true },
      { headerName: "Claim", field: "claim_type", width: 150, minWidth: 130, cellRenderer: claimCell },
      { headerName: "State", field: "state", width: 120, minWidth: 110, cellRenderer: stateCell },
      { headerName: "Rule", field: "rule_id", width: 210, minWidth: 150, cellClass: "mono", hide: true },
      { headerName: "PayPal action", field: "link_rel", width: 160, hide: true },   // searchable via the filter box
      { headerName: "Dispute reason", field: "reason", width: 260, hide: true }
    ];
  }

  function renderTiles(rows) {
    var host = document.getElementById("tiles");
    if (!host) return;
    var pending = rows.filter(function (r) { return r.state === "PENDING"; });
    var byCur = {}, due = null;
    pending.forEach(function (r) {
      var c = r.currency || "?"; byCur[c] = (byCur[c] || 0) + (r.amount || 0);
      var t = r.due ? Date.parse(r.due) : null; if (t && (due === null || t < due)) due = t;
    });
    var exposure = Object.keys(byCur).sort().map(function (c) {
      try { return new Intl.NumberFormat(undefined, { style: "currency", currency: c }).format(byCur[c]); }
      catch (e) { return c + " " + byCur[c].toFixed(2); }
    }).join(" · ") || "—";
    var next = "—";
    if (due !== null) { var d = (due - Date.now()) / 86400000; next = d < 0 ? "overdue" : d < 1 ? "today" : Math.floor(d) + (Math.floor(d) === 1 ? " day" : " days"); }
    var disagree = pending.filter(function (r) { return r.ai_disagrees; }).length;
    var tiles = [
      [String(pending.length), "Awaiting your approval", disagree ? disagree + " where the AI disagrees with policy" : "Human approval required"],
      [exposure, "Exposure in pending proposals", "Grouped by currency"],
      [next, "Nearest response deadline", "Earliest pending dispute"],
      [String(rows.filter(function (r) { return r.state === "EXECUTED"; }).length), "Executed actions", "Each one human-approved"]
    ];
    host.textContent = "";
    tiles.forEach(function (t, i) {
      var d = el("div", "stat");
      d.appendChild(el("div", "stat-top", t[1]));
      d.appendChild(el("div", "stat-value" + (i === 1 && t[0].length > 14 ? " small" : ""), t[0]));
      d.appendChild(el("div", "stat-foot", t[2]));
      host.appendChild(d);
    });
  }

  function mountProposals(host) {
    var api = agGrid.createGrid(host, {
      theme: theme, columnDefs: proposalColumns(), rowData: [],
      defaultColDef: { sortable: true, resizable: true, filter: "agTextColumnFilter", floatingFilter: true },
      rowHeight: 44, animateRows: true, tooltipShowDelay: 400,
      autoSizeStrategy: { type: "fitGridWidth" }, includeHiddenColumnsInQuickFilter: true,
      getRowId: function (p) { return String(p.data.id); },
      rowClassRules: { "dc-pending": function (p) { return p.data && p.data.state === "PENDING"; } },
      onRowClicked: function (e) { window.location.href = "/proposals/" + e.data.id; },
      overlayNoRowsTemplate: "<span>No proposals yet. Click &ldquo;Poll PayPal now&rdquo; or wait for a webhook.</span>"
    });
    var q = document.getElementById("q");
    if (q) q.addEventListener("input", function () { api.setGridOption("quickFilterText", q.value); });
    function refresh() {
      getJSON("/api/proposals").then(function (rows) { api.setGridOption("rowData", rows); renderTiles(rows); })
        .catch(function (e) { api.setGridOption("overlayNoRowsTemplate", "<span>Could not load proposals.</span>"); console.error(e); });
    }
    refresh();
    setInterval(refresh, 20000);
  }

  // ---- audit log ----------------------------------------------------------------------------
  function eventKind(e) {
    if (/^(execute\.(failed|refused)|webhook\.rejected|agent\.error|webhook\.ingest_error)/.test(e)) return "bad";
    if (/^execute\.done/.test(e)) return "ok";
    if (/^proposal\./.test(e)) return "accent";
    if (/^agent\./.test(e)) return "purple";
    if (/^webhook\./.test(e)) return "teal";
    return "neutral";
  }
  function eventCell(p) { return pill(p.value, eventKind(p.value)); }

  function showDetail(host, row) {
    host.textContent = "";
    var pretty = row.payload;
    try { pretty = JSON.stringify(JSON.parse(row.payload), null, 2); } catch (e) { /* show raw */ }
    var head = el("h3", null, "#" + row.seq + "  " + row.event);
    head.appendChild(el("small", "muted", "   " + row.actor + " · " + row.ts));
    host.appendChild(head);
    host.appendChild(el("pre", "mono", pretty));
  }

  function mountAudit(host) {
    var detail = document.getElementById("detail"), count = document.getElementById("count");
    var api = agGrid.createGrid(host, {
      theme: theme, rowData: [],
      columnDefs: [
        { headerName: "#", field: "seq", width: 90, sort: "desc", filter: "agNumberColumnFilter" },
        { headerName: "Time", field: "ts", width: 200, valueFormatter: function (p) { return p.value ? new Date(p.value).toLocaleString() : ""; } },
        { headerName: "Actor", field: "actor", width: 130 },
        { headerName: "Event", field: "event", width: 230, cellRenderer: eventCell },
        { headerName: "Payload", field: "payload", flex: 1, minWidth: 300, cellClass: "mono", tooltipField: "payload" }
      ],
      defaultColDef: { sortable: true, resizable: true, filter: "agTextColumnFilter", floatingFilter: true },
      rowHeight: 38, tooltipShowDelay: 400, autoSizeStrategy: { type: "fitGridWidth" },
      getRowId: function (p) { return String(p.data.seq); },
      onRowClicked: function (e) { showDetail(detail, e.data); },
      overlayNoRowsTemplate: "<span>No audit entries yet.</span>"
    });
    var q = document.getElementById("q");
    if (q) q.addEventListener("input", function () { api.setGridOption("quickFilterText", q.value); });
    function refresh() {
      getJSON("/api/audit").then(function (res) {
        api.setGridOption("rowData", res.rows);
        if (count) count.textContent = res.rows.length + (res.rows.length === 1 ? " entry · " : " entries · ") + (res.ok ? "hash chain verified" : "CHAIN BROKEN at #" + res.bad_seq);
      }).catch(function (e) { console.error(e); });
    }
    refresh();
    setInterval(refresh, 20000);
  }

  var host = document.getElementById("grid");
  if (host && window.agGrid) {
    if (host.dataset.kind === "audit") mountAudit(host); else mountProposals(host);
  }
})();
