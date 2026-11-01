// The /auto-import page's script (templates/auto_import.html): Google + YNAB
// setup steps, password testing, saving mappings, and "Turn off and delete".
const $ = (id) => document.getElementById(id);

const mainEl = $("auto-import-main");
const state = {
  gmailConnected: mainEl ? mainEl.dataset.gmailConnected === "true" : false,
  enabled: mainEl ? mainEl.dataset.enabled === "true" : false,
  hasSavedYnab: mainEl ? mainEl.dataset.hasSavedYnab === "true" : false,
  ynabConnected: false,
  plans: [],
  saving: false,
  deleting: false,
};

// ---- small helpers ---------------------------------------------------------

function show(el, on = true) {
  if (el) el.classList.toggle("hidden", !on);
}

function message(id, text, kind = "err") {
  const el = $(id);
  if (!el) return;
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

// One row per wallet: an account at a source ("gcash", "gcash-2", ...)
function walletRows() {
  return Array.from(document.querySelectorAll("#mapping-table tbody tr[data-wallet]"));
}

// The digits that tell several accounts at one source apart ("" if the row has none)
function rowAccount(row) {
  const input = row.querySelector("input[data-account-input]");
  return input ? input.value.trim() : "";
}

// "GCash" or "GCash · 0917123", as the server names wallets (sources.py wallet_name)
function walletName(row) {
  const account = rowAccount(row);
  const name = row.dataset.sourceName || row.dataset.source;
  return account ? `${name} · ${account}` : name;
}

// A source's rows: {"gcash": [row, row], "gotyme_goalsave": [row], ...}, in page order
function rowsBySource() {
  const bySource = new Map();
  walletRows().forEach((row) => {
    if (!bySource.has(row.dataset.source)) bySource.set(row.dataset.source, []);
    bySource.get(row.dataset.source).push(row);
  });
  return bySource;
}

function isMapped(row) {
  return !!row.querySelector(".account-select").value;
}

// Problems with the accounts' digits, the same checks as the server's
// (_clean_wallet_accounts in auto_import.py): each matches its pattern; with
// several rows, or on a mapped row that needs them (GoalSave), they're entered;
// and they're all different
function walletsMessage() {
  for (const rows of rowsBySource().values()) {
    const field = rows[0].querySelector("input[data-account-input]");
    if (!field) continue;
    const name = rows[0].dataset.sourceName;
    const label = field.getAttribute("aria-label");
    const pattern = new RegExp(field.dataset.pattern);
    const accounts = rows.map(rowAccount);
    if (accounts.some((a) => a && !pattern.test(a))) return field.dataset.patternError;
    if (field.dataset.required && rows.some((row) => isMapped(row) && !rowAccount(row))) {
      return `Enter the ${label} for each ${name} account.`;
    }
    if (rows.length > 1 && accounts.some((a) => !a)) {
      return `With more than one ${name} account, enter the ${label.toLowerCase()} for each.`;
    }
    if (new Set(accounts).size < accounts.length) {
      return `Two ${name} accounts have the same ${label.toLowerCase()}.`;
    }
  }
  return "";
}

// The row that holds a wallet's password inputs: its own, or with
// data-shared-password (one PDF for every account) its source's first row
function passwordRow(row) {
  if (!row.dataset.sharedPassword) return row;
  return walletRows().find((r) => r.dataset.source === row.dataset.source);
}

function readRowInputs(row) {
  const inputs = {};
  let complete = true;
  row.querySelectorAll("input[data-input]").forEach((input) => {
    const value = input.value.trim();
    inputs[input.dataset.input] = value;
    if (!value) complete = false;
  });
  return { inputs, complete };
}

// ---- step state & validation -----------------------------------------------

function refreshState() {
  if (!$("auto-setup-view")) return;

  $("card-gmail").classList.toggle("complete", state.gmailConnected);
  show($("gmail-on"), state.gmailConnected);
  show($("gmail-off"), !state.gmailConnected);

  $("card-ynab").classList.toggle("complete", state.ynabConnected);
  show($("ynab-on"), state.ynabConnected);
  show($("ynab-off"), !state.ynabConnected);

  const planSelect = $("plan");
  const planId = planSelect.value;
  $("card-plan").classList.toggle("disabled", !state.ynabConnected);
  $("card-plan").classList.toggle("complete", state.ynabConnected && !!planId);

  const selectedPlan = planSelect.selectedOptions[0];
  $("account-col-header").textContent =
    state.ynabConnected && planId && selectedPlan
      ? `YNAB account in "${selectedPlan.text}"`
      : "YNAB account";

  let mappedCount = 0;
  let allInputsFilled = true;
  let duplicateMessage = walletsMessage();
  const seenAccounts = new Map();

  walletRows().forEach((row) => {
    const select = row.querySelector(".account-select");
    const testBtn = row.querySelector(".test-btn");
    select.disabled = !state.ynabConnected || !planId;
    testBtn.disabled = !state.gmailConnected;

    const accountId = select.value;
    if (!accountId) return;
    mappedCount += 1;

    const sourceName = walletName(row);
    if (seenAccounts.has(accountId) && !duplicateMessage) {
      duplicateMessage =
        `${seenAccounts.get(accountId)} and ${sourceName} cannot use the same YNAB account, ` +
        "because their balance checks would conflict.";
    }
    seenAccounts.set(accountId, sourceName);

    const { complete } = readRowInputs(passwordRow(row));
    if (!complete) allInputsFilled = false;
  });

  if (duplicateMessage) {
    message("save-msg", duplicateMessage, "err");
    state.shownRowsMessage = duplicateMessage;
  } else if (state.shownRowsMessage && $("save-msg").textContent === state.shownRowsMessage) {
    message("save-msg", "");
    state.shownRowsMessage = "";
  }

  const ready =
    state.gmailConnected &&
    state.ynabConnected &&
    !!planId &&
    mappedCount > 0 &&
    allInputsFilled &&
    !duplicateMessage;

  $("card-mappings").classList.toggle("complete", ready);
  $("save-btn").disabled = !ready || state.saving;
}

// ---- ② YNAB & ③ Plan / Accounts --------------------------------------------

function startYnabLogin() {
  window.location.href = "/oauth/login?next=/auto-import";
}

function setYnabConnected(on, reason) {
  state.ynabConnected = on;
  if (!on) {
    state.plans = [];
    $("plan").replaceChildren(new Option("— connect to YNAB first —", ""));
    walletRows().forEach((row) => {
      const select = row.querySelector(".account-select");
      select.replaceChildren(new Option("Don't import", ""));
    });
  }
  if (reason) message("ynab-msg", reason);
  refreshState();
}

// This browser's own YNAB session, or else auto-import's saved YNAB
// connection (the browser's session ends after 30 minutes idle), so the page
// doesn't ask to log in to YNAB again while auto-import is still connected.
async function connectYnab() {
  try {
    const status = await api("/api/status");
    let connected = status.connected;
    let reason = status.connect_error;
    if (!connected && state.hasSavedYnab) {
      connected = (await api("/api/auto-import/ynab-session", { method: "POST" })).connected;
      reason = connected ? null : "Your saved YNAB connection has ended. Reconnect to YNAB.";
    }
    setYnabConnected(connected, reason);
    if (connected) await loadPlans();
  } catch (err) {
    message("ynab-msg", err.message);
  }
}

async function loadPlans() {
  try {
    state.plans = await api("/api/plans");
    const select = $("plan");
    const savedPlanId = select.dataset.savedPlanId || "";
    select.replaceChildren(new Option("Choose a plan…", ""));
    state.plans.forEach((p) => select.add(new Option(p.name, p.id)));

    if (savedPlanId && state.plans.some((p) => p.id === savedPlanId)) {
      select.value = savedPlanId;
      await loadAccounts();
    } else if (state.plans.length === 1) {
      select.value = state.plans[0].id;
      await loadAccounts();
    }
    message("plan-msg", "");
  } catch (err) {
    if (err.reconnect) setYnabConnected(false, err.message);
    else message("plan-msg", err.message);
  }
  refreshState();
}

async function loadAccounts() {
  const planId = $("plan").value;
  const selects = walletRows().map((r) => r.querySelector(".account-select"));
  selects.forEach((s) => s.replaceChildren(new Option(planId ? "Loading…" : "Don't import", "")));
  refreshState();
  if (!planId) return;

  try {
    const accounts = await api(`/api/plans/${encodeURIComponent(planId)}/accounts`);
    selects.forEach((select) => {
      const savedAccountId = select.dataset.savedAccountId || "";
      select.replaceChildren(new Option("Don't import", ""));
      accounts.forEach((a) => {
        const opt = new Option(`${a.name} (${a.type}) – ${a.balance}`, a.id);
        opt.dataset.accountName = a.name;
        select.add(opt);
      });
      if (savedAccountId && accounts.some((a) => a.id === savedAccountId)) {
        select.value = savedAccountId;
      }
    });
    message("plan-msg", "");
  } catch (err) {
    selects.forEach((s) => s.replaceChildren(new Option("Don't import", "")));
    if (err.reconnect) setYnabConnected(false, err.message);
    else message("plan-msg", err.message);
  }
  refreshState();
}

// ---- ④ Test, Save & Delete -------------------------------------------------

async function testSource(row) {
  const sourceKey = row.dataset.source;
  const btn = row.querySelector(".test-btn");
  const msgId = `test-msg-${row.dataset.wallet}`;
  const { inputs, complete } = readRowInputs(row);
  if (!complete) {
    message(msgId, "Fill in every password field before testing.", "err");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Testing…";
  message(msgId, "Checking your newest statement email in Gmail…", "info");
  try {
    const res = await api("/api/auto-import/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // With the account's digits, the newest email for that account is tried
      body: JSON.stringify({ source: sourceKey, inputs, account: rowAccount(row) || null }),
    });
    message(msgId, (res.ok ? "✓ " : "✗ ") + res.message, res.ok ? "ok" : "err");
  } catch (err) {
    message(msgId, err.message, "err");
  } finally {
    btn.disabled = !state.gmailConnected;
    btn.textContent = "Test";
  }
}

async function saveSettings() {
  const planSelect = $("plan");
  const planId = planSelect.value;
  const planName = planSelect.selectedOptions[0] ? planSelect.selectedOptions[0].text : "";
  const mappings = {};
  const passwordInputs = {};

  walletRows().forEach((row) => {
    const walletId = row.dataset.wallet;
    const select = row.querySelector(".account-select");
    const accountId = select.value;
    const opt = select.selectedOptions[0];
    const accountName = (opt && opt.dataset.accountName) || (opt ? opt.text : "");
    const enabled = !!accountId;
    mappings[walletId] = {
      source: row.dataset.source,
      account_id: enabled ? accountId : "",
      account_name: enabled ? accountName : "",
      enabled,
    };
    // A shared password goes with the source's first row, mapped or not
    const sharedFirst =
      row.dataset.sharedPassword &&
      passwordRow(row) === row &&
      rowsBySource().get(row.dataset.source).some(isMapped);
    const inputs = enabled || sharedFirst ? readRowInputs(row).inputs : {};
    const accountInput = row.querySelector("input[data-account-input]");
    if (accountInput && rowAccount(row)) inputs[accountInput.dataset.accountInput] = rowAccount(row);
    if (Object.keys(inputs).length) passwordInputs[walletId] = inputs;
  });

  state.saving = true;
  const label = state.enabled ? "Save changes" : "Turn on";
  $("save-btn").disabled = true;
  $("save-btn").textContent = "Saving…";
  message("save-msg", "");
  try {
    await api("/api/auto-import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: planId,
        plan_name: planName,
        mappings,
        password_inputs: passwordInputs,
        // only on the page when the server can categorize
        categorize: !!($("categorize") && $("categorize").checked),
      }),
    });
    window.location.href = "/auto-import";
  } catch (err) {
    if (err.reconnect) setYnabConnected(false, err.message);
    else message("save-msg", err.message, "err");
    state.saving = false;
    $("save-btn").textContent = label;
    refreshState();
  }
}

// ---- ④ Several accounts at one source (GCash) --------------------------------

function wireWalletRow(row) {
  row.querySelector(".account-select").addEventListener("change", refreshState);
  row.querySelectorAll("input[data-input], input[data-account-input]").forEach((input) => {
    input.addEventListener("input", refreshState);
  });
  row.querySelector(".test-btn").addEventListener("click", () => testSource(row));
  row.querySelector(".remove-wallet-btn").addEventListener("click", () => {
    row.remove();
    refreshState();
  });
}

// "+ Add another GCash account": a copy of the source's first row, emptied,
// as wallet "gcash-2", "gcash-3", ... (the next number not in use)
function addWallet(sourceKey) {
  const rows = walletRows().filter((r) => r.dataset.source === sourceKey);
  const numbers = rows.map((r) => Number(r.dataset.wallet.split("-")[1] || 1));
  const walletId = `${sourceKey}-${Math.max(...numbers) + 1}`;

  // A copy keeps what was typed: each field goes back to what the page came
  // with (e.g. the last name from Google), the account's digits to empty
  const row = rows[0].cloneNode(true);
  row.dataset.wallet = walletId;
  const select = row.querySelector(".account-select");
  select.dataset.savedAccountId = "";
  select.value = "";
  row.querySelectorAll("input[data-input]").forEach((input) => { input.value = input.defaultValue; });
  row.querySelector("input[data-account-input]").value = "";
  if (row.dataset.sharedPassword) {
    // The first row's password inputs open every account's PDF (GoalSave)
    const labels = Array.from(rows[0].querySelectorAll("input[data-input]"), (i) => i.getAttribute("aria-label"));
    const note = document.createElement("span");
    note.className = "muted shared-note";
    note.textContent = `Uses the ${labels.join(", ").toLowerCase()} above`;
    const cell = row.querySelector(".inputs-cell");
    cell.querySelectorAll(".input-group, .small-note").forEach((el) => el.remove());
    cell.prepend(note);
    show(row.querySelector(".test-btn"), false);
  }
  const testMsg = row.querySelector('[id^="test-msg-"]');  // message() replaces its classes
  testMsg.id = `test-msg-${walletId}`;
  testMsg.textContent = "";
  show(testMsg, false);
  show(row.querySelector(".remove-wallet-btn"));

  rows[rows.length - 1].after(row);
  wireWalletRow(row);
  row.querySelector("input[data-account-input]").focus();
  refreshState();
}

async function deleteAllData() {
  if (state.deleting) return;
  state.deleting = true;
  $("delete-btn").disabled = true;
  $("delete-btn").textContent = "Deleting…";
  message("delete-msg", "");
  message("page-msg", "");
  try {
    const res = await api("/api/auto-import/delete", { method: "POST" });
    state.gmailConnected = false;
    state.enabled = false;
    show($("auto-on-view"), false);
    show($("card-delete"), false);
    show($("auto-setup-view"), true);
    if ($("cancel-edit-btn")) show($("cancel-edit-btn"), false);
    $("save-btn").textContent = "Turn on";
    walletRows().forEach((row) => {
      if (row.dataset.wallet !== row.dataset.source) {  // an added account
        row.remove();
        return;
      }
      row.querySelector(".account-select").value = "";
      row.querySelectorAll("input[data-input], input[data-account-input]").forEach((input) => {
        input.value = "";
      });
      row.querySelectorAll("[data-input] + .field-note").forEach((note) => show(note, false));
      message(`test-msg-${row.dataset.wallet}`, "");
    });
    refreshState();
    if (res.warning) {
      message("page-msg", "⚠ " + res.warning, "warn");
    } else {
      message("page-msg", "✓ Auto-import turned off and your stored data was deleted.", "ok");
    }
  } catch (err) {
    message("delete-msg", err.message, "err");
    $("delete-btn").disabled = false;
    $("delete-btn").textContent = "Turn off and delete my data";
  } finally {
    state.deleting = false;
  }
}

async function clearRuns() {
  const ok = window.confirm(
    "Hide these imports from the list? Emails already imported won't be imported again.",
  );
  if (!ok) return;
  $("clear-runs-btn").disabled = true;
  try {
    await api("/api/auto-import/runs/clear", { method: "POST" });
    window.location.href = "/auto-import";
  } catch (err) {
    message("runs-msg", err.message, "err");
    $("clear-runs-btn").disabled = false;
  }
}

// ---- Recent imports: refreshed while an import runs -------------------------

// Every 3 s while an import runs (the card's data-running), every 30 s
// otherwise, and only while the page is on screen.
const RUNS_REFRESH_MS = { running: 3000, idle: 30000 };

function scheduleRunsRefresh() {
  const card = $("card-runs");
  if (!card) return;
  const wait = card.dataset.running === "true" ? RUNS_REFRESH_MS.running : RUNS_REFRESH_MS.idle;
  window.setTimeout(refreshRuns, wait);
}

async function refreshRuns() {
  const onScreen =
    document.visibilityState === "visible" && !$("auto-on-view").classList.contains("hidden");
  if (onScreen) {
    try {
      const resp = await fetch("/auto-import/runs");
      if (resp.ok) $("card-runs").outerHTML = await resp.text();
    } catch (_) {
      /* offline for a moment: the next refresh tries again */
    }
  }
  scheduleRunsRefresh();
}

// ---- wire up events & initialize -------------------------------------------

if ($("auto-setup-view")) {
  $("connect-btn").addEventListener("click", startYnabLogin);
  $("reconnect-ynab-btn").addEventListener("click", startYnabLogin);
  $("plan").addEventListener("change", () => { loadAccounts(); });
  $("save-btn").addEventListener("click", saveSettings);

  if ($("edit-btn")) {
    $("edit-btn").addEventListener("click", async () => {
      // Check YNAB first, so the setup view opens already connected
      const editBtn = $("edit-btn");
      editBtn.disabled = true;
      editBtn.textContent = "Checking YNAB…";
      await connectYnab();
      editBtn.disabled = false;
      editBtn.textContent = "Edit mappings";
      show($("auto-on-view"), false);
      show($("auto-setup-view"), true);
      refreshState();
    });
  }
  if ($("cancel-edit-btn")) {
    $("cancel-edit-btn").addEventListener("click", () => {
      show($("auto-setup-view"), false);
      show($("auto-on-view"), true);
    });
  }
  if ($("delete-btn")) {
    $("delete-btn").addEventListener("click", deleteAllData);
  }
  // The Recent imports card is replaced on each refresh, so its button's
  // clicks are caught here rather than on the button itself
  document.addEventListener("click", (e) => {
    if (e.target.id === "clear-runs-btn") clearRuns();
  });
  if ($("card-runs")) scheduleRunsRefresh();

  walletRows().forEach(wireWalletRow);
  document.querySelectorAll(".add-wallet-btn").forEach((btn) => {
    btn.addEventListener("click", () => addWallet(btn.dataset.source));
  });

  (async function init() {
    const params = new URLSearchParams(location.search);
    const oauthError = params.get("oauth_error");
    if (oauthError) {
      message("ynab-msg", `Could not connect to YNAB (${oauthError}). Try again.`);
    }
    if (oauthError || params.get("google_error")) {
      history.replaceState(null, "", "/auto-import");
    }
    // With auto-import on, the setup view waits for "Edit mappings", so a
    // visit that only reads the run log doesn't refresh the saved YNAB token
    if (!state.enabled) await connectYnab();
    refreshState();
  })();
}
