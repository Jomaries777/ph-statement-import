// The /transfer-rules page's script (templates/transfer_rules.html): draws the
// wallets and rules tables from the page's data-* values, keeps them in `state`
// as they're edited, and saves them with POST /api/transfer-rules.
const $ = (id) => document.getElementById(id);

const mainEl = $("transfer-rules-main");
const hosted = mainEl ? mainEl.dataset.hosted === "true" : false;
const state = {
  // [{id, name, source, account_id, account_name}]; local wallets' id is the YNAB account id
  wallets: mainEl ? JSON.parse(mainEl.dataset.wallets) : [],
  // [{description, from_wallet, to_wallet}]
  rules: mainEl ? JSON.parse(mainEl.dataset.rules) : [],
  sources: mainEl ? JSON.parse(mainEl.dataset.sources) : [],
  sourceNames: mainEl ? JSON.parse(mainEl.dataset.sourceNames) : [],
  accounts: [], // local mode: the chosen plan's YNAB accounts [{id, name, type}]
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

function sourceName(key) {
  return state.sourceNames[state.sources.indexOf(key)] || key;
}

// "GCash · 0917123 → GCash1": the bank (wallet) and the YNAB account it imports into
function walletLabel(wallet) {
  const name = hosted ? wallet.name : sourceName(wallet.source);
  return wallet.account_name ? `${name} → ${wallet.account_name}` : name;
}

function select(options, value, onChange) {
  const el = document.createElement("select");
  options.forEach(([optionValue, label]) => el.add(new Option(label, optionValue)));
  el.value = value;
  el.addEventListener("change", () => onChange(el.value, el));
  return el;
}

function removeButton(label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary small-btn";
  button.textContent = "Remove";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", onClick);
  return button;
}

function cell(...children) {
  const td = document.createElement("td");
  td.append(...children);
  return td;
}

// ---- local wallets ---------------------------------------------------------

// Each local wallet row: which source, and which YNAB account it imports into
function renderWallets() {
  const tbody = document.querySelector("#local-wallets-table tbody");
  if (!tbody) return;
  tbody.replaceChildren();
  state.wallets.forEach((wallet, index) => {
    const sourceSelect = select(
      state.sources.map((key) => [key, sourceName(key)]),
      wallet.source,
      (value) => { wallet.source = value; renderRules(); },
    );
    const accounts = state.accounts.map((a) => [a.id, a.name]);
    if (wallet.id && !state.accounts.some((a) => a.id === wallet.id)) {
      accounts.unshift([wallet.id, wallet.account_name || "Saved account"]);
    }
    const accountSelect = select(
      [["", "Choose an account…"], ...accounts],
      wallet.id,
      (value, el) => {
        replaceWalletId(wallet, value);
        wallet.account_id = value;
        wallet.account_name = value ? el.selectedOptions[0].textContent : "";
        renderRules();
      },
    );
    const remove = removeButton("Remove this bank", () => {
      state.wallets.splice(index, 1);
      renderWallets();
      renderRules();
    });
    const tr = document.createElement("tr");
    tr.append(cell(sourceSelect), cell(accountSelect), cell(remove));
    tbody.append(tr);
  });
}

// A local wallet's id is its YNAB account's id: when the account changes, the
// rules that use the wallet follow it
function replaceWalletId(wallet, newId) {
  if (wallet.id) state.rules.forEach((rule) => {
    if (rule.from_wallet === wallet.id) rule.from_wallet = newId;
    if (rule.to_wallet === wallet.id) rule.to_wallet = newId;
  });
  wallet.id = newId;
}

function addWallet() {
  state.wallets.push({ id: "", name: "", source: state.sources[0], account_id: "", account_name: "" });
  renderWallets();
}

async function loadPlans() {
  const planSelect = $("plan");
  try {
    const plans = await api("/api/plans");
    const savedPlanId = mainEl.dataset.planId || "";
    planSelect.replaceChildren(new Option("Choose a plan…", ""));
    plans.forEach((p) => planSelect.add(new Option(p.name, p.id)));
    if (plans.some((p) => p.id === savedPlanId)) planSelect.value = savedPlanId;
    else if (plans.length === 1) planSelect.value = plans[0].id;
    await loadAccounts();
  } catch (err) {
    message("wallets-msg", `${err.message} Connect on the manual upload page, then come back.`);
  }
}

async function loadAccounts() {
  const planId = $("plan").value;
  state.accounts = [];
  if (planId) {
    try {
      state.accounts = await api(`/api/plans/${encodeURIComponent(planId)}/accounts`);
      message("wallets-msg", "");
    } catch (err) {
      message("wallets-msg", err.message);
    }
  }
  renderWallets();
}

// ---- rules -----------------------------------------------------------------

function renderRules() {
  const tbody = document.querySelector("#rules-table tbody");
  if (!tbody) return;
  const wallets = state.wallets.filter((w) => w.id).map((w) => [w.id, walletLabel(w)]);
  const options = [["", "Choose a bank…"], ...wallets];
  tbody.replaceChildren();
  state.rules.forEach((rule, index) => {
    const text = document.createElement("input");
    text.type = "text";
    text.maxLength = 100;
    text.placeholder = "e.g. Transfer to 0917123";
    text.value = rule.description;
    text.setAttribute("aria-label", "Description");
    text.addEventListener("input", () => { rule.description = text.value; });
    const from = select(options, rule.from_wallet, (value) => { rule.from_wallet = value; });
    const to = select(options, rule.to_wallet, (value) => { rule.to_wallet = value; });
    from.setAttribute("aria-label", "Source bank");
    to.setAttribute("aria-label", "Destination bank");
    const remove = removeButton("Remove this rule", () => {
      state.rules.splice(index, 1);
      renderRules();
    });
    const tr = document.createElement("tr");
    tr.append(cell(text), cell(from), cell(to), cell(remove));
    tbody.append(tr);
  });
  show($("no-rules"), state.rules.length === 0);
  const addRule = $("add-rule-btn");
  if (addRule) addRule.disabled = wallets.length < 2;
}

function addRule() {
  const ids = state.wallets.filter((w) => w.id).map((w) => w.id);
  state.rules.push({ description: "", from_wallet: ids[0] || "", to_wallet: ids[1] || "" });
  renderRules();
  const inputs = document.querySelectorAll("#rules-table tbody input");
  if (inputs.length) inputs[inputs.length - 1].focus();
}

// ---- save ------------------------------------------------------------------

async function save() {
  if (state.saving) return;
  state.saving = true;
  $("save-btn").disabled = true;
  message("save-msg", "Saving…", "info");
  const body = { rules: state.rules };
  if (!hosted) {
    const plan = $("plan");
    body.plan_id = plan.value;
    body.plan_name = plan.value ? plan.selectedOptions[0].textContent : "";
    body.wallets = state.wallets.map((w) => ({ id: w.id, source: w.source, account_name: w.account_name }));
  }
  try {
    const result = await api("/api/transfer-rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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
  if (!hosted) {
    $("add-wallet-btn").addEventListener("click", addWallet);
    $("plan").addEventListener("change", loadAccounts);
    renderWallets();
    loadPlans();
  }
  renderRules();
}
