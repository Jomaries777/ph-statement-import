// The page's script (templates/index.html): the 4 steps, talking to the
// /api/* routes in upload.py and ynab_auth.py.
const $ = (id) => document.getElementById(id);
// toUpload: how many transactions YNAB will really add (from the starting
// balance check; null until checked). 0 disables the Upload button.
const state = { parsed: null, connected: false, plans: [], uploading: false, startCheckId: 0,
                toUpload: null };

// ---- small helpers ---------------------------------------------------------

function show(el, on = true) { el.classList.toggle("hidden", !on); }

function message(id, text, kind = "err") {
  const el = $(id);
  el.className = "msg " + kind;
  el.textContent = text || "";
  show(el, !!text);
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let body = null;
  try { body = await resp.json(); } catch (_) { /* non-JSON error page */ }
  if (!resp.ok) {
    const err = new Error((body && body.error) || `Request failed (${resp.status})`);
    err.reconnect = !!(body && body.reconnect);
    throw err;
  }
  return body;
}

// Build a table with textContent only (no HTML injection from PDF text)
function renderTable(table, headers, rows, cellClass) {
  table.replaceChildren();
  const thead = table.createTHead().insertRow();
  headers.forEach((h, i) => {
    const th = document.createElement("th");
    th.textContent = h;
    const cls = cellClass(h, "", i);
    if (cls.includes("amt")) th.className = "amt";
    thead.appendChild(th);
  });
  const tbody = table.createTBody();
  rows.forEach((row) => {
    const tr = tbody.insertRow();
    row.forEach((value, i) => {
      const td = tr.insertCell();
      td.textContent = value;
      td.className = cellClass(headers[i], value, i);
      if (td.className.includes("desc")) td.title = value;
    });
  });
}

// milliunits -> "₱-1,124.25" style text, using the selected plan's currency format
function formatAmount(milliunits) {
  const plan = state.plans.find((p) => p.id === $("plan").value);
  const cf = (plan && plan.currency_format) || {};
  const digits = cf.decimal_digits ?? 2;
  const [whole, frac] = Math.abs(milliunits / 1000).toFixed(digits).split(".");
  let text = whole.replace(/\B(?=(\d{3})+(?!\d))/g, cf.group_separator ?? ",");
  if (frac) text += (cf.decimal_separator ?? ".") + frac;
  if (cf.display_symbol !== false && cf.currency_symbol) {
    text = cf.symbol_first === false ? text + cf.currency_symbol : cf.currency_symbol + text;
  }
  return (milliunits < 0 ? "-" : "") + text;
}

// ---- step state ------------------------------------------------------------

function refreshSteps() {
  const planId = $("plan").value, accountId = $("account").value;
  $("card-parse").classList.toggle("complete", !!state.parsed);
  $("card-ynab").classList.toggle("complete", state.connected);
  $("card-target").classList.toggle("disabled", !state.connected);
  $("card-target").classList.toggle("complete", state.connected && !!accountId);

  const ready = !!state.parsed && state.connected && !!planId && !!accountId;
  $("card-upload").classList.toggle("disabled", !ready && !$("results").offsetParent);
  // Nothing new to upload (all already in YNAB): keep the button disabled
  const nothingNew = ready && state.toUpload === 0;
  $("upload-btn").disabled = !ready || state.uploading || nothingNew;
  const n = state.parsed ? state.parsed.count : 0;
  const m = ready && state.toUpload != null ? state.toUpload : n;  // what YNAB will add
  if (!state.uploading) {
    $("upload-btn").textContent = nothingNew
      ? "Nothing new to upload"
      : `Upload ${m} transaction${m === 1 ? "" : "s"} to YNAB`;
  }
  $("upload-summary").textContent = ready
    ? `${n} transactions → ${$("plan").selectedOptions[0].text} / ${$("account").selectedOptions[0].text}`
    : "Parse a statement, connect to YNAB and choose an account.";

  const pill = $("status-pill");
  pill.textContent = state.connected ? "● Connected" : "○ Not connected";
  pill.classList.toggle("on", state.connected);
  show($("ynab-on"), state.connected);
  show($("ynab-off"), !state.connected);
}

function setConnected(on, reason) {
  state.connected = on;
  if (!on) {
    state.plans = [];
    $("plan").replaceChildren(new Option("— connect to YNAB first —", ""));
    $("account").replaceChildren(new Option("—", ""));
  }
  if (reason) message("ynab-msg", reason);
  refreshSteps();
}

// ---- ① Statement -----------------------------------------------------------

// Column names differ per statement: which columns are amounts, money in / out
// and wrapping text comes from the source (sources.py) via /api/parse
function showParsed(parsed) {
  state.parsed = parsed;
  if (parsed.source) $("source").value = parsed.source;
  const extra = parsed.count - parsed.statement_count;
  message("parse-msg", `Parsing successful: ${parsed.statement_count} ${parsed.source_name} transactions ` +
    `from ${parsed.filename}` +
    (extra ? ` (+${extra} balance correction${extra === 1 ? "" : "s"})` : ""), "ok");
  const amounts = new Set(parsed.amount_columns);
  const moneyIn = new Set(parsed.money_in_columns);
  const moneyOut = new Set(parsed.money_out_columns);
  renderTable($("preview-table"), parsed.columns, parsed.rows, (h, v) => {
    if (h === parsed.text_column) return "desc";
    if (!amounts.has(h)) return "";
    return "amt" + (v && moneyOut.has(h) ? " neg" : v && moneyIn.has(h) ? " pos" : "");
  });
  $("preview-totals").textContent = parsed.summary || "";
  showBalanceWarning(parsed);
  show($("preview"));
  show($("results"), false);
  $("card-upload").classList.remove("complete");
  message("upload-msg", "");
  message("upload-skipped", "");
  showBalanceCheck("end-check", null);
  refreshSteps();
  checkStartingBalance();
}

// Running balance didn't add up: warning box listing the correction rows,
// and the same rows highlighted in the preview table
function showBalanceWarning(parsed) {
  const box = $("balance-msg");
  const corrections = parsed.corrections || [];
  box.replaceChildren();
  show(box, corrections.length > 0);
  if (!corrections.length) return;

  const title = document.createElement("strong");
  title.textContent = `⚠ Balance does not add up — ${corrections.length} correction row` +
    `${corrections.length === 1 ? "" : "s"} added`;
  const list = document.createElement("dl");
  corrections.forEach((c) => {
    const dt = document.createElement("dt"); dt.textContent = `${c.date_time} · ${c.description}`;
    const dd = document.createElement("dd"); dd.textContent = c.amount;
    list.append(dt, dd);
  });
  const note = document.createElement("div");
  note.textContent = "Correction rows are uploaded to YNAB with the rest, unapproved and flagged red for review.";
  note.style.marginTop = "6px";
  box.append(title, list, note);

  const body = $("preview-table").tBodies[0];
  (parsed.correction_rows || []).forEach((i) => {
    if (body && body.rows[i]) body.rows[i].classList.add("missing");
  });
}

$("parse-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("parse-btn").disabled = true;
  $("parse-btn").textContent = "Parsing…";
  message("parse-msg", "");
  try {
    showParsed(await api("/api/parse", { method: "POST", body: new FormData(e.target) }));
    $("password").value = "";
  } catch (err) {
    state.parsed = null;
    show($("preview"), false);
    show($("balance-msg"), false);
    message("parse-msg", err.message);
    refreshSteps();
  } finally {
    $("parse-btn").disabled = false;
    $("parse-btn").textContent = "Parse";
  }
});

// ---- ② YNAB ----------------------------------------------------------------

// Personal access token typed on the page (local mode only): the server checks
// it with YNAB first
$("ynab-off").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("token-btn").disabled = true;
  $("token-btn").textContent = "Connecting…";
  message("ynab-msg", "");
  try {
    const res = await api("/api/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: $("ynab-token").value, remember: $("remember-token").checked }),
    });
    $("ynab-token").value = "";
    setTokenKind(res.token_kind);
    setConnected(true);
    if (res.save_error) message("ynab-msg", res.save_error, "info");
    await loadPlans();
  } catch (err) {
    message("ynab-msg", err.message);
  } finally {
    $("token-btn").disabled = false;
    $("token-btn").textContent = "Connect";
  }
});

// Optional: YNAB OAuth login (only shown when YNAB_CLIENT_ID / SECRET are in .env)
$("connect-btn").addEventListener("click", () => { window.location.href = "/oauth/login"; });

// kind: "saved" (from the OS secure store), "personal" (this session only) or "oauth"
function setTokenKind(kind) {
  $("ynab-on-label").textContent =
    kind === "oauth" ? "● Connected to YNAB (YNAB login)"
    : kind === "saved" ? "● Connected with your saved token 🔒"
    : "● Connected with your personal access token";
  show($("forget-btn"), kind === "saved");
}

// Disconnect: this session only; a saved token stays for the next run
$("disconnect-btn").addEventListener("click", async () => {
  await api("/oauth/logout", { method: "POST" }).catch(() => {});
  message("ynab-msg", "");
  setConnected(false);
});

// Forget saved token: also remove it from the OS secure store
$("forget-btn").addEventListener("click", async () => {
  try {
    await api("/oauth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ forget: true }),
    });
    message("ynab-msg", "Saved token removed from this computer.", "info");
  } catch (err) {
    message("ynab-msg", err.message);
  }
  setConnected(false);
});

// ---- ③ Plan & Account ------------------------------------------------------

async function loadPlans() {
  try {
    state.plans = await api("/api/plans");
    const select = $("plan");
    select.replaceChildren(new Option("Choose a plan…", ""));
    state.plans.forEach((p) => select.add(new Option(p.name, p.id)));
    if (state.plans.length === 1) { select.value = state.plans[0].id; await loadAccounts(); }
    message("target-msg", "");
  } catch (err) {
    handleYnabError(err, "target-msg");
  }
  refreshSteps();
}

async function loadAccounts() {
  const planId = $("plan").value;
  const select = $("account");
  select.replaceChildren(new Option(planId ? "Loading…" : "—", ""));
  refreshSteps();
  if (!planId) return;
  try {
    const accounts = await api(`/api/plans/${encodeURIComponent(planId)}/accounts`);
    select.replaceChildren(new Option(accounts.length ? "Choose an account…" : "No open accounts", ""));
    accounts.forEach((a) => select.add(new Option(`${a.name} (${a.type}) – ${a.balance}`, a.id)));
    message("target-msg", "");
  } catch (err) {
    select.replaceChildren(new Option("—", ""));
    handleYnabError(err, "target-msg");
  }
  refreshSteps();
}

$("plan").addEventListener("change", () => { loadAccounts(); checkStartingBalance(); });
$("account").addEventListener("change", () => { refreshSteps(); checkStartingBalance(); });

function handleYnabError(err, msgId) {
  if (err.reconnect) setConnected(false, err.message);
  else message(msgId, err.message);
}

// ---- ④ Upload --------------------------------------------------------------

// A balance check from the server ({kind, matches, new_account, statement,
// ynab_before, ynab_after, message}): green when it matches, blue for a new
// YNAB account (its Starting Balance is set on upload), and an amber warning
// with the balances when not:
//   Statement starting balance: 7,873.54
//   YNAB balance BEFORE upload: -2,598.98
//   YNAB balance AFTER upload: -10,472.52
// Warnings never block the upload.
function showBalanceCheck(id, check, errorText) {
  const box = $(id);
  box.replaceChildren();
  if (errorText) { message(id, errorText, "warn"); return; }
  show(box, !!check);
  if (!check) return;
  if (check.new_account) { message(id, "ⓘ " + check.message, "info"); return; }
  // Amber when the balance is off or some transactions are already in YNAB
  const ok = check.matches && !check.already_in_ynab;
  box.className = "msg " + (ok ? "ok" : "warn");
  // Heading lines of the message ("3 transactions already in YNAB. Only 22 will
  // be uploaded.", "Starting balance does not match YNAB. ..."); the balance
  // lines are shown in the list below instead
  check.message.split("\n")
    .filter((line) => !/^(Statement \w+ balance:|YNAB balance )/.test(line))
    .forEach((line, i) => {
      const title = document.createElement(i ? "div" : "strong");
      title.textContent = (i ? "" : ok ? "✓ " : "⚠ ") + line;
      box.append(title);
    });
  if (ok) return;
  const list = document.createElement("dl");
  [[`Statement ${check.kind} balance:`, check.statement],
   ["YNAB balance BEFORE upload:", check.ynab_before],
   ["YNAB balance AFTER upload:", check.ynab_after]].forEach(([label, value]) => {
    if (value == null) return;
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value;
    list.append(dt, dd);
  });
  box.append(list);
}

// Before the upload: compare the chosen account's YNAB balance with the
// statement's starting balance. Runs when the statement or account changes;
// only the latest request is shown.
async function checkStartingBalance() {
  const planId = $("plan").value, accountId = $("account").value;
  const id = ++state.startCheckId;
  state.toUpload = null;
  showBalanceCheck("start-check", null);
  refreshSteps();
  if (!state.parsed || !state.connected || !planId || !accountId) return;
  message("start-check", "Checking the starting balance against YNAB…", "info");
  try {
    const check = await api("/api/starting-balance", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, account_id: accountId }),
    });
    if (id === state.startCheckId) {
      showBalanceCheck("start-check", check);
      state.toUpload = check.to_upload ?? null;
      refreshSteps();
    }
  } catch (err) {
    if (id !== state.startCheckId) return;
    if (err.reconnect) setConnected(false, err.message);
    else showBalanceCheck("start-check", null, `Could not check the starting balance: ${err.message}`);
  }
}

$("upload-btn").addEventListener("click", async () => {
  state.uploading = true;
  $("upload-btn").textContent = "Uploading…";
  refreshSteps();
  message("upload-msg", "");
  message("upload-skipped", "");
  showBalanceCheck("end-check", null);
  show($("results"), false);
  try {
    const res = await api("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: $("plan").value, account_id: $("account").value }),
    });
    message("upload-msg",
      `Import successful: ${res.created.length} transactions created in YNAB (unapproved)`, "ok");
    if (res.skipped) message("upload-skipped", `Skipped ${res.skipped} already imported`, "info");
    if (res.created.length) {
      renderTable($("results-table"), ["Date", "Payee", "Category", "Memo", "Amount"],
        res.created.map((t) => [t.date, t.payee, t.category, t.memo, formatAmount(t.amount)]),
        (h, v) => h === "Amount" ? "amt " + (String(v).startsWith("-") ? "neg" : "pos")
                : h === "Payee" || h === "Memo" ? "desc" : "");
      // Highlight the flagged balance-correction rows, like in the preview
      res.created.forEach((t, i) => {
        if (t.flag) $("results-table").tBodies[0].rows[i].classList.add("missing");
      });
      $("ynab-link").href = res.ynab_url;
      show($("results"));
    }
    $("card-upload").classList.add("complete");
    // Everything is in YNAB now: uploading again would add nothing
    state.toUpload = 0;
    // New YNAB account: its Starting Balance was set before the upload
    if (res.new_account_message) message("start-check", "ⓘ " + res.new_account_message, "info");
    // After the upload: YNAB balance vs. the statement's ending balance
    showBalanceCheck("end-check", res.ending_check,
      res.ending_check_error && `Could not check the ending balance: ${res.ending_check_error}`);
  } catch (err) {
    handleYnabError(err, "upload-msg");
  } finally {
    state.uploading = false;
    refreshSteps();
  }
});

// ---- start: restore state (e.g. after the OAuth redirect) ------------------

(async function init() {
  const oauthError = new URLSearchParams(location.search).get("oauth_error");
  if (oauthError) {
    message("ynab-msg", `Could not connect to YNAB (${oauthError}). Try again.`);
    history.replaceState(null, "", "/");
  }
  try {
    const status = await api("/api/status");
    show($("connect-btn"), status.oauth_configured);
    // Hosted mode (token_allowed false) has no token field: YNAB login only
    if (status.token_allowed) {
      // "Remember" only when this computer has a secure store (Keychain, ...)
      show($("remember-row"), status.remember_available);
      if (status.remember_available) {
        $("token-note").textContent = `Ticked, it is saved in your ${status.store_name}, ` +
          "never in this folder or the browser.";
      }
    }
    if (status.token_kind) setTokenKind(status.token_kind);
    if (status.parsed) showParsed(status.parsed);
    setConnected(status.connected, status.connect_error);
    if (status.connected) await loadPlans();
  } catch (err) {
    message("ynab-msg", err.message);
  }
  refreshSteps();
})();
