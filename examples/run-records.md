# 执行路径记录（ReAct 决策日志）

三条真实运行记录，由 CLI 生成，可直接复现：

```sh
python -m src.main --input examples/conflict.input.json      --log examples/run-record-soft-conflict.jsonl
python -m src.main --input examples/hard-conflict.input.json --log examples/run-record-hard-conflict.jsonl
python -m src.main --input examples/metadata-lag.input.json  --log examples/run-record-metadata-lag.jsonl
```

每行是一轮「模型选择动作 → 执行工具 → 观察结果 → 决定下一步」，字段含义：

| 字段 | 含义 |
| --- | --- |
| `step` | 第几轮决策 |
| `action` | 模型选的动作（`finish` 表示收手） |
| `args` | 传给工具的参数 |
| `observation.summary` | 工具返回的摘要（模型下一轮看到的就是这句话） |
| `next` | 本轮之后模型选了哪个动作（收尾时补齐） |
| `rationale` | 决策依据，一句话，不是思维链 |

机器可读版本在同目录的 `run-record-*.jsonl`。

---

## 路径 A：软性冲突 → 改档并存草稿（`conflict`）

素材 M01 无风险，用户希望 18:00（软性），但 18:00 已被 M00 占用。

| 步 | 动作 | 参数 | 观测摘要 | 后续 | 决策依据 |
|---|---|---|---|---|---|
| 1 | `material_get` | `M01` | 素材存在，最近审核状态 未审核 | `audit_check` | 先确认素材存在、元数据可读 |
| 2 | `audit_check` | `M01` | 审核结论 PASSED，无风险 | `slot_query` | 审核通过才谈排期 |
| 3 | `slot_query` | `2026-11-05` | 占用：18:00(M00)；可用 13 个时段 | `slot_find_alternative` | 目标日期有占用，先看有没有可用替代 |
| 4 | `slot_find_alternative` | `18:00` | 替代时段：19:00、20:00（占用方 M00） | `schedule_save_draft` | 用户没锁定时间，顺延到最近可用档 |
| 5 | `schedule_save_draft` | `M01` @ `19:00` | 已保存草稿 #2（DRAFT），未发布 | `finish` | 草稿落地，但时间被改过，要标待确认 |
| 6 | `finish` | `needs_confirmation` | — | — | 有需人确认的点，逐条列出而非替用户拍板 |

最终：`status=needs_confirmation`，`suggested_slot=19:00`，`draft.saved=true`，`published=false`。

---

## 路径 B：硬性冲突 → 不改期（`hard-conflict`）

同样的素材、同样的 18:00 冲突，只有用户语气从「希望」变成「必须」。

| 步 | 动作 | 参数 | 观测摘要 | 后续 | 决策依据 |
|---|---|---|---|---|---|
| 1 | `material_get` | `M01` | 素材存在 | `audit_check` | 同上 |
| 2 | `audit_check` | `M01` | 审核结论 PASSED，无风险 | `slot_query` | 同上 |
| 3 | `slot_query` | `2026-11-05` | 占用：18:00(M00) | `slot_find_alternative` | 同上 |
| 4 | `slot_find_alternative` | `18:00` | 替代时段：19:00、20:00 | `finish` | **用户说必须，硬性要求不得改动** |
| 5 | `finish` | `blocked` | — | — | 把改期/取消/换素材的选择权交回用户 |

**分叉点在第 4 步之后**：两条路径的 1–4 步完全一致（有测试断言 `actions(soft)[:4] == actions(hard)[:4]`），
第 5 步一个落草稿、一个收手。这就是「模型根据工具结果调整动作」而不是固定分支。

最终：`status=blocked`，`suggested_slot=null`，`draft.saved=false`，`pending_confirmations` 含三个选项。

---

## 路径 C：观测到缺字段 → 补查后重审（`metadata-lag`）

M07 首次读取模拟上游延迟，缺 `cover_url`。

| 步 | 动作 | 参数 | 观测摘要 | 后续 | 决策依据 |
|---|---|---|---|---|---|
| 1 | `material_get` | `M07` | 素材存在 | `audit_check` | 先读素材 |
| 2 | `audit_check` | `M07` | **INCOMPLETE，缺少 cover_url** | `material_get` | 缺字段可能是上游延迟，补查一次 |
| 3 | `material_get` | `M07` | 素材存在（这次元数据完整） | `audit_check` | 拿到新快照，旧审核结论已过期 |
| 4 | `audit_check` | `M07` | 审核结论 PASSED，无风险 | `slot_query` | 重审通过，继续排期 |
| 5 | `slot_query` | `2026-11-05` | 占用：18:00(M00) | `schedule_save_draft` | 21:00 空闲 |
| 6 | `schedule_save_draft` | `M07` @ `21:00` | 已保存草稿 #2（DRAFT） | `finish` | 目标时段空闲，直接落草稿 |
| 7 | `finish` | `completed` | — | — | 审核通过且时段空闲 |

对比 `missing`（M03 是真的缺封面）：同样补查一次，但重审仍 `INCOMPLETE`，
第 5 步走 `finish(needs_input)` 而不是存草稿——同一套规则，观测不同，动作不同。

---

## 与「固定流程」的区别

| | 固定流程（上一版） | 本版 ReAct 循环 |
|---|---|---|
| 下一步由谁决定 | 代码里的条件分支 | 策略每轮读 `observations` 重新决定 |
| 换模型 | 无法换 | 换 `Policy` 实现即可（`MockPolicy` ↔ `LLMPolicy`） |
| 决策可追溯 | 只有节点序列 `trace` | `decisions` 含动作、参数、观测摘要、后续、依据 |
| 死循环风险 | 无（分支有限） | 有，靠 `max_steps` 兜底并返回 `step_limit_reached` |

单测另有一条「脚本化假模型」路径（`test_llm_policy_drives_the_same_loop`）：
同一套循环由模型给出 5 次 tool_call 驱动走完，证明循环不依赖 `MockPolicy` 的规则。

---

## 真实模型记录（DeepSeek `deepseek-flash`）

上面 A/B/C 三条是离线 `MockPolicy` 的记录。下面三条是**真实调用** `api.deepseek.com` 的结果
（`thinking: disabled`，2026-10-30 采集，未使用缓存）。

```sh
python -m src.main --input examples/conflict.input.json      --provider deepseek --log examples/run-record-deepseek-soft-conflict.jsonl
python -m src.main --input examples/hard-conflict.input.json --provider deepseek --log examples/run-record-deepseek-hard-conflict.jsonl
python -m src.main --input examples/missing.input.json       --provider deepseek --log examples/run-record-deepseek-missing.jsonl
```

| 路径 | 最终状态 | 步数 | 草稿 | token（prompt+completion） |
|---|---|---|---|---|
| 软性冲突 | `needs_confirmation` | 6 | #2 @ 19:00 | 12558 |
| 硬性冲突 | `blocked` | 4 | 无 | 6640 |
| 信息缺失 | `needs_input` | 5 | 无 | 8102 |

### 路径 D：软性冲突（与 mock 结论一致）

| 步 | 动作 | 观测摘要 |
|---|---|---|
| 1 | `material_get(M01)` | 素材存在，最近审核状态 未审核 |
| 2 | `audit_check(M01)` | 审核结论 PASSED，无风险 |
| 3 | `slot_query(2026-11-05)` | 占用：18:00(M00)；可用 13 个时段 |
| 4 | `slot_find_alternative(18:00)` | 替代时段：19:00、20:00 |
| 5 | `schedule_save_draft(M01 @ 19:00)` | 已保存草稿 #2（DRAFT），未发布 |
| 6 | `finish(needs_confirmation)` | 待确认：原 18:00 已改排 19:00，备选 20:00 / 17:00 |

模型自己生成的结论：*"18:00 已被 M00 占用，属软性要求，已顺延存草稿至 19:00（DRAFT，未发布），请确认是否接受 19:00，或改用 20:00。"*
——与离线路径的结论、草稿 ID 完全一致。

### 路径 E：硬性冲突（少一步也不越线）

| 步 | 动作 | 观测摘要 |
|---|---|---|
| 1–3 | 同路径 D | 18:00 被 M00 占用 |
| 4 | `finish(blocked)` | "用户要求必须该时间，属硬性约束，不得自行改期，未保存草稿" |

注意它**没有**调用 `slot_find_alternative`：从 `slot_query` 的摘要里已经看到 17:00/19:00 可用，
就直接把选项写进 `pending` 收手。这就是真实模型与规则 mock 的差别之一——mock 固定多走一步。

### 路径 F：信息缺失（提示词规则生效）

首轮实测时模型只审一次就 `finish(needs_input)`，与设计中的「补查一次」不一致。
在 system prompt 加了第 9 条规则（审核 INCOMPLETE 时先重读再重审）后，行为变成：

| 步 | 动作 | 观测摘要 |
|---|---|---|
| 1 | `material_get(M03)` | 素材存在 |
| 2 | `audit_check(M03)` | INCOMPLETE，缺少 `cover_url` |
| 3 | `material_get(M03)` | 重读（排除上游延迟） |
| 4 | `audit_check(M03)` | 仍 INCOMPLETE |
| 5 | `finish(needs_input)` | 请补充封面地址 |

### 接入过程中被真实 API 打出的两个问题

| 现象 | 根因 | 修法 |
|---|---|---|
| `400 Invalid 'tools[0].function.name'` | 工具名带点号（`material.get`），而 function name 只允许 `[a-zA-Z0-9_-]` | 全部改为下划线（`material_get`），源码/文档/记录同步 |
| `status=needs_confirmation` 但 `draft.saved=false` | 模型没调 `schedule_save_draft` 就直接收尾 | ① prompt 加规则 7；② `finalize` 兜底：声称完成但无草稿 → 降级为 `blocked` |

这两条在离线 mock 里都不会出现——只有真跑一次真实模型才会暴露，也是这次接 DeepSeek 的主要收获。
