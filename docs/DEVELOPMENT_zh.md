# 开发指南

[English version](DEVELOPMENT.md)

本文面向想扩展 SimpleAutoResearch 的贡献者。命令细节见 [CLI 参考](CLI_REFERENCE_zh.md)，TOML schema 见 [配置参考](CONFIG_REFERENCE_zh.md)，安装 walkthrough 见 [使用与配置](USAGE_zh.md)，工作流概念和产物见 [工作流与产物](WORKFLOWS_zh.md)。

## 项目形态

SimpleAutoResearch 以文件产物和持久化 session state 为中心：

- capability 读取和写入具体 artifact；
- `session_manifest.json`、attempt 和 `ArtifactRef` 展示保存状态与交接；
- 测试检查契约和产物，不依赖隐藏的内存状态；
- 代码修改发生在隔离工作区，不直接修改原项目。

主要职责分布：

```text
src/simple_ar/
  cli/              用户输入、配置与命令分发
  app/              任务协调与下一步判断
  core/             会话、attempt、产物、预算与进程
  research/         文档、阅读、综合、设计与分析契约
  code_task/        项目调查、受限修改、验证与修复
  result_analysis/  确定性表格分析与数据图
  report/           写作、审阅、装配、书目、审计与导出
  integrations/     模型与服务适配
  agent_backends/   有范围的交接与后端接口
```

控制台入口使用 `simple_ar.cli.main:main`，Python 调用使用
`from simple_ar.cli.main import main`。入口变更后先重新安装，再使用
`--no-sync`；不要恢复依赖子模块导入顺序的包级同名转发。

保存原文的段落排序使用 Python `sqlite3` 的 FTS5/BM25；自定义 Python 运行环境须提供
FTS5。词项表仅在本次检索内存中存在，用后关闭，不增加数据库文件或索引服务。

## 工程原则与代码审查标准

代码提示共用 `scope.prompt_file_inventory`，全部索引路径保留编辑/证据权限，详细
描述只供可改或已选文件。`interfaces.source_snippet_views` 将相符的相邻/重叠源码
用于统一展示和接口提取，不拼接未读缺口或不同访问角色。完整覆盖须有已知全文件
长度，冲突窗口保持分开。这是不可变投影，不是另一套源码索引或缓存。
源码清单在遍历前按标准 `pyvenv.cfg` 排除虚拟环境，不仅依赖 `.venv`/`venv` 名字。
项目内的任务环境不能变成源码或配置上下文；名为 `environment` 的普通源码目录保留。

`execution/repair` 复用同一源码窗口读取及呈现：traceback/静态报错行优先，再读当前
diff锚点，每文件至多两个位置共用原字符额度。小文件完整，旧坐标失效回退当前部分
视图；只读测试提供证据但不能获得编辑权。旧计划是待检验假设，不是固定诊断。
这是改善信息供给，不等于证明模型已改对代码。

最重要的原则是：**为当前真实任务提供可靠路径；遇到研究上的不确定性，保留证据并继续判断；只有触及明确的执行边界时才阻止操作。**

SimpleAutoResearch 同时要避免两种失控：不断增加暂时没有消费者的通用架构，以及不断增加检查、回退和防御分支，最后让真正的研究路径难以理解。下面的标准适用于新功能、重构、兼容层和测试，不仅适用于业务代码。

### 改动、结构与抽象

1. **一个改动必须对应一个具体问题。** 开始前说明什么输入或操作会触发当前问题、修改后应产生什么可观察变化。如果只能解释为“以后可能需要”或“这样更完整”，先不做；新增抽象、配置和检查也遵循同一标准。
2. **正常路径应当直白可读。** 从研究输入到下一动作，应能沿着少量应用函数看明白。已有 capability registry 可以用于明确的能力边界，但普通内部函数不必全部注册，也不要为了可替换性让一次调用穿过多层 factory、manager、gateway 和 adapter。
3. **先复用具体代码，再决定抽象。** 两处确实相同的处理可以提取共同函数；只是表面相似时允许少量重复。抽象应来自已经出现的共同需求，而不是来自预想中的所有未来实现。合并 CodeTask bridge 与独立入口前，先核对默认值、授权和失败语义，不能只统一函数名称。
4. **配置项必须有实际使用场景。** 不为每个 `if` 增加开关，也不要求用户理解内部阶段才能运行。预算、可修改范围、研究目标等会影响用户决定的内容才需要显式配置；内部选择优先使用合理默认值。

依次优先标准库、已有依赖、合适的新库。引库须说明实际消费者、替代的自造实现、
行为/序列化兼容、许可及安装成本；直接使用时显式声明，不偷偷依赖传递安装。
开发检查工具不必成为运行时依赖。库的价值是减少维护职责，不只是少写几行。

### 契约、真实性与异常

5. **只在边界做必要校验，内部相信已经成立的契约。** 用户输入、模型输出、外部文件和进程结果需要校验；进入内部后使用明确类型，不让每层重复判断空值、字典和字段。尤其不能用连续的 `.get(..., 默认值)` 把缺失的实验结果悄悄变成零或空结果。
6. **科学不确定性不等于系统错误。** 摘要不完整、idea 新颖性未知、实验没有提升，都应形成带限制的结果。只有当前动作确实缺少必要条件时才暂停该动作；缺少数据可以阻塞实验，但不应阻塞不依赖该数据的文献分析。
7. **回退必须保持语义真实。** 模型不可用时可以输出确定性的状态摘要，但不能标为模型完成的分析；全文不可用时可以分析摘要，但不能称为全文阅读；实验失败时可以保留已有结果，但不能生成固定指标让流程通过。每种回退都要说明实际完成了什么。
8. **异常在能处理它的地方处理一次。** provider 层处理可恢复的网络错误，应用边界记录运行失败和恢复位置。不要每层都捕获 `Exception` 后返回空对象、默认成功或模糊字符串；保留错误原因和调试信息，让真实缺陷尽早暴露。
9. **每种事实只有一个负责维护它的地方。** 实验执行器负责实测结果，研究模块负责解释，应用层负责下一步，报告负责表达。CodeTask、session 和 Writer 不应分别维护互相矛盾的预算或成功状态。外部 Agent 不能通过自报成功覆盖框架观察。

### 测试、兼容与交付

10. **测试验证用户结果和关键边界。** 优先验证无实验请求不启动进程、恢复不重复训练、负结果不进入无限修复、指标与条件正确对应等行为。少测私有函数调用次数和内部对象数量；不能用大量 mock 取代一次小型真实执行，也不要求每个小改动都重跑昂贵验证。
11. **兼容层有边界，也有退出条件。** 旧入口可以保留使用方式，但不能继续承载新业务。记录消费者、替代方案和删除条件；不要长期维护两套完整编排器，也不要为了删除旧目录而破坏有效的历史读取。
12. **每批改动小而完整。** 一批解决一个可见问题，包含必要实现、验证和说明。尽量不要同时改变目录、接口、行为、依赖版本和输出格式；完成后及时删除已被替代的局部实现，避免只加代码而不结束迁移。

局部修改先跑受影响能力及直接消费者，再检查一个真实用户结果。共享配置、会话、执行或恢复
改变时扩大相关覆盖；全量回归留给发布冻结或大范围重构。记录未测边界，不用测试数量代表质量。
昂贵模型调用、依赖安装和训练放在选定的计算服务器；共享 `uv` 环境使用 `uv sync --inexact`
保留额外科学包，benchmark 依赖另建环境，不装进应用环境。

### 固定的代码审查问题

每次审查至少回答：

- 这个改动解决了哪个已经确认的问题？
- 正常路径是否更容易看懂？
- 有没有新增重复状态、隐藏回退或不必要的配置？
- 信息不足时，是否仍能完成不受影响的部分？
- 用什么实际证据证明它有效？

实施蓝图中的锁、预算、迁移和恢复，只实现当前路径需要的最小可靠版本；蓝图不是“先把所有基础设施和防护做齐，才允许交付功能”的清单。示例中的时间、请求和资源限制是可调整的工程起点，不能成为回避真实任务的理由。完整架构与施工顺序保存在项目本地的 `MDfiles/` 规划笔记中；该目录按项目约定不提交 GitHub，公开贡献者规则以本文为准。

### 当前 research-session 扩展边界

以下是结构化研究的开发契约，不是所有 provider、项目或科研循环都已经通过 live acceptance 的声明。

`research-session` 把任务/资产/约束解释成有界 accepted plan，再接到 typed capability。Design 和 execution
复用 supplied entrypoint、protocol、CodeTask 修改范围和 process budget。模型只能在检查过且已授权的边界内
提出条件，不能自授予 cwd、installer、repair limit 或 process permission。baseline 只有 `run`、`skip` 或
同条件 `reuse`；只有命令、schema、protocol、准备 lineage 和保护资产都匹配时才复用保存的测量。
Analysis、实现、实验、报告写作和审计各自拥有不同事实。plan 不是 execution，零退出码不是科研成功，
报告正文也不能创造测量结果。恢复沿用保存的 attempt/state 引用，不静默重复已完成副作用；provider 错误、
进程失败和科研负结果保持区分。

- 一条执行链：任务/资产 → 近期计划 → 类型化能力请求 → SessionController → 实际产物 → 必要时重新判断。
- 基础研究记忆从任务、计划、相关经历和产物引用组装，不是第二份事实库。
  硬约束、版本和数字来自原记录；模型总结不覆盖它们。技术失败与方法负结果分开。
- Prompt 保留稳定职责与输出契约，动态输入区分用户要求、观察事实、建议和未知项。
  缺失输入先修数据传递，不用更多提示语掩盖；只在外部解析边界纠正格式，不层层回退。
- 执行回传复用 CapabilityResult/ArtifactRef，绑定实际条件、代码版本和输入来源。
  计划不等于执行，返回码成功不等于目标达成，报告不产生测量事实。
- 已接受计划前提未变时继续；出现影响判断的新证据才重规划。基本恢复从第一条路径就要成立。

不要因为 schema 或 fixture 存在就把未来能力写成已验收。provider、项目、GPU、检索和报告质量的 live 证据
必须与离线 parser/behavior coverage 分开记录。新工作应扩展当前 application/capability 路径，并在真实消费者
出现前保持有界。

### 兼容性审计

普通新任务使用目标驱动的原生入口；精确结构化协议、分段命令和历史恢复仍有实际消费者。
旧八阶段执行器已经删除：

```text
start（普通原生目标与可迁新建偏好）
  -> ToolApplication -> CommonToolGateway -> 共享专业模块
  -> SessionController -> ArtifactStore

research-session（精确结构化协议与已保存会话恢复）
  -> ResearchApplication
  -> typed research capabilities -> SessionController -> ArtifactStore

research-brief（分段/开发接口）
  -> typed research capabilities -> SessionController -> ArtifactStore

simple-ar status / inspect / search-artifacts
  -> 历史产物读取（不启动旧流程）
```

`start` 将普通目标和可迁新建偏好交给 `ToolApplication`，经现有专业工具组合调查、实现、
分析、写作、视觉/图像及出版。`cli/start.py` 只适配输入，不拥有执行。精确表格列、固定
编辑/验证范围及复现协议仍沿 TOML/`research-session` 和 `ResearchApplication` 执行。
已保存任务按原所有者和协议恢复，结构化执行不是原生失败后的隐式 fallback。目标和偏好
不授予权限：挂载输入只读，访问与资源须明确授权，进程命令仍需精确批准；这不是操作系统
沙箱，也不证明可靠交付或科学质量。
`research-brief`
仍保留，但只用于分段调试、已有 handoff 接续和库级组合，不与完整主线
并列作为产品入口。`simple-ar run/resume` 已退出，不静默转换旧参数。新的 capability 应
放在 `research/`、`code_task/`、`experiment/`、`result_analysis/` 或 `report/` 中。旧阶段层已删除；历史消费者只读，当前
实验和报告行为由下文模块负责。

当前树通过 read cards 和 `evidence_pack_from_read()` 统一承担阅读到综合的证据交接，不增加第二份
planning 或 lifecycle store。CodeTask 的 external CLI 支持保留为显式、默认
禁用的 backend，因为当前实验路径仍使用它的 provider factory；它不是 research-session 的
workflow controller。

历史 reader 和兼容 facade 只在仍有消费者或旧格式时保留。删除前必须搜索 import、CLI 分发、
文档、fixture 和历史 reader，迁移真实消费者并保留旧格式回归。若真实消费者已经退出，旧
facade、registry 分支和 projection 应直接删除，不继续保留“以后可能有用”
的整套入口。

### 交付审查清单

准备交付前检查真实用户路径及证据：

1. 从公开命令追踪到负责 capability 和 artifact 的输入链；
2. 用定向测试覆盖正常、失败、恢复和无实验路径；
3. 将生成声明与原始测量、protocol、来源和图表对照；
4. 直接列出 provider、环境、数据和 live 项目限制，不削弱 gate；
5. 删除被替代分支和重复职责，不为保留旧路径再造一套 lifecycle。

普通任务由同一原生行动主干组合专业能力；精确结构化新任务和历史续接保留原语义。
内部 capability 仍然保持模块化，复用既有 session、产物与账本，不另建竞争状态或记忆。

### 清理规则

如果一个生成产物是声明过的 handoff、审计记录、可移植的用户输出，或旧格式的兼容输入，
就应当保留。可重建缓存只能通过显式的 `simple-ar clean` 清理；pipeline 不应静默删除它们。
删除代码前，应搜索 import、CLI 分发、文档、fixture 和历史 reader，并为替代路径补一个针对性
回归测试。优先删除已经确认的无效导入或有证据的重复分支；不要仅仅因为某个 adapter 较长，
就在没有真实消费者的情况下继续拆出更多层。

生产消费者消失的代码应删除，但不为降低文件行数而拆散内聚实现。旧研究门面和阶段别名
已经删除。剩余报告、实验及应用生命周期仍須收束，共享 projection 不等于统一执行 owner。

## 职责边界表

CLI/TOML 合并后的输入校验归 `cli.research_config.validate_session_arguments`；同模块
`report_settings` 沿既有 `FIELDS["report"]` 投影新建与显式续跑的报告参数。续跑省略项
不覆盖保存值，显式 false、0 和空映射保留；模板选择仍属于 CLI 的任务策略。行动分发、
执行决策与持久化仍归应用，普通行动处理方法不另建运行时。有界报告提示视图归
`report.narrative`，不保存第二份报告记忆。编辑上下文选择归 `code_task.editing.scope`，
初次改码与修复共用。整理纯视图不意味着可以改变 checkpoint、预算或采用稿语义。

决定改动放在哪里时先看这张表。稳定入口是新调用方可以依赖的最小公开边界；
最后一列同样重要，它用来防止领域策略泄漏进 core。

| 区域 | 稳定入口 | 负责内容 | 不负责内容 |
| --- | --- | --- | --- |
| Core runtime | `simple_ar.core` | 产物引用、attempt lineage、有界决策、profile、转移校验和共享资源账本 | 领域 schema、provider 调用、代码编辑、重试或选择最佳结果 |
| Sources、documents、evidence | `research.sources`、`research.documents`、`research.evidence` | provider/parser port、文档 bundle、cards、chunks 和带 provenance 的 handoff | workflow 调度、把 provider 专属策略塞进 core，或把全文复制进每份 handoff |
| 保存原文检索 | `research.store.retrieval` | 共享词面排序、已知来源顺序、真实文本窗口 | 语义真伪、论文身份或新索引；report 仅保留薄兼容导入 |
| Synthesis | `research.synthesis`、`research.brief` | 基于证据的方向、研究契约和最小的文献到想法组合 | 自动宣称创新性、自动选择实验或隐式调用模型 |
| Experiment 与 analysis | `research.experiment`、`research.analysis`、现有 `experiment.execution` | 显式运行请求、规范化结果、指标比较和结果证据状态 | 代码生成、repair 策略、重试策略或决定下一研究阶段 |
| 供材表格与数据图 | `result_analysis.table`、`result_analysis.figures` | 共享解析/预检、描述计算、显式坐标绘图和可重建分析包 | 猜行语义、认证科学有效性、任意统计/代码执行 |
| Report 与 audit | `report.projection`、`report.capability`、`report.audit`、`report.writing` | 证据投影、显式章节组装、可选图表渲染、引用/指标审计和旧报告兼容 | 隐藏缺失证据、凭空生成图表，或没有迁移契约就替换旧 writer/reviewer |
| 写作上下文与修订 | `report.narrative`、既有 `report.agent`/`editor` | 已采用正文投影、原修改契约、候选验证、既有检查点/额度消费 | 第二套记忆、把拒绝稿当事实或无界反复审阅 |
| Application 与 benchmark | `app`、`cli`、`code_task` 和 benchmark adapter | 面向用户的编排、旧 projection、code-task 策略和外部评测接入 | 成为 core runtime 的依赖，或为了单个 benchmark 改变通用 capability 语义 |

阅读查找投影由 `research.evidence.reader.reading_followup_context` 负责。综合只消费
当前采用笔记、补读原文和剩余问题，不将嵌套旧笔记重复当作当前判断；完整历史留在
原Read产物，报告投影复用同一查找视图，不复制另一份历史。

如果一个功能看起来跨越两行，应把协调放在 application 或显式 adapter 中，
通过声明的 `ArtifactRef` 传递输入；不要让下层直接导入上层的私有文件。只有当
现有边界无法表达真实消费者的输入、输出或失败状态时，才新增 class 或 artifact；
否则优先在现有边界增加函数、adapter 或 fixture。

## 新模块的能力边界

新的可替换模块可以使用 `src/simple_ar/core/` 中的轻量能力边界，暂时不必
改动现有 pipeline。`ArtifactRef` 标识已经声明的产物，`ArtifactStore` 提供
相对于 run 和 attempt 的文件读写，`CapabilityContext` 传递已登记的输入和
profile，`CapabilityResult` 返回状态、输出引用、诊断和 provenance。
`CapabilityRegistry` 只使用显式注册，不扫描仓库，也不动态导入任意 provider。
controller 管理的 capability 如果声明了 `available` 输出，但该文件并不存在于自己的
attempt store 中，controller 会将引用标为 `missing`、追加诊断，并把原本声称的
`completed` 降为 `partial`。这里只检查 capability 明确声明的输出，不扫描整个 attempt，
也不计算文件 hash；显式的 `missing`、`not_rendered` 和 `failed` 状态保持不变。

内置 research 适配器可以通过
`research.register_research_capabilities(registry, names=...)` 注册。这个 helper
只有被调用时才加载实现，支持显式选择子集，也支持替换指定实现；它不会注册旧的
八阶段 handler，也不会创建 workflow 调度器。`plan` 适配器默认复用已有的
问题、查询和来源预算 builder 并写出 `research_plan.v1`；调用方显式传入
`use_llm=True` 和共享 client 时，才会调用 research-planner 生成并规范化模型辅助的计划。
领域专属的 `design`、`code`、`run` 实现仍由应用调用方负责，直到它们的
契约足够稳定。
`research.planning.search_request_from_plan()` 是交给 `SearchRequest` 的对应内存
适配器；它不调用 provider，也不负责检索策略。

`SessionController` 负责实际 attempt 和持久化，ResearchApplication 选择下一研究动作；
不需要第二套八阶段执行器。旧 runner 和 `core/stage_results.py` 收集器已经删除。
旧 Context、阶段契约表及无调用方的控制台事件框架已退出。capability 使用上述 artifact/session API；
历史文档读取只是只读适配，不再附带运行生命周期。
直接调用 `execute_attempt()` 时会在创建 attempt 前解析请求的 handler；拼写错误或未注册的
capability 因此不会消耗预算，也不会留下伪造的失败 attempt。
`execute_attempt()` 是新应用层使用的物理 attempt 入口：它持久化 running/result
manifest 并更新有界计数，但不选择转移，也不追加 `DecisionRecord`。旧 `execute()` 及其固定阶段
决策执行已退出，只保留一个物理执行入口；显式中断恢复也只返回执行事实，不追加研究决策。
历史决策记录仍可读取。能力测试通过 `execute_attempt()` 检查实际结果和
产物传递，研究完成、重试等决策由应用测试验证。
CodeTask 用量回调统一使用 `integrations.usage.record_usage()` 写日志、批次投影与显示摘要；
它不负责 BudgetLedger 结算，不在计划、改码、修复模块分别维护用量写入实现。
新入口可以省略 `attempt_id`；controller 会用持久化的单调序号生成可读 ID，序号允许因
崩溃或预检失败而跳号，但不会复用可能含有证据的目录。`SessionManifest` 现在保存
`revision`、`status_reason`、`next_attempt_sequence`、状态引用和可选账本引用，写出
`session_manifest.v2`；读取仍兼容 v1。v1 保持只读，避免改写历史；
`research-session-migrate` 将支持的证据导入新的应用会话，不在原地升级历史 manifest。
新应用在检查交付条件后可以调用 `pause(reason)`、`complete(reason)` 或
`continue_with_revision(reason)`。暂停会阻止下一次物理 attempt；显式继续只增加修订号，
不重置旧的 attempt 预算。core 不判断论文或实验是否科学充分，`complete()` 的交付检查
由应用层负责；状态查询和恢复不会自动触发新工作。

`DecisionRecord` 和 manifest 的 recipe 标签只保留为历史数据，不再驱动策略。
`status_snapshot()` 展示执行计数、预算和已记录的历史决策，不推断允许的下一步。
新 attempt 在 handler 启动前持久化为 running，以保留中断后的恢复位置。
如果进程级中断后 result 已经落盘，调用方可以重新加载 session，并显式调用
`reconcile_attempt()`；它根据已持久化的 result 关闭 running attempt，或幂等补齐已结束 attempt 的计数。
ResearchApplication 会先恢复缺失的状态引用再推进，避免重复调用已完成的动作。
如果没有 result，调用方必须确认中断，再调用 `recover_interrupted()` 写入明确的 failed
capability result。两者都不会自动重试、覆盖已有的 result envelope，也不会替领域逻辑选择下一步。
只要仍有 attempt 保持 `running`，controller 就会拒绝创建新的 attempt，直到调用方显式完成
上述恢复；这样可以保持单一活动 attempt 的 lineage，不会悄悄产生第二条分支。
如果调用方确实需要从较早节点比较另一条假设或修复路径，可以在
`execute_attempt()` 中明确传入 `parent_attempt_id`。父节点必须是已经收束为
`completed` 或 `failed` 的现有 attempt，研究顺序由应用负责。
默认行为仍使用持久化的当前 attempt，因此普通线性运行
不变；这只是显式 lineage 分支，不是图调度器或自动重试。
如果调用方需要为比较或恢复界面展示从根节点到某个节点的父链，可以使用
`attempt_lineage()`。它只读取 attempt manifest，不合并产物、不选择最佳结果，也不调度新
工作；父节点缺失或链路成环时会显式报错。

application 层负责给出有序 capability 序列，并显式调用
`SessionController.execute_attempt()`。精确结构化入口为
`simple_ar.app.research_application.ResearchApplication`：它保存 `ResearchBrief` 和归一化
资产，再按 `plan -> search -> document_ingest -> read -> synthesize -> summarize` 每次推进
一个有界动作，并将已接受产物写入 `SessionManifest.state_refs`。重新加载后不需要重建前面
的内存对象。`ResearchApplicationServices` 只提供这条路径实际需要的 LLM client、provider
registry 和小型资源设置，不是通用 service registry。

应用已支持研究摘要、评估/设计、显式实验与分析，以及在测量之间修改已初始化的 CodeTask；请求 report/paper 时，
canonical application 会继续执行 report_write → report → report_audit，也支持不带实验的 report-only 请求，
不会把缺失产物伪装成已经存在。`advance_session()` 和 `load_session()`
是同一入口的库函数，正式 `research-session` CLI 直接使用它。所有输入仍必须是 session 中已经存在的 artifact，
缺失 handoff 不会创建 attempt 或消耗预算。`SessionController.mutation_scope()` 是应用层
组合一次 capability、状态引用和 manifest 更新时使用的公共单写者边界，不是调度器。
持锁后会核对 manifest 是否仍与加载版本一致；旧 controller 必须重新加载，不能覆盖其他写入者的预算和引用。
重新注入服务时保留已保存的数量限制；运行配置读取失败会明确报错，不静默替换成默认值。

应用只维护一份派生 WorkPlan，以 JSON/Markdown 写入 `planning/`，包含交付状态、缺口和下一动作。
重复的 readiness 视图/文件已退出；执行受阻时仍将已完成研究标为部分进展。当请求实验或 idea assessment 时，应用会在 synthesis
之后执行有界的 `assess_ideas` capability；其 JSON/Markdown 产物记录证据引用是否可解析、相似工作风险、
未知项和面向准备度的建议，不宣称新颖性，也不授予执行权限。

初始 LLM task plan 接收一份与校验器共用的 planning boundary。默认步骤只帮助描述任务，不授予进程权限。
在 `research_design` 接受执行协议之前，已配置的准备、改码、baseline、candidate 和 repair 动作会明确标为 deferred；
survey 或没有执行配置的 research 路径则标为 unauthorized。设计完成后，只有现有执行步骤物化器及其条件能产生可执行
进程动作。这样保留明确的设计检查点，不增加第二套生命周期或权限存储。

应用只维护一份能力产物契约，供注册、正常执行与恢复复用。引用登记先解析全部声明产物再
更新状态；baseline/candidate/repair 具体角色沿用 attempt trigger，不再维护第二套产物映射。

传入 LLM client 时，候选评估使用按文档轮流选取的共同原文片段，并保存模型实际所见内容和截断信息；
回复中的候选 ID 与证据引用在此边界校验。失败时产物标为 `deterministic_fallback`，不自动推荐候选。
应用先保存摘要，再执行评估；请求 `research_design` 或实验/报告时，复用现有 `research_design`
capability 承接建议及选择理由，不另做一次模型选择。模型不推荐时保留摘要并暂停；用户可通过
`config.research_selected_idea_id` 显式选择。设计产物本身不授权执行。

已有可执行实验时，需请求 `experiment` / `experiments`，并在 `services.config["execution"]` 提供
`command`（argv 列表）、`cwd`（已存在的绝对目录）、`timeout_sec`，以及可选的 `result_schema`、`label`；
`budget_limits` 同时提供有限的 `process_invocations` 与 `process_wall_seconds`。命令来自显式用户配置，
不从模型文本推断；只请求摘要时，即使配置命令也不会启动进程。CodeTask 的 repair/retest 有界；普通显式实验
的技术失败可由调用方显式重试而不重建研究证据，科学负结果不会被静默重跑。
执行与确定性分析是独立持久化 attempt，失败进程可交付诊断但原失败状态不变；应用 completed 表示交付产物齐备，
不是实验成功。重载复用已保存测量，覆盖物理 attempt 完成但应用引用未保存的窗口。
显式资产检查和报告生命周期见下文；交付完成不代表科学结论或正文质量通过验收。

执行配置还可提供 `protocol`，复用已有 `ResearchExperimentContract` 的 `protocol_revision`、
`dataset_refs`、`split_spec`、`metric_specs` 和 `comparison_conditions` 保存对照设置；未知协议字段会
被拒绝，不静默忽略尚不支持的约束。canonical 结果将进程 invocation ID 记为 `measurement_id`，
运行 label 记为 `condition_id`，并保存声明协议和指标契约的指纹。新结果的协议不完整、不匹配或复用
同一测量时，比较保留描述性差值，但结论为 inconclusive；匹配标记为 `declared_match`，不冒充独立验证。
目前只校验显式指定文件（见下文），完整访问保护仍待完成。无测量元数据的旧结果保持兼容行为，
同时标记 `legacy_unverified`。

成对实验可添加 `execution.baseline` 并明确给出其 `command`；默认继承共有 cwd、timeout、protocol 和
指标设置，baseline 自己提供的字段覆盖默认值。顶层 command 是 candidate。应用把两侧分别作为同一
experiment capability 的独立动作执行，analysis 声明带两侧 artifact 引用的 `comparison.json`。
attempt trigger 中的 `application:baseline` 保存动作角色，覆盖结果已落盘但状态引用未保存时的恢复。
有效回退结果可用 `metric_below_target` 分析完成交付，不自动追加训练。这是一个显式对照，不等于
有限研究迭代已经完成。

修改 candidate 时添加 `execution.code_task`，提供绝对路径 `run_dir` 和明确的 `approval_note`；
candidate 的 cwd 必须是该 CodeTask 隔离工作区。保留原 task 的用户要求，并在规划前追加选定设计、
声明的执行协议及实际 baseline 指标，使用研究说明渲染函数。`research_handoff.json`
固定原始任务及所消费的研究上下文，attempt 保存最终任务 Markdown。研究输入改变不能复用旧计划，
该修订需准备新的 CodeTask run。
`implement` 复用现有规划、提案、编辑与验证，在 validate 处结束，不暗中运行 baseline 或 benchmark；
模型调用沿用 session 账本和 attempt ID，协议指定的工作区文件
并入既有编辑保护规则。产物关联设计、已有 baseline、patch 和验证证据，已完成的修改恢复时不再执行。
若要测未修改 baseline，应使用新初始化工作区；面向任意来源和复杂任务的通用资源准备、完整执行包迁移及
所有旧入口收口仍未完成；已准备工程路径的 CodeTask→实验→报告主链可运行。

旧分段 research-code-task 创建入口与桥接执行器已退出。validate_repair_patch 只做修复后
审查和静态验证，重新测量由正式 experiment 动作负责；独立 CodeTask 保留明确的修复提案
审批流程。不要在 implementation 边界内重新引入隐藏的修复/复测循环。

生成项目的审查修复不得猜测实现意图：缺失入口、配置、文档、公共接口保持为审查发现，框架
不再自动补固定实现，也不清空语法错误的包代码。模型提案复用快照和编辑校验；无模型时
保留原文件与失败审查，不通过造文件让检查通过或启动实验。

运行修复也遵循这一原则：匹配报错字符串不等于可以猜测模块新名称、批量重写 import 或替换
results 路径。实际失败交给已有的有界模型修复路径，复用快照及校验；无模型时保持未解决。

修复定位优先使用失败图路径、明确关联文件和源码匹配，其余项目文件提供有界上下文。
不再把 `runner`、`data`、`artifact` 等文件名当作职责证据。

结构化修复与整文件 content 共用动作应用器，content 先转换成 rewrite 动作。动作拒绝后
不能再触发第二次整文件覆盖；部分拒绝会回滚目标文件，成功记录保留应用器观察到的哈希与 API。

修复记录描述尝试过的编辑，不代表科研或执行成功。review/run 次数由同一记账函数维护；
后续复查负责自己的结果，不把修复记录改写为 effective/recovered，也不在 implementation
中复制修复状态。历史状态字段保持可读。

`propose_repair_edits(..., failure_evidence=RepairEvidence(...))` 可接收测量所有者显式提供的失败
报告与分析，不查找或伪造旧 CodeTask benchmark 记录；证据快照随提案保存，不应用修改、不重新测量。
外部证据必须是 failed/timed_out 执行，不能把进程成功的科学负结果当作运行故障。未提供该参数时
保留独立入口原有失败发现流程。

初始化的 `execution.code_task` 可通过 `max_repairs` 显式授权技术修复轮数（非负整数，默认 `0`）。
candidate failed/timed_out 后分别创建实现 attempt 和 canonical 复测 attempt，不重跑 baseline；
同时服从既有 attempt、模型与进程预算。原 `experiment` 失败证据不覆盖，各轮保存为 `repair_N` /
`experiment_repair_N`，analysis 用最后一次测量与 baseline 对照。达到轮数上限后交付剩余失败，不循环。
提案、审阅或验证无效时在复测前暂停。完成修复的持久化结果可恢复而不重复编辑；patch 应用内部中断
仍需检查 interrupted attempt，不能盲目重放。这只是有限技术修复，不是模型驱动研究修订或完整报告。
较长流程可能需显式提高总 attempt 上限；修复轮数不扩大其他预算。

同一显式上限也适用于**单条件科研修订**后的执行故障：接受的计划可依次安排
`repair_candidate`、`retest_candidate`，最终按已有观察进入 `reanalysis`，包括最终仍失败的情况。
技术修复读取不可变失败产物和原修订交接，复测不改既定协议。配对修订的自动修复尚不支持；
提案缺失或无效时在复测前暂停。报告输入按计划顺序保留全部已完成测量：失败只提供状态及
捕获的诊断，不能补造指标。分析使用该测量实际引用的实现版本，而非全局最新补丁。

`ResearchApplication.latest_experiment_ref()` 按应用动作顺序选择最后已有的 candidate 测量，
分析、实验交付引用和导出快照共用这一选择，避免复测后仍把首次失败当最终结果。复测尚未落盘时
仍指向上一份已记录结果，不改写任何历史引用。

已有 baseline 可用 `execution.code_task.code_root` 替代 `run_dir`：提供绝对源目录、approval_note，
以及执行 argv/timeout/protocol；cwd 可省略或等于 code_root。`prepare_execution` 在所属 attempt 内
复用 CodeTask copy 初始化器，记录复制/跳过清单，交付隔离 cwd/run_dir，原项目不修改、不执行。
准备不安装、不下载、不调用 setup hook/benchmark；baseline 继承隔离 cwd，显式等于 code_root 的
baseline cwd 也映射到副本。完成准备后恢复不重复初始化；初始化内部中断仍需检查。沿用既有复制限制，
大数据应为明确共享资产；此项不是仅数据自动生成 baseline 或论文复现准备。

仅数据的文本基线使用 execution.dataset（绝对 UTF-8 CSV 路径）和 timeout_sec，不要求用户命令或
CodeTask。固定列 text,label,split；split 明确为 train/eval，两侧非空且训练至少两个标签。当前上限
10 MB / 10,000 行，超限明确停止准备，不偷偷采样。归一化文本跨 split 重复记录为潜在泄漏限制，
不隐瞒，也不当成系统崩溃。准备保存源哈希、检查结果、归一化数据和 csv_text_classification 脚本，
协议保护数据与 evaluator；随后由普通 experiment 动作仅使用 train 训练词袋＋逻辑回归，eval 上计算
accuracy/macro-F1，BLAS/OpenMP 单线程，复用同一进程预算和分析路径。这是单一基线，不是自动候选
实现、语言适配或论文复现；其他方法可使用已有源项目准备路径。

prepared_execution 是实验的显式输入；其实测结果在 preparation 字段保存来源引用和限制，再进入
分析 audit 与 Markdown。潜在划分泄漏等信息因此能传至面向报告的交接，不改写实际指标或进程状态。

`ResearchApplication.report_inputs()` 将已完成产物投影为既有 ReportContext/ReportMemory；旧 session
也调用同一 build_research_report_inputs。保留 baseline/candidate/comparison 独立来源、最后已分析测量和
准备限制，实际执行协议优先于拟议设计。目前只是要求实验/分析的只读投影，不是报告快照、章节恢复或
Writer/audit 生命周期。

同时请求 experiment/experiments 和 report/paper 时，新应用已执行 report_write → report → report_audit。
Writer 在正式 attempt 内运行，调用模型前保存带内容指纹的 context/memory/config/template/来源快照；
组装和审计复用固定快照及 Writer memory，不刷新研究上下文。复用既有 agent/assembler/audit，默认总
attempt 上限16，资源预算独立。Writer 失败保留快照并暂停，显式继续只重试写作；完整 Writer 结果可恢复到
组装而不再调用模型。章节级 checkpoint 已在 Writer 边界持久化，canonical application 也支持无实验
report-only 请求；旧报告入口执行顺序尚未迁移。

报告 MetricSource 保留测量 ID、协议 ID/版本/指纹、条件、单位和来源类型；baseline/candidate 分别使用
自己的指标方向与协议单位。历史缺失身份保留为空并标 legacy_unverified，比较差值标为派生；指标附表显示
单位、条件和来源。这是可追溯元信息，不代表自由正文中的比较或科学结论已完成语义验证。

实现证据（patch、验证、已有 review、工作计划和研究 handoff）复制进所属 attempt，并注册为 capability
输出。`implementation.json.artifact_refs` 按 `artifact_base: "attempt"` 相对该 attempt 解析；
原 CodeTask/workspace 绝对路径仅记录来源，不再承担证据读取。复制 session 后无需原 CodeTask 目录
即可审阅这些证据，但不包含数据集、依赖环境、检查点或重新执行所需的完整源代码。

`execution.protocol.protected_assets` 接受显式 `{asset_id, path}` 文件条目，可用于数据、拆分索引和
评估器；相对路径按执行 cwd 解析，不递归扫描目录。必需文件启动前计算指纹，结束后复查，观察保存在
`measurement.asset_integrity`。变化或删除会令 `validity_status=invalid`、总体结果 failed，同时保留
真实 `execution_status`、退出码和指标；guard 给出 `protected_asset_changed`。即使声明协议相同，
跨运行的受检文件内容不同也不能作同条件提升比较。它是指定文件的审计，不是 OS 写保护、访问隔离，
也无法发现最终快照前已恢复的临时修改。哈希按块读取，时间不计入子进程墙钟预算。

应用 session 持久化会话级 `BudgetLedger`，并在 manifest 中保存引用；传入标准
`LLMClient` 时会创建绑定该账本的副本。下游调用沿用同一会话的累计用量，恢复不重置额度。

LLM 超时后，账本记录已知请求次数；有输出上限时保留 token 预留作为保守估计，实际用量仍标为未知，
剩余额度内可以重试。没有上限的未知用量仍会阻止继续消费有限的 token 预算。

attempt manifest 中的输出路径相对于各自的 attempt 目录。若后一个 capability 需要读取前一个
能力的输出，应使用 `SessionController.attempt_output_refs()`；它会返回例如
`attempts/attempt-001/result.json` 这样的 session 根目录引用，不复制文件，也不替调用方
选择最佳 attempt。这样跨能力交接仍然是显式的，也不会把相对路径解析到错误的 store。
如果一个 capability 有多个领域输出，应使用
`attempt_output_ref(..., kind=..., schema=...)` 精确要求一个 artifact，而不要依赖输出顺序；
kind 不唯一时会明确失败。

`LifecycleProfile` 提供四个可选的内置能力范围：`research_brief`、`survey`、
`experiment` 和 `full_research`。session 使用这些名称之一时，
controller 会在执行前拒绝超出 allow-list 的 capability。这只是
范围校验，不是自动工作流，也不强制规定起始能力；无法识别的旧 profile 仍保持
不加范围限制，以兼容历史调用和实验。
新建 session 如果使用已知 profile 但没有显式传入 `BudgetState`，默认 attempt 预算为该
profile 的能力数量加两次有界恢复机会。显式预算始终优先；旧 manifest 继续使用其中保存的
计数和限制；v1 保持只读，显式迁移创建独立应用会话，保留原历史目录。
attempt 可以继承 session 的 profile，也可以省略 profile；但不能把已有范围的 session
悄悄改成另一个 profile。
下一动作由应用展示，Core 不再推断研究顺序。
内置 capability 名称就是实际阶段边界：`plan`、`search`、`document_ingest`、`read`、
`synthesize`、`research_design`、`experiment`、`analysis`、`report` 和 `report_audit`。
`analyze` 仍是 `analysis` 的旧别名。`research_brief` 是应用/profile 名称，不是隐藏的
组合 capability；任意自定义名称只适合未设置范围的旧 session 或兼容场景。
内置 profile 的有序 `capabilities` 元组只作为范围和文档参考，不是隐式执行计划。
调用方仍需提供 capability 输入，并在需要时选择白名单内的回退路径。

最小的端到端参考实现位于 `tests/fixtures/capability_package_minimal/`。可以运行
`uv run simple-ar-checks core` 做离线验收。新的能力开发应先遵循这组边界，
具体的 request/result schema 放在对应领域模块中，不要继续膨胀 core。

每个由 controller 管理的 attempt 会在 manifest 中记录 capability，并保存一个
`capability_result.json`，其中只包含
`CapabilityResult` 的状态、输出引用、诊断、usage 和 provenance。它用于进程结束后恢复
失败原因和结果边界，不复制全文或原始日志；旧八阶段不会因此改变产物布局。

旧 research facade 和 `pipeline_stages/` 源码包已删除；research 领域模块负责研究行为。
历史产物读取与执行路径分离保留。

CLI 代码按职责拆分：

```text
src/simple_ar/cli/
  parser.py  argparse 命令与参数声明
  main.py    命令分发与用户可见输出
```

旧阶段 registry、handlers、common 和 import aliases 已退出。

顶层实现模块已经收束到领域包中。新代码应直接从 `core/*`、`app/*`、
`integrations/*`、`research/*`、`experiment/*`、`report/*` 或 `code_task/*`
导入，不再重新引入宽泛的 compatibility facade。

Research 代码按 evidence 生命周期分组：

```text
src/simple_ar/research/
  planning/    research questions 和可执行 query plans
  sources/     source plan contracts 与 connector-neutral query objects
  connectors/  OpenAlex、Semantic Scholar、arXiv、本地文件 adapters
  documents/   document records、full-text hints、parser/extractor helpers
  store/       chunks 与本地 index backends
  evidence/    retrieval screening、coverage、可选 debug evidence cards
  outputs/     search-stage artifact writers
```

新的检索、全文、证据和 card 能力应放入这些包中，不再回到旧的 `research/*.py` 平铺结构。

### 替换检索 Provider

`research.sources.base` 定义了轻量 provider 接口：`SearchQuery -> SearchResponse`。
`research.sources.registry.SearchProviderRegistry` 只负责显式登记和构造 connector；
查询规划、去重、缓存和阶段产物投影仍由 search stage 负责。默认 pipeline 保持兼容，
库调用方可以向 `execute_search` 传入 `provider_registry=`，也可以登记新的 source 名称，
而不必改动这些策略。新的 provider 应只负责访问数据源并返回规范化的 `Paper` 对象，
不要写 run 产物，也不要自行决定研究覆盖范围。

### 使用独立 Search 边界

`research.sources.capability` 为库调用方提供最小的多来源检索入口：

```python
from simple_ar.research.sources import (
    SearchRequest,
    default_search_provider_registry,
    search_sources,
)

result = search_sources(
    SearchRequest(
        queries=("research topic",),
        providers=("openalex", "arxiv"),
        max_results_per_query=5,
    ),
    registry=default_search_provider_registry(),
)
```

结果会保留每个 provider/query 组合的一条响应，并用
`completed`、`partial`、`empty` 或 `failed` 区分可用结果、部分来源失败和成功但为空的检索。
这个边界不写 stage 文件，不负责候选选择、去重或全文下载；这些仍由现有 Search stage
及其调用方决定。
如果调用方需要 session handoff，可以使用 `run_search_capability()`，将相同的规范化论文
行以及 provider/query 响应元数据写成一次 attempt-local 的 `search_result.json`。部分结果或
空结果会保持为非完成状态；该适配器不会额外加入重试或候选选择策略。
之后可以使用 `SearchResult.from_handoff_dict()` 在不访问网络的情况下恢复
`search_handoff.v1`；如果响应引用了不存在的论文元数据，它会保留为诊断信息。

### 复用 Document Ingest

`research.documents.ingest.build_document_bundle()` 是当前文档元数据、受许可的全文处理、
section 和 chunk 之间的最小组合边界。它复用现有 research record，不调用 LLM，也不直接写
阶段产物。Search 仍负责索引持久化和旧 JSON/JSONL projection；当前调用方传递 typed bundle 或其
handoff 表示。`_legacy.documents.load_search_document_bundle(search_dir)` 显式读取历史 Search
目录的 JSON/JSONL，不构造运行时 Context，也不推进历史流程。

`research.documents.ports` 提供 manifest 选出本地资源之后使用的轻量
`DocumentResolver` 和 `DocumentParser` 边界。`build_local_document_bundle()` 是直接从本地
文献进入 Read 的入口；它复用现有 bundle、section、chunk 和 Read 逻辑，不运行 Search。
默认 resolver 和 parser 保持现有本地/缓存行为，调用方可以注入其他存储或文档服务的实现。
`research.documents.LocalDocumentParser` 是现有纯文本、HTML、可选 PDF 和
`unstructured` 路径的可复用默认实现。旧的 extraction helper 仍委托给它，因此替换 parser
不需要改动 bundle 构造或旧 Search projection。
`DocumentBundle.to_handoff_dict()` 与 `from_handoff_dict()` 定义可恢复的
`document_bundle.v1` 表示。如果需要由 session 管理文档摄取，可以使用
`run_document_ingest_capability()`：它只在 attempt 目录写入一份 `document_bundle.json`，
并通过 attempt manifest 暴露；后续 Read 可以在另一个进程中显式恢复这份 bundle，不需要重新
下载，也不会复制到其他 stage 产物。

### 复用 Read 边界

`research.evidence.reader.ReadRequest` 接收 `DocumentBundle` 以及可选的文档或论文标识；
`read_documents()` 返回 typed evidence cards 和诊断信息，不写入文件。默认确定性；显式
`use_llm=True` 并提供 client 时可进行有界筛选、笔记与缺口补读。现有的
`write_read_card_artifacts()` 仍作为该边界的兼容 projection，因此阶段产物路径和旧调用方保持不变。
如果需要由 session 持有这次结果，可以使用 `run_read_capability()`，它把相同的 cards 和来源位置
写成一次 `read_result.json` handoff；不会复制 chunk 原文、下载文档或扩大选择范围。
生成和恢复 Read 结果时都会校验 card 声明的 `evidence_refs` 是否能在同一份
`DocumentBundle` 的 chunks 中解析；缺失引用会留下诊断并将结果降为 `partial`。也可以直接调用
`validate_read_evidence()` 做同一项无副作用检查。该校验只检查显式引用，不扫描文件或判断
引用内容的语义正确性。
`query_evidence()` 是 P05a 的来源解析边界：给定文档或明确的 chunk ID，它返回带来源身份、内容版本、精确位置、
提取状态、目标原文和同文档真实相邻上下文的 `EvidenceRef`。未知 ID 会显式报错；相邻 chunk 不能替代缺失的目标引用。
Read handoff 的 `source_spans` 使用该投影但不复制原文。模型笔记保留有界概览覆盖、
缺口查询与返回窗口，不认证完整阅读。新解析可将混合大小写字母附录与 references 分开，
正文交叉引用、作者缩写不作标题。共用 `research.store.retrieval` 用正文词和保存的章节
标题导航，按相邻不同查询词选择精确窗口，保持词/数字边界；标题、图注和精确短语
只是导航线索，不认证主张。旧 bundle/笔记不回写，重新解析另存；词面可达性不代表
论文身份或语义正确。
基础PDF默认全部页面，显式正整数页数上限仍生效。可选 `ParsedDocument.coverage`
进入原record/extraction元数据、Reader诊断及Writer限制。解析成功仍可能截断或有空文本页，
不当OCR或完整理解；旧两参数外部parser及历史bundle保持未知覆盖，不新建状态层或追认。

### 复用 Synthesis 边界

`research.synthesis.SynthesisRequest` 接收 research pipeline 已经组装好的 expanded evidence pack；
`synthesize_evidence()` 返回有上限的 `IdeaCandidate`、`NoveltyCheck`、可选的
`ExperimentContract` 以及证据缺口摘要。默认路径是确定性的且不写入文件；调用方显式提供
`SynthesisRequest(use_llm=True, llm_client=...)` 时，会保留结构化推导并增加有证据依据的模型文本。
阶段级 policy 仍负责持久化和更大范围的写作流程。现有 synthesis artifact writer 已通过这个 facade 进行结构化证据推导，
原有阶段产物路径保持不变。持久化的 compact pack 只保存 card 引用，因此调用这个边界前应先恢复
对应的 card rows。
`run_synthesis_capability()` 是 session 适配器：它接收调用方提供的 expanded pack，将完整的有界
方向 handoff 写成一次 `synthesis_result.json`，不会读取私有阶段路径，也不决定是否应该运行实验。
`SynthesisResult.from_handoff_dict()` 可以在不访问网络或调用 LLM 的情况下恢复这个
`synthesis_result.v1` handoff，包括 idea、novelty check 和可选的 research-level 实验契约。

需要注意，`research.contracts` 中的 research-level `ExperimentContract` 描述有证据依据的假设和
拟议改动。无消费者的旧设计包、运行配置转换和domain profiles已删除；执行设置属于当前experiment请求，
实现要求属于CodeTask契约，不再另建第二套设计包。
`ResearchExperimentContract.from_row()` 可以恢复 research-level handoff，
`ExperimentRequest` 同时接受这个 typed 对象和历史上使用的 mapping；canonical execution
result 会保留该契约，不重建已退休的设计包。
纵向 fixture 会把恢复出的契约显式传给 `ExperimentRequest`，因此执行结果记录的是真实的
research-to-experiment 交接，而不是从私有阶段目录重新猜测假设。
如果调用方传入 typed research contract 但没有 execution result schema，契约声明的 metric
名称会形成供下游分析使用的最小 expected-metric 视图；显式 execution schema 始终优先，
历史 mapping 输入保持原有行为。
如果要把这条 handoff 交给 standalone Experiment，使用
`experiment_request_from_synthesis()`。它只恢复 `synthesis_result.v1`、转移其中已有的
research-level contract，并要求调用方显式提供 `RunRequest`；不会因为 handoff 存在就批准
`needs_review`、选择命令、执行、重试或决定下一阶段。

### 组合 Research Brief

`research.brief.build_research_brief()` 是一个小型内存便捷组合：它先调用 Read 边界，
再把返回的 evidence cards 交给 Synthesis 边界。它接受 Search 产出的 `DocumentBundle`、
缓存文档或本地文献 bundle，返回 `ready`、`partial`、`needs_review` 或 `empty`，不会把
metadata-only 输入伪装成充分证据。它不搜索、不写文件；默认使用确定性 synthesis，调用方
显式提供 `use_llm=True` 和共享 client 时才增加有证据依据的模型文本。

面向用户的 `research-brief` 和 `research-session` 应用现在会显式持久化独立的
`read-001` 与 `synthesize-001` attempt，不再把这两个阶段藏在一个组合 attempt 中。
聚合 helper 仍供库调用方和旧的 `research_brief.v1` handoff 使用，但它不再是另一条
lifecycle 阶段。

这里刻意不提供这个聚合的默认 session 适配器。session 必须分别持久化 `read` 和
`synthesize` attempts；只需要内存值的库调用方可以使用 `build_research_brief()`。历史
`research_brief.v1` 文件仍可读取，但不会再作为第二条可执行 lifecycle。

对于多 attempt 的组合，使用 `ReadResult.from_handoff_dict(..., bundle=...)` 恢复持久化的
Read 结果，再用 `evidence_pack_from_read()` 形成最小的 Synthesis 输入。显式传入 bundle
是有意的：source text 只保留一个 owner，而论文选择和执行顺序仍由调用方决定。
下游也遵循同一规则：调用方先恢复持久化的 analysis handoff，从实际观测结果中组装报告段落，
再交给 standalone report assembler；assembler 不会根据输入引用自行推断或编造分析数值。

### 复用 Experiment 边界

`research.experiment.ExperimentRequest` 在现有执行 `RunRequest` 外增加可选的结果 schema、
contract、artifact、comparison 和 guard 元数据。`run_experiment()` 使用已有的
`ExecutionBackend` protocol，默认采用 `LocalExecutionBackend`，返回现有的 `RunResult` 以及
统一规范化后的 canonical results。它不写文件，也不决定实验如何分析。需要一次完成“执行后
分析”时可使用 `run_and_analyze()`：它复制 `AnalysisContext`，加入实际观测到的指标和
canonical 执行记录，再委托现有的 result-analysis service。失败和超时仍会作为分析输入保留；
该组合入口不负责重试、repair 或阶段决策，只有明确传入输出目录时才持久化分析产物。因此
code-task 仍然是一个 backend，而不是又一套实验 API。
如果 request 带有 primary 或 required metrics，组合入口会把这些要求转换为分析所需的最小
视图，调用方不必在 context 中重复填写。

分析模块负责自身证据与审计产物，不反向维护 CodeTask 修复状态；无调用方的
`record_result_analysis_memory` 桥接已退出。CodeTask 摘要展示已记录的结果和修复说明，
不再根据负结果、旧失败文件或缺少 memory event 推导另一套 blocker。

`research.experiment.run_experiment_capability()` 是执行边界的可选 session 适配器。
调用方自行注册名称后，它会把现有 canonical result 写成 `results.json`，并把捕获到的
stdout/stderr 以同一 attempt 下的 `execution/stdout.txt` 和 `execution/stderr.txt` 产物保存。
这些日志通过 attempt manifest 显式声明，可供诊断或其他下游 capability 读取。所有非 passed
的执行仍映射为 failed capability result；结果中保留 `passed`、`failed` 或 `timed_out` 的
真实状态，因此 session 层不会把超时误认为实验成功。分析仍是独立 capability，
`research.analysis.analyze_experiment_capability()` 可以显式读取这个结果引用，并写出带有
execution ref 的单个 `analysis.json` handoff。它只在分析状态为 `passed` 时返回
`completed`；缺少执行/指标证据返回 `partial`，明确失败或阻塞则保留对应状态，避免
session 层把“写出了分析文件”误认为“分析已经通过”。下游需要跨进程恢复时可使用
`AnalysisHandoff.from_handoff_dict()`；它只恢复 execution ref、原始执行状态和
`AnalysisResult`，不会重新运行实验或复制执行产物。
该适配器还复用现有的 result guard 和 diagnosis 实现，在同一 attempt 中写出
`guard_report.json`、`diagnosis.json` 和精简的 `diagnosis.md`；guard 出错时 capability
会失败，但 canonical result 仍保留底层真实执行状态。它不会隐式 retry、repair 或选择研究转移。

### 复用 Result Analysis 边界

`research.analysis.AnalysisRequest` 和 `analyze_results()` 提供独立的结果分析入口。
它们复用现有的 metric normalization、claim grounding 和 audit 实现；默认使用确定性分析，
只有显式传入 `output_dir` 才会写入产物。这个边界不自行创造指标、不运行代码，也不决定研究流程转移。

如果调用方已经有两个 canonical execution result，可以使用
`research.analysis.compare_experiment_results()` 生成精简的
`experiment_comparison.v1` mapping。它只比较两次结果共有的数值指标，使用显式提供的方向
或结果 schema 中的方向，保留执行状态变化；证据缺失或方向无法确定时返回
`inconclusive`。这个 mapping 可以放入 `ExperimentRequest.comparisons`，但该 helper 不会
重试、选择所谓最佳结果，也不会决定 session 的下一步转移。

`AnalysisResult.status` 是与之配套的证据状态摘要。它只根据显式的 canonical execution
记录、guard、required metrics 和显式 comparison verdict 确定，取值为 `passed`、`failed`、
`blocked`、`incomplete` 或 `metric_below_target`。没有 execution record 的独立分析仍保持
`incomplete`；这个状态不会替调用方选择 retry 或研究阶段转移。请求持久化时，同一份精简
状态也会写入 `analysis_status.json`。

应用直接消费类型化的分析结果和执行证据，不再通过适配器接入另一套 Core 科研策略。

### 复用 Report Figure 边界

`report.ports.FigureRenderer` 是报告视觉输出的最小替换点。
`DeterministicFigureRenderer` 包装现有 SVG 实现，作为 report assembly 默认实现。
调用方可向 `assemble_report_document(..., figure_renderer=...)` 或对应
`run_report_capability()` 传入 renderer；旧 stage service 已退出。
未来的图像或图表 backend 可以实现同一个 render 方法，消费已有的 `ReportDocumentPlan`、
`ReportFigureConfig` 和 `ReportFigureResult`，无需改动 writer、citation audit 或报告组装逻辑。

### 使用 Report 组装边界

`report.capability.ReportAssemblyRequest` 和 `run_report_capability()` 为已经得到 section
draft 的调用方提供下游报告边界。该适配器复用 `assemble_report_sections()`、现有标题编号策略
和 `FigureRenderer` port，在 attempt 目录写出一个 `report.md`；renderer 报告的每个图文件也会
登记为同一 attempt 的 `figure` 输出引用，figure manifest 仍只是便于阅读的索引。如果 renderer
报告了但实际没有生成文件，能力会返回 `partial` 和 `missing` 引用，不会伪装成完成。它不选择
大纲、不调用 LLM、不修改正文，也不做 citation audit；这些仍由独立的上层编排或审计能力负责。
这样可以明确连接 report 到 audit，同时不复制 writer，也不改变旧 report stage。

`report.audit.ReportAuditRequest` 和 `audit_report()` 提供对应的无副作用审查边界。
它们复用现有 citation、metric 和 claim 检查；报告写作与修订仍由上层编排负责。
`ReportAuditCapabilityRequest` 和 `run_report_audit_capability()` 是可选的 session 适配器：
调用方显式传入报告/正文 artifact 引用、typed report context 和 memory，适配器在当前 attempt
中写出一个 `report_audit.json`。warning 映射为 partial，failed 映射为 failed；它不会隐式
重试、改写报告，也不会自行寻找所谓的“最新”产物。

## 添加 Canonical Capability

新的 research 能力应进入 capability/session 主线，而不是继续写入历史八阶段实现。
建议按以下顺序添加：

1. 在所属领域包中定义 typed request/result 和稳定的 handoff schema。
2. 让领域行为独立于 CLI 参数、`Context` 和 run 目录扫描；外部副作用放到显式 adapter
   或 port 后面。
3. 添加 session adapter，在一个 attempt 目录中写出产物，并把领域状态映射为
   `CapabilityResult`。
4. 在对应 capability registry（例如 `research.registry`）中注册，再由 `app/` 用例以
   显式输入、输出、预算和 transition 进行组合。
5. 按需要补充 contract、application、失败/恢复以及 CLI/example 测试。

Canonical capability 使用显式 artifact 引用和紧凑 handoff，不再引入已退出的 stage registry
或第二套生命周期。`Stage` 与隐式目录搜索暂用于历史兼容。

## 实验准备与执行

准备好的代码通过 `research.experiment` 与 `experiment.execution.backend` 测量，CodeTask
负责隔离改码和验证。旧 Context design/code/run 执行器及阶段归档 helper 已退出，不再扩展。

`research.preparation` 在支持的小数据场景复用 CSV 文本分类模板。新增准备能力必须对应明确
输入契约和有界真实用例，不为凑齐所有实验类型扩展通用项目生成器；未知指标保持未知。

历史实验读取和当前 bridge 仍保留；其中重复的生命周期是待清理债务，不是新的扩展位置。

## 扩展 Code Task

Code-task workflow 现在按生命周期分包，而不是把所有文件平铺在同一层：

```text
src/simple_ar/code_task/
  runtime/        config、path 和 manifest helpers
  workspace/      copy、git worktree、sparse-copy 准备
  analysis/       codebase index、repo map、locate、context packs
  editing/        work plan、patch plan、edit budgets、patch proposal/application
  execution/      environment probe、validation、benchmark、comparison、repair
  orchestration/  init 和 execute 这类组合下层模块的流程
```

新增 code-task 能力时，应放入拥有该行为的生命周期包中。除非是 public facade 或真正跨切面的边界，不要继续在 `code_task/` 根目录新增平铺文件。

新增 code-task 功能时：

- 原始 source directory 必须保持只读；
- artifacts 写入 `code_task/meta`、`code_task/run` 或 `code_task/repairs`；
- 底层行为稳定前，CLI 步骤应保持显式；
- 适合时同时测试 library function 和 CLI path；
- 优先使用小而可组合的函数，不要过早塞进单个 agent loop。

Metric comparison 应保持保守。未知数值指标可以记录 delta，但除非 manifest 显式配置方向或命中简单本地 heuristic，否则不应用于 improved/regressed verdict。只有当指标命名约定足够常见、不令人意外时，才添加新的默认 heuristic。

## 扩展 Report 与 Audit

`report/survey.py` 保留来源分配与已有综述章节元数据的读取。旧服务失去消费者的契约构建、
分类生成、规划文件写入和独立覆盖审计已退出；扩展应进入现用 Writer/document-plan 与
report/audit 边界，不恢复第二套流水线。历史 `survey_contract` 上下文仍可读，但旧构建器专属的
runtime 开关和 longform `planning_artifacts` 配置已删除。

Report system 是承接 research-only survey、experiment report 和 embedded
code-task result 的出口。它应该保持 template-driven 和 evidence-aware，
不要退回到单个大 prompt 或单个大 service 文件。

```text
src/simple_ar/report/
  schema.py        context、memory、tools、draft、review 的 Pydantic models
  projection.py    将持久化研究与测量证据投影为报告输入
  templates.py     加载 Markdown 模板和 reviewer criteria
  memory.py        紧凑 section plan、evidence handles、claims、limitations
  tools.py         report tool schema definitions
  tool_gateway.py  有边界的只读 tool execution
  retrieval.py     source-handle backtracking
  agent.py         Writer/Reviewer orchestration
  citations.py     citation key 映射、显示标签和 citation cleanup
  audit.py         citation、metric、claim 和 reviewer audit 汇总
  assembler.py     section drafts 组装为最终 Markdown
  writing.py       controller 管理的 Writer 执行和检查点
  capability.py    报告组装和 artifact packaging
```

新增 report 行为时：

- schema 放在 `schema.py`，不要在 Writer 中堆自由 dict；
- source projection / backtracking 放到 `projection.py`、`retrieval.py` 或
  `tool_gateway.py`；
- Writer/Reviewer loop 行为放到 `agent.py`；
- citation 映射、显示转换和 cleanup 放到 `citations.py`；
- 机械一致性检查放到 `audit.py`；
- 模板和审查标准放在 `templates/report/`，不要硬编码成长 prompt；
- Writer 执行放在 `writing.py`，报告组装放在 `capability.py`。

来源定位不是语义判定。通过但仍请求证据的审阅，必须消费补读结果并再判才能采纳；
未解决请求保留为可见核查缺口。检查点与格式恢复 prompt 不能丢取回的证据，
上下文限额优先保留最近结果；不增加第二套来源索引或论文/任务特判。

`app/research_report.py` 现仅投影历史证据，不再执行 Writer 或固定 report/audit attempt。
`app/research_application.py` 负责 session 决策，`cli/main.py` 负责用户入口分发；二者职责应保持分开。
优先删除重复状态和执行 owner，而不是把复杂度分散到更多文件。

## 扩展 Tools 和外部 Agent Backend

common tool 与 handoff 层提供可选边界，但不替换已有领域实现：

```text
src/simple_ar/tools/
  specs.py        CommonToolSpec、ToolCall、ToolResult、permission/risk enums
  registry.py     把 report 和 experiment tools 组合成统一 registry
  gateway.py      带权限检查的本地 dispatch 和紧凑 trace 写入
  permissions.py  read/write/execution/network policy checks
  openai_schema.py / mcp_schema.py
                  只导出 schema；默认不启动 server
  mcp_server.py   显式启动的 stdio MCP server，只暴露 run-local 只读 tools

src/simple_ar/agent_backends/
  base.py         AgentBackend protocol 和 run result models
  policy.py       写入 handoff 的外部 agent permission policy
  handoff.py      workspace-scoped handoff package 和不可信输出收集
  factory.py      fake/local_llm/Codex/Claude/OpenCode 的 provider selection
  fake.py         deterministic backend，用于集成测试和 dry-run
  local_llm.py    LLM-backed bounded reviewer/planner backend
  external_cli.py subprocess wrapper，包含 cwd、timeout、env allowlist 和日志
  profiles/       Codex / Claude Code / OpenCode profile Markdown
```

这个 common layer 刻意保持很薄。`experiment/tools/` 和
`report/tool_gateway.py` 仍然拥有具体业务逻辑；`tools/` 只负责给未来
OpenAI tool calling、MCP adapter 和外部 agent backend 提供统一、可审计的出口。

新增 tool/backend 时：

- 只注册真实可用的工具；不要为了展示 MCP/OpenAI schema 添加 stub tool；
- experiment 工具目前只读历史编号阶段目录，不是正式 session 结果 API；无实现的运行/修复/应用占位工具
  和重复的实验专用 OpenAI 导出器已退出，使用统一 schema 导出；
- write、shell、network、secret access 默认关闭，除非配置和审批路径明确开启；
- 外部 agent 上下文写入 `agent_handoff/<name>/`，默认不要写用户全局工具目录；
- 外部 agent 输出一律视为不可信。先收集到 `agent_outputs/<name>/`，再交给已有
  patch、result guard、report audit 或 code-task validation 路径；
- 外部 CLI provider 必须保持 opt-in。`fake` 和 `local_llm` 可用于测试与本地 review；
  `codex`、`claude_code`、`opencode` 和 `external_cli` 必须等配置显式允许后才能启动；
- trace 默认保持紧凑。raw prompt、raw output 或大 payload 只能在 debug 设置下保留。

### Code-Task Environment Policy

当前 code-task runner 通过 `copy`、`git_worktree` 或实验性 `sparse_copy` 提供 workspace isolation，并支持 command timeout、可选 benchmark output streaming、stdout/stderr 捕获、受限 environment map 和显式 execution interpreter policy。它支持 `current` 和 `external`，但还不会创建或安装到单独 Python environment。除非未来功能明确改变这一点，否则不要默认把用户项目依赖安装到 SimpleAutoResearch 自己的 `.venv`。

环境支持应分层演进：

- `current`：使用当前 SimpleAutoResearch Python。简单，适合 demo，但不是依赖隔离。已支持。
- `external`：使用用户提供的 Python 或 Conda interpreter。是真实项目已有环境的第一逃生口。已支持。
- `project-venv`：在 `code_task/.venv/` 下创建 per-run 环境。隔离好，但可能浪费磁盘。计划中。
- `shared-env-cache`：在 `.simple_ar_cache/envs/<env-hash>/` 之类的缓存目录下创建或复用环境，按 OS、Python version 和 dependency files 哈希。长期推荐方向。计划中。
- `docker`：在容器内运行以获得更强隔离。应与 Python runner 分离，因为 Windows、GPU 和 image build 行为需要谨慎处理。计划中。

未来任何环境创建或依赖安装都必须显式、可审计，并记录到 artifacts。安全实现应记录 selected mode、interpreter path、dependency files、install commands、exit codes 和 warnings 到 `code_task/meta/environment_report.json` 或专门 environment artifact。

## 文档规则

文档分工：

- `README.md` / `README_zh.md`：项目入口、安装、quickstart、workflow 概览和链接。
- `docs/USAGE.md` / `docs/USAGE_zh.md`：安装、环境变量和 workflow walkthrough。
- `docs/CLI_REFERENCE.md` / `docs/CLI_REFERENCE_zh.md`：命令组和参数表。
- `docs/CONFIG_REFERENCE.md` / `docs/CONFIG_REFERENCE_zh.md`：TOML schema 和配置示例。
- `docs/WORKFLOWS.md` / `docs/WORKFLOWS_zh.md`：每个 workflow/stage 做什么，以及产物结构。
- `docs/DEVELOPMENT.md` / `docs/DEVELOPMENT_zh.md`：贡献者指南。
- `CHANGELOG.md` / `CHANGELOG_zh.md`：按日期倒序记录用户可见变化、兼容与迁移提示。
- `MDfiles/`：私有或学习型规划笔记，通常不提交 GitHub。

修改所属章节并沿用已有文风，不在开头追加进度说明。操作步骤、可运行示例和接口契约有各自用途，不能只为缩短篇幅删除。CHANGELOG 每个日期只出现一次，相关变化在日期内归并；内部调试与验收细节留在维护记录。

英文文档应链接到对应中文版本；中文文档内部链接应优先指向中文版本。

## 测试

开发时优先使用分层 checks：

```bash
uv run simple-ar-checks --list
uv run simple-ar-checks quick
uv run simple-ar-checks intake
uv run simple-ar-checks data
uv run simple-ar-checks code-task
uv run --extra examples simple-ar-checks pipeline
uv run simple-ar-checks research
uv run --extra examples simple-ar-checks code-task-examples
uv run simple-ar-checks core
```

也可以不用 console script，直接运行脚本入口：

```bash
uv run python scripts/run_checks.py code-task
```

推荐验证层级：

| 修改范围 | 建议检查 |
| --- | --- |
| 仅文档 | `git diff --check` 加人工检查链接。 |
| 小型 parser、prompt、config、metric 或 CLI 改动 | `uv run simple-ar-checks quick`。 |
| Code-task 内部、workspace、repo-map、patching、validation、runner、repair | `uv run simple-ar-checks code-task`。 |
| 内置 code-task 示例或 benchmark 示例 | `uv run --extra examples simple-ar-checks code-task-examples`。 |
| 实验模板及其执行 | `uv run --extra examples simple-ar-checks pipeline`。 |
| Literature、产物检索、研究证据、report generation、LLM adapter | `uv run simple-ar-checks research`。 |
| Core capability boundary、registry、attempt store 和能力包示例 | `uv run simple-ar-checks core`。 |
| 输入、应用状态、候选评估 | `uv run simple-ar-checks application`。 |
| LLM 传输与计量 | `uv run simple-ar-checks llm`。 |
| 仅报告相关改动 | `uv run simple-ar-checks report`。 |
| 引导/对话及共用CLI/TOML输入 | `uv run simple-ar-checks intake`。 |
| 表格分析、图与写作交接 | `uv run simple-ar-checks data`。 |
| 本地进程控制、执行结果 | `uv run --extra examples simple-ar-checks execution`。 |
| 共享接口/架构收口或发布候选 | `uv run --extra examples simple-ar-checks all`。 |

小改动直接选择受影响的 unittest 模块或方法。组合分组时自动去重；选择 `all` 后不再重复运行其他分组。
提交动作本身不是全量重跑的理由。删除测试要确认对应行为已废弃或有保留测试实际覆盖，不能仅按数量删减。
mock 测试通过仍不能替代小型真实执行。

分组是烟测选择，不是某领域的完整验收。小改动直接运行对应模块，例如
`uv run python -m unittest tests.test_report_source_backtracking`。
`code-task` 分组覆盖 `test_code_task` 的执行及 `test_code_source_views` 的目标选择、
命名窗口、审阅和修复消费者；不要每次定位修正再建测试模块。
执行场景只共用临时根目录夹具，不把任务策略放进夹具。
整稿起草、审阅与联合修订归 `test_report_document_first`；当前正文记忆的历史和
持久输入身份归 `test_report_checkpoints`。保留各场景明确的客户端和断言，
不要每次修正再增加一个测试模块。
数据测试按职责归属：`test_table_analysis` 共管五种图的 CLI/冻结/恢复/搬迁/导入及分布包检查，
`test_table_figures` 管分布、矩阵和格式导出，`test_coordinate_figures` 管坐标、分组、关联证据与恢复，
`test_report_data_delivery` 管写作投影；新图不要再复制完整 CLI 夹具。
产品的 `table._write_analysis_delivery` 同样共管分析、写作导入和显式重建的包输出；
输入复算/验证与完成恢复仍保留各自语义。共享夹具归 `tests` 包，
命名测试和完整发现均不应要求将 `tests/` 加进 `PYTHONPATH`；标准发现命令为
`python -m unittest discover -s tests -t .`。优先断言实际结果与副作用，不锁死私有函数次数
或提示文案。可用 `uvx ruff check src/simple_ar tests --select F401,F811` 辅助静态清理，
逐项核查，不能自动删除公共导出或有必要副作用的导入。

Experiment 与 CodeTask 已复用 `core/process.py` 的输出和进程生命周期：每个流保留 200 KB 内存尾部，
指定输出目录时流式保存最多 2 MB 日志前缀，同时记录丢弃字节数。stdout 指标解析只覆盖保留尾部；
独立指标文件的统一消费仍待后续接入。canonical experiment 将 invocation/日志声明为 attempt 产物，
CodeTask 历史记录指向对应 invocation。Windows Job Object 在启动后挂接，存在启动挂接窗口；
POSIX 使用进程组，两者都不构成不可信代码沙箱。CPU/GPU/内存强制隔离未实现，元数据明确标注；
`LocalExecutionBackend(budget_ledger=...)` 与 `execute_code_task(..., budget_ledger=...)`
可共用应用账本，按 `session_id`、`attempt_id` 关联实际 benchmark invocation。执行边界预留
`process_invocations`、`process_wall_seconds`，按实测墙钟时间结算，不冒充 GPU 利用率或 GPU 小时。
`settle_process_record` 只恢复已完成记录的结算，不执行代码；环境探查/setup helpers 暂未纳入 process 计量，
它们是显式的前置输入，不是隐藏的实验动作。新 ResearchApplication 的显式执行动作已经通过该 process 边界。
`execute_code_task(..., llm_client=...)` 已将客户端传入已有项目的规划、编辑、审阅、修复，
以及 greenfield 的生成和修复。`LLMClient.for_task` 保留 provider 设置、会话账本和 attempt 身份，
附加所属任务的用量观察；角色模型覆盖不修改原客户端。生成流程的 reviewer client 已拥有观察者，
不能再重复登记。旧 experiment bridge 的准备到验证已委托正式执行器，仅保留这些步骤的显式审批、
停止状态转换和产物投影。基线进程失败先停止准备，不等同于有效的研究负结果。它的有界验证/自动修复
仍须继续削减；新应用已经调用此 CodeTask 准备路径，并将 implementation 与实际测量动作分开。
旧 bridge 的剩余行为尚未全部退出，因此不能据此宣称所有旧入口已完成替换。

必要时仍可直接运行完整测试：

```bash
uv run --extra examples python -m unittest discover -s tests
```

运行真实 code-task 示例测试：

```bash
uv run --extra examples python -m unittest tests.test_code_task_examples
```

运行执行边界测试（输出捕获、超时及小型模板）：

测试直接调用 `LocalExecutionBackend`；无消费者的 `experiment.runner` 脚本包装及
`ExperimentRunError`/`ExperimentRunResult` 别名已退出。

```bash
uv run --extra examples python -m unittest tests.test_experiment_runner
```

运行配置解析和公开 example 配置加载测试：

```bash
uv run python -m unittest tests.test_run_config
```

## 预览收尾重点

当前预览不是已完成的V2.9发布。下一阶段的验收应集中于：

- 完成从论文到有限复现的完整任务，包括准备、实测、一次实质反馈修改和便携交付。
  已准备命令或初稿PDF不能单独关闭该要求。
- 按用户要求核对概念图反馈后的含义和布局，并在同一安装候选验证跨模块使用。
  其他候选已完成的报告或图形保留原有证据范围，不拼接成新版通过。
- 查清公开与历史消费者后，同批退出被替代实现、模板和重复测试。
  权限、实际执行、未知用量及恢复行为保留；移动文件不等于降低复杂度。

命名连接认证只是部署检查，不证明长流稳定或任务质量。冻结依据完整用户成果，
不是测试数量、编译成功或多个演示的集合。

## Git 卫生

- 保持 feature commit 聚焦，避免混入无关重构。
- 不提交 `.env`、run outputs、caches 或私有学习笔记。
- README 保持简洁；详细行为放到 docs。
- 用户可见命令、artifact 或 workflow 行为变化时，更新 `CHANGELOG.md`。
