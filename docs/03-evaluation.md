# 测试与评估

本页记录离线流程验证 + 一次真实模型（DeepSeek）联调。
离线部分见 [test-results.txt](../examples/test-results.txt)：`Ran 96 tests … OK`。

## 必须验收的六类

| 用例 | 输入变化 | 预期 | 实测 |
|---|---|---|---|
| 正常排期 | M02 要 20:00，时段空闲 | `completed`，草稿 `DRAFT`，`published=false` | 通过（[输出](../examples/normal.output.json)） |
| 素材信息缺失 | M03 缺 `cover_url` | `needs_input`，列出 `cover_url`，不落库 | 通过（[输出](../examples/missing.output.json)） |
| 规则风险 | M04 标题含「最低价」 | `blocked`，`risk_reason` 含 R005 | 通过（[输出](../examples/risk.output.json)） |
| 时段冲突（软） | M01 要 18:00，被 M00 占用 | 建议 19:00，存草稿，标待确认 | 通过（[输出](../examples/conflict.output.json)） |
| 时段冲突（硬） | 同上 + 「必须」 | **不改期**，`blocked`，给三个选项 | 通过（[输出](../examples/hard-conflict.output.json)） |
| 重复提交 | 同一请求连提两次 | 同一 `draft_id`，`deduped=true`，表里一行 | 通过（[输出](../examples/duplicate.output.json)） |
| 保存失败 | 注入 `db_unavailable` | `tool_error`，重试 1 次后明确失败，无半条数据 | 通过（[输出](../examples/save-failure.output.json)） |

## 其它边界

| 用例 | 输入变化 | 预期 | 实测 |
|---|---|---|---|
| 工具失败 | `material_lookup=true` | `tool_error`，`SOURCE_UNAVAILABLE`，不落库 | 通过 |
| 元数据延迟 | 首次读缺封面，补查完整 | `decisions` 为 `get→audit→get→audit→…`，最终 `completed` | 通过（[执行记录](../examples/run-records.md)） |
| 素材不存在 | `M99` | `not_found`，请用户确认 ID | 通过 |
| 提醒失败 | `notify_failure=true` | 排期照常，`notify.degraded=true` | 通过 |
| 取消后重排 | 已有 `CANCELLED` 行 | 复用原行，`reused=true`，不产生第二行 | 通过 |
| 别的素材占用 | M00 占 20:00 | 顺延 21:00 并标待确认 | 通过 |
| 没给时间 | 只有 `material_id` | 取当天首个可用时段，标待确认 | 通过 |
| 没给素材 ID | 只有一段话 | `needs_input`，`missing_fields=["material_id"]` | 通过 |
| 达到步数上限 | `--max-steps 2` | `step_limit_reached`，`finished=false`，不落库 | 通过 |

## 执行路径记录

| 路径 | 输入 | 决策序列 | 记录 |
|---|---|---|---|
| A 软性冲突 | `conflict` | get→audit→query→alternative→**save(19:00)**→finish | [JSONL](../examples/run-record-soft-conflict.jsonl) |
| B 硬性冲突 | `hard-conflict` | get→audit→query→alternative→**finish(blocked)** | [JSONL](../examples/run-record-hard-conflict.jsonl) |
| C 元数据延迟 | `metadata-lag` | get→audit(INCOMPLETE)→**get→audit(PASSED)**→query→save→finish | [JSONL](../examples/run-record-metadata-lag.jsonl) |

A 与 B 的前 4 步完全相同，第 5 步因「用户语气」分叉；C 因「审核结论缺字段」回头重读。
三条路径的可读版见 [执行路径记录](../examples/run-records.md)。

## 真实模型联调（DeepSeek `deepseek-flash`，2026-10-30）

用真实 key 跑了 3 条路径，与 mock 结论对照：

| 路径 | 状态 | 步数 | 草稿 | token | 与 mock 是否一致 |
|---|---|---|---|---|---|
| 软性冲突 `conflict` | `needs_confirmation` | 6 | #2 @ 19:00 | 12558 | 一致（mock 也是 6 步） |
| 硬性冲突 `hard-conflict` | `blocked` | 4 | 无 | 6640 | 结论一致，少走一步 `slot_find_alternative` |
| 信息缺失 `missing` | `needs_input` | 5 | 无 | 8102 | 加提示词规则后一致（补查一次再重审） |

记录：[软性](../examples/run-record-deepseek-soft-conflict.jsonl) ·
[硬性](../examples/run-record-deepseek-hard-conflict.jsonl) ·
[缺失](../examples/run-record-deepseek-missing.jsonl)（可读版见 [run-records](../examples/run-records.md)）。

联调打出的两个真实缺陷（离线 mock 永远测不到）：

| 现象 | 根因 | 修法 | 回归 |
|---|---|---|---|
| `400 Invalid 'tools[0].function.name'` | 工具名带点号，不满足 `^[a-zA-Z0-9_-]+$` | 全部改下划线 | `test_clients` + 联调复跑 |
| `needs_confirmation` 但 `draft.saved=false` | 模型不存草稿就收尾 | prompt 规则 7 + `finalize` 降级为 `blocked` | `test_finish_without_draft_is_downgraded_to_blocked` |

## 单元/集成测试分布（共 96）

| 层 | 数量级 | 关注点 |
|---|---|---|
| `tests/test_store.py` | 10 | 唯一键、NULL 语义、自增、回滚不落盘、原子写、8 线程并发、锁 PermissionError 重试、空库初始化 |
| `tests/test_rules.py` | 13 | R001–R008 逐条、缺失优先于风险、`metadata=None` |
| `tests/test_llm.py` | 15 | 结构化优先、文本回退、硬/软识别、非法输入、HttpLLM 报文与代码围栏剥离 |
| `tests/test_clients.py` | 20 | DeepSeek 报文、tool_calls/usage 解析、429 重试与 Retry-After、400/401/402 不重试、超时/网络错误、思考模式参数、env 工厂 |
| `tests/test_workflow.py` | 28 | 六类验收 + 补查 + 降级 + 复活 + 限步 + 决策日志 + 假模型循环 + 模型失败降级 + 参数防护 + 未落草稿不得声称完成 |
| `tests/test_cli.py` | 10 | 退出码、`invalid_input`、BOM 容错、跨进程幂等、`--now`/`--compact`/`--max-steps`/`--log` |

## 可自动化的硬断言

| 断言 | 含义 | 落点 |
|---|---|---|
| `status in {completed, needs_confirmation, needs_input, blocked, not_found, tool_error, step_limit_reached}` | 状态机没有野生状态 | `test_every_status_is_known` |
| 所有 `publish_schedule.status ∈ {DRAFT, CONFIRMED, CANCELLED}` | 没有任何路径写 `PUBLISHED` | `test_repeat_runs_do_not_publish_anything` |
| 所有 `material_review.release_date IS NULL` | 结构上证明「未真实发布」 | 同上 |
| 重复提交后同素材同时段只有 1 行 | 幂等成立 | `test_duplicate_submission_creates_one_draft` |
| 保存失败后行数不变 | 无半条数据 | `test_save_failure_is_reported_loudly` |
| 运行后请求对象深比较相等 | 不篡改输入 | `test_input_object_is_not_mutated` |
| `max_steps=2` 时 `status=step_limit_reached` 且 `finished=false` | 限步兜底有效 | `test_step_limit_returns_unfinished_state` |
| 软/硬冲突前 4 步动作完全相同、第 5 步分叉 | 决策随观测变化 | `test_action_changes_after_observing_conflict` |

## 示例如何评判

| 观察项 | 证据 | 结论 |
|---|---|---|
| 目录、文档齐全 | README + docs 六篇 + sql + tests | 满足目录规范 |
| 可复现 | 10 组 CLI 样例、`test-results.txt`、`scripts/regen_examples.py` | 已在 Python 3.12.4 + langgraph 1.2.18 验证 |
| 使用 LangGraph | `src/graph.py` 的 `StateGraph`，`agent ⇄ act` 循环 | 已实现，非伪代码 |
| ReAct 决策 | `src/policy.py` 选动作 + `src/registry.py` 执行 + `observations` 回灌；三条执行路径见 [run-records](../examples/run-records.md) | 循环真实；默认策略是规则版 mock |
| 步数与未完成状态 | `max_steps` 默认 10，超限返回 `step_limit_reached` | 已实现并有测试 |
| 六类验收 | 六个示例 + 对应测试 | 全部通过 |
| MySQL 设计保留 | `sql/schema.sql` + `docs/06` 映射表 | 保留且与 JSON 库一一对应 |
| DeepSeek 接入 | `src/clients.py` + `tests/test_clients.py`（20 例） | 报文/重试/错误码已验证；**已用真实 key 跑通** 3 条路径（见下） |
| 模型决策质量 | `deepseek-flash` 实跑 3 条路径 | 结论与 mock 一致；样本量小，不作质量结论 |
| 生产可靠性 | 单文件库、文件锁、无服务化 | 局部实现；属加分项而非底线 |

本仓库不能仅因目录完整被判题目达标；真实模型效果、多素材批量、并发压测均未验证。
