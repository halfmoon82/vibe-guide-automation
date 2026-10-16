# Vibe Coding Development Guide

[中文 README](README.md)

Vibe Guide is a local CLI for scanning a project, planning work, and running a recoverable development, independent review, rework, and acceptance flow under one explicit authorization. Technical delivery, review acceptance, push, merge, and deploy remain separate states; deploy is never included by default.

## Installation

The current release is **5.0.15**. Treat the matching `v5.0.15` tag, commit, and Release assets as the version source of truth; `dist/` is only a local build cache, not a download entry point. Release validation covers the wheel, source distribution, and source installation separately. Local builds only produce verification artifacts; they do not create tags or releases, push, merge, or deploy.

### Installation and initialization prerequisites

- **Runtime**: Python >= 3.9, zero third-party dependencies (standard library only); install with pip or pipx from wheel, sdist, or source (all three are pinned by tests to ship the protocol files).
- **Post-install verification**: `python3 -c "import vibe_guide; print(vibe_guide.__version__)"` and `vibe --help`.
- **Initialization order**: in a project, run read-only `vibe scan --json` first; run `vibe init --confirm` only when initialization is confirmed; AGENTS.md rule blocks are merged by `vibe apply-agentsmd --confirm` after human review.
- **No host-framework requirement**: the entry protocol is fully self-contained. When the host runs its own mandatory gate/status-line protocol (e.g. an alignment card), the S1 score merges into its first mandatory status line as an `S1：` field -- no second scoring pass and no second gate; otherwise the agent emits one standalone S1 line.
- **Upgrading existing projects**: materialized files (`.vibe/proposals/skills/*/SKILL.md` and already-merged AGENTS.md blocks) are never rewritten; a newer init prints a drift note when they differ from the shipped content, and a human merges manually after a manual diff.

### Installation protocol for Agents

When a user asks to install or upgrade, the Agent must:

1. Query the newest tag instead of trusting README text, `dist/` filenames, or memory:
   `git ls-remote --tags --sort='-v:refname' https://github.com/halfmoon82/vibe-guide-automation.git 'refs/tags/v*'`
2. Select the newest stable tag (currently `v5.0.15`) and verify that the tag, commit, and downloadable Release assets agree. If that cannot be verified, keep the result `unknown`.
3. Install that version and verify both:
   `python3 -c "import vibe_guide; print(vibe_guide.__version__)"`
   `vibe --help`
4. In the user-selected project, run the read-only `vibe scan --json` first. Run `vibe init --confirm --json` only after the user explicitly confirms initialization.
5. Report the initialization result and `.vibe/session-contract.json`. Package installation does not initialize a project or authorize monitor, push, merge, or deploy.

An Agent must not assume that downloading or installing the package automatically starts the wizard or writes to a project. Initialization is a separate, visible, traceable step.

Install the published wheel:

```bash
python3 -m pip install --user --upgrade vibe_guide-5.0.15-py3-none-any.whl
vibe --help
```

For an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/vibe --help
```

The package supports Python 3.9 and later paths covered by the repository's release contract. Installation does not change the system Python.

## Commands

```text
vibe scan                         Read-only project scan; does not create .vibe/
vibe init --confirm               Initialize the minimum project state after confirmation
vibe doctor                       Report observable environment, Skill, and Agent facts
vibe plan --request <request>     Run S0 and, when needed, explicit S1 and node-spec checks
vibe monitor --plan <ID>         Start only with exact authorization and valid evidence
vibe status --plan <ID>          Read the current snapshot without polling providers
vibe resume --plan <ID>          Continue from snapshots, task records, and event evidence
vibe skill-install --source <GitHub> --sha <40hex> --name <name> [--subdir <dir>] --confirm   Fetch a GitHub skill at an exact SHA, validate an optional repo subdir, materialize into .vibe/proposals/skills/<name>/ and register it in .vibe/config.json; traversal, absolute, or edge-slash subdirs are rejected without writes
```

Every command supports `--json`. Exit codes are `0` for success, `2` for argument errors, `3` for confirmation or design blocking, and `4` for unknown external or runtime state.

New-session entry: the host agent self-screens S0/S1 in-session following `.vibe/proposals/skills/vibe-entry/SKILL.md` (materialized by `vibe init`, never rewritten). Requests scored <=8 are executed directly and 9-15 get a light plan, neither touching vibe; every scored request emits one S1 score-and-band line; only requests scored >15 (or uncertain) enter `vibe scan` + `vibe plan --request --s1`. The protocol is fully self-contained and needs no external skill.

## V4.2 engine evidence

Complex-plan publication records a real provider engine attestation containing the plan, revision, provider, content digest, and freshness checks. Monitor start and resume validate that evidence before dispatch. Missing, altered, mismatched, expired, future-dated, or symlinked plan/evidence files fail closed.

## V4.6 dispatch topology

Since V4.6, true DAG parallelism is carried by one visible worker session per node (Codex: `create_thread`, user-owned); the supervisor only dispatches, waits, and closes out, and is never the writer of any node. The task registry records each node's dispatch topology in its `topology` field:

- `visible-sdd`: one visible session per node running in-session SDD — a dev subagent implements while an independent-context, read-only review subagent audits (protocol: `vibe_guide/protocols/visible-sdd-worker.md`); rework and re-review close the loop inside the same session identity; dispatch inlines the full shipped protocol text into the create prompt (`VISIBLE_SDD_PROTOCOL_REF` stays a verbatim version pointer), and a missing packaged protocol fails closed before any session is created;
- `dual-visible`: the conservative default, with two distinct visible tasks for developer and reviewer; UNKNOWN platform evidence fails closed to this topology and never upgrades to `visible-sdd`;
- `background`: the explicit downgrade when a platform has no visible bridge. The downgrade and its limitations (not visible, not directly enterable, limited rework continuation) must be disclosed in the capability report, the authorization card, and the delivery; a `mode=background` worker without disclosure fails authorization-card validation.

Platform topology is ruled by the adapter registry's `DISPATCH_TOPOLOGY_MATRIX` from each platform's `in_session_sdd` probe evidence. `in_session_sdd` and `visible-sdd` live on different layers and are not interchangeable: the former is an adapter-layer name — both the manifest capability-probe field name and the ruling value in `DISPATCH_TOPOLOGY_MATRIX`; the latter is an enum value of the `topology` field (dispatch layer), translated from that ruling by the supervisor and describing how the node is actually dispatched; a passing probe does not by itself mean the node's topology is `visible-sdd` (the matrix ruling decides).

Concurrency cap: `max_active_worker_sessions` in `.vibe/config.json` caps simultaneously active worker sessions (default 5, valid range 1–64) and takes effect as the minimum with the authorization-card snapshot; an explicit but invalid value is a configuration error, never silently replaced. Accepted and archived sessions release capacity for later ready nodes.

Single-shift supervisor: with `"supervisor_single_shift": true` in `.vibe/config.json`, one supervisor session serves the whole run; the preflight no longer rotates on context growth and only outputs `rotate` at the 200k absolute cap (or a temporary `--context-hard-cap`). Default false keeps growth-based rotation. A non-boolean value makes the preflight report `unknown`; an unreadable config file falls back to the default with a `config_warning`.

Acceptance-based scope expansion: `acceptance_scope_rule_approved` in `.vibe/config.json` defaults to false, keeping supervisor auto-scope to `tests/` and `auto_scope_paths`. Set it to true only after the product manager approves and writes the matching `AGENTS.md` §6 patch; then low-risk files whose change keeps every PRD acceptance example unchanged may also be added.

Decision notification bridge: the project `.vibe/config.json` may only contain `"notify_bridge": {"enabled": true}`; the command and timeout are read only from `notify_bridge` in the user-level `$VIBE_HOME/config.json`. A project-level `command` is refused and recorded as UNKNOWN.

### Claude Code background dispatch (downgraded background)

Once configured, one authorization on Claude Code lets the supervisor dispatch workers unattended: it starts background sessions with `claude --bg` (provider `claude-code-background`). This is a downgraded background mode with three limits: the desktop sidebar does not show these sessions; you can only enter them from a real terminal with `claude attach <short-id>`; rework resumes the same session. Without configuration nothing changes and you still click each card.

1. Prepare a Claude Code settings file containing only `apiKeyHelper` (keep it outside the repo; never put a key in any vibe file). Background sessions ignore keys in the `env` block of `~/.claude/settings.json`, hence this file:

```json
{"apiKeyHelper": "<absolute path of the script that prints your key>"}
```

2. Register it in the **user-level** `$VIBE_HOME/config.json` (default `~/.vibe-guide/config.json`):

```json
{"claude_bg": {"settings_path": "<absolute path of the settings file>", "model": "sonnet"}}
```

vibe records the path only; it never reads or stores the key. `model` defaults to sonnet, and the supervisor still passes `--model` explicitly on every dispatch (it may vary per node). In the project `.vibe/config.json`, `claude_bg` may only contain `enabled`; credentials, command or model there are refused (repos get cloned by others).

3. When the Claude Code session registers its capability facts with `vibe attest`, state `claude-code.background_task.launch` as `true` (evidence: `claude --bg` was observed to start on this machine). A successful registration reports `level background`; if step 2 is not configured, the same line says "background launcher unavailable: <reason>" (in Chinese), and the authorization card lists it too. `vibe scan` does not report the dispatch mode; do not use it for this.

Viewing background workers: in a terminal run `claude agents --json --all` to list every background session with its `state` (working / done / failed / blocked / stopped), and `claude attach <short-id>` to enter one (a real terminal is required; without a TTY, `claude agents` without `--json` errors out). Closing the terminal tab does not end the session. Do not resume a running background session with foreground `claude --resume` (it is refused, to prevent double writers).

Permissions and commit identity: workers run with `bypassPermissions`; the safety boundary is worktree isolation plus the authorization card. If the machine has no global git `user.name` / `user.email`, commit authors will be the machine name. `git_user_name` / `git_user_email` in the user-level `claude_bg` set an identity for the worker's worktree only, and require `git config extensions.worktreeConfig true` in the repo; otherwise the launch fails with the reason (the main repo's identity is never changed).

Accepted on real sessions (2026-10-14, Claude Code 2.1.260): background dispatch → delivery → review finds a defect → rework continues the same session → re-review passes, with no clicks, in about 30 seconds. Re-run locally with `VIBE_LIVE_CLAUDE_BG=1 python3 -m unittest tests.test_claude_bg_live -v` (skipped by default; it starts real background sessions and spends usage). Workers must run in a linked worktree (`.worktrees/<node>`): in the main checkout, newer Claude Code background sessions open their own worktree and write there, so deliveries do not land where expected.

Unverified, do not treat as available: pre-assigning a session id with `--session-id`, combining `-w` with `--bg`, recovery after a long task / sleep / daemon exit, and `--bg` under the desktop-bundled CLI.

## Governance boundaries

- `scan` is read-only.
- Existing `AGENTS.md` files are never overwritten; missing rules produce a proposal only (four rule blocks: capability truth, complex-request entry, `New Session Entry`, and `Engineering Principles`), merged by `vibe apply-agentsmd --confirm` after human review.
- Provider identity, login, task visibility, permissions, push, merge, and deploy are verified independently.
- Tests, `PASS`, or `CANMERGE` markers are not approval or release truth.
- Secrets, tokens, passwords, and private business data must not be stored in `.vibe/`, logs, plans, or chat.

See the Chinese README for the full workflow and governance contract.
