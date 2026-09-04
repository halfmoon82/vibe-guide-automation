# Vibe Guide V4.0.0 Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 V4 Rev4 SDD-first 实现发布为 `4.0.0`，并让安装/升级向导显式登记 SDD 相关 Skill 依赖，同时保留 V3.10 升级兼容。

**Architecture:** 包元数据、运行时版本和迁移目标统一指向 `4.0.0`；V3.10 仅作为兼容来源版本与能力投影，不伪造已安装外部 Skill。安装/升级状态机在项目 `.vibe/config.json` 与安装状态中写入固定的 provider-managed SDD Skill 依赖声明，依赖是否真实可用仍由 provider/Skill 探针核验。

**Tech Stack:** Python 3.9+, setuptools, wheel, Python unittest, JSON 状态文件。

## Global Constraints

- 不执行 deploy、凭据获取、系统权限变更或真实 provider Skill 下载。
- 不把 provider-managed Skill 的登记声明解释为已安装或能力已验证。
- 保留 V2.0.0 与 V3.10.0 两条直接升级来源、兼容字段和回滚路径；V4 明确覆盖当前发行版本与迁移目标。
- 远端同步只 push 当前 `codex/v4-sdd-first-rev4` 分支到 `origin`，不创建或合并 PR/MR。
- 仅修改与版本、安装/升级向导、依赖配置、测试、README 和发布证据直接相关的文件。

### Task 1: V4 version and SDD dependency contract

**Files:**
- Modify: `setup.py`, `pyproject.toml`, `vibe_guide/__init__.py`, `vibe_guide/migration.py`, `vibe_guide/installation.py`, `vibe_guide/models.py`
- Test: `tests/test_v4_release_contract.py`

- [x] Write failing tests for `4.0.0` metadata/runtime/migration target and the required SDD Skill dependency declaration.
- [x] Run the focused tests and confirm they fail for the missing version/dependency behavior.
- [x] Add the minimal shared constants and JSON-safe dependency projection.
- [x] Run the focused tests and confirm they pass.

### Task 2: Install/upgrade wizard persistence

**Files:**
- Modify: `vibe_guide/installation.py`, `vibe_guide/cli.py`, `vibe_guide/initializer.py`
- Test: `tests/test_installation.py`, `tests/test_v4_release_contract.py`

- [x] Add a failing test proving install and upgrade persist the same SDD dependency declaration in `.vibe/config.json` and installation state.
- [x] Implement atomic persistence using the existing project-root boundaries; dependency records remain provider-managed and unverified.
- [x] Run install/upgrade focused tests and confirm both modes and V2/V3.10 upgrade sources remain compatible.

### Task 3: Packaging and documentation verification

**Files:**
- Modify: `README.md`, `tests/test_v4_packaging_compatibility.py`, `tests/test_v310_packaging.py`, `tests/test_v39_standalone.py`
- Create: `.superpowers/sdd/2026-09-04-v4-release-verification.md`

- [x] Update installation, upgrade, rollback and SDD dependency instructions for `4.0.0`.
- [x] Build wheel and sdist into a temporary output directory and verify clean wheel/sdist/source installs.
- [x] Verify V2.0.0 and V3.10.0 direct upgrade sources, rollback evidence, CLI help, compileall and diff-check.
- [x] Record frozen baseline failures separately from V4 release results.

### Task 4: Remote synchronization

**Files:**
- No source files; Git state only.

- [x] Run the full fresh verification suite and inspect the exact allowlist before staging.
- [x] Commit only the V4.0.0 release files and evidence; exclude generated `build/` and `vibe_guide.egg-info/` artifacts unless explicitly required by packaging evidence.
- [x] Push the current branch to `origin` and verify local/remote commit SHA equality.
- [x] Do not create or merge PR/MR.
