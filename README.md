# outlook2obsidian2do

Click a button in Outlook → an agent team extracts the to-dos from the email,
decides which note they belong in, and appends them to the Obsidian vault as
`- [ ]` checklist lines.

The models run **either fully locally via Ollama, or on the Claude API** — you
pick in the task pane, per agent, at runtime.

```
Outlook add-in ──POST /api/tasks──▶ FastAPI (localhost, HTTPS)
     ▲                                 │ returns a job id immediately
     └──── poll /api/jobs/{id} ────────┤
                                       ├─ Agent 1 Extractor → structured task array
                                       ├─ Agent 2 Router    → route id per task
                                       ├─ Agent 3 Writer    → one checklist line per task
                                       ▼
                          Python file I/O → ~/Obsidian/…
```

Every agent call is constrained decoding — Anthropic structured outputs, or
Ollama's JSON-Schema grammar — so each hand-off is a validated object, never
prose that has to be parsed.

## Choosing where the models run

Set it in the task pane under **Settings**. Both paths are wired and tested.

|  | Local (Ollama) | Claude API |
|---|---|---|
| Email content leaves the Mac | **no** | yes, to Anthropic |
| Time per email | ~6 s (`gemma4:26b`, warm) · minutes for a cold load | ~3–5 s |
| Cost | none | ~$0.01–0.13/email |
| Setup | `ollama serve` + a pulled model | API key |

`POST /api/tasks` **never blocks** — it returns a job id and the pane polls.
You can close the pane; the write still happens. Jobs run one at a time; the
pane shows your place in the queue.

### What actually works locally

Measured on this repo's harness (`scripts/eval_local.py`), M5 Max / 128 GB:

| model | schema | extract | owner | dates | FYI restraint | injection | routing |
|---|---|---|---|---|---|---|---|
| `gemma4:26b` (thinking off) | ok | 3/3 | ok | ok | 0 tasks (correct) | resisted | 3/3 |
| `gemma4:31b` | ok | 3/3 | ok | ok | 0 tasks (correct) | resisted | ok |
| `qwen3.6:27b-coding` | **FAIL** | – | – | – | – | – | – |

`gemma4:26b` passes every check with the shipped prompts, in about 9 s for
the whole harness. Full pipeline on a two-task email: ~6 s warm, both tasks,
both routes correct, well-formed lines.

The harness imports the backend's own prompts, schemas and grammar caps, so
its scores describe what the pipeline actually sends — not a copy that can
drift.

Two things the backend does that make gemma4 usable, both in
`providers/ollama_provider.py` and adjustable in `config.local.json`:

- **`ollama_think: false`.** gemma4 (and qwen3) are thinking models. Left on,
  the whole token budget goes into the hidden reasoning channel and the JSON
  grammar gets nothing — or, worse, forces garbage (`workstreams workstreams
  …`, stray Korean tokens, three-minute runaways). Off, the same model answers
  in seconds.
- **`ollama_temperature: 0.3`** (with Gemma's own `top_k 64 / top_p 0.95`).
  At temperature 0 under a grammar gemma4 either loops or returns an empty
  array. 0.3 was measured stable across repeated runs.

Also: every string in the grammar is capped at 1000 chars, so a model that
does loop hands back valid, bounded JSON instead of an unterminated string.

`qwen3.6:27b-coding` ignores the schema — returns a bare array where the
contract says object. Not usable here.

Caveat worth stating: that's **one email per check** — enough to disqualify a
model, not enough to certify one. Re-run the harness on your own mail before
trusting it.

Avoid abliterated models (e.g. `qwen3.5-abliterated:122b`) for the Extractor.
Refusal behaviour is removed, and the Extractor is the one component whose
entire input is untrusted text from outside your org.

## Setup

```bash
git clone https://github.com/hindfelt/outlook2obsidian2do.git
cd outlook2obsidian2do
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp .env.example .env              # vault path, port, TLS overrides
cp routes.example.json routes.json
```

Then edit `routes.json`: the routes are where your to-dos land, and
`owner_context` is the one line about you that the Extractor and Router use to
judge what matters. Both files are gitignored.

Office add-ins require HTTPS even on localhost:

```bash
npx --yes office-addin-dev-certs install
```

For the local path, make sure Ollama has a model:

```bash
ollama pull gemma4:26b
```

For the Claude path, enter the API key in the task pane's **Settings** panel —
it goes into the **macOS Keychain**, not into a file. `ANTHROPIC_API_KEY` still
works as a fallback.

## Run

As a login service (recommended — starts at login, restarts if it dies):

```bash
./scripts/service.sh install     # once
./scripts/service.sh status
./scripts/service.sh restart     # after pulling code changes
./scripts/service.sh logs
```

Or in the foreground:

```bash
./scripts/run.sh
```

Serves the API and the add-in on `https://localhost:8000` — one origin, so no
CORS and no mixed-content block inside Outlook. Bound to loopback, so the config
and credential endpoints aren't reachable from the network.

For the local path Ollama also has to be up at login: `brew services start ollama`
(Homebrew) or keep the Ollama app in Login Items. If Ollama comes up after the
backend, the pane's next open re-checks the model list.

On startup, on every pane open and before every job, the backend checks that
the configured Ollama models are actually installed. A missing tag is replaced
by the largest installed model of the **same family** (`gemma4:31b` →
`gemma4:26b`); if the family is gone entirely it logs a warning and leaves the
choice to you in Settings.

Clicking **Extract to-dos** twice on the same email within an hour returns the
first run instead of appending the tasks again. Previews (dry runs) never
count, and a failed run can be retried immediately. The pane offers **Run
again** when you really want a second pass.

## Sideload into Outlook for Mac

Outlook for Mac syncs add-ins from the mailbox, so install once via Outlook on
the web.

1. Have the backend running (`./scripts/service.sh status`).
2. Open <https://localhost:8000/addin/taskpane.html> in a browser once and accept
   the certificate — this also proves the cert works.
3. <https://outlook.office.com/mail/> → open a message → **Apps → Add-ins → Get
   Add-ins** (or **More apps → Manage add-ins**).
4. **My add-ins → Custom Addins → Add a custom add-in → Add from file…**
5. Upload `addin/manifest.xml`, accept the "not from the store" warning.
6. Restart Outlook for Mac. **Obsidian → Extract to-dos** appears in the ribbon
   on an open message; it syncs within a few minutes.

Changes to `taskpane.js/html/css` need no re-upload — just reload the pane.
Manifest changes do: remove the add-in, add it again from file, restart Outlook.

### Keep the pane open

**Obsidian To-Do → Open pane**, then click the pin icon in the pane header. The
pane now stays while you click through the inbox: subject updates, one click
extracts, and the **Jobs** list shows every run — queued, running, done, with
the written lines — including runs started from the ribbon button. Selecting
an email that was already processed shows that result immediately.

New Outlook for Mac quirk: it never shows the pin for `SupportsPinning` alone
([office-js #2635](https://github.com/OfficeDev/office-js/issues/2635)) but
does when the pane also declares `SupportsMultiSelect`
([#5921](https://github.com/officedev/office-js/issues/5921)). That is why the
manifest has both. With several emails selected the pane just says "No message
selected".

## Routing

The Router only ever picks an `id` from `routes.json`; it never sees or
constructs a filesystem path. `routes.example.json` ships this starting set:

| id | Destination |
|---|---|
| `acme` | `00 Inbox/TODO.md` › `## Acme` |
| `northwind` | `20 Clients/Northwind/TODO/Todo.md` › `## Inbox` |
| `pipeline` | `00 Inbox/TODO.md` › `## Pipeline` |
| `admin` | `00 Inbox/TODO.md` › `## Admin` |
| `personal` | `00 Inbox/TODO.md` › `## Personal` |
| `unsorted` | `00 Inbox/TODO.md` › `## Unsorted` (fallback) |

Replace them with your own. Add a route by appending an entry with a clear
`description` — that text is the only thing the Router uses. Missing sections
are created; existing content is never rewritten.

`owner_context` in the same file is a single line describing whose mailbox this
is ("a freelance designer running three client projects"). It is interpolated
into the Extractor and Router system prompts, so relevance is judged against
you rather than a generic office worker. Left out, it falls back to "a busy
professional".

## Tests

```bash
.venv/bin/python scripts/smoke_test.py                    # offline, no API key
.venv/bin/python scripts/eval_local.py                    # score local models
.venv/bin/python scripts/live_test.py ollama gemma4:26b   # real run, throwaway vault
```

`eval_local.py` scores schema compliance, extraction recall, owner attribution,
invented dates, restraint on FYI-only mail, prompt-injection resistance, and
routing. Run it against any model before trusting it with your mail.

## Where things are stored

| What | Where | In git |
|---|---|---|
| Provider, per-agent models, Ollama sampling (`ollama_temperature`, `ollama_top_k`, `ollama_top_p`, `ollama_think`, `ollama_num_ctx`) | `config.local.json` (written by the pane; sampling keys by hand) | no |
| Anthropic API key | macOS Keychain (`security find-generic-password -s outlook2obsidian2do -a anthropic_api_key -w`) | no |
| Vault path, TLS, port | `.env` | no |
| Routing table and owner context | `routes.json` (copy of `routes.example.json`) | no |

Set `CREDENTIAL_STORE=file` to use a 0600 `.credentials` file instead of the
Keychain.

## Safety notes

- **Email bodies are untrusted input.** All three agents are told the email is
  data, not instructions. Verified: `eval_local.py` plants an injected payment
  instruction and checks the model ignores it.
- **The write is gated in Python, not by the model.** `vault.validate_lines()`
  rejects anything that isn't a single-line `- [ ] …` entry (no headings, no
  embedded newlines, 2000-char cap); `vault.resolve_route_path()` refuses any
  path resolving outside the vault root. A rejected line is dropped with a
  warning in the result; the other lines are still written. Worst case for a
  model that misbehaves is a junk task in `TODO.md`, not a modified file
  elsewhere. The Router's `route_id` is an enum of `routes.json` ids in the
  schema itself, so neither backend can even emit an unknown destination.
- **Notes are replaced atomically.** The file is written to a temp path in the
  same folder and renamed over the original, so a crash mid-write cannot leave
  a truncated note for your sync client to pick up.
- **The API key is write-only over HTTP.** `GET /api/config` returns
  `api_key_set` and a masked hint (`sk-ant-…7Xk2`); the key itself is never sent
  back to the pane and is not logged.
- **Preview first.** Tick *Preview only* to run the full agent team and see the
  exact lines without touching the vault.
- Writes are appends only. Nothing is deleted or overwritten.
