# Vibe Coding Development Guide

[中文 README](README.md)

Vibe Guide is a local CLI for scanning a project, planning work, and running a recoverable development, independent review, rework, and acceptance flow under one explicit authorization. Technical delivery, review acceptance, push, merge, and deploy remain separate states; deploy is never included by default.

## Installation

The current release is **4.4.0**. Treat the matching `v4.4.0` tag, commit, and Release assets as the version source of truth; `dist/` is only a local build cache, not a download entry point. Release validation covers the wheel, source distribution, and source installation separately. Local builds only produce verification artifacts; they do not create tags or releases, push, merge, or deploy.

### Installation protocol for Agents

When a user asks to install or upgrade, the Agent must:

1. Query the newest tag instead of trusting README text, `dist/` filenames, or memory:
   `git ls-remote --tags --sort='-v:refname' https://github.com/halfmoon82/vibe-guide-automation.git 'refs/tags/v*'`
2. Select the newest stable tag (currently `v4.4.0`) and verify that the tag, commit, and downloadable Release assets agree. If that cannot be verified, keep the result `unknown`.
3. Install that version and verify both:
   `python3 -c "import vibe_guide; print(vibe_guide.__version__)"`
   `vibe --help`
4. In the user-selected project, run the read-only `vibe scan --json` first. Run `vibe init --confirm --json` only after the user explicitly confirms initialization.
5. Report the initialization result and `.vibe/session-contract.json`. Package installation does not initialize a project or authorize monitor, push, merge, or deploy.

An Agent must not assume that downloading or installing the package automatically starts the wizard or writes to a project. Initialization is a separate, visible, traceable step.

Install the published wheel:

```bash
python3 -m pip install --user --upgrade vibe_guide-4.4.0-py3-none-any.whl
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
```

Every command supports `--json`. Exit codes are `0` for success, `2` for argument errors, `3` for confirmation or design blocking, and `4` for unknown external or runtime state.

## V4.2 engine evidence

Complex-plan publication records a real provider engine attestation containing the plan, revision, provider, content digest, and freshness checks. Monitor start and resume validate that evidence before dispatch. Missing, altered, mismatched, expired, future-dated, or symlinked plan/evidence files fail closed.

## Governance boundaries

- `scan` is read-only.
- Existing `AGENTS.md` files are never overwritten; missing rules produce a proposal only.
- Provider identity, login, task visibility, permissions, push, merge, and deploy are verified independently.
- Tests, `PASS`, or `CANMERGE` markers are not approval or release truth.
- Secrets, tokens, passwords, and private business data must not be stored in `.vibe/`, logs, plans, or chat.

See the Chinese README for the full workflow and governance contract.
