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

function sourceRows() {
  return Array.from(document.querySelectorAll("#mapping-table tbody tr[data-source]"));
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
  let duplicateMessage = "";
  const seenAccounts = new Map();

  sourceRows().forEach((row) => {
    const select = row.querySelector(".account-select");
    const testBtn = row.querySelector(".test-btn");
    select.disabled = !state.ynabConnected || !planId;
    testBtn.disabled = !state.gmailConnected;

    const accountId = select.value;
    if (!accountId) return;
    mappedCount += 1;

    const sourceName = row.dataset.sourceName || row.dataset.source;
    if (seenAccounts.has(accountId) && !duplicateMessage) {
      duplicateMessage =
        `${seenAccounts.get(accountId)} and ${sourceName} cannot use the same YNAB account, ` +
        "because their balance checks would conflict.";
    }
    seenAccounts.set(accountId, sourceName);

    const { complete } = readRowInputs(row);
    if (!complete) allInputsFilled = false;
  });

  if (duplicateMessage) {
    message("save-msg", duplicateMessage, "err");
  } else if ($("save-msg") && $("save-msg").textContent.includes("same YNAB account")) {
    message("save-msg", "");
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
    sourceRows().forEach((row) => {
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
  const selects = sourceRows().map((r) => r.querySelector(".account-select"));
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
  const msgId = `test-msg-${sourceKey}`;
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
      body: JSON.stringify({ source: sourceKey, inputs }),
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

  sourceRows().forEach((row) => {
    const sourceKey = row.dataset.source;
    const select = row.querySelector(".account-select");
    const accountId = select.value;
    const opt = select.selectedOptions[0];
    const accountName = (opt && opt.dataset.accountName) || (opt ? opt.text : "");
    const enabled = !!accountId;
    mappings[sourceKey] = {
      account_id: enabled ? accountId : "",
      account_name: enabled ? accountName : "",
      enabled,
    };
    if (enabled) {
      passwordInputs[sourceKey] = readRowInputs(row).inputs;
    }
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
    sourceRows().forEach((row) => {
      row.querySelector(".account-select").value = "";
      row.querySelectorAll("input[data-input]").forEach((input) => { input.value = ""; });
      row.querySelectorAll(".field-note").forEach((note) => show(note, false));
      message(`test-msg-${row.dataset.source}`, "");
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
  if ($("clear-runs-btn")) {
    $("clear-runs-btn").addEventListener("click", clearRuns);
  }

  sourceRows().forEach((row) => {
    row.querySelector(".account-select").addEventListener("change", refreshState);
    row.querySelectorAll("input[data-input]").forEach((input) => {
      input.addEventListener("input", refreshState);
    });
    row.querySelector(".test-btn").addEventListener("click", () => testSource(row));
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
