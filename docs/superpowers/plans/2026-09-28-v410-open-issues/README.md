# V4.10 开放 issue 集中修复 · PRD 交接包

本目录是 2026-09-28 在 Claude Code 会话中完成的 PRD 讨论与计划发布产物，交给另一台机器上的 Codex 执行开发。覆盖 issue：#80 #88 #91 #92 #93 #94。背景方案稿见 `../2026-09-28-pm-ai-shipping-review-integration.md`（#95）。

## 文件说明

| 文件 | 作用 |
|---|---|
| `product-spec.json` | **唯一真相来源**。产品 spec（7 条已由产品经理拍板的决策、PRD 各节、8 个节点合同、6 个目标）。在新机器上重新发布计划只用它。 |
| `prd.md` / `plan.md` / `planning-brief.md` | 由 vibe 渲染的可读版本，供人阅读。 |
| `specs/` / `issues/` | 每个节点的 Spec 与 Issue。 |
| `dag.yaml` / `nodes.json` / `plan.json` / `dag-audit.json` | 本次发布派生的 DAG 与审计结果，仅供对照。 |
| `authorization-card.json` | 本次发布的授权卡，**绑定 claude-code 适配器，不可在 Codex 上直接使用**，仅供对照范围。 |

未收录：`engine-attestation.json`、`plan-confirmation.json`（绑定本机会话能力，换机无效）。

## 在 Codex 机器上开工

1. 拉取本仓库最新 main，按需 `vibe init --confirm`。
2. 按 `vibe_guide/adapters/manifests/codex.yaml` 的 probes 登记 Codex 会话能力，**必须带 `--project-id`**（Codex 的 project id），否则发布会报 `project_id_unavailable`：

   ```bash
   vibe attest --adapter codex --facts <codex-facts.json> --provenance "<依据>" --project-id <codex project id>
   ```

3. 从本目录的产品 spec 重新发布（**必须带 `--s1`**，且 `--plan-id` 用新机器上尚不存在的编号）：

   ```bash
   vibe plan --request "从GitHub上获取所有开放issue（#80 #88 #91 #92 #93 #94），用vibeguide一次性解决他们；从讨论迭代PRD开始" --s1 5,4,4,3,3 --plan-id v410-open-issues --from-prd docs/superpowers/plans/2026-09-28-v410-open-issues/product-spec.json --json
   ```

4. 把新生成的授权卡念给产品经理，由其选择远端 Git 动作（提交/推送/开 PR/合并）允许或禁止，然后 `vibe authorize` → `vibe monitor`，按 prd-guide §6 服务信箱。

## 发布时踩到的两个坑（不在本轮 issue 范围）

- **草案编号不能复用**：prd-guide §5.3 写的是用草案的 `plan_id` 发布，实际草案目录已存在，会报 `plan already exists`；需换一个新编号（端到端测试也是这样做的）。
- **不带 `--s1` 会被降级**：`--from-prd` 发布时若不传 `--s1`，vibe 重新按文本评分，本请求会落到 `simple` 并忽略产品 spec。

## 节点一览

8 个节点全部在 `wave-1`，无硬依赖，可同时开工；联调关系：

- `review-methodology` 联调在 `inline-sdd-protocol`、`skill-subdir-install` 之后；
- `prd-guide-methodology` 联调在 `skill-subdir-install` 之后；
- `worker-push-delivery` 联调在 `supervisor-registry`、`inline-sdd-protocol`、`delivery-gate-rereport` 之后。

vibe 会自动追加最终的 `integration-review` 节点。

## 未验证

- Codex 侧的监工换班、心跳预检、worker 主动叫醒监工，均待在 Codex 机器上实证。
- `skill-subdir-install` 合并后，实际安装 pm-ai-shipping 的 3 个 skill（锁定 commit `8607e3b077817f89bf4a9b623246219734ac3be0`）是运行时步骤，尚未执行。
