# Vibe Coding Development Guide

[中文 README](README.md)

Vibe Guide is a local CLI for scanning a project, planning work, and running a recoverable development, independent review, rework, and acceptance flow under one explicit authorization. Technical delivery, review acceptance, push, merge, and deploy remain separate states; deploy is never included by default.

## Installation

The current release is **4.2.0**. Release validation covers the wheel, source distribution, and source installation separately. Local builds only produce verification artifacts; they do not create tags or releases, push, merge, or deploy.

Install the published wheel:

```bash
python3 -m pip install --user --upgrade vibe_guide-4.2.0-py3-none-any.whl
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
