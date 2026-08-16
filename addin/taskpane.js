/* Outlook task pane.
   Reads the current message via Office.js, queues it on the local backend, and
   polls for the result. The backend may be running a local model that takes
   minutes, so nothing here blocks on a single request. */

// Same origin as this page — the backend serves these files, so there is no
// CORS preflight and no mixed-content block inside Outlook.
const API_BASE = window.location.origin;
const POLL_MS = 1500;
// Bump when changing this file. Shown in the footer so it is obvious at a
// glance whether Outlook's webview is running the current build or a cached one.
const BUILD = "2026-08-16.2";

const AGENTS = ["extractor", "router", "writer"];
const STAGE_TEXT = {
  queued: "Queued…",
  extracting: "Agent 1: reading the email…",
  routing: "Agent 2: choosing the destination…",
  writing: "Agent 3: formatting and writing…",
  done: "Done",
};

const els = {};
let config = null;
let polling = null;
// Jobs list: refreshed fast while anything is in flight, slowly otherwise.
const JOBS_ACTIVE_MS = 2000;
const JOBS_IDLE_MS = 15000;
let jobsTimer = null;
let jobsCache = [];

Office.onReady((info) => {
  if (info.host !== Office.HostType.Outlook) return;

  const id = (x) => document.getElementById(x);
  Object.assign(els, {
    subject: id("subject"), run: id("run"), dryRun: id("dryRun"),
    status: id("status"), results: id("results"),
    jobs: id("jobs"), jobList: id("jobList"),
    backend: id("backend"), health: id("health"),
    settings: id("settings"), settingsStatus: id("settingsStatus"),
    save: id("saveSettings"), test: id("testSettings"),
    apiKey: id("apiKey"), keyState: id("keyState"), keyField: id("keyField"),
    models: {
      extractor: id("extractorModel"),
      router: id("routerModel"),
      writer: id("writerModel"),
    },
  });

  els.backend.textContent = API_BASE;
  els.run.addEventListener("click", () => run(false));
  els.save.addEventListener("click", saveSettings);
  els.test.addEventListener("click", testSettings);
  for (const input of document.querySelectorAll('input[name="provider"]')) {
    input.addEventListener("change", () => {
      applyProviderToForm(selectedProvider());
    });
  }

  reportHostCapabilities();
  bindToCurrentItem();

  // When the pane is pinned it stays open as you move between messages, and
  // Office swaps Office.context.mailbox.item underneath it without reloading
  // the page. Without this handler the pane would keep showing - and act on -
  // the message that was selected when it opened.
  if (Office.context.requirements.isSetSupported("Mailbox", "1.5")) {
    Office.context.mailbox.addHandlerAsync(
      Office.EventType.ItemChanged,
      onItemChanged
    );
  }

  loadConfig();
  refreshJobs();
});

function reportHostCapabilities() {
  // Pinning needs Mailbox 1.5 AND a client that implements it. If the pin icon
  // never appears, this line says which of the two is missing.
  const supported = (v) => {
    try {
      return Office.context.requirements.isSetSupported("Mailbox", v);
    } catch (err) {
      return false;
    }
  };
  const d = (Office.context.mailbox && Office.context.mailbox.diagnostics) || {};
  const sets = ["1.3", "1.5", "1.7", "1.9", "1.10", "1.12", "1.14"]
    .filter(supported)
    .join(", ");

  const line = document.createElement("div");
  line.className = "muted small";
  line.style.marginTop = "4px";
  line.textContent =
    `build ${BUILD} · ${d.hostName || "?"} ${d.hostVersion || "?"} · ` +
    `Mailbox sets: ${sets || "none"}`;
  document.querySelector("footer").appendChild(line);
  console.log("[obsidian-todo] host diagnostics", {
    hostName: d.hostName,
    hostVersion: d.hostVersion,
    owaView: d.OWAView,
    mailbox15: supported("1.5"),
  });
}

function bindToCurrentItem() {
  const item = Office.context.mailbox.item;
  if (!item) {
    // Pinned pane with no message selected (e.g. a folder view).
    els.subject.textContent = "No message selected";
    els.run.disabled = true;
    return;
  }
  els.subject.textContent = item.subject || "(no subject)";
  els.run.disabled = false;
}

function currentItemId() {
  const item = Office.context.mailbox.item;
  return item ? item.itemId || null : null;
}

function onItemChanged() {
  // A run already in flight belongs to the previous message. The backend keeps
  // going and still writes to the vault; the jobs list below keeps showing it.
  if (polling) {
    clearTimeout(polling);
    polling = null;
  }

  els.results.hidden = true;
  els.results.innerHTML = "";
  els.status.hidden = true;
  shownJobKey = null;

  bindToCurrentItem();
  // If this email was already processed, show that straight away rather
  // than an empty pane and a button that would only hand back the same job.
  showJobForCurrentItem();
  renderJobs();
}

/* ---------- jobs list ---------- */

function jobForItem(itemId) {
  if (!itemId) return null;
  const mine = jobsCache.filter((j) => j.item_id === itemId);
  // Prefer a real run over a preview; the list is newest-first already.
  return mine.find((j) => !j.dry_run) || mine[0] || null;
}

// id:status of what the header area currently shows, so periodic refreshes
// only redraw when the job actually changed (and don't wipe a "Run again").
let shownJobKey = null;

function showJobForCurrentItem() {
  if (polling !== null) return; // actively tracking a job started here
  const job = jobForItem(currentItemId());
  if (!job) return;
  const key = `${job.id}:${job.status}`;
  const live = job.status === "queued" || job.status === "running";
  if (key === shownJobKey && !live) return; // live states redraw for the timer
  shownJobKey = key;
  els.results.innerHTML = "";
  if (job.status === "done" && job.result) {
    render(job.result);
    const p = document.createElement("p");
    p.className = "small muted";
    p.textContent = `${job.dry_run ? "Previewed" : "Extracted"} ${minutesAgo(job.created_at)}.`;
    els.results.prepend(p);
    els.results.hidden = false;
  } else if (job.status === "error") {
    setStatus(`Last run failed: ${job.error || "unknown error"}`, "error");
  } else {
    setStatus(`${stageText(job)} (${job.elapsed_s}s)`);
  }
}

async function refreshJobs() {
  if (jobsTimer) {
    clearTimeout(jobsTimer);
    jobsTimer = null;
  }
  let active = false;
  try {
    const res = await fetch(`${API_BASE}/api/jobs?limit=12`);
    const data = await res.json();
    if (res.ok) {
      jobsCache = data.jobs || [];
      renderJobs();
      active = jobsCache.some((j) => j.status === "queued" || j.status === "running");
      // While a job we are not polling ourselves runs (ribbon click, other
      // pane), keep the header area for the current item live too.
      if (polling === null) showJobForCurrentItem();
    }
  } catch (err) {
    // Backend down: the footer already says so; try again later.
  }
  jobsTimer = setTimeout(refreshJobs, active ? JOBS_ACTIVE_MS : JOBS_IDLE_MS);
}

function jobDetail(job) {
  if (job.status === "queued" || job.status === "running") {
    return `${stageText(job)} (${job.elapsed_s}s)`;
  }
  if (job.status === "error") return job.error || "Failed";
  const r = job.result;
  if (!r) return "Done";
  if (!r.tasks.length) return r.summary || "No to-dos found";
  const files = [...new Set(r.tasks.map((t) => t.file))].join(", ");
  const n = r.tasks.length;
  const verb = r.dry_run ? "Would write" : "Wrote";
  const warn = r.warnings.length ? ` · ${r.warnings.length} warning${r.warnings.length === 1 ? "" : "s"}` : "";
  return `${verb} ${n} to-do${n === 1 ? "" : "s"} → ${files}${warn}`;
}

function renderJobs() {
  if (!jobsCache.length) {
    els.jobs.hidden = true;
    return;
  }
  const currentId = currentItemId();
  // The list is rebuilt on every refresh; keep whatever the user expanded.
  const open = new Set(
    [...els.jobList.querySelectorAll(".job details[open]")].map((d) => d.closest(".job").dataset.id)
  );
  const frag = document.createDocumentFragment();

  for (const job of jobsCache) {
    const row = document.createElement("div");
    row.className = `job${job.item_id && job.item_id === currentId ? " current" : ""}`;
    row.dataset.id = job.id;

    const head = document.createElement("div");
    head.className = "head";
    const badge = document.createElement("span");
    badge.className = `badge ${job.status}`;
    badge.textContent = job.dry_run && job.status === "done" ? "preview" : job.status;
    const subject = document.createElement("span");
    subject.className = "subject";
    subject.textContent = job.subject || "(no subject)";
    subject.title = job.subject || "";
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = minutesAgo(job.created_at);
    head.append(badge, subject, when);
    row.appendChild(head);

    const detail = document.createElement("div");
    detail.className = `detail${job.status === "error" ? " error" : ""}`;
    detail.textContent = jobDetail(job);
    row.appendChild(detail);

    if (job.status === "done" && job.result && job.result.tasks.length) {
      const d = document.createElement("details");
      d.open = open.has(job.id);
      const s = document.createElement("summary");
      s.textContent = "lines";
      d.appendChild(s);
      for (const t of job.result.tasks) {
        const line = document.createElement("div");
        line.className = "line";
        line.textContent = t.markdown;
        d.appendChild(line);
      }
      row.appendChild(d);
    }
    frag.appendChild(row);
  }

  els.jobList.innerHTML = "";
  els.jobList.appendChild(frag);
  const active = jobsCache.filter((j) => j.status === "queued" || j.status === "running").length;
  const h2 = els.jobs.querySelector("h2");
  h2.innerHTML = "";
  h2.append("Jobs");
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = active ? `${active} in flight` : `${jobsCache.length} recent`;
  h2.appendChild(count);
  els.jobs.hidden = false;
}

/* ---------- settings ---------- */

function selectedProvider() {
  const checked = document.querySelector('input[name="provider"]:checked');
  return checked ? checked.value : "anthropic";
}

async function loadConfig() {
  try {
    const res = await fetch(`${API_BASE}/api/config`);
    config = await res.json();
  } catch (err) {
    els.health.textContent = "backend unreachable";
    return;
  }

  const radio = document.querySelector(
    `input[name="provider"][value="${config.provider}"]`
  );
  if (radio) radio.checked = true;

  applyProviderToForm(config.provider);
  renderHealth();
}

function applyProviderToForm(provider) {
  if (!config) return;
  const options = config.available_models[provider] || [];

  for (const agent of AGENTS) {
    const select = els.models[agent];
    const currentValue = config.models[agent];
    select.innerHTML = "";

    const seen = new Set();
    for (const opt of options) {
      seen.add(opt.id);
      select.appendChild(new Option(opt.label || opt.id, opt.id));
    }
    // The configured ids belong to the configured provider. Only carry one
    // across when the user is looking at that same provider - it may be a
    // model that was deleted or hand-edited in, and it should stay visible as
    // such. When they have switched provider in the form, the old id is
    // meaningless here (a gemma tag sent to Anthropic is a 404), so start from
    // that provider's first option instead.
    const sameProvider = provider === config.provider;
    let value = "";
    if (currentValue && seen.has(currentValue)) {
      value = currentValue;
    } else if (currentValue && sameProvider) {
      select.appendChild(new Option(`${currentValue} (not installed)`, currentValue));
      value = currentValue;
    } else if (options.length) {
      value = options[0].id;
    } else {
      select.appendChild(new Option("no models available", ""));
    }
    select.value = value;
  }

  // The key field is only meaningful for the hosted API.
  els.keyField.hidden = provider !== "anthropic";
  els.apiKey.value = "";
  els.apiKey.placeholder = config.api_key_set ? "•••• saved ••••" : "sk-ant-…";
  els.keyState.textContent = config.api_key_set
    ? `Saved (${config.api_key_hint}) in ${config.credential_store}. ` +
      `Leave blank to keep it; type a new key to replace it.`
    : `No key stored. It will be saved in ${config.credential_store}, not in the vault or this page.`;
}

function renderHealth() {
  if (!config) return;
  const h = config.health[config.provider] || {};
  const where = config.provider === "ollama" ? "local" : "Claude API";
  els.health.textContent = h.ready
    ? `${where} · ${h.detail}`
    : `${where} · NOT READY — ${h.detail}`;
}

async function saveSettings() {
  els.save.disabled = true;
  els.settingsStatus.textContent = "Saving…";

  const body = { provider: selectedProvider() };
  for (const agent of AGENTS) {
    const value = els.models[agent].value;
    if (value) body[`${agent}_model`] = value;
  }
  // Only send the key when the user actually typed one — an empty field means
  // "leave the stored key alone", not "delete it".
  if (els.apiKey.value.trim()) body.anthropic_api_key = els.apiKey.value.trim();

  try {
    const res = await fetch(`${API_BASE}/api/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `Backend returned ${res.status}`);

    config = data;
    els.apiKey.value = "";
    applyProviderToForm(config.provider);
    renderHealth();
    els.settingsStatus.textContent = "Saved.";
  } catch (err) {
    els.settingsStatus.textContent = `Save failed: ${err.message}`;
  } finally {
    els.save.disabled = false;
  }
}

async function testSettings() {
  els.test.disabled = true;
  els.settingsStatus.textContent = "Testing (a local model may need to load first)…";
  try {
    const res = await fetch(`${API_BASE}/api/config/test`, { method: "POST" });
    const data = await res.json();
    els.settingsStatus.textContent = data.ok
      ? `OK — ${data.provider}/${data.model}: ${data.detail}`
      : `Failed — ${data.detail}`;
  } catch (err) {
    els.settingsStatus.textContent = `Failed — ${err.message}`;
  } finally {
    els.test.disabled = false;
  }
}

/* ---------- extraction ---------- */

function readBody() {
  return new Promise((resolve, reject) => {
    Office.context.mailbox.item.body.getAsync(Office.CoercionType.Text, (result) => {
      if (result.status === Office.AsyncResultStatus.Succeeded) resolve(result.value || "");
      else reject(new Error(result.error ? result.error.message : "Could not read body"));
    });
  });
}

function senderString(item) {
  const from = item.from || item.sender;
  if (!from) return "";
  const name = from.displayName || "";
  const address = from.emailAddress || "";
  if (name && address) return `${name} <${address}>`;
  return name || address;
}

/* Jobs run one at a time on the backend; while waiting, say how many are
   ahead rather than counting seconds on something that has not started. */
function stageText(job) {
  if (job.status === "queued" && job.queue_position > 0) {
    const n = job.queue_position;
    return `Queued behind ${n} other job${n === 1 ? "" : "s"}…`;
  }
  return STAGE_TEXT[job.stage] || job.stage;
}

function setStatus(text, kind) {
  els.status.hidden = false;
  els.status.textContent = text;
  els.status.className = `status${kind ? ` ${kind}` : ""}`;
}

function minutesAgo(epochSeconds) {
  const m = Math.round((Date.now() / 1000 - epochSeconds) / 60);
  return m < 1 ? "just now" : `${m} min ago`;
}

/* Shown when the backend handed back an earlier job for this same email
   instead of running again. Offers an explicit re-run for the rare case where
   that is what the user meant. */
function offerRerun(job) {
  const p = document.createElement("p");
  p.className = "small muted";
  p.append(`Already extracted ${minutesAgo(job.created_at)} - showing that result. `);
  const again = document.createElement("a");
  again.href = "#";
  again.textContent = "Run again";
  again.addEventListener("click", (e) => {
    e.preventDefault();
    run(true);
  });
  p.appendChild(again);
  els.results.appendChild(p);
  els.results.hidden = false;
}

async function run(force) {
  const item = Office.context.mailbox.item;
  if (polling) {
    clearTimeout(polling);
    polling = null;
  }
  els.run.disabled = true;
  els.results.hidden = true;
  els.results.innerHTML = "";
  shownJobKey = null;
  setStatus("Reading message…");

  try {
    const body = await readBody();
    const res = await fetch(`${API_BASE}/api/tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subject: item.subject || "",
        sender: senderString(item),
        body,
        received_at: item.dateTimeCreated ? item.dateTimeCreated.toISOString() : null,
        item_id: item.itemId || null,
        dry_run: els.dryRun.checked,
        force: Boolean(force),
      }),
    });

    const job = await res.json();
    if (!res.ok) throw new Error(job.detail || `Backend returned ${res.status}`);

    if (job.provider === "ollama" && !job.reused) {
      setStatus(
        "Queued on the local model. This takes a couple of minutes — you can " +
        "close this pane, the tasks will still be written."
      );
    }
    poll(job.id, job.reused ? job : null);
    refreshJobs();
  } catch (err) {
    setStatus(err.message || String(err), "error");
    els.run.disabled = false;
  }
}

function poll(jobId, reusedJob) {
  const tick = async () => {
    let job;
    try {
      const res = await fetch(`${API_BASE}/api/jobs/${jobId}`);
      job = await res.json();
      if (!res.ok) throw new Error(job.detail || `Backend returned ${res.status}`);
    } catch (err) {
      polling = null;
      setStatus(`Lost contact with the backend: ${err.message}`, "error");
      els.run.disabled = false;
      return;
    }

    if (job.status === "queued" || job.status === "running") {
      setStatus(`${stageText(job)} (${job.elapsed_s}s)`);
      polling = setTimeout(tick, POLL_MS);
      return;
    }

    polling = null;
    els.run.disabled = false;
    // What we render now is this job's final state; the periodic refresh
    // must not redraw over it (and lose the "Run again" link).
    shownJobKey = `${job.id}:${job.status}`;
    if (job.status === "error") {
      setStatus(job.error || "Job failed", "error");
    } else {
      render(job.result);
      if (reusedJob) offerRerun(reusedJob);
    }
    refreshJobs();
  };
  tick();
}

function render(data) {
  if (!data.tasks.length) {
    setStatus(data.summary || "No actionable to-dos found in this email.", "ok");
    return;
  }

  const verb = data.dry_run ? "Previewed" : "Wrote";
  const files = [...new Set(data.tasks.map((t) => t.file))].join(", ");
  setStatus(`${verb} ${data.tasks.length} to-do(s) → ${files}`, "ok");

  const frag = document.createDocumentFragment();

  if (data.summary) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = data.summary;
    frag.appendChild(p);
  }

  for (const task of data.tasks) {
    const card = document.createElement("div");
    card.className = "task";

    const md = document.createElement("div");
    md.className = "md";
    md.textContent = task.markdown;
    card.appendChild(md);

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent =
      `${task.file} › ## ${task.section} · ${task.route_id} ` +
      `(${Math.round(task.confidence * 100)}%) · ${task.reason}`;
    card.appendChild(meta);

    frag.appendChild(card);
  }

  for (const warning of data.warnings) {
    const p = document.createElement("p");
    p.className = "small";
    p.textContent = `⚠ ${warning}`;
    frag.appendChild(p);
  }

  els.results.appendChild(frag);
  els.results.hidden = false;
}
