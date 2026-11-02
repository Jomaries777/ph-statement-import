// The /transfer-rules page's script (templates/transfer_rules.html): connects
// to YNAB, lists the chosen plan's accounts, draws the rules table from the
// page's data-* values, keeps it in `state` as it's edited, and saves it with
// POST /api/transfer-rules.
const $ = (id) => document.getElementById(id);

const mainEl = $("transfer-rules-main");
const hosted = mainEl ? mainEl.dataset.hosted === "true" : false;
const state = {
  // [{account_1_id, account_1_name, description_1, account_2_id, account_2_name, description_2}]
  rules: mainEl ? JSON.parse(mainEl.dataset.rules) : [],
  accounts: [], // the chosen plan's open YNAB accounts [{id, name, type}]
  saving: false,
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
  if (!resp.ok) throw new Error((body && body.error) || `Request failed (${resp.status})`);
  return body;
}

function cell(...children) {
  const td = document.createElement("td");
  td.append(...children);
  return td;
}

// A bank dropdown: the plan's accounts, plus the saved one if it isn't among
// them (another plan, or not loaded yet), so a saved rule shows what it has.
// Once the plan's accounts are loaded, such an account is marked: saving
// refuses it until another bank is chosen.
function accountSelect(rule, side, label) {
  const el = document.createElement("select");
  el.setAttribute("aria-label", label);
  el.add(new Option("Choose a bank…", ""));
  const savedId = rule[`account_${side}_id`];
  if (savedId && !state.accounts.some((a) => a.id === savedId)) {
    const name = rule[`account_${side}_name`] || "Saved account";
    el.add(new Option(state.accounts.length ? `${name} (not in this plan)` : name, savedId));
  }
  state.accounts.forEach((a) => el.add(new Option(a.name, a.id)));
  el.value = savedId;
  el.addEventListener("change", () => {
    rule[`account_${side}_id`] = el.value;
    rule[`account_${side}_name`] = el.value ? el.selectedOptions[0].textContent : "";
  });
  return el;
}

function descriptionInput(rule, side, label, placeholder) {
  const el = document.createElement("input");
  el.type = "text";
  el.maxLength = 100;
  el.placeholder = placeholder;
  el.value = rule[`description_${side}`];
  el.setAttribute("aria-label", label);
  el.addEventListener("input", () => { rule[`description_${side}`] = el.value; });
  return el;
}

// ---- YNAB and the plan's accounts ------------------------------------------

function setYnabConnected(on) {
  show($("ynab-on"), on);
  show($("ynab-off"), !on);
  if (!on) $("plan").replaceChildren(new Option("— connect to YNAB first —", ""));
}

// This browser's own YNAB session, or else (hosted) auto-import's saved YNAB
// connection, so the page doesn't ask to log in to YNAB again
async function connectYnab() {
  try {
    let connected = (await api("/api/status")).connected;
    if (!connected && mainEl.dataset.hasSavedYnab === "true") {
      connected = (await api("/api/auto-import/ynab-session", { method: "POST" })).connected;
    }
    setYnabConnected(connected);
    if (connected) await loadPlans();
  } catch (err) {
    setYnabConnected(false);
    message("ynab-msg", err.message);
  }
}

async function loadPlans() {
  const planSelect = $("plan");
  const plans = await api("/api/plans");
  const savedPlanId = mainEl.dataset.planId || "";
  planSelect.replaceChildren(new Option("Choose a plan…", ""));
  plans.forEach((p) => planSelect.add(new Option(p.name, p.id)));
  if (plans.some((p) => p.id === savedPlanId)) planSelect.value = savedPlanId;
  else if (plans.length === 1) planSelect.value = plans[0].id;
  await loadAccounts();
}

async function loadAccounts() {
  const planId = $("plan").value;
  state.accounts = [];
  message("ynab-msg", "");
  if (planId) {
    try {
      state.accounts = await api(`/api/plans/${encodeURIComponent(planId)}/accounts`);
    } catch (err) {
      message("ynab-msg", err.message);
    }
  }
  matchRulesToPlan();
  renderRules();
}

// A rule's account that isn't in the chosen plan, e.g. from the plan before a
// YNAB "fresh start" (a copy whose accounts have new ids), switches to the
// plan's account with the same name, if there's one
function matchRulesToPlan() {
  state.rules.forEach((rule) => {
    [1, 2].forEach((side) => {
      const id = rule[`account_${side}_id`];
      if (!id || state.accounts.some((a) => a.id === id)) return;
      const same = state.accounts.find((a) => a.name === rule[`account_${side}_name`]);
      if (same) rule[`account_${side}_id`] = same.id;
    });
  });
}

// ---- rules -----------------------------------------------------------------

function renderRules() {
  const tbody = document.querySelector("#rules-table tbody");
  if (!tbody) return;
  tbody.replaceChildren();
  state.rules.forEach((rule, index) => {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary small-btn";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", "Remove this rule");
    remove.addEventListener("click", () => {
      state.rules.splice(index, 1);
      renderRules();
    });
    // Money goes one way, from Bank #1 to Bank #2
    const arrow = cell("→");
    arrow.className = "rule-arrow";
    arrow.setAttribute("aria-hidden", "true");
    const tr = document.createElement("tr");
    tr.append(
      cell(accountSelect(rule, 1, "From: Bank #1")),
      cell(descriptionInput(rule, 1, "Bank #1 shows (money out)", "Transfer to 0917123")),
      arrow,
      cell(accountSelect(rule, 2, "To: Bank #2")),
      cell(descriptionInput(rule, 2, "Bank #2 shows (money in)", "Optional")),
      cell(remove),
    );
    tbody.append(tr);
  });
  show($("no-rules"), state.rules.length === 0);
}

// Kept enabled: with too few accounts, clicking it says why
function addRule() {
  if (state.accounts.length < 2) {
    const why = $("plan").value
      ? "Your plan needs at least two open accounts."
      : "Connect to YNAB and choose a plan first.";
    message("rules-msg", why);
    return;
  }
  message("rules-msg", "");
  const [first, second] = state.accounts;
  state.rules.push({
    account_1_id: first.id, account_1_name: first.name, description_1: "",
    account_2_id: second.id, account_2_name: second.name, description_2: "",
  });
  renderRules();
  const inputs = document.querySelectorAll("#rules-table tbody input");
  if (inputs.length) inputs[inputs.length - 2].focus(); // the new row's Bank #1 description
}

// ---- save ------------------------------------------------------------------

async function save() {
  if (state.saving) return;
  state.saving = true;
  $("save-btn").disabled = true;
  message("save-msg", "Saving…", "info");
  try {
    const result = await api("/api/transfer-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: $("plan").value, rules: state.rules }),
    });
    const count = result.rules === 1 ? "1 rule" : `${result.rules} rules`;
    message("save-msg", `Saved ${count}.`, "ok");
  } catch (err) {
    message("save-msg", err.message);
  } finally {
    state.saving = false;
    $("save-btn").disabled = false;
  }
}

// ---- start -----------------------------------------------------------------

if (mainEl && $("save-btn")) {
  $("save-btn").addEventListener("click", save);
  $("add-rule-btn").addEventListener("click", addRule);
  $("plan").addEventListener("change", loadAccounts);
  if ($("connect-btn")) {
    $("connect-btn").addEventListener("click", () => {
      window.location.href = "/oauth/login?next=/transfer-rules";
    });
  }
  renderRules();
  connectYnab();
}
