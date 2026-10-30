# AI 使用说明

## 分工

> **设计由我拍板**：数据模型、主流程、边界与验收标准。
> **AI 负责按这些约束把代码、测试、文档落地**；我审阅、挑错、要求返工，并在真实环境（DeepSeek）里验收。

我交付的标准是「可运行 + 可复核」，所以下面每一处设计都指向能直接打开核对的证据：
`题目.md`（我的设计稿）、`sql/schema.sql`、`tests/`、`examples/`、`docs/images/`。
代码由 AI 生成这件事我不隐瞒——评审要看的是判断与迭代，不是谁敲的键盘。

## 一、数据层：我定的模型与约束

| 设计点 | 我的决定 | 为什么 | 证据 |
|---|---|---|---|
| 存储架构 | **双轨**：`sql/schema.sql` 保留 MySQL 设计，运行时用 JSON 单文件库 | 交付环境不能假设有数据库服务，但设计要能被评审；换库存只改一个文件 | `sql/schema.sql`、`src/store.py` |
| 表与字段 | 两张表：排期（`id/date/material_id/status/confirm_time/…`）与审核（`id/material_id/status/metadata/release_date/…`），`release_date` 为 NULL 即未发布 | 排期与审核生命周期不同，混在一张表里会互相污染 | `题目.md` §1、`sql/schema.sql` |
| 唯一键 | 同素材同时段只允许一条（`uk_material_date`）+ 请求幂等键（`uk_idem`） | 「重复提交只产生一份草稿」不能靠提示词记性 | `src/store.py` `TableSpec`、`test_duplicate_submission_creates_one_draft` |
| 占用口径 | `DRAFT` 也算占用（软锁），一个时段容量 1 | 草稿是"占位"，不是"没发生"；否则两个素材会排到同一档 | `ACTIVE_SCHEDULE_STATUS`、`slot_query` |
| 一致性边界（我补充的） | 一个素材只允许一条有效排期；同一文件换 ID 重传按内容指纹判重 | `uk_material_date` 挡不住"换时段再排"，ID 去重挡不住"同一文件重传" | `ALREADY_SCHEDULED`、R009、`content_fingerprint` |
| 状态上限 | `published` 恒 `false`，`status` 最高 `CONFIRMED`，`release_date` 永不写 | 结构性保证"只存草稿"，而不是靠代码自觉 | `docs/03` 硬断言、`test_repeat_runs_do_not_publish_anything` |

## 二、主流程：我定的骨架

| 环节 | 我的设计 | 为什么 | 落地 |
|---|---|---|---|
| 意图解析 | 先把自然语言收敛成"素材 + 时间 + 硬/软约束"，再进循环 | 时间要求分硬软，是后面所有冲突处置的前提 | `src/llm.py`、`MockLLM` |
| 决策循环 | **预设骨架 + ReAct**：`prepare → agent ⇄ act → finalize`，模型每次选一个动作 | 固定条件分支不算 ReAct；骨架只是收口，不替模型决策 | `src/graph.py`、`docs/06 §2` |
| 补查 | 审核缺字段时重读一次，仍缺才阻断 | 上游元数据同步有延迟，不补查会把可排的素材误杀 | `recheck` 链路、`test_metadata_lag_recovers_after_recheck` |
| 冲突分流 | 软性→改档并标待确认；硬性→**不改期**，交回选项 | 「必须 18:00」被自动改期是最得罪用户的行为 | 结构化决策表、`test_hard_conflict_never_moves_slot` |
| 限步 | 上限 10 次工具调用，超限返回 `step_limit_reached`（`finished=false`） | 有循环就必须有兜底，宁可明确未完成也不能死循环 | `test_step_limit_returns_unfinished_state` |
| 提醒 | 旁路：失败降级为 `notify.degraded`，不影响排期 | 通知挂了不该让排期失败 | `notify_push`、`test_notify_failure_degrades_without_blocking` |

## 三、边界与验收：我划的线

- **只存草稿**：不接真实发布，`CONFIRMED` 也只是"确认过的草稿"；
- **不谎报**：保存失败必须显式失败，回复里不得出现「已排期/已完成」；
- **失败可区分**：`DB_UNAVAILABLE` 可重试一次、`DB_WRITE_REJECTED` 不重试；
- **模型可替换**：`MockPolicy`（离线、规则）与 `LLMPolicy`（真实模型）共用同一个循环，mock 只证明流程、不证明模型质量；
- **配置单一来源**：只从 `.env` 读，代码统一 `os.getenv`；
- **文档纪律**：没实现的不写；写了就要有测试或示例能对上。

验收标准就是题目那六类 + 我补的两条一致性守卫，每一条都有对应示例与测试
（对照表见 [需求文档](01-requirements.md)、实际结果见 [评估](03-evaluation.md)）。

## 四、我发现的缺陷（我推动修的）

| 我指出的问题 | 后果 | 修法 |
|---|---|---|
| 初版是固定七节点流程图 | 不满足「模型选动作 → 执行 → 观察 → 调整」 | 重构成 `agent ⇄ act` 循环，加限步与决策日志 |
| 唯一键只有 `(material_id, date)` | 同一素材换个时段还能再排一条，占两个档 | 保存前查该素材其它有效排期，`ALREADY_SCHEDULED` 并带出原草稿 |
| 去重全靠 `material_id` | 同一份文件换 ID 就是"新素材" | 内容指纹判重（R009），建素材时 409 |
| 配置只读 `os.environ` | 我在 `.env` 配好 key，程序还说没有 | `.env` 加载器 + 全项目 `os.getenv`，`.env` 覆盖同名变量 |
| 没有素材维护 | 只能改种子文件，不像应用 | 补素材增删改查接口 + 前端表单 |
| 只挡了同素材同时段 | 同一素材换时段可重复排期 | 见上，`ALREADY_SCHEDULED` 守卫 |

## 五、我怎么验收（不是"看它说通过了"）

| 方式 | 动作 | 结果 |
|---|---|---|
| 单测 | `python -m unittest discover -s tests -v` | 145 个用例全通过 |
| 示例回归 | 11 条 CLI 示例逐条比对 `status` | 11/11 符合预期 |
| 真实模型 | 用自己的 DeepSeek key 跑 `--provider deepseek` | 3 条路径结论与 mock 一致（token 见 [run-records](../examples/run-records.md)） |
| 从零安装 | 空目录 `git clone` → venv → `pip install` → CLI → 起服务 | 全通（这步抓到 `requirements.txt` 编码问题） |
| 界面 | headless 浏览器逐页截图 | 抓到输入框样式与「弹窗常驻」两个问题 |

## 六、AI 做了什么，我怎么验

| 模块 | AI 产出 | 我的验证 |
|---|---|---|
| 存储层 | JSON 库（表/唯一键/事务/文件锁） | `tests/test_store.py`：唯一键、NULL 语义、回滚不落盘、8 线程并发 |
| 审核规则 | R001–R009 骨架 | `tests/test_rules.py` 逐条断言结论与规则码 |
| 编排与策略 | LangGraph 循环、动作注册表、`MockPolicy` / `LLMPolicy` | `tests/test_workflow.py` 断言决策序列、限步、假模型循环 |
| 模型客户端 | DeepSeek 报文、重试矩阵、错误码翻译 | `tests/test_clients.py` 假 transport 覆盖 429/5xx/400/401/402/超时 |
| Web 层 | 标准库 HTTP + 单页界面 | `tests/test_server.py` 31 例 + 截图实看 |
| 文档 | 各篇初稿 | 与代码逐条核对，删掉未实现的能力 |

## 七、我纠正过的 AI 做法

| AI 的做法 | 我的决定 |
|---|---|
| 把审核结论交给模型输出 | 改纯函数规则——结论必须可复现、可回归 |
| 计划写"支持批量排期" | 删掉，没实现就不写 |
| 文档里写"已接入真实模型" | 先标"未联调"，真跑通再改回来 |
| 工具名用 `material.get` | 被 DeepSeek 400 拒绝，改下划线并复跑联调 |
| 模型不落草稿就 `finish(needs_confirmation)` | prompt 加约束 + `finalize` 降级为 `blocked` |
| `ToolContext` 每轮重建，"补查"读到同一份数据 | 整轮共用一个上下文 |
| 用 PowerShell 批量改文档，混入 BOM/CRLF | 改脚本 + 全仓编码检查 |
| 直接 `git add -A`，把我改到一半的 README 也提交了 | 要求先看 `git diff`，并补回被截断的表格行 |
| `.modal { display: grid }` 盖掉 `[hidden]` | 补 `.modal[hidden]`，截图复验 |

## 边界

- 真实模型只跑了 `deepseek-flash` 的 3 条路径（各 1 次），样本不足以评价稳定性；
- 没有真人问答与用户测试，产品判断来自题目描述与常识；
- 实际投入与未完成部分见 [复盘](05-retrospective.md)；
- 代码量与改动次数说明不了能力，**能复核的是**：`tests/` 的断言、`examples/` 的真实输出、
  `docs/images/` 的截图、真实 API 的调用记录——这些都不依赖我口头描述。
