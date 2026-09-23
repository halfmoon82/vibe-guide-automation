# PRD：审计完整性与代码库卫生（audit integrity & codebase hygiene）——V4.7

状态：草稿（2026-09-23 起草，来源：V4.6 run log 五条遗留项，用户确认全部纳入同一迭代）
上游证据：[V4.6 run log](../plans/2026-09-23-v46-dispatch-run-log.md) §登记后续第 1–5 条；reviewer 子代理 Feynman/Russell/Dirac P3 意见
修订对象：`vibe_guide/dag.py`、`vibe_guide/monitor.py`、`vibe_guide/lifecycle.py`、`vibe_guide/binding_lifecycle.py`、`docs/superpowers/specs/2026-08-24-vibe-coding-development-guide-design.md`、代码库内 `in_session_sdd` 命名散落点

## 背景与问题

V4.6 完成后，五条 P3 遗留项被登记为"不阻塞当前发布，但实质影响系统可靠性"。把它们放在同一迭代的原因：五条都属于"机制存在但有空洞"，放着不修会让审计记录在关键时刻无法机器验证，或让依赖检查在边缘输入上静默放行。

**第 1 条：跨组/无组写范围冲突的安全网形同虚设。**
`path_ownership.validate_path_ownership` 函数存在，但 monitor.py 派发路径没有调用它。两个节点的白名单相交时，dag.py 的 parallel_group 审计器会拒绝它们入同一组——但如果两个节点压根没被标为同组（分属不同 parallel_group 或无组），这个相交就不会被任何机制发现。直到两个 worker 同时写了同一个文件，才在 git merge 时暴露。

**第 2 条：会话内 review 的证据链没有"防篡改"锚点。**
accepted 事件里的 `evidence_ref` 只是一个字符串，没有和"当时审的是哪个版本的 contract"绑定；协议常量也没有被比对——等于审计记录可以指向任意内容而系统不报错。这和 V4.6 强调的"可审计可验证"目标矛盾。

**第 3 条：dag.py 有两个已知边缘漏检。**
一是当节点 owned_paths 包含根目录 `.` 时，相交判断没有专项测试，行为未验证；二是产物路径引用检测不处理 CJK 字符粘连的情况（如 `输出结果.json` 中文文件名没有空格分隔符），引用检测器会漏掉。

**第 4 条：同一概念有两个名字，文档和代码对不上。**
设计基线文档 §7.3 还在用旧词"任务对"；代码里 `in_session_sdd`（manifest 探针字段名）和 `visible-sdd`（topology 枚举值）指向同一概念但在不同层使用了不同名字，注释和文档里两个词混用，新 worker 读到时会理解错字段名。

**第 5 条：状态快照字段清单缺 topology。**
`lifecycle.py` 的 `_CANONICAL_FIELDS` 没有加 `topology`。目前影响轻微，但如果后续工具依赖快照做完整性校验，这里会产生误报，或者让真正的字段缺失被遮掩。

## 设计意图

本迭代无新功能，只补齐 V4.6 机制的五个空洞：

1. **path_ownership 接线**：派发前对所有 ready 节点的白名单两两比对，发现相交即拒绝派发并给出冲突节点列表。这是已有函数补接已有派发路径，不新增逻辑层。
2. **evidence_ref 绑 contract digest**：accepted 事件写入时，`evidence_ref` 必须携带 contract 内容的 SHA（已有 contract 对象，直接 hash），校验器读取时比对；protocol 常量比对一并加入 accepted 事件校验。
3. **dag.py 边缘漏检修补**：根 `.` 相交加专项单测并修复行为；产物路径引用检测加 CJK 字符粘连处理。
4. **命名统一**：代码与文档统一使用 `visible-sdd`（topology 枚举值），manifest 探针字段 `in_session_sdd` 保留（它描述的是平台能力，不是 topology 枚举，语义不同，保留是正确的）；设计基线 §7.3 "任务对"改写为"worker 会话"。
5. **_CANONICAL_FIELDS 补齐**：`lifecycle.py` 加入 `topology`，补单测。

## 目标

1. 任意两个 ready 节点白名单相交时，派发被机器拒绝，监工收到明确的冲突报告而不是等到 merge 时才发现。
2. accepted 事件的证据链可机器验证：`evidence_ref` 指向的 contract 版本可追溯，protocol 遵从可断言。
3. dag.py 在根目录相交和 CJK 路径两种边缘输入下行为正确且有单测覆盖。
4. 代码库中 `visible-sdd` 含义唯一；设计基线文档与实现用语一致。
5. `_CANONICAL_FIELDS` 完整，lifecycle 快照字段不遗漏。

## 用户故事与验收

| AC | 内容 | 验证 |
|---|---|---|
| AC-01 | 监工派发 ready 集前，对所有节点的 owned_paths/白名单两两比对；任意一对相交则拒绝派发，返回包含冲突节点名和冲突路径的错误，并写入 dag-audit；无组/跨组情况同等检查 | 单测：两节点白名单相交 → 派发被拒 + audit 有记录；真正不相交 → 放行；parallel_group 内/跨组/无组三种情况均覆盖 |
| AC-02 | accepted 事件写入时，`evidence_ref` 必须携带 contract 内容 digest（SHA-256 前 16 字节即可）；校验器读取 accepted 事件时比对 digest；accepted 事件缺 digest 或 digest 不匹配时校验失败 | 单测：digest 正确 → pass；digest 缺失 → fail；digest 不匹配 → fail；protocol 常量比对加入现有 visible_sdd_contract 测试套件 |
| AC-03 | dag.py 相交判断在 owned_paths 含 `.` 时行为正确（`.` 与任意路径相交）并有专项单测；产物引用检测在路径含 CJK 字符且无空格分隔时能正确提取引用 | 单测：`.` 节点与任意节点 → 相交；CJK 路径名 → 引用检出；回归：现有单测全绿 |
| AC-04 | 代码库内 `in_session_sdd`（manifest 探针字段）与 `visible-sdd`（topology 枚举）的使用场景分离、无混用；设计基线 §7.3 "任务对"全部替换为"worker 会话"；README 同步 | grep 验证：topology 枚举值使用点无 `in_session_sdd`；文档关键词检查 |
| AC-05 | `lifecycle.py` 的 `_CANONICAL_FIELDS` 包含 `topology`；补单测；全量回归绿 | 单测：带/不带 topology 字段的快照 roundtrip；`python3 -m unittest discover -s tests -v` 全绿 |

## 非目标

- 不新增拓扑类型或平台适配器。
- 不修改 DAG 依赖语义（depends_on / integration_after 定义不变）。
- 不改动 authorization.py 的授权卡 schema（V4.6 已定稿）。
- 不追加降级披露规则（V4.6 已完整）。
- `in_session_sdd` manifest 探针字段名不改（语义正确，改会破坏 7 个 manifest 和对应测试）。

## 关键产品语义

- **AC-01 是本迭代最高优先级**：写范围冲突目前完全靠监工人工发现，这是 V4.6 真实发生过的风险（I06/I04 交叉点靠 worker 自律）。机器门禁补上后，这类冲突在派发前即暴露。
- **AC-02 是可审计性的底线**：没有 contract digest 锚点的 evidence_ref，理论上可以指向任何内容。V4.6 强调的"证据链可验证"在这个环节是空的。
- **AC-03/AC-04/AC-05 是卫生项**：独立、低风险，可并行开发。
- 五条 Issue 的文件白名单两两不相交，首批可全部并行派发。
