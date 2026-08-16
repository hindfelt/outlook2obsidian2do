/* Ribbon command: extract to-dos from the selected message without opening a
   task pane.

   Runs in the hidden function file, so there is no UI - progress and results
   are reported through the notification bar at the top of the message. This is
   the path that lets you work through several emails one click at a time; the
   task pane is only needed for Settings and for previewing.

   Office kills the function's runtime once event.completed() is called, so the
   poll loop below is bounded: past the budget it hands off to the backend,
   which finishes and writes to the vault regardless. */

// This file is served by the backend, so its origin is the API's origin.
const API_BASE = window.location.origin;
const NOTIFY_KEY = "obsidianTodo"; // 32 char max
const POLL_MS = 2000;
// Office expects a command to finish in about 5 minutes. Stop reporting before
// that, so the last thing the user sees is a deliberate message rather than a
// runtime that vanished mid-poll.
const REPORT_BUDGET_MS = 4 * 60 * 1000;

const STAGE_TEXT = {
  queued: "Queued…",
  extracting: "Reading the email…",
  routing: "Choosing the destination note…",
  writing: "Formatting and writing…",
};

Office.onReady(() => {
  // Must be registered at load, not inside a handler.
  Office.actions.associate("extractTodos", extractTodos);
});

/* `bar` is the notificationMessages handle of the message the button was
   clicked on, captured once in extractTodos. Office.context.mailbox.item is
   "whatever is selected now", so resolving it here on every poll tick would
   paint this job's progress onto whichever email the user has moved to. */
function notify(bar, type, message, persistent) {
  const details = { type, message };
  if (type === "informationalMessage") {
    details.icon = "icon16";
    details.persistent = Boolean(persistent);
  }
  return new Promise((resolve) => {
    // replaceAsync adds the notification when the key is absent, so it covers
    // both the first call and every update after it.
    bar.replaceAsync(NOTIFY_KEY, details, () => resolve());
  });
}

function currentItemId() {
  const item = Office.context.mailbox.item;
  return item ? item.itemId || null : null;
}

function readBody() {
  return new Promise((resolve, reject) => {
    Office.context.mailbox.item.body.getAsync(Office.CoercionType.Text, (r) => {
      if (r.status === Office.AsyncResultStatus.Succeeded) resolve(r.value || "");
      else reject(new Error(r.error ? r.error.message : "Could not read the message body"));
    });
  });
}

function senderString(item) {
  const from = item.from || item.sender;
  if (!from) return "";
  const name = from.displayName || "";
  const address = from.emailAddress || "";
  return name && address ? `${name} <${address}>` : name || address;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function summarise(result) {
  if (!result.tasks.length) {
    return result.summary
      ? `No to-dos found. ${result.summary}`
      : "No actionable to-dos found in this email.";
  }
  const files = [...new Set(result.tasks.map((t) => t.file))].join(", ");
  const verb = result.dry_run ? "Would write" : "Wrote";
  const n = result.tasks.length;
  return `${verb} ${n} to-do${n === 1 ? "" : "s"} → ${files}`;
}

function minutesAgo(epochSeconds) {
  const m = Math.round((Date.now() / 1000 - epochSeconds) / 60);
  return m < 1 ? "just now" : `${m} min ago`;
}

async function extractTodos(event) {
  const item = Office.context.mailbox.item;
  const bar = item.notificationMessages;
  const startItemId = item.itemId || null;
  const progress = (msg) => notify(bar, "progressIndicator", msg);
  const inform = (msg) => notify(bar, "informationalMessage", msg, true);
  const fail = (msg) => notify(bar, "errorMessage", msg);

  try {
    await progress("Sending to the local agent team…");

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
        // The ribbon button writes. Use the task pane when you want to preview
        // first - a button has nowhere to put a checkbox.
        dry_run: false,
      }),
    });

    const job = await res.json();
    if (!res.ok) throw new Error(job.detail || `Backend returned ${res.status}`);

    // Same email clicked again within the reuse window: the backend hands back
    // the earlier job instead of writing the to-dos a second time.
    const reusedPrefix = job.reused ? `Already extracted ${minutesAgo(job.created_at)}. ` : "";

    const startedAt = Date.now();
    for (;;) {
      if (currentItemId() !== startItemId) {
        // The user moved on. The backend owns the job and still writes to the
        // vault; leave a final note on the original message and release the
        // runtime rather than polling for a result nobody is looking at.
        await inform(
          "Still running in the background. The to-dos will be written to the " +
          "vault when it finishes - come back to this message to check."
        );
        break;
      }

      const poll = await fetch(`${API_BASE}/api/jobs/${job.id}`);
      const state = await poll.json();
      if (!poll.ok) throw new Error(state.detail || `Backend returned ${poll.status}`);

      if (state.status === "done") {
        await inform(reusedPrefix + summarise(state.result));
        break;
      }
      if (state.status === "error") {
        await fail(state.error || "Extraction failed");
        break;
      }
      if (Date.now() - startedAt > REPORT_BUDGET_MS) {
        // Handing off rather than failing: the backend owns the job and still
        // writes to the vault when it finishes.
        await inform(
          "Still running. It will finish in the background and write to the " +
          "vault - check the task pane or the vault in a minute."
        );
        break;
      }

      const n = state.queue_position || 0;
      const text = state.status === "queued" && n > 0
        ? `Queued behind ${n} other job${n === 1 ? "" : "s"}…`
        : STAGE_TEXT[state.stage] || state.stage;
      await progress(`${text} (${state.elapsed_s}s)`);
      await sleep(POLL_MS);
    }
  } catch (err) {
    const message = (err && err.message) || String(err);
    await fail(
      message.includes("Failed to fetch") || message.includes("Load failed")
        ? "Cannot reach the backend. Is ./scripts/run.sh running?"
        : message
    );
  } finally {
    // Always release the runtime, or the button stays spinning.
    event.completed();
  }
}
