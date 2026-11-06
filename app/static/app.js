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


  // ---- shared grid helpers -------------------------------------------------------------------
  function store(key) {   // per-viewer convenience only: remembers columns, sort and filters; the page works without it
    return {
      load: function () { try { var v = window.localStorage.getItem(key); return v ? JSON.parse(v) : undefined; } catch (e) { return undefined; } },
      save: function (state) { try { window.localStorage.setItem(key, JSON.stringify(state)); } catch (e) { /* private window */ } }
    };
  }
  function csvSafe(v) {   // spreadsheet formula injection: a cell starting with = + - @ would run in Excel
    var t = v == null ? "" : String(v);
    return /^[=+\-@\t\r]/.test(t) ? "'" + t : t;
  }
  function wireExport(api, name) {
    var b = document.getElementById("export");
    if (!b) return;
    b.addEventListener("click", function () {
      api.exportDataAsCsv({ fileName: name + "-" + new Date().toISOString().slice(0, 10) + ".csv",
        processCellCallback: function (p) { return csvSafe(p.formatValue ? p.formatValue(p.value) : p.value); } });
    });
  }
  function wireCount(api, total) {
    var el = document.getElementById("shown");
    if (!el) return;
    function upd() { var n = api.getDisplayedRowCount(); el.textContent = n === total() ? n + " rows" : n + " of " + total() + " rows"; }
    api.addEventListener("modelUpdated", upd); upd();
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
  function riskCell(p) {
    if (p.value === "high") return pill("High risk", "bad");
    if (p.value === "elevated") return pill("Risk flagged", "warn");
    return el("span", "muted", p.value ? "—" : "");
  }
  function stateCell(p) { return pill(p.value, STATES[p.value] || "neutral"); }
  function money(p) {
    if (p.value == null) return "";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: p.data.currency || "USD" }).format(p.value); }
    catch (e) { return String(p.value); }
  }

  function proposalColumns() {
    return [
      { headerName: "#", field: "id", width: 64, minWidth: 56, sort: "desc", cellRenderer: idCell, floatingFilter: false, filter: "agNumberColumnFilter" },
      { headerName: "Dispute", field: "dispute_id", width: 150, minWidth: 140, cellClass: "mono" },
      { headerName: "Proposed action", field: "action", width: 230, minWidth: 180, cellRenderer: actionCell },
      { headerName: "Amount", field: "amount", width: 100, minWidth: 80, type: "rightAligned", valueFormatter: money, floatingFilter: false, filter: "agNumberColumnFilter" },
      { headerName: "Respond by", colId: "due", width: 120, minWidth: 100, floatingFilter: false, filter: "agNumberColumnFilter",
        valueGetter: function (p) { return p.data.due ? Date.parse(p.data.due) : null; }, cellRenderer: dueCell },
      { headerName: "Stage", field: "stage", width: 110, minWidth: 90, hide: true },
      { headerName: "Claim", field: "claim_type", width: 130, minWidth: 110, cellRenderer: claimCell },
      { headerName: "Risk", field: "risk", width: 120, minWidth: 100, cellRenderer: riskCell },
      { headerName: "State", field: "state", width: 110, minWidth: 100, cellRenderer: stateCell },
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

  var VIEWS = {
    all: function () { return true; },
    pending: function (r) { return r.state === "PENDING"; },
    attention: function (r) { return r.state === "APPROVED" || r.state === "UNCERTAIN"; },
    disagree: function (r) { return !!r.ai_disagrees; },
    risk: function (r) { return r.risk === "high" || r.risk === "elevated"; }
  };
  var CHIPS = [["all", "All"], ["pending", "Awaiting approval"], ["attention", "Needs attention"], ["disagree", "AI disagrees"], ["risk", "Risk flagged"]];

  function mountProposals(host) {
    var saved = store("dc.grid.proposals.v1"), view = "all", all = [];
    var api = agGrid.createGrid(host, {
      theme: theme, columnDefs: proposalColumns(), rowData: [],
      initialState: saved.load(), onStateUpdated: function (e) { saved.save(e.api.getState()); },
      isExternalFilterPresent: function () { return view !== "all"; },
      doesExternalFilterPass: function (node) { return VIEWS[view](node.data); },
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
    var chips = document.getElementById("chips");
    if (chips) CHIPS.forEach(function (c) {
      var b = el("button", "chip", c[1]); b.type = "button"; b.setAttribute("aria-pressed", c[0] === view ? "true" : "false");
      b.addEventListener("click", function () {
        view = c[0];
        Array.prototype.forEach.call(chips.children, function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        api.onFilterChanged();
      });
      chips.appendChild(b);
    });
    wireExport(api, "dispute-proposals"); wireCount(api, function () { return all.length; });
    function refresh() {
      getJSON("/api/proposals").then(function (rows) { all = rows; api.setGridOption("rowData", rows); renderTiles(rows); })
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
    var saved = store("dc.grid.audit.v1"), total = 0;
    var api = agGrid.createGrid(host, {
      theme: theme, rowData: [],
      initialState: saved.load(), onStateUpdated: function (e) { saved.save(e.api.getState()); },
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
    wireExport(api, "audit-trail"); wireCount(api, function () { return total; });
    function refresh() {
      getJSON("/api/audit").then(function (res) {
        total = res.rows.length; api.setGridOption("rowData", res.rows);
        if (count) count.textContent = res.rows.length + (res.rows.length === 1 ? " entry · " : " entries · ") + (res.ok ? "hash chain verified" : "CHAIN BROKEN at #" + res.bad_seq);
      }).catch(function (e) { console.error(e); });
    }
    refresh();
    setInterval(refresh, 20000);
  }

  var EVIDENCE = { verified: "ok", unverified: "warn", missing: "bad", conflict: "bad" };
  function mountEvidence(host) {
    var api = agGrid.createGrid(host, {
      theme: theme, rowData: [], domLayout: "autoHeight", suppressCellFocus: true,
      defaultColDef: { sortable: true, resizable: true, wrapText: true, autoHeight: true },
      columnDefs: [
        { headerName: "Evidence", field: "label", flex: 2, minWidth: 180 },
        { headerName: "Status", field: "status", width: 130, cellRenderer: function (p) { return pill(p.value, EVIDENCE[p.value] || "neutral"); } },
        { headerName: "Detail", field: "detail", flex: 3, minWidth: 220 },
        { headerName: "Source", field: "source", width: 160 }
      ],
      overlayNoRowsTemplate: "<span>No evidence items were recorded for this proposal.</span>"
    });
    getJSON("/api/proposals/" + encodeURIComponent(host.dataset.pid) + "/evidence")
      .then(function (rows) { api.setGridOption("rowData", rows); }).catch(function (e) { console.error(e); });
  }

  var host = document.getElementById("grid");
  if (host && window.agGrid) {
    if (host.dataset.kind === "audit") mountAudit(host);
    else if (host.dataset.kind === "evidence") mountEvidence(host);
    else mountProposals(host);
  }
})();
