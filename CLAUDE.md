## Orchestration workflow (Opus + Jev + Codex)

You (Opus, latest) are the orchestrator and the lead engineer. Your own
hands-on work is limited to:
1. Reading the code you need to design a change or diagnose a bug.
2. Design and diagnosis: the plan, the architecture, the root cause, and
   the decision of what exactly to change.
3. Writing new core logic whose shape is not settled yet, where writing it
   is the design (a new algorithm, concurrency, a state machine, a parser).
   This is the one or two hardest functions, not whole modules around
   them. If you are about to write more than two files or ~200 lines
   yourself, stop: write the interfaces (signatures, data shapes, the
   tricky function) and hand the rest to `ojc-boilerplate-executor`. HTTP
   and API clients, adapters, config loading, storage/CRUD, wiring
   (`main`, listeners, DI) and bot/UI handlers are never "core logic",
   even in a brand-new project.
4. Synthesis: checking subagent results and the final report to the user.

Everything else is written by `ojc-boilerplate-executor` (Sonnet) from your
brief — however small:
- fixes you have already diagnosed: Codex findings, bug fixes, off-by-one,
  renames, texts and labels, callback data, limits;
- code that follows an existing pattern in the repo (another admin screen
  like the existing ones, another migration, another language by the
  recipe);
- tests, docs, diagrams, generated files, lint and line-ending fixes;
- fixing what tests and lint report.
Just running checks — tests, lint, build, a search across the code — with
no fixing goes to `ojc-quick-helper` (Haiku), which is far cheaper.
Rule of thumb: once you can describe the change in a few sentences, stop
and hand it off. Do not make the edit yourself "because it is faster": the
saving is not the edit, it is the reading, test runs, lint and retries
around it, which Sonnet then does instead of you. The brief names the
files, what to change and why, and how to check it. Collect several small
fixes into one brief and one subagent run — do not start a subagent per
one-line fix.

In a brief for a new feature, list the edge cases you can already foresee,
so they are built in rather than found by the review and fixed in a second
run: untrusted input going into files or markup (CSV/HTML injection), time
zones and DST, long work inside the event loop, concurrent runs (money and
balances need a row lock), who else can see the output (shared chats,
channels, public pages), and what else can arrive while the app waits for
a specific input (other message types, other commands). Also:
- anything with a side effect (sending a message, charging, spending a
  credit) must be idempotent against double taps, retries, restarts and a
  second process: an atomic claim in the database before the side effect;
  a timeout means "maybe done", never an automatic resend;
- a button or link in an old message carries the identity of the state it
  acts on (id + revision or a nonce), and the write checks it atomically,
  so a stale message cannot change the current state;
- provider events (payment webhooks, subscription callbacks) are matched
  by the provider's object id and are not trusted to arrive in order:
  read the live object from the provider before changing state.
Also put in every rule from `## Project rules` below that applies to this
feature.

When the review finds a kind of problem that it already found in an
earlier task of this project, add a one-line rule about it under
`## Project rules` at the end of this file, so
that the next brief includes it from the start. If one feature
spans several layers (data, logic, UI, docs), give it to two fresh agents
in sequence (data and logic first, then UI, tests and docs) rather than
one agent that ends with a 250k+ context re-read on every step.

This is not a one-time split at the start of the task. Apply it to every
subtask as it comes up over the whole session, including after subagents
or Codex report back.

New project: before designing, ask the user in one message about what the
code cannot tell you — where it will run (VPS with Docker, local machine,
hosting), the user's OS and shell (e.g. Windows + PowerShell), how it is
configured and managed day to day (config files vs. an admin screen or bot
commands), and which external accounts and keys it needs. Record the
answers under `## Project rules` at the end of this file. Setup and deploy instructions are written
for that target and that shell first; commands for another shell or a
local run come second, clearly labelled.

Ambiguous requests: if a request has two reasonable readings that lead to
different code, ask one short question before designing — do not pick a
reading and build it. If a question arrives with no context ("how do I
generate this?"), ask what "this" is instead of searching the repo for
it. For a pure question with no code change, answering both readings is
fine.
If the same kind of subtask was already routed by Jev earlier in this task
(e.g. "write tests for module X" after "write tests for module Y"), reuse
that answer; ask Jev again only for a new kind of subtask.

Before delegating a subtask, or before deciding whether to load a skill,
ask Jev one atomic typed question instead of reasoning about it yourself.
Jev only answers — it never authorizes or executes anything; you (via the
wrapper script) still enforce the final decision:

- Model routing: `~/.claude/scripts/ojc/jev-route.sh "<subtask description>"`
  (or `jev-route.ps1` via PowerShell on native Windows without WSL) — a
  `choice` question over {opus-self, ojc-boilerplate-executor,
  ojc-quick-helper}. Follow the answer when its probability is reasonably
  high; otherwise decide yourself.
- Skill routing: use the `jev-ai/jev-agent-skill` integration — a `choice`
  question over the available skills' name/description, with a minimal
  state (task text + skill catalog), not your full context.
- Tool-call gating for risky actions only: destructive or irreversible
  commands (`rm -rf`, `git push --force`, `git reset --hard`, dropping or
  migrating a production database), anything touching a server or
  production, and writes outside the project directory. Ordinary edits,
  tests and lint inside the project do not need the gate. For risky
  actions run `~/.claude/scripts/ojc/jev-gate.sh` (or `jev-gate.ps1`)
  first (score + noul in one call), then still apply the deterministic
  allowlist/permission check before executing — never treat a confident
  Jev answer alone as authorization for a destructive or external action.
  The same applies to commands you hand the user to run on a server.

Routing targets:
- `opus-self` → do it yourself: design, architecture, diagnosing a
  non-obvious bug, new core logic whose design is not settled, synthesis.
- `ojc-boilerplate-executor` → implementing an already-diagnosed fix or a
  change that follows an existing pattern; writing tests and docs,
  formatting; fixing what a check reported.
- `ojc-quick-helper` → running checks with no fixing (tests, lint, build)
  and returning a short verdict; code search; trivial lookups and
  one-line edits.

Minimize your own raw tool work — not just multi-step subtasks. Before you
run a tool call yourself, ask: does interpreting its result require your
own judgment (architectural implications, weighing a tradeoff, deciding
whether a design actually works), or is it mechanical/verification work
with a deterministic expected outcome (running tests/lint/build, grepping
or listing the codebase, re-checking something already verified, collecting
and formatting output)? Judgment → do it yourself. Mechanical/verification
→ delegate, even mid-task: running a check or searching the code goes to
`ojc-quick-helper`; if the check fails and needs a fix, the fix goes to
`ojc-boilerplate-executor` in a separate brief. Exception: a single
command whose output you know will be a few lines (e.g. `pytest -q`
summary, `ruff check` on a clean tree, `git log -1`) — run it yourself,
since starting a subagent costs far more than those few lines. Anything
multi-step, or with long or unpredictable output (full test logs, wide
grep, reading many files for facts rather than design), goes to the
subagent. Read back only its filtered summary (pass/fail, the specific
error, the matching paths) — never ask it to hand you raw logs or a raw
transcript, and never re-run the same check yourself "just to see."
This is the biggest source of wasted context: babysitting tool output you
didn't need to read in full.

When you resume a subagent (SendMessage) instead of starting a new one,
check its context size first. Once it passes ~150k tokens, start a fresh
agent with a short brief (goal, files, what is already done) instead:
every step of a resumed agent re-reads its whole context, so a 500k-token
agent costs far more per step than a fresh one that re-reads a few files.

Codex is a REVIEWER, not a peer or co-executor, and it runs only for:
- the first build of a new project;
- significant changes: a new feature or module, a database schema change
  or migration, anything touching money, payments, auth, permissions or
  data deletion, or a change to the architecture or across many files;
- an explicit request from the user.
Everything else — bug fixes, small edits, texts and labels, UI tweaks,
config values, docs, a change that follows an existing recipe in a few
files — gets no Codex review: your own check of the diff plus the tests is
enough. When in doubt, it is not significant.
When the review does run, it runs exactly ONCE: a single final review
after the whole implementation is done and your tests pass — not after
each subtask, and not again after you fix its findings. Use `/codex:review` (or `/codex:adversarial-review` for anything
security- or correctness-critical). If those commands are not available to
you as tools (plugin slash commands are often user-only), use the
`codex:codex-rescue` subagent with a review-only brief: read-only, do not
edit or create files, review the full diff of this task, return findings
with severity. Resolve every finding it raises, or
state explicitly why you are not. Verify your fixes with tests (run by
`ojc-quick-helper`), never by re-running Codex. A second Codex run
happens only if the user explicitly asks for it. Never delegate primary
implementation work to Codex.

When a task does get a review, do not report it as done and do not give
the user push or deploy commands until the review has returned and its
findings are resolved. In the final report, say in one line whether the
task got a Codex review and why (significant change / small change).

Writing the review brief:
- The scope is the whole diff of the task. You may list areas to look at
  closely, but as extras — never narrow the review to them.
- Only read-only commands inside the review (reading files, `git diff`,
  `git log`). No test runs, builds or long commands: you already ran the
  tests, and a test run inside the review is the usual reason it hangs.
- If it returns nothing in ~10 minutes, read the raw task output instead of
  waiting. Stop it and restart once with a narrower brief. A hung or
  failed run does not count as the one review, but do not proceed without
  a completed one.
- If the Codex subagent cannot be started (permission check, classifier
  block, plugin error), retry once — a block can be one-off and a retry
  bypasses nothing. If it is blocked again, ask the user to run
  `/codex:review` in this session and read its result yourself. "Wait for
  the review" from the user means you get the review done, not that the
  user will do it.
- If Codex itself fails for a reason outside the code — the account,
  plan, model, quota or an outage (e.g. HTTP 400 "model is not supported
  when using Codex with a ChatGPT account", 401, 429) — do not block the
  task and do not ask the user to run `/codex:review`: it uses the same
  account and fails the same way. Run the same review brief once with a
  fresh read-only subagent instead (the `/code-review` skill if available,
  otherwise `ojc-boilerplate-executor` told to review only and edit
  nothing), resolve its findings, and continue as if the review had run.
  In the final report say in one line that Codex was unavailable, quote
  the error, and that a fallback review was used — so the user can fix
  the Codex setup separately.

Keep your own context lean: read subagent summaries, not their raw
transcripts or tool-call streams.

Multi-line code or text edits go through the Write/Edit tools, never
through a Python or sed script inside a bash heredoc: the shell rewrites
`\n`, `\r` and quotes inside it and silently breaks the file.

Commands you hand the user to run (deploy, SQL, server setup) must be
complete and in executable order: no placeholders like `<username>` —
look the value up or ask for it — and each block ends with a check that
it worked (e.g. `git log --oneline -1`, a health request, the expected
log line).

Servers and secrets:
- Connect to a server only with key-based access the user has already set
  up. Never take a password from the chat and never automate entering one
  (SSH_ASKPASS, paramiko, expect, a temp file with the password). If key
  access fails, stop and give the user the commands to run instead.
- Before any server work, check the target is current: resolve the domain
  and compare with the deploy doc. Hosts, IPs and commands in memory or
  `~/.ssh/config` go stale after a migration.
- Reading production (logs, read-only SQL) is still touching production:
  it goes through jev-gate like any other server action.

## Project rules

<!-- Rules specific to this project. The orchestrator adds a one-line rule
here when the Codex review finds a kind of problem for the second time. -->

- Цель развёртывания: VPS Ubuntu + Docker Compose (сервисы `bot` и `admin`, общий volume `/data`), Telegram long polling, без webhook. Разработка: Windows + PowerShell, venv на Python 3.12 (`py -3.12`), как в образе `python:3.12-slim`.
- Управление базой знаний — только вручную через админку (FastAPI). Секреты в `.env`, некритичные константы в `config.ini`. Никакого автоматического чтения сайтов и обновления правил.
- Production-бот @bginfosu_bot не останавливать и его webhook не менять. Переключение — только с разрешения владельца, по `docs/SWITCHOVER.md`.
- JEV: `POST https://openrouter.ai/api/v1/systemone`, модель `~typesafe/jev-latest`. Типы вопросов `noul` (criteria `{"true","false"}` обязательны), `choice`, `score`; стоимость в `usage.cost`. Для «да / явно нет / не указано / противоречие» использовать `choice`. Проверено живым вызовом 2026-10-09.
- Юридически значимое (статусы, условия, суммы, ссылки) — только из базы и кода. LLM пишет лишь персональное «почему», проверенное `ai/explain.check`; при сомнении — шаблон.
- Суммы в текстах базы — только `{param:ID}`, без цифр руками.
- Начальное наполнение — черновики с `verified=false`. Фиктивных одобрений от имени владельца не ставить. Демо — только отдельная `data/demo.db` (`bundle.demo=true`); production отказывается работать на демо-выпуске.
- Lex.bg и Ciela — консолидированные тексты, не официальное опубликование. Существенные изменения сверять с ДВ. Законопроекты в действующие правила не включать.
- Любой текст в Telegram HTML — через `present.h()`; разметку добавляет только код.
- Шаг диалога сохраняется одной транзакцией вместе с отметкой update_id. Кнопки несут `session_id:revision`, устаревшие отклоняются.
