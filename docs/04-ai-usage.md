# AI 使用说明

## 我的工作

### 1. 设计：数据模型、主流程与边界

这些是我拍板的，AI 按约束实现；每行都给了可核对的证据。

| 设计 | 我的决定 | 证据 |
|---|---|---|
| 存储架构 | **双轨**：`sql/schema.sql` 保留 MySQL 设计，运行时用 JSON 单文件库 | [schema.sql](../sql/schema.sql)、`src/store.py` |
| 表与字段 | 排期表与审核表分开；`release_date` 为 NULL 即未发布 | [题目](../题目.md) §1、`schema.sql` |
| 唯一键与占用口径 | 同素材同时段一条 + 请求幂等键；`DRAFT` 也算占用、容量 1 | `TableSpec`、`slot_query` |
| 一致性边界 | 一个素材只允许一条有效排期；同一文件换 ID 按内容指纹判重 | `ALREADY_SCHEDULED`、R009 |
| 主流程 | `prepare → agent ⇄ act → finalize`；补查限一次；软性可改档、硬性不改期 | `src/graph.py`、`src/policy.py` |
| 限步与未完成 | 上限 10 次工具调用，超限返回 `step_limit_reached` | `test_step_limit_returns_unfinished_state` |
| 验收口径 | 题目六类 + 我补的两条守卫，各配示例与测试 | [评估](03-evaluation.md) |

### 2. 定方向与红线

| 我定的规则 | 为什么 | 落地位置 |
|---|---|---|
| 只存草稿，不接真实发布 | 题目明确要求，也是最容易"顺手加个发布按钮"的地方 | `status` 上限 `CONFIRMED`；`published` 恒 `false`；`release_date` 永不写 |
| 判定必须确定性，模型只能选动作 | 同一素材不能两次跑出两个审核结论 | 审核走 `rules.py` 纯函数；时段占用查表；重复由唯一键判 |
| 硬性时间不得擅自改 | "必须 18:00"被改期是最得罪用户的行为 | 冲突时只 `finish(blocked)` 交回选项，不落库 |
| 保存失败必须说失败 | 宁可报错，也不能让用户以为排好了 | `tool_error` + `message` 带错误码；`finalize` 兜底降级 |
| 没有实现的功能不许写进文档 | 交的是作业，不是 PPT | 删掉过「支持批量」「已接入真实模型」等不实描述 |

### 3. 我发现的缺陷

| 我指出的问题 | 后果 | 修法 |
|---|---|---|
| 初版是固定七节点流程图，"下一步走哪条边"是写死的 | 不满足考核规则里「模型选动作 → 执行 → 观察 → 调整」的要求 | 重构成 `prepare → agent ⇄ act → finalize`，策略每轮读观测再决定，并加 `max_steps` 兜底 |
| 唯一键只有 `(material_id, date)` | 同一素材换个时段还能再排一条，一个素材占两个档 | 保存前查该素材其它有效排期，命中 `ALREADY_SCHEDULED` 并带出原草稿 |
| 去重全靠 `material_id` | 同一份文件换个 ID 重传就是"新素材" | 加内容指纹（`file_url+duration+format` 归一化后 sha256），同指纹判 R009 / RISK，建素材时直接 409 |
| 配置只读 `os.environ` | 我在 `.env` 里配好了 key，程序还说没有 | 加 `.env` 加载器，全项目统一 `os.getenv`；`.env` 覆盖同名进程变量，改文件就生效 |
| 需求里没有素材维护 | 只能靠改种子文件加素材，不像个应用 | 补 `POST/PUT/DELETE /api/materials` + 前端弹窗，写入即审核 |

### 4. 我做过的验收（不是"看它说通过了"）

| 验收方式 | 具体动作 | 结果 |
|---|---|---|
| 单测 | `python -m unittest discover -s tests -v` | 155 个用例全通过 |
| 示例回归 | 11 条 CLI 示例逐条比对 `status` | 11/11 符合预期 |
| 真实模型 | 用自己的 DeepSeek key 跑 `--provider deepseek` | 3 条路径与 mock 结论一致（token 用量见 [run-records](../examples/run-records.md)） |
| 从零安装 | 在空目录 `git clone` → venv → `pip install` → CLI → 起服务 | 全部走通（这一步抓到 `requirements.txt` 编码问题） |
| 界面 | headless 浏览器截图逐页看 | 抓到弹窗样式与「弹窗常驻」两个问题 |

## 数据库与编排

| 模块 | AI 产出 | 我的验证 |
|---|---|---|
| 存储层 | JSON 库（表/唯一键/事务/文件锁） | `tests/test_store.py`：唯一键、NULL 语义、回滚不落盘、8 线程并发 |
| 审核规则 | R001–R009 骨架 | `tests/test_rules.py` 逐条断言结论与规则码 |
| 编排与策略 | LangGraph 循环、`@tool` 注册与发现（`tooling.py` + `toolbox/`）、`MockPolicy` / `LLMPolicy` | `tests/test_workflow.py` 断言决策序列/限步；`tests/test_tooling.py` 断言发现与注册校验 |
| 模型客户端 | DeepSeek 报文、重试矩阵、错误码翻译 | `tests/test_clients.py` 用假 transport 覆盖 429/5xx/400/401/402/超时 |
| Web 层 | 标准库 HTTP + 单页界面 | `tests/test_server.py` 31 例 + 截图实看 |
| 文档 | 各篇初稿 | 与代码行为逐条核对，删掉未实现的能力 |

## 实现过程中暴露、我要求修正的问题

这些是 AI 实现里真实犯过的错。列出来不是为了甩锅，而是说明**每一处都有对应的回归测试或验证动作**，
下次换个题目也知道该盯哪里：

| 现象 | 根因 | 现在的防线 |
|---|---|---|
| 重复提交产生两份草稿 | 冲突判断比幂等检查更早，把请求推到了别的时段 | `slot_query` 排除自身占位 + 唯一键 + 幂等键，`test_duplicate_submission_creates_one_draft` |
| 补查永远读到同一份数据 | `ToolContext` 每轮重建 | 整轮共用一个上下文，`test_metadata_lag_recovers_after_recheck` |
| 模型不落草稿就 `finish(needs_confirmation)` | 把"完成"交给模型自觉 | prompt 规则 + `finalize` 降级为 `blocked`，`test_finish_without_draft_is_downgraded_to_blocked` |
| 工具名 `material.get` 被 DeepSeek 400 | function name 只允许 `[a-zA-Z0-9_-]` | 全部改下划线，联调复跑 |
| `pip install -r requirements.txt` 在中文 Windows 崩 | pip 用 cp936 解码，文件里有中文注释 | `requirements.txt` 改 ASCII |
| 弹窗在每次加载都显示 | `.modal{display:grid}` 盖掉了 `[hidden]` 的默认样式 | `.modal[hidden]{display:none}`，截图复验 |
| Windows 并发测试偶发 `PermissionError` | 锁文件 delete-pending 时返回的不是 `FileExistsError` | `_file_lock` 把 `PermissionError` 也当占用重试，另加压力脚本 |

## 我在 AI 输出上做的删改

| AI 的写法 | 我的决定 |
|---|---|
| 把审核结论交给模型输出 | 改为纯函数规则——结论必须可复现、可回归 |
| 计划写"支持批量排期" | 删掉，没实现就不写 |
| 文档里出现"已接入真实模型" | 先改成"未联调"，等真的跑通再改回来 |
| 用 PowerShell 批量改文档 | 导致 BOM/CRLF 混入，改为脚本 + 全仓归一检查 |
| 工具清单写成手写字典 + `summarize()` 的 if/elif，新增工具要改三处 | 改成 `@tool` 装饰器 + 扫包发现，注册时校验名字字符集、重名与 formatter |
| 直接 `git add -A`，把我手动改到一半的 README 一起提交（还带了一行截断的表格） | 要求提交前先看 `git diff`；补回表格行，并加全仓表格/编码检查 |

## 我没有交给 AI 的判断

- 硬性/软性时间要求的边界：冲突时只能请求确认，不能自动改期；
- 保存失败必须显式失败、回复里不得出现「已排期」；
- `published` 恒 `false`、`status` 上限 `CONFIRMED` 的状态机；
- 六类验收与示例的一一对应关系；
- ReAct 的判定权划分：模型选动作、代码定事实，并加步数上限。

