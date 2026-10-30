# 素材审核与发布排期 Agent（LangGraph · ReAct · mock 模式）

小影校招笔试交付。按 [题目](题目.md) 实现：帮内容团队找出待处理素材，审出风险，形成无冲突、可确认的发布排期。
**只保存草稿，不接真实发布；决策循环是真实的，但模型默认是离线 Mock。**

- 决策循环：[LangGraph](https://langchain-ai.github.io/langgraph/) 的 `prepare → agent ⇄ act → finalize`，模型每轮读工具观测再决定下一步
- 存储：JSON 单文件数据库，**MySQL 设计保留在 [`sql/schema.sql`](sql/schema.sql)**，两者字段与约束一一对应
- 判定：审核规则、时段占用、幂等全部是确定性代码；模型只负责「选哪个动作、什么时候收手」
- 限步：`max_steps` 默认 10，超限返回 `step_limit_reached`（明确的未完成状态）

## 快速运行

Python 3.10+，只需 `langgraph` 一个第三方依赖。

```sh
pip install -r requirements.txt

python -m src.main --input examples/normal.input.json          # 正常排期 → completed
python -m src.main --input examples/conflict.input.json        # 18:00 被占，建议 19:00 → needs_confirmation
python -m src.main --input examples/hard-conflict.input.json   # 硬性要求不被改动 → blocked
python -m src.main --input examples/missing.input.json         # 缺封面 → needs_input
python -m src.main --input examples/risk.input.json            # 敏感词 → blocked
python -m src.main --input examples/duplicate.input.json       # 重复提交只产生一份草稿
python -m src.main --input examples/save-failure.input.json    # 保存失败明确报错
python -m src.main --input examples/tool-failure.input.json    # 工具失败
python -m src.main --input examples/metadata-lag.input.json    # 元数据延迟 → 补查后重审再排
python -m src.main --input examples/not-found.input.json       # 素材不存在

# 决策日志（每行一轮：动作 / 参数 / 观测摘要 / 后续 / 依据）
python -m src.main --input examples/conflict.input.json --log examples/run-record-soft-conflict.jsonl

# 限步演示：正常要 7 步，卡到 2 步会返回未完成
python -m src.main --input examples/metadata-lag.input.json --max-steps 2

python -m unittest discover -s tests -v                        # 68 个用例
```

成功输出 JSON；业务异常（缺字段、风险、冲突、保存失败）以 JSON 的 `status` 返回，退出码 0；
入参本身不合法（不是 JSON 对象、字段类型错）退出码 2 并输出 `{"status":"invalid_input"}`。

其它参数：

| 参数 | 作用 |
| --- | --- |
| `--db var/db.json` | 持久化到指定文件（默认用临时库，保证示例可复现） |
| `--now 2026-11-05T09:00:00+08:00` | 注入当前时间，便于复现示例 |
| `--compact` | 单行 JSON 输出 |
| `--max-steps N` | 工具调用次数上限（默认 10），超限返回 `step_limit_reached` |
| `--log PATH` | 把决策日志写成 JSONL |

## ReAct 决策循环

`src/graph.py` 只做四件事：解析意图（`prepare`）、让策略选动作（`agent`）、执行动作（`act`）、汇总（`finalize`）。
`agent ⇄ act` 之间没有写死的业务分支——**下一步是什么，取决于上一步工具返回了什么**。

```
agent: 还不知道素材是否存在        → material.get(M01)
act  : 素材存在                    → observation: 存在，最近审核 未审核
agent: 有了素材，该审核了          → audit.check(M01)
act  : PASSED                      → observation: 无风险
agent: 审核过了，查目标日          → slot.query(2026-11-05)
act  : 18:00 被 M00 占            → observation: 19:00、20:00 可用
agent: 用户没说死，先找替代        → slot.find_alternative(18:00)
act  : 19:00 / 20:00              → observation: 替代时段
agent: 顺延到 19:00 但要人确认     → schedule.save_draft(M01, 19:00)
act  : 草稿 #2                    → observation: 已保存 DRAFT
agent: 收手，把待确认项交出去      → finish(needs_confirmation)
```

两条对照路径（同一素材、同一冲突，只因用户语气不同而在第 5 步分叉）：

| 输入 | 第 4 步观测 | 第 5 步动作 | 结果 |
| --- | --- | --- | --- |
| `conflict`（希望 18:00） | 替代 19:00/20:00 | `schedule.save_draft(19:00)` | 草稿 #2，待确认 |
| `hard-conflict`（必须 18:00） | 替代 19:00/20:00 | `finish(blocked)` | 不落库，交回选项 |

完整记录（含参数、观测、后续动作、决策依据）见 [examples/run-records.md](examples/run-records.md)；
机器可读版是同目录的 `run-record-*.jsonl`。

**边界**：默认 `MockPolicy` 是规则决策，它证明循环成立、判定可靠，**不证明真实模型的决策质量**。
真实模型路径 `LLMPolicy`（OpenAI 兼容 tool-calling）已实现但未端到端联调，仓库里用一个脚本化假模型跑通了整圈（`test_llm_policy_drives_the_same_loop`）。

## 项目目录

```text
.
├── README.md                     # 安装、运行、评审导航
├── requirements.txt              # 依赖（langgraph）
├── .env.example                  # 不含真实密钥；默认 mock
├── .gitignore
├── 题目.md                        # 原始题目与设计思路
├── sql/
│   └── schema.sql                # MySQL 设计（保留稿，不参与运行）
├── data/
│   └── seed.json                 # 合成素材 + 已有排期（充当数据库初始数据）
├── docs/
│   ├── 01-requirements.md         # 用户、范围、验收映射
│   ├── 02-solution.md             # ReAct 流程、模块、取舍
│   ├── 03-evaluation.md           # 用例和实际测试记录
│   ├── 04-ai-usage.md             # AI 使用及验证
│   ├── 05-retrospective.md        # 用时、不足、未完成部分
│   └── 06-implementation.md       # 实现细节（循环/动作/数据/错误码/测试）
├── src/
│   ├── main.py                   # CLI：读请求 → 跑循环 → 输出 JSON / 决策日志
│   ├── graph.py                  # LangGraph：prepare → agent ⇄ act → finalize
│   ├── policy.py                 # 决策策略：MockPolicy（默认）/ LLMPolicy（tool-calling）
│   ├── registry.py               # 动作注册表：5 个工具 + JSON Schema + 观测摘要
│   ├── tools.py                  # 工具实现（ToolResult + 错误码）
│   ├── rules.py                  # 审核规则 R001–R008（纯函数）
│   ├── llm.py                    # 意图解析：MockLLM（默认）/ HttpLLM（可选）
│   ├── repository.py             # 数据访问层（换 MySQL 只改这里）
│   ├── store.py                  # JSON 单文件库：唯一键 + 事务 + 文件锁
│   ├── util.py                   # 时区、整点对齐、幂等键
│   └── errors.py
├── tests/
│   ├── support.py                # 每个用例一份临时库
│   ├── test_store.py             # 唯一键/回滚/并发/原子写
│   ├── test_rules.py             # 规则逐条
│   ├── test_llm.py               # 意图解析 + HttpLLM 报文
│   ├── test_workflow.py          # 六类验收 + ReAct 循环 + 限步
│   └── test_cli.py               # 退出码、跨进程幂等、--log/--max-steps
├── scripts/
│   ├── regen_examples.py         # 重新生成示例输出、决策记录与测试记录
│   └── stress_lock.py            # 文件锁并发压力脚本
└── examples/
    ├── *.input.json / *.output.json
    ├── run-record-*.jsonl        # 三条 ReAct 执行路径（机器可读）
    ├── run-records.md            # 同上，可读版
    └── test-results.txt          # 实际执行记录
```

## 怎么读输出

```jsonc
{
  "status": "needs_confirmation",        // 七种状态之一（含 step_limit_reached）
  "finished": true,                      // 流程是否走完
  "completed": true,                     // 业务目标是否达成（草稿已存）
  "review_status": "PASSED",             // 审核结论
  "suggested_slot": "2026-11-05T19:00:00+08:00",
  "pending_confirmations": ["2026-11-05 18:00 该时段已被素材 M00 占用，建议改为 2026-11-05 19:00，需你确认"],
  "draft": {"saved": true, "draft_id": 2, "status": "DRAFT", "deduped": false},
  "published": false,                    // 恒为 false：本期不接真实发布
  "steps": 6, "tool_calls": 5, "max_steps": 10,
  "decisions": [                         // 每轮模型决策，可逐条复核
    {"step": 4, "action": "slot.find_alternative", "args": {"slot": "2026-11-05T18:00:00+08:00"},
     "rationale": "18:00 不可用，先找最近的替代时段再决定",
     "observation": {"ok": true, "summary": "替代时段：19:00、20:00（原档占用方 M00）"},
     "next": "schedule.save_draft"}
  ],
  "message": "已保存草稿（草稿ID 2），但有待确认项：……未发布。"
}
```

`examples/*.output.json` 是离线 mock 的真实运行结果，不是手写样例。

## 两份设计为什么并存

| 文件 | 角色 |
| --- | --- |
| `sql/schema.sql` | **目标态设计**：真实部署用 MySQL 时的表、字段、唯一键、注释 |
| `data/seed.json` | **当前实现**：mock 模式下充当数据库的单文件，表结构与 DDL 一一对应 |

`src/store.py` 把 MySQL 的四件事——表、自增主键、唯一键、事务——用最小实现复刻出来，
`src/repository.py` 是唯一的换库边界。迁移步骤见 [实现细节 §9](docs/06-implementation.md)。

## 评审导航

| 检查项 | 入口 |
| --- | --- |
| 业务目标与范围 | [需求](docs/01-requirements.md) |
| ReAct 流程与取舍 | [方案](docs/02-solution.md) |
| 实现细节 | [实现](docs/06-implementation.md) |
| 执行路径记录 | [run-records](examples/run-records.md) |
| MySQL 设计 | [schema.sql](sql/schema.sql) |
| 测试、预期、结果 | [评估](docs/03-evaluation.md) |
| AI 辅助边界 | [AI 使用](docs/04-ai-usage.md) |
| 用时与未完成部分 | [复盘](docs/05-retrospective.md) |

## 边界（先说明，避免误解）

- `published` 恒为 `false`，没有任何代码路径写 `PUBLISHED` 或 `release_date`；`status` 上限是 `CONFIRMED`（仍是草稿）。
- 硬性时间要求（「必须 18:00」）遇到冲突时**不会被自动改期**，只会把冲突和选项交回用户。
- 素材信息缺失时不猜、不填默认值，只列出缺什么。
- 默认决策是规则版 `MockPolicy`；`LLMPolicy` 未端到端联调（无密钥），只用一个脚本化假模型验证了循环。
- 达到 `max_steps` 会返回 `step_limit_reached`，`finished=false`，不产生草稿。

## 提交信息

| 项 | 值 |
| --- | --- |
| 题号 | 素材审核与发布排期 Agent |
| GitHub | _（待填）_ |
| commit SHA | _（待填）_ |
| 运行方式 | CLI（`python -m src.main --input ...`） |

提交前自查：`python -m unittest discover -s tests` 应 `OK`；`examples/` 下的输出应由
`python -m scripts.regen_examples` 生成而非手改。不提交密钥或真实业务数据——`data/seed.json` 全部为合成数据。
