# CLI 参考

[English](CLI_REFERENCE.md) · [使用指南](USAGE_zh.md) · [配置参考](CONFIG_REFERENCE_zh.md)

本页用于查语法、参数及恢复限制。从源码目录运行可在示例前加 `uv run`。SESSION、RUN_DIR 为保存目录的完整路径；大写占位符请替换。含空格路径需加引号。所有命令接受 `-h / --help`。research／start 的 `--command ARGV...` 放最后；后续 token 均属于子命令 argv，不作 shell 解释。

## 日常命令

| 用途 | 命令 |
| --- | --- |
| 启动目标驱动任务、继续已有工作或提供反馈 | [`simple-ar start`](#simple-ar-start) |
| 列出当前可复用成果文件、限制和续接命令，不执行任务 | [`simple-ar results`](#simple-ar-results) |
| 读取进度、停止条件及资源用量，不调用模型 | [`simple-ar status`](#simple-ar-status) |
| 查看本地连接、路由及凭据是否存在，不调用 API 或测试提供商 | [`simple-ar models`](#simple-ar-models) |

## 独立工具

| 用途 | 命令 |
| --- | --- |
| 用具名 Images 连接生成或编辑一张概念图；分别保留各版本 | [`simple-ar image`](#simple-ar-image) |
| 用视觉模型检查渲染图并保存反馈，不修改原图 | [`simple-ar image-review`](#simple-ar-image-review) |
| 读取项目说明、依赖、候选入口及命名数据位置，不安装或执行 | [`simple-ar project-info`](#simple-ar-project-info) |
| 将已组装报告导出为可编辑 LaTeX，可选编译 PDF；不调用模型或实验 | [`simple-ar report-export`](#simple-ar-report-export) |
| 生成包含元数据和有限预览的成果索引，不是全文读取 | [`simple-ar inspect`](#simple-ar-inspect) |
| 用新索引对成果作词法检索；默认排除运行元数据 | [`simple-ar search-artifacts`](#simple-ar-search-artifacts) |
| 预览并删除识别出的可重建缓存，保留运行目录、证据及成果 | [`simple-ar clean`](#simple-ar-clean) |

## 高级／兼容接口

| 用途 | 命令 |
| --- | --- |
| 使用精确结构化输入或继续已有研究 | [`simple-ar research-session`](#simple-ar-research-session) |
| 为分析已就绪的历史会话生成或刷新报告 | [`simple-ar research-report`](#simple-ar-research-report) |
| 新建仅摘要会话的兼容名称；继承 research-session 参数，受下述限制约束 | [`simple-ar research-brief`](#simple-ar-research-brief) |
| 在已有 canonical 会话的简单实验失败／超时后运行一次修正的显式实验 | [`simple-ar research-session-continue`](#simple-ar-research-session-continue) |
| 从只读 v1 会话创建后继，仅导入具名小成果，不修改源会话 | [`simple-ar research-session-migrate`](#simple-ar-research-session-migrate) |
| 为显式已有项目或从零创建合同准备隔离工作区 | [`simple-ar code-task init`](#simple-ar-code-task-init) |
| 继续已有 CodeTask 的有界规划、审阅、编辑及基准 | [`simple-ar code-task execute`](#simple-ar-code-task-execute) |
| 记录对当前补丁计划的人类决定 | [`simple-ar code-task decide-plan`](#simple-ar-code-task-decide-plan) |
| 检查运行时／环境，不安装依赖或运行基准 | [`simple-ar code-task probe`](#simple-ar-code-task-probe) |
| 重建工作区仓库地图及索引 | [`simple-ar code-task map`](#simple-ar-code-task-map) |
| 对可能需要编辑的文件及只读证据排序 | [`simple-ar code-task locate`](#simple-ar-code-task-locate) |
| 根据定位文件构建有界提示上下文包 | [`simple-ar code-task context`](#simple-ar-code-task-context) |
| 生成面向批次的实现工作计划 | [`simple-ar code-task work-plan`](#simple-ar-code-task-work-plan) |
| 为一个工作计划项创建尝试／批次 | [`simple-ar code-task batch`](#simple-ar-code-task-batch) |
| 生成供人类审阅的补丁计划 | [`simple-ar code-task plan`](#simple-ar-code-task-plan) |
| 提出受控旧／新文本编辑，不应用 | [`simple-ar code-task propose-edits`](#simple-ar-code-task-propose-edits) |
| 在允许的工作区范围内应用已审阅编辑提案 | [`simple-ar code-task apply-edits`](#simple-ar-code-task-apply-edits) |
| 运行轻量静态验证；通过不是运行证明 | [`simple-ar code-task validate`](#simple-ar-code-task-validate) |
| 编辑前运行已保存的基准 | [`simple-ar code-task baseline`](#simple-ar-code-task-baseline) |
| 编辑后运行已保存的基准，并比较已有基线指标 | [`simple-ar code-task run`](#simple-ar-code-task-run) |
| 确定性汇总最近验证／基准失败 | [`simple-ar code-task analyze-failure`](#simple-ar-code-task-analyze-failure) |
| 提出有界修复编辑；另行审阅及应用 | [`simple-ar code-task repair`](#simple-ar-code-task-repair) |
| 向标准输出或文件导出已注册只读工具的 schema | [`simple-ar tools schema`](#simple-ar-tools-schema) |
| 用 JSON 对象调用一个已注册只读工具，并保存追踪 | [`simple-ar tools call`](#simple-ar-tools-call) |
| 通过 MCP stdio 提供已注册只读运行工具；不授予 shell、写入、网络或安装能力 | [`simple-ar tools serve-mcp`](#simple-ar-tools-serve-mcp) |

---

<a id="引导入口"></a>
<a id="原生任务入口实验性"></a>
<a id="原生任务恢复"></a>
<a id="恢复与反馈"></a>
<a id="补充剩余额度"></a>
<a id="切换已配置连接"></a>
<a id="重试失败的模型决策"></a>
<a id="补齐或修订合同"></a>
<a id="结构化兼容入口"></a>
<a id="已有数据分析无需-api"></a>
<a id="目标驱动的代码分析与图形"></a>

### `simple-ar start`

启动目标驱动任务、继续已有工作或提供反馈。直接 start 询问目标；普通 --kind 选择成果偏好。

```text
simple-ar start [REQUEST] [OPTIONS]
simple-ar start --resume-task SESSION [FEEDBACK] [OPTIONS]
```

#### 目标、材料与新建

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `REQUEST` | 新任务目标，或恢复时的反馈；不要同时传 --goal。 | 未设置 |
| `--task-config PATH` | 加载可编辑的 [task] TOML 新建原生任务；CLI 参数优先。 | 未设置 |
| `--write-task-config PATH` | 导出带注释的原生新建任务 TOML 后退出；不修订已有任务。 | 未设置 |
| `--source PATH` | 只读本地文件／目录挂载，可重复；恢复时确认新增材料。 | [] |
| `--guided` | 交互选择原生任务的成果偏好。 | 关闭 |
| `--kind CHOICE` | 成果偏好；精确协议参数可能选择结构化兼容路径。不授予访问权限。 可选：`survey`, `bug_fix`, `reproduction`, `writing`, `data_analysis`, `figure`, `code`. | 未设置 |
| `--from-session PATH` | 从此会话复用成果；配合 --reuse。 | 未设置 |
| `--reuse NAME` | results 列出的登记成果名，可重复；code_project 保留编辑范围／检查器。 | [] |
| `--scripted` | 配合 --kind data_analysis 使用原生代码分析；命令仍需逐项批准。 | 关闭 |
| `--goal TEXT` | 目标文本，与位置参数 request 二选一。 | 未设置 |
| `--document PATH` | 本地参考论文，可重复。普通原生入口拒绝 URL；复现 --chat 可在确认后获取公开 HTTPS 论文。 | [] |
| `--material PATH` | 笔记、草稿或分析包，可重复。普通材料未经核验，不等同文献论文。 | [] |
| `--project PATH` | 已有项目；普通目标只读挂载，显式修复／复现使用隔离副本。 | 未设置 |
| `--project-python PATH` | 已有执行解释器；复现 venv 以它为基础 Python。 | 未设置 |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | env |
| `--output-root PATH` | 新任务／运行目录的父目录。 | runs/assistant |
| `--prepare-only` | 保存／预览，不调用模型／进程；新原生任务仍检查凭据。 | 关闭 |
| `--yes` | 接受显示的新建／访问／资源摘要；不批准待执行的原生进程。 | 关闭 |

#### 访问与资源额度

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--max-processes N` | 原生累计进程次数，含检索后端；0 禁止进程。 | 没有合同设置时为 10 × max-turns |
| `--max-process-seconds N` | 累计进程实际耗时（秒）；不是单命令超时。 | 没有合同设置时为 300 × max-turns |
| `--allow-execution` | 允许提出进程命令；每条命令仍需精确批准。执行不是操作系统沙箱。 | 关闭 |
| `--allow-network` | 授权公开来源检索／获取；不授权安装软件包或任意命令。 | 关闭 |
| `--allow-vision` | 将挂载的 PNG/JPEG/WebP 渲染图发往 vision 路由；共用模型预算。 | 关闭 |
| `--max-images N` | 通过 image 路由生成／编辑图片的提交额度；与视觉检查分开。 | 0 |
| `--max-turns N` | 原生任务累计模型请求额度，含反馈。 | 30 |
| `--max-tokens N` | 原生任务累计模型 token 额度，含反馈。 | 600000 |

#### 显式结构化兼容

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--chat` | 显式结构化协议的模型辅助设置；要求终端及模型连接。 | 关闭 |
| `--resume-setup PATH` | 继续保存的结构化设置／账目；不是恢复任务执行。 | 未设置 |
| `--with-report` | 显式请求模型撰写报告；数据分析默认仅交付分析。 | 关闭 |
| `--asset-max-mb N` | 聊天中每个项目／数据的获取容量；ZIP 展开限四倍。不等同下载授权。 | 聊天设置为 20 |
| `--template NAME_OR_PATH` | 报告模板名称或 Markdown 路径。 | 未设置 |
| `--sources CHOICE` | 结构化调研策略：仅给定文档或在线来源。 可选：`materials`, `search`. | 未设置 |
| `--fulltext` | 结构化全文／PDF 授权；复现 --chat URL 仍要求确认。 | 关闭 |
| `--web-extract-backend CHOICE` | 公开网页提取后端；与 PDF 获取分开。 可选：`direct`, `tavily_basic`. | 未设置 |
| `--max-cited-sources N` | 报告引用的不同来源数量上限。 | 未设置 |
| `--validate VALUE` | 显式验证命令；严格复现将验证与正式测量分开。 | 未设置 |
| `--allow VALUE` | 可编辑的项目相对路径／glob，可重复。 | [] |
| `--hypothesis TEXT` | 固定协议复现要核查的已发表结论。 | 未设置 |
| `--dataset TEXT` | 数据说明及已接受的适配。 | 未设置 |
| `--expected-outcome TEXT` | 声明的比较标准，不是编造的结果。 | 未设置 |
| `--metric NAME` | 要求输出的指标名，可重复。 | [] |
| `--output-files JSON` | SIMPLE_AR_OUTPUT_DIR 下名称到相对文件的 JSON 映射；不推断输出路径。 | {} |
| `--metric-sources JSON` | 已登记输出指标的显式 JSON／CSV 选择器。 | {} |
| `--cwd PATH` | 声明命令的工作目录。 | 固定协议复现为当前目录 |
| `--timeout-sec N` | 单进程超时限制（秒）。 | 固定协议复现为 300 |
| `--environment CHOICE` | 使用当前环境，或显式创建复现任务本地 venv。 可选：`current`, `venv`. | current |
| `--requirements PATH` | 项目相对 requirements 文件，可重复；venv 默认使用存在的 requirements.txt，否则创建空 venv。 | [] |
| `--install-project` | 在复现任务 venv 安装项目；要求打包声明，可能执行构建代码／联网。 | 关闭 |
| `--check-argv JSON` | 测量前显式短环境检查的 JSON argv；不是科学证据。 | 未设置 |
| `--interaction CHOICE` | 设计／协议确认策略；新原生任务没有阶段／交付里程碑对话。不授予进程权限。 可选：`assisted`, `checkpoints`, `autonomous`. | checkpoints |
| `--command ARGV...` | 显式 argv；放最后，后续 token 均属于命令。research／start 的 argv 不作 shell 解释。 | 未设置 |

#### 继续、批准与恢复

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--resume-task PATH` | 恢复已有原生任务；配合 --tasks-in 也接受列表显示的精确名称。 | 未设置 |
| `--tasks-in PATH` | 列出本目录及直接子目录的原生任务，最多 256 项；不递归搜索。 | 未设置 |
| `--select-task` | 从 --tasks-in 交互选择；空输入取消。 | 关闭 |
| `--supply-task PATH` | 恢复时记录 TOML 条件或首次模型绑定，不执行；replace_existing=true 修订合同，保留证据／账目。 | 要求 --resume-task；独立操作 |
| `--retry-model` | 恢复时以剩余预算重试已终止的失败模型决策；可用 --model 改为兼容 Chat 连接。 | 关闭 |
| `--switch-model profile:NAME` | 恢复时记录 profile:NAME 或 route:NAME 文本连接变更；要求切换 ID／原因。仅记录，不执行。 | 未设置 |
| `--switch-id NAME` | --switch-model 的稳定幂等 ID。 | 未设置 |
| `--switch-reason TEXT` | --switch-model 的人类可读原因。 | 未设置 |
| `--authorize-remaining DIMENSION=AMOUNT` | 以 DIMENSION=AMOUNT 授权新的剩余额度，可重复；保留历史用量／未知预留。 | 未设置 |
| `--authorization-id NAME` | 稳定幂等 ID；相同 ID／条款不会补回已用额度。 | 未设置 |
| `--authorization-reason TEXT` | 变更剩余额度的人类可读原因。 | 未设置 |
| `--approve-execution VALUE` | 待执行命令显示的精确令牌；要求 --resume-task。 | 要求 --resume-task 及精确令牌 |
| `--decision-id NAME` | 待处理设计／协议决定的精确 ID；原生回复要求 --resume-task。 | 未设置 |
| `--decision-response CHOICE` | 回复待处理决定；接受设计不等同批准进程。 可选：`accept`, `reject`, `revise`. | 未设置 |
| `--decision-guidance TEXT` | --decision-response revise 时提供的事实或修订方向。 | 未设置 |

#### 显式表格分析

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV／TSV 或 JSON 记录。显式表格选择器选择无需 API 的捷径；仅 --kind data_analysis 不会。 | 未设置 |
| `--data-attribution TEXT` | 用户声明的来源／署名／版本；保留但不独立核验。 | 空 |
| `--value-column NAME` | 显式数值列；多指标或矩阵／坐标列可重复。 | [] |
| `--group-column NAME` | 分类／分组列；values 柱图／热图要求行标签唯一。 | 空 |
| `--observation-unit TEXT` | 每行代表什么；表格捷径要求提供。 | 空 |
| `--value-unit TEXT` | 所选数值列单位；省略时为未知。 | 空 |
| `--paired-baseline NAME` | 相同量／单位的所选观测列中显式指定同行基线。 | 空 |
| `--data-association CHOICE` | 可选折线／散点对的描述性 Pearson r；不推断显著性／因果。 可选：`none`, `pearson`. | none |
| `--data-mode CHOICE` | observations 汇总观测行；values 保留给定数值，不推断聚合。 可选：`observations`, `values`. | observations |
| `--data-missing CHOICE` | 拒绝缺失值或显式忽略；缺失 y 会断开折线。 可选：`reject`, `omit`. | reject |
| `--figure-width CHOICE` | 通用 column（3.5 英寸）或 wide（7 英寸）图宽。 可选：`column`, `wide`. | wide |
| `--data-max-mb N` | 实际输入大小上限，单位 MiB。 | 20 |
| `--data-max-figures N` | 实际 SVG 页数限制；超限报错，不丢弃类别。 | 100 |
| `--data-plot CHOICE` | bar：均值／数值；box：观测四分位／极值；line／scatter：显式坐标；heatmap：给定矩阵。 可选：`bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | values 模式折线／散点的数值 x 列；不同于数值／分组列。 | 空 |
| `--x-unit TEXT` | 声明的 x 坐标单位；省略时为未知。 | 空 |
| `--data-max-points N` | 每张坐标图实际点数上限；超限报错，不抽样。 | 10000 |
| `--series-layout CHOICE` | 分轴，或相同声明单位的折线／散点列共轴。 可选：`separate`, `shared`. | separate |
| `--data-path PATH` | 已有数据文件／目录，可重复；不改写命令或自动复制外部数据。 | [] |

普通目标、--guided 和普通 --kind 偏好使用原生任务。显式协议控制、--chat 和显式表格选择器选择各自支持的结构化路径。仅 --kind data_analysis 仍由模型驱动；显式 --value-column／--x-column／--paired-baseline 选择确定性表格捷径，除非使用 --scripted。捷径要求 --data-file、--value-column、--observation-unit；除非请求报告，否则不发起任务 API 调用。

新任务从当前工作目录读取 .env。来源挂载拒绝符号链接和私有／运行时路径；原生 data-file 偏好仍限 20 MiB。公开 URL 应写入目标并授权联网，不要作为普通 document URL 传入。

恢复保留模型、解释器及累计预算。增加资源应使用 --authorize-remaining，不是新建限额。支持的维度包括 llm_requests、total_tokens、process_invocations、process_wall_seconds、image_requests。要求稳定 ID／原因；保留历史费用及未知预留。这里只授予容量，不授予访问或批准命令。新增访问／资源须与 --approve-execution 分开确认。

--retry-model 与反馈、访问／资源授权及命令批准分开。它仅在没有已接受回复且保存的有限容量足够时重试已终止的失败决策，不重放工具。缓存回复用普通恢复。进行中请求、无界费用和未知进程副作用须先对账。--switch-model 仅记录，与反馈、重试、补齐、--model 及授权分开。

新原生任务的 assisted／checkpoints 等待设计／协议回复；autonomous 可在保存限额内接受有效设计。没有额外阶段／交付里程碑对话。接受设计不批准进程；即使 --yes 仍要求精确待执行命令令牌。

示例：

```bash
simple-ar start "解释这些结果" --source ./results --model profile:daily
simple-ar start --write-task-config task.toml
simple-ar start --task-config task.toml --yes
simple-ar start --resume-task SESSION "精简结果说明"
simple-ar start --resume-task SESSION --approve-execution TOKEN
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "完成当前任务的剩余工作" --yes
simple-ar start --kind data_analysis --data-file measurements.csv --value-column score --observation-unit "一次独立运行" --yes
```

#### 补齐已有任务的配置（高级）

仅在已有任务缺少配置时使用此操作。补齐操作保存设置，之后再恢复任务。以下示例为任务补齐模型连接：

```toml
supply_id = "first-model"
reason = "继续已有工作"

[changes]
model = "profile:daily"

[resource_allowances]
llm_requests = 20
total_tokens = 600000
```

补齐要求 supply_id、reason、[changes]；来源别名相对补齐文件解析。默认补缺失合同；显式 replace_existing = true 修订已有合同，替换 sources 要求完整别名映射。保留证据及费用。新协议可能需重新接受；进程另行批准。补齐与反馈、重试、CLI 授权及命令批准分开。

```bash
simple-ar start --resume-task SESSION --supply-task supply.toml --prepare-only
simple-ar start --resume-task SESSION --supply-task supply.toml --yes
simple-ar start --resume-task SESSION
```

<a id="成果复用"></a>


### `simple-ar results`

列出当前可复用成果文件、限制和续接命令，不执行任务。

```text
simple-ar results SESSION [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `SESSION` | 列出当前成果的会话目录。 | 必填 |

示例：

```bash
simple-ar results SESSION
simple-ar start --from-session SESSION --reuse code_project --goal "修改图例"
```


### `simple-ar status`

读取进度、停止条件及资源用量，不调用模型。续接提示不表示后台进程正在运行。

```text
simple-ar status RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |

示例：

```bash
simple-ar status SESSION
```


### `simple-ar models`

查看本地连接、路由及凭据是否存在，不调用 API 或测试提供商。

```text
simple-ar models [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--config PATH` | 本命令的 TOML 配置；显式 CLI 参数优先。 | 先 SIMPLE_AR_MODELS_CONFIG，再用户 models.toml |
| `--profile NAME` | 检查一个具名连接，不显示凭据。 | 未设置 |
| `--purpose CHOICE` | 选择 --profile 时的能力用途；列出连接不探测提供商。 可选：`text`, `code`, `writing`, `vision`, `image`. | text |

示例：

```bash
simple-ar models
simple-ar models --config models.toml --profile daily --purpose text
```

<a id="概念图"></a>

### `simple-ar image`

用具名 Images 连接生成或编辑一张概念图；分别保留各版本。

```text
simple-ar image --prompt TEXT --output PATH [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--prompt TEXT` | 图片描述或编辑要求。 | 必填 |
| `--input PATH` | 待编辑原图或待审阅渲染图；保留原件。 | 未设置 |
| `--output PATH` | 本命令的输出目录／文件。 | 必填 |
| `--config PATH` | 本命令的 TOML 配置；显式 CLI 参数优先。 | 未设置 |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 配置的 image 路由 |

每次变更请求用新输出目录。相同的已存已完成请求可恢复而不再次提交；未知请求不自动重放。每份输出旁保存单次图片请求账本。要求具名 Images 连接／image 路由。位图不是实测数据或可编辑矢量图层。

示例：

```bash
simple-ar image --prompt "科研概念图" --output figures/v1 --model profile:illustration
simple-ar image --prompt "增大标签字号" --input SOURCE_IMAGE --output figures/v2
```

<a id="检查已渲染图形"></a>

### `simple-ar image-review`

用视觉模型检查渲染图并保存反馈，不修改原图。模型意见不是科学核验。

```text
simple-ar image-review --input PATH --goal TEXT --output PATH [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--input PATH` | 待编辑原图或待审阅渲染图；保留原件。 可重复。 | 必填 |
| `--goal TEXT` | 目标文本，与位置参数 request 二选一。 | 必填 |
| `--output PATH` | 本命令的输出目录／文件。 | 必填 |
| `--config PATH` | 本命令的 TOML 配置；显式 CLI 参数优先。 | 未设置 |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 配置的 vision 路由 |

--input 可重复，最多四张 PNG/JPEG/WebP。变更输入需新审阅目录；相同的已完成输入可恢复而不再次调用。每份输出保存一次模型请求、40000 token 的预算。

示例：

```bash
simple-ar image-review --input figure.png --goal "检查标签和视觉层次" --output reviews/v1
```

<a id="项目准备"></a>

### `simple-ar project-info`

读取项目说明、依赖、候选入口及命名数据位置，不安装或执行。

```text
simple-ar project-info --project PATH --output PATH [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--project PATH` | 已有项目；普通目标只读挂载，显式修复／复现使用隔离副本。 | 必填 |
| `--data-path PATH` | 已有数据文件／目录，可重复；不改写命令或自动复制外部数据。 | [] |
| `--output PATH` | 本命令的输出目录／文件。 | 必填 |

--output 必须不存在。数据路径相对项目解析，不读取其内容。生成 preparation.md 和 preparation.json，可复用为写作材料。

示例：

```bash
simple-ar project-info --project ./project --data-path ./data --output preparation/v1
```


### `simple-ar report-export`

将已组装报告导出为可编辑 LaTeX，可选编译 PDF；不调用模型或实验。

```text
simple-ar report-export --report-dir PATH --output PATH [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--report-dir PATH` | 含 report_body.md 及引用所需文献表的已组装报告。 | 必填 |
| `--output PATH` | 本命令的输出目录／文件。 | 必填 |
| `--title TEXT` | 面向读者的出版标题。 | 未设置 |
| `--citation-style CHOICE` | 数字 natbib 显示或模板默认值；不改变来源引用键。 可选：`numeric`, `template`. | 未设置 |
| `--latex-template PATH` | 相对 --report-dir 的可信 Pandoc LaTeX 模板；否则使用 ACM。 | 未设置 |
| `--template-asset NAME` | 模板同目录的平面配套文件名，可重复；要求 --latex-template。 | [] |
| `--tex-engine CHOICE` | 自定义模板的已安装引擎；要求 --latex-template；使用 BibTeX 文献表。 可选：`pdflatex`, `xelatex`. | 未设置 |
| `--compile` | 用已安装 TeX／BibTeX 编译；失败保留源码／日志。 | 关闭 |

输出必须为新目录。自定义模板需在正文中包含 $body$，在导言区包含 $for(header-includes)$$header-includes$$endfor$。配套文件须为模板同目录的平面文件名。不自动发现依赖，不运行自定义构建命令。编译要求已安装 TeX／BibTeX；失败保留源码／日志并返回失败。

示例：

```bash
simple-ar report-export --report-dir REPORT_DIR --output publication/v1 --compile
simple-ar report-export --report-dir REPORT_DIR --output publication/v2 --latex-template layout/article.latex --citation-style numeric --compile
```

<a id="成果工具"></a>

### `simple-ar inspect`

生成包含元数据和有限预览的成果索引，不是全文读取。

```text
simple-ar inspect RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |

不跟随符号链接／特殊文件，默认不计算完整文件哈希。生成 artifact_index.json。

示例：

```bash
simple-ar inspect RUN_DIR
```


### `simple-ar search-artifacts`

用新索引对成果作词法检索；默认排除运行元数据。

```text
simple-ar search-artifacts RUN_DIR QUERY [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `QUERY` | 运行成果的文本查询。 | 必填 |
| `--top-k N` | 返回检索结果数量上限。 | 8 |
| `--include-operational` | 同时搜索 manifest 等运行元数据。 | 关闭 |

生成 artifact_index.json、artifact_chunks.jsonl 和 artifact_search_results.json。二进制文件不分块；超大文件标记说明，不视为已完整搜索。

示例：

```bash
simple-ar search-artifacts RUN_DIR "验证结果" --top-k 5
```


### `simple-ar clean`

预览并删除识别出的可重建缓存，保留运行目录、证据及成果。共享清理影响多个运行。

```text
simple-ar clean [RUN_DIR] [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 未选择共享清理时必填 |
| `--yes` | 接受显示的新建／访问／资源摘要；不批准待执行的原生进程。 | 关闭；预览后确认删除 |
| `--all-caches` | 包含所有识别出的可重建运行缓存／索引／上下文；保留证据／成果。 | 关闭 |
| `--shared-index` | 清理跨运行共享研究索引。 | 关闭 |
| `--shared-cache` | 清理共享索引、文献提供商缓存及保留的外部代理会话记录。 | 关闭 |
| `--index-root PATH` | 共享清理的索引根目录。 | SIMPLE_AR_RESEARCH_INDEX_ROOT 或 .simple_ar_cache/research_index |
| `--literature-cache-root PATH` | --shared-cache 使用的文献提供商缓存根目录。 | .simple_ar_cache/literature |
| `--allow-external-index-root` | 允许清理当前工作区之外的共享索引／缓存。 | 关闭 |

未传 --yes 时须在预览后确认才删除。仅共享清理可省略 RUN_DIR。保留当前原生证据／文档／全文／尝试和未识别文件。--all-caches 还包含成果索引／分块；历史布局可识别的可重建目标可能更多。共享清理保留运行本地成果，但可能需重新向提供商查询。

示例：

```bash
simple-ar clean RUN_DIR
simple-ar clean RUN_DIR --all-caches
simple-ar clean --shared-cache
```

<a id="research-命令"></a>
<a id="报告控制参数"></a>
<a id="参与策略与待处理决定"></a>
<a id="固定协议复现"></a>
<a id="simple-ar-research-session任务驱动的-canonical-入口"></a>
<a id="simple-ar-research-session结构化与兼容入口"></a>

### `simple-ar research-session`

使用精确结构化输入或继续已有研究。新任务共用原生任务执行入口；遵循显示的续接命令。

```text
simple-ar research-session --topic TEXT [OPTIONS] [--command ARGV...]
```

#### 目标、材料与新建

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--config PATH` | 本命令的 TOML 配置；显式 CLI 参数优先。 | 未设置 |
| `--interaction CHOICE` | 设计／协议确认策略；新原生任务没有阶段／交付里程碑对话。不授予进程权限。 可选：`assisted`, `checkpoints`, `autonomous`. | 新建 checkpoints；恢复保留策略 |
| `--topic TEXT` | 研究目标；新建时需提供或由 TOML 给出，恢复时可省略。 | 新建需提供或写入 TOML；恢复可省略 |
| `--task-kind CHOICE` | 显式结构化任务类别。 可选：`auto`, `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis`. | auto |
| `--outputs CHOICE ...` | 请求的成果；不能与 --with-report 或 --no-report 同用。 可选：`summary`, `report`, `experiments`, `bug_fix`, `code_task`, `data_analysis`. | 未设置 |
| `--material PATH` | 笔记、草稿或分析包，可重复。普通材料未经核验，不等同文献论文。 | [] |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 直接 CLI：无；TOML 通常 env；恢复保留模型 |
| `--feasibility-review-model SELECTOR` | 用于有来源依据的可行性审阅的独立模型。 | 未设置 |
| `--output-root PATH` | 新任务／运行目录的父目录。 | runs/research-session |
| `--cache-dir PATH` | 可选共享全文缓存位置。 | 会话本地缓存 |
| `--local-document PATH` | 本地 Markdown／文本参考资料，可重复。 | [] |
| `--query TEXT` | 覆盖检索查询，可重复。 | [] |
| `--provider NAME` | 覆盖检索提供商，可重复。 | [] |
| `--max-results N` | 检索结果数量限制。 | 10 |
| `--max-chunks N` | 摄取分块数量限制。 | 300 |
| `--max-fulltext-documents N` | 远程全文文档数量上限。 | 省略时不设数量上限 |
| `--max-pdf-mb N` | 每份获取 PDF 的大小上限，单位 MiB。 | 省略时不设大小上限 |
| `--web-extract-backend CHOICE` | 公开网页提取后端；与 PDF 获取分开。 可选：`direct`, `tavily_basic`. | 未设置 |
| `--idea-limit N` | 结构化想法候选数量上限。 | 3 |
| `--max-research-iterations N` | 首次分析后的新增轮数；0 表示首次分析后停止。 | 1 |
| `--cwd PATH` | 声明命令的工作目录。 | . |
| `--timeout-sec N` | 单进程超时限制（秒）。 | 未设置 |
| `--label NAME` | 声明实验的标签。 | research-session |
| `--primary-metric NAME` | 主要比较指标。 | 未设置 |
| `--metric NAME` | 要求输出的指标名，可重复。 | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION，可重复；方向包括 higher、lower、resource、ignore。 | [] |
| `--code-task-config PATH` | 实现所选方向的显式 CodeTask TOML。 | 未设置 |
| `--command ARGV...` | 显式 argv；放最后，后续 token 均属于命令。research／start 的 argv 不作 shell 解释。 | 未设置 |

#### 访问与资源额度

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--total-tokens N` | 共享 API token 预算；恢复保留账目。 | 直接 CLI 省略时不设上限；配置／恢复可能设限 |
| `--llm-requests N` | 共享 API 请求预算。 | 直接 CLI 省略时不设上限；配置／恢复可能设限 |
| `--max-output-tokens N` | 每次模型响应的输出上限。 | 未设置 |
| `--process-invocations N` | 累计进程调用额度；显式 0 有效。 | 省略时由执行入口计算；0 有效 |
| `--process-wall-seconds N` | 累计进程实际耗时额度（秒）。 | 省略时由执行入口计算；0 有效 |

#### 继续、批准与恢复

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--session-root PATH` | 已有研究会话；遵循显示的恢复命令。共用原生任务用 start --resume-task 恢复。 | 未设置 |
| `--recover-interrupted` | 仅历史会话：确认旧工作进程停止后，关闭其无结果的运行中尝试。 | 关闭 |
| `--reanalyze` | 历史会话：重新分析已有测量，与修订／额度／报告请求分开。 | 关闭 |
| `--authorization-id NAME` | 稳定幂等 ID；相同 ID／条款不会补回已用额度。 | 未设置 |
| `--authorization-reason TEXT` | 变更剩余额度的人类可读原因。 | 未设置 |
| `--authorize-remaining DIMENSION=AMOUNT` | 以 DIMENSION=AMOUNT 授权新的剩余额度，可重复；保留历史用量／未知预留。 | 未设置 |
| `--additional-attempts N` | 提高历史总尝试上限，不清除已有尝试。 | 0 |
| `--additional-no-progress N` | 提高历史连续无进展次数上限。 | 0 |
| `--decision-id NAME` | 待处理设计／协议决定的精确 ID；原生回复要求 --resume-task。 | 未设置 |
| `--decision-response CHOICE` | 回复待处理决定；接受设计不等同批准进程。 可选：`accept`, `reject`, `revise`. | 未设置 |
| `--decision-guidance TEXT` | --decision-response revise 时提供的事实或修订方向。 | 未设置 |

#### 显式表格分析

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV／TSV 或 JSON 记录。显式表格选择器选择无需 API 的捷径；仅 --kind data_analysis 不会。 | 未设置 |
| `--data-attribution TEXT` | 用户声明的来源／署名／版本；保留但不独立核验。 | 空 |
| `--value-column NAME` | 显式数值列；多指标或矩阵／坐标列可重复。 | [] |
| `--group-column NAME` | 分类／分组列；values 柱图／热图要求行标签唯一。 | 空 |
| `--observation-unit TEXT` | 每行代表什么；表格捷径要求提供。 | 空 |
| `--value-unit TEXT` | 所选数值列单位；省略时为未知。 | 空 |
| `--paired-baseline NAME` | 相同量／单位的所选观测列中显式指定同行基线。 | 空 |
| `--data-association CHOICE` | 可选折线／散点对的描述性 Pearson r；不推断显著性／因果。 可选：`none`, `pearson`. | none |
| `--data-mode CHOICE` | observations 汇总观测行；values 保留给定数值，不推断聚合。 可选：`observations`, `values`. | observations |
| `--data-missing CHOICE` | 拒绝缺失值或显式忽略；缺失 y 会断开折线。 可选：`reject`, `omit`. | reject |
| `--figure-width CHOICE` | 通用 column（3.5 英寸）或 wide（7 英寸）图宽。 可选：`column`, `wide`. | wide |
| `--data-max-mb N` | 实际输入大小上限，单位 MiB。 | 20 |
| `--data-max-figures N` | 实际 SVG 页数限制；超限报错，不丢弃类别。 | 100 |
| `--data-plot CHOICE` | bar：均值／数值；box：观测四分位／极值；line／scatter：显式坐标；heatmap：给定矩阵。 可选：`bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | values 模式折线／散点的数值 x 列；不同于数值／分组列。 | 空 |
| `--x-unit TEXT` | 声明的 x 坐标单位；省略时为未知。 | 空 |
| `--data-max-points N` | 每张坐标图实际点数上限；超限报错，不抽样。 | 10000 |
| `--series-layout CHOICE` | 分轴，或相同声明单位的折线／散点列共轴。 可选：`separate`, `shared`. | separate |
| `--data-path PATH` | 已有数据文件／目录，可重复；不改写命令或自动复制外部数据。 | [] |

#### 报告控制

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--report-writing-method CHOICE` | 新报告使用 article；显式分节控制选择 sections。恢复保留已存方法。 可选：`article`, `sections`. | 新建 article；显式分节控制为 sections |
| `--max-section-tokens N` | 每个写作单元输出上限；0／未设表示不加此上限。 | 未设置 |
| `--with-report` | 显式请求模型撰写报告；数据分析默认仅交付分析。 | 关闭 |
| `--no-report` | 模型会话跳过报告撰写。 | 关闭 |
| `--report-template NAME_OR_PATH` | 报告结构名称或 Markdown 路径。 | auto |
| `--report-reviewer CHOICE` | 报告审阅器；disabled 保留确定性审计。 可选：`llm`, `disabled`. | llm |
| `--max-review-iterations N` | 报告修订／纠正上限；失败提案也计数。 | 1 |
| `--report-document-review / --no-report-document-review` | 启用／禁用有界全文审阅／修订。 | Off（sections）；恢复保留 |
| `--report-review-scope CHOICE` | section 或 document；document 要求启用 document-review。 可选：`section`, `document`. | section（sections）；恢复保留 |
| `--report-draft-scope CHOICE` | section、联合 document 或 auto（不超过 2000 计划词数联合）；document／auto 要求全文审阅及其范围。 可选：`section`, `document`, `auto`. | section（sections）；恢复保留 |
| `--max-document-review-prompt-chars N` | 完整审阅请求字符上限；0 使用客户端／会话容量，不截断证据。 | 未设置 |
| `--report-outline-strategy CHOICE` | 模板提纲或按证据组织的提纲规划。 可选：`auto`, `template`, `adaptive`. | auto（sections）；恢复保留 |
| `--report-data-tables CHOICE` | 链接完整复制的分析记录，或同时在报告列出完整表格。 可选：`linked`, `full`. | linked（sections）；恢复保留 |

显式 CLI 参数覆盖 research TOML。直接新建不传 --model 时使用确定性收集／分析；research TOML 通常将模型默认为 env。无模型收集不等同完成请求的实验／报告。补齐条件／模型授权后，按显示的 start --resume-task 命令继续。模型失败不静默降级。

模型会话通常请求报告，但数据分析须显式请求。--outputs 不能与 --with-report／--no-report 同用。详细配对协议、输出文件选择器和环境准备应写 research TOML，不是额外 CLI 参数。精确进程合同不证明科学成功。

新任务采用 start 的决定／批准规则。历史会话保留已有里程碑策略：assisted 确认协议／交付／后续选择；checkpoints 确认首个协议／候选修订／交付变更，允许有界同协议补充；autonomous 在保存限额内选择。--session-root 保留证据／账目。仅在旧进程停止后用 --recover-interrupted，与输入修订分开。--reanalyze 与修订／授权／报告刷新分开；仅复用兼容测量。

恢复时省略的报告参数保留已有选择。新 article 报告以完整文章为单元；显式分节控制选择 sections。分节默认逐节撰写／审阅、关闭全文审阅、auto 提纲、linked 表格。联合撰写要求全文审阅／范围及兼容的完整来源设置。关闭审阅仍保留审计。报告刷新前另行授权资源；授权本身不重开已完成工作。

示例：

```bash
simple-ar research-session --config research.toml
simple-ar research-session --config examples/data-curves/research.toml
simple-ar research-session --topic "总结本地资料" --local-document paper.md --outputs summary
```


### `simple-ar research-report`

为分析已就绪的历史会话生成或刷新报告。共用原生任务通过 start 反馈或补齐请求写作。

```text
simple-ar research-report --session-root PATH --model SELECTOR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--session-root PATH` | 已有研究会话；遵循显示的恢复命令。共用原生任务用 start --resume-task 恢复。 | 必填 |
| `--refresh` | 用已有证据新建报告／审计尝试；保留旧报告，不重复实验。 | 关闭 |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 必填 |
| `--template NAME_OR_PATH` | 报告模板名称或 Markdown 路径。 | experiment |
| `--reviewer CHOICE` | 报告审阅器；disabled 保留审计。 可选：`llm`, `disabled`. | llm |
| `--max-review-iterations N` | 报告修订／纠正上限；失败提案也计数。 | 1 |
| `--document-review / --no-document-review` | 启用／禁用有界全文审阅／修订。 | Off（sections）；恢复保留 |
| `--review-scope CHOICE` | section 或 document；document 要求启用 document-review。 可选：`section`, `document`. | section（sections）；恢复保留 |
| `--draft-scope CHOICE` | section、联合 document 或 auto（不超过 2000 计划词数联合）；document／auto 要求全文审阅及其范围。 可选：`section`, `document`, `auto`. | section（sections）；恢复保留 |
| `--max-document-review-prompt-chars N` | 完整审阅请求字符上限；0 使用客户端／会话容量，不截断证据。 | 未设置 |
| `--outline-strategy CHOICE` | 模板提纲或按证据组织的提纲规划。 可选：`auto`, `template`, `adaptive`. | auto（sections）；恢复保留 |
| `--data-tables CHOICE` | 链接完整复制的分析记录，或同时在报告列出完整表格。 可选：`linked`, `full`. | linked（sections）；恢复保留 |
| `--max-section-tokens N` | 每个写作单元输出上限；0／未设表示不加此上限。 | 未设置 |

要求历史会话的报告交接已就绪；v1 只读。除非传 --refresh，已有报告幂等返回。省略参数保留已有设置。刷新消耗保存的尝试／API 预算，不重跑实验；耗尽时另行授权资源。关闭审阅器保留审计。

示例：

```bash
simple-ar research-report --session-root SESSION --model profile:daily --refresh
```

<a id="历史与开发命令"></a>
<a id="simple-ar-research-brief兼容别名"></a>

### `simple-ar research-brief`

新建仅摘要会话的兼容名称；继承 research-session 参数，受下述限制约束。

```text
simple-ar research-brief --topic TEXT [OPTIONS]
```

#### 目标、材料与新建

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--config PATH` | 本命令的 TOML 配置；显式 CLI 参数优先。 | 未设置 |
| `--interaction CHOICE` | 设计／协议确认策略；新原生任务没有阶段／交付里程碑对话。不授予进程权限。 可选：`assisted`, `checkpoints`, `autonomous`. | autonomous |
| `--topic TEXT` | 研究目标；新建时需提供或由 TOML 给出，恢复时可省略。 | 新建需提供或写入 TOML；恢复可省略 |
| `--task-kind CHOICE` | 显式结构化任务类别。 可选：`auto`, `survey`, `bug_fix`, `measurement`, `reproduction`, `writing`, `data_analysis`. | auto |
| `--outputs CHOICE ...` | 请求的成果；不能与 --with-report 或 --no-report 同用。 可选：`summary`, `report`, `experiments`, `bug_fix`, `code_task`, `data_analysis`. | 仅 summary |
| `--material PATH` | 笔记、草稿或分析包，可重复。普通材料未经核验，不等同文献论文。 | [] |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 直接 CLI：无；TOML 通常 env；恢复保留模型 |
| `--feasibility-review-model SELECTOR` | 用于有来源依据的可行性审阅的独立模型。 | 未设置 |
| `--output-root PATH` | 新任务／运行目录的父目录。 | runs/research-brief |
| `--cache-dir PATH` | 可选共享全文缓存位置。 | 会话本地缓存 |
| `--local-document PATH` | 本地 Markdown／文本参考资料，可重复。 | [] |
| `--query TEXT` | 覆盖检索查询，可重复。 | [] |
| `--provider NAME` | 覆盖检索提供商，可重复。 | [] |
| `--max-results N` | 检索结果数量限制。 | 10 |
| `--max-chunks N` | 摄取分块数量限制。 | 300 |
| `--max-fulltext-documents N` | 远程全文文档数量上限。 | 省略时不设数量上限 |
| `--max-pdf-mb N` | 每份获取 PDF 的大小上限，单位 MiB。 | 省略时不设大小上限 |
| `--web-extract-backend CHOICE` | 公开网页提取后端；与 PDF 获取分开。 可选：`direct`, `tavily_basic`. | 未设置 |
| `--idea-limit N` | 结构化想法候选数量上限。 | 3 |
| `--max-research-iterations N` | 首次分析后的新增轮数；0 表示首次分析后停止。 | 1 |
| `--cwd PATH` | 声明命令的工作目录。 | . |
| `--timeout-sec N` | 单进程超时限制（秒）。 | 未设置 |
| `--label NAME` | 声明实验的标签。 | research-session |
| `--primary-metric NAME` | 主要比较指标。 | 未设置 |
| `--metric NAME` | 要求输出的指标名，可重复。 | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION，可重复；方向包括 higher、lower、resource、ignore。 | [] |
| `--code-task-config PATH` | 实现所选方向的显式 CodeTask TOML。 | 未设置 |
| `--command ARGV...` | 显式 argv；放最后，后续 token 均属于命令。research／start 的 argv 不作 shell 解释。 | 未设置 |

#### 访问与资源额度

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--total-tokens N` | 共享 API token 预算；恢复保留账目。 | 直接 CLI 省略时不设上限；配置／恢复可能设限 |
| `--llm-requests N` | 共享 API 请求预算。 | 直接 CLI 省略时不设上限；配置／恢复可能设限 |
| `--max-output-tokens N` | 每次模型响应的输出上限。 | 未设置 |
| `--process-invocations N` | 累计进程调用额度；显式 0 有效。 | 省略时由执行入口计算；0 有效 |
| `--process-wall-seconds N` | 累计进程实际耗时额度（秒）。 | 省略时由执行入口计算；0 有效 |

#### 继续、批准与恢复

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--session-root PATH` | 已有研究会话；遵循显示的恢复命令。共用原生任务用 start --resume-task 恢复。 | 此别名拒绝 |
| `--recover-interrupted` | 仅历史会话：确认旧工作进程停止后，关闭其无结果的运行中尝试。 | 关闭 |
| `--reanalyze` | 历史会话：重新分析已有测量，与修订／额度／报告请求分开。 | 关闭 |
| `--authorization-id NAME` | 稳定幂等 ID；相同 ID／条款不会补回已用额度。 | 未设置 |
| `--authorization-reason TEXT` | 变更剩余额度的人类可读原因。 | 未设置 |
| `--authorize-remaining DIMENSION=AMOUNT` | 以 DIMENSION=AMOUNT 授权新的剩余额度，可重复；保留历史用量／未知预留。 | 未设置 |
| `--additional-attempts N` | 提高历史总尝试上限，不清除已有尝试。 | 0 |
| `--additional-no-progress N` | 提高历史连续无进展次数上限。 | 0 |
| `--decision-id NAME` | 待处理设计／协议决定的精确 ID；原生回复要求 --resume-task。 | 未设置 |
| `--decision-response CHOICE` | 回复待处理决定；接受设计不等同批准进程。 可选：`accept`, `reject`, `revise`. | 未设置 |
| `--decision-guidance TEXT` | --decision-response revise 时提供的事实或修订方向。 | 未设置 |

#### 显式表格分析

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--data-file PATH` | UTF-8 CSV／TSV 或 JSON 记录。显式表格选择器选择无需 API 的捷径；仅 --kind data_analysis 不会。 | 未设置 |
| `--data-attribution TEXT` | 用户声明的来源／署名／版本；保留但不独立核验。 | 空 |
| `--value-column NAME` | 显式数值列；多指标或矩阵／坐标列可重复。 | [] |
| `--group-column NAME` | 分类／分组列；values 柱图／热图要求行标签唯一。 | 空 |
| `--observation-unit TEXT` | 每行代表什么；表格捷径要求提供。 | 空 |
| `--value-unit TEXT` | 所选数值列单位；省略时为未知。 | 空 |
| `--paired-baseline NAME` | 相同量／单位的所选观测列中显式指定同行基线。 | 空 |
| `--data-association CHOICE` | 可选折线／散点对的描述性 Pearson r；不推断显著性／因果。 可选：`none`, `pearson`. | none |
| `--data-mode CHOICE` | observations 汇总观测行；values 保留给定数值，不推断聚合。 可选：`observations`, `values`. | observations |
| `--data-missing CHOICE` | 拒绝缺失值或显式忽略；缺失 y 会断开折线。 可选：`reject`, `omit`. | reject |
| `--figure-width CHOICE` | 通用 column（3.5 英寸）或 wide（7 英寸）图宽。 可选：`column`, `wide`. | wide |
| `--data-max-mb N` | 实际输入大小上限，单位 MiB。 | 20 |
| `--data-max-figures N` | 实际 SVG 页数限制；超限报错，不丢弃类别。 | 100 |
| `--data-plot CHOICE` | bar：均值／数值；box：观测四分位／极值；line／scatter：显式坐标；heatmap：给定矩阵。 可选：`bar`, `box`, `line`, `scatter`, `heatmap`. | bar |
| `--x-column NAME` | values 模式折线／散点的数值 x 列；不同于数值／分组列。 | 空 |
| `--x-unit TEXT` | 声明的 x 坐标单位；省略时为未知。 | 空 |
| `--data-max-points N` | 每张坐标图实际点数上限；超限报错，不抽样。 | 10000 |
| `--series-layout CHOICE` | 分轴，或相同声明单位的折线／散点列共轴。 可选：`separate`, `shared`. | separate |
| `--data-path PATH` | 已有数据文件／目录，可重复；不改写命令或自动复制外部数据。 | [] |

#### 报告控制

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--report-writing-method CHOICE` | 新报告使用 article；显式分节控制选择 sections。恢复保留已存方法。 可选：`article`, `sections`. | 新建 article；显式分节控制为 sections |
| `--max-section-tokens N` | 每个写作单元输出上限；0／未设表示不加此上限。 | 未设置 |
| `--with-report` | 显式请求模型撰写报告；数据分析默认仅交付分析。 | 关闭 |
| `--no-report` | 模型会话跳过报告撰写。 | 关闭 |
| `--report-template NAME_OR_PATH` | 报告结构名称或 Markdown 路径。 | auto |
| `--report-reviewer CHOICE` | 报告审阅器；disabled 保留确定性审计。 可选：`llm`, `disabled`. | llm |
| `--max-review-iterations N` | 报告修订／纠正上限；失败提案也计数。 | 1 |
| `--report-document-review / --no-report-document-review` | 启用／禁用有界全文审阅／修订。 | Off（sections）；恢复保留 |
| `--report-review-scope CHOICE` | section 或 document；document 要求启用 document-review。 可选：`section`, `document`. | section（sections）；恢复保留 |
| `--report-draft-scope CHOICE` | section、联合 document 或 auto（不超过 2000 计划词数联合）；document／auto 要求全文审阅及其范围。 可选：`section`, `document`, `auto`. | section（sections）；恢复保留 |
| `--max-document-review-prompt-chars N` | 完整审阅请求字符上限；0 使用客户端／会话容量，不截断证据。 | 未设置 |
| `--report-outline-strategy CHOICE` | 模板提纲或按证据组织的提纲规划。 可选：`auto`, `template`, `adaptive`. | auto（sections）；恢复保留 |
| `--report-data-tables CHOICE` | 链接完整复制的分析记录，或同时在报告列出完整表格。 可选：`linked`, `full`. | linked（sections）；恢复保留 |

入口限定仅摘要，默认 runs/research-brief 及 autonomous 交互，拒绝 --session-root 或 summary 以外的 outputs。下表列出继承的解析参数；执行／报告／恢复参数仍受 research-session 验证约束。用显示的 start --resume-task 命令恢复。

示例：

```bash
simple-ar research-brief --topic "总结本地资料" --local-document paper.md
```


### `simple-ar research-session-continue`

在已有 canonical 会话的简单实验失败／超时后运行一次修正的显式实验。

```text
simple-ar research-session-continue --session-root PATH [OPTIONS] --command ARGV...
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--session-root PATH` | 已有研究会话；遵循显示的恢复命令。共用原生任务用 start --resume-task 恢复。 | 必填 |
| `--parent-attempt-id NAME` | 分支起点的失败实验；默认最新 canonical 实验，旧布局为 experiment-001。 | 最新实验；旧布局为 experiment-001 |
| `--model SELECTOR` | 模型选择器：env、profile:NAME 或 route:NAME。 | 未设置 |
| `--cwd PATH` | 声明命令的工作目录。 | . |
| `--timeout-sec N` | 单进程超时限制（秒）。 | 300 |
| `--label NAME` | 声明实验的标签。 | research-session-recovery |
| `--primary-metric NAME` | 主要比较指标。 | 未设置 |
| `--metric NAME` | 要求输出的指标名，可重复。 | [] |
| `--metric-direction NAME=DIRECTION` | NAME=DIRECTION，可重复；方向包括 higher、lower、resource、ignore。 | [] |
| `--command ARGV...` | 显式 argv；放最后，后续 token 均属于命令。research／start 的 argv 不作 shell 解释。 | 必填 |

仅支持修正简单显式实验。科学阴性结果不是技术失败。配对／预处理数据／CodeTask 失败使用各自续接路径。未显式覆盖时保留父证据／schema。v1 不可执行；原生任务用 start --resume-task。

示例：

```bash
simple-ar research-session-continue --session-root SESSION --metric accuracy --command python experiment.py
```


### `simple-ar research-session-migrate`

从只读 v1 会话创建后继，仅导入具名小成果，不修改源会话。

```text
simple-ar research-session-migrate --source-root PATH --destination-root PATH [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--source-root PATH` | 含 session_manifest.v1 的只读历史会话。 | 必填 |
| `--destination-root PATH` | 位于源目录以外的新的空后继目录。 | 必填 |
| `--artifact NAME` | 显式指定导入的小成果文件，可重复。 | [] |
| `--requested-output NAME` | 后继会话请求的成果，可重复。 | 历史 brief 或 research_summary |
| `--max-artifact-bytes N` | 每份显式导入成果的字节上限。 | 2097152 |

只复制显式指定且符合大小限制的成果。保留跳过事实及未知历史预算的说明，不猜测或清零。

示例：

```bash
simple-ar research-session-migrate --source-root OLD_SESSION --destination-root NEW_SESSION --artifact research_summary.md
```

<a id="codetask-命令"></a>
<a id="高级编排命令"></a>
<a id="底层原语命令"></a>

### `simple-ar code-task init`

为显式已有项目或从零创建合同准备隔离工作区。

```text
simple-ar code-task init [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--config PATH` | init 设置 TOML；CLI 参数覆盖配置值。 | 未设置 |
| `--code-root PATH` | 源项目。`existing_project` 必填；`greenfield` 仅在需要 scaffold/source root 时填写。 | 未设置 |
| `--kind CHOICE` | `existing_project` 表示已有项目 patch；`greenfield` 表示从零生成项目。 可选：`existing_project`, `greenfield`. | 未设置 |
| `--task-file PATH` | Markdown/text 任务描述。除非配置中已设置，否则必填。 | 未设置 |
| `--output-root PATH` | code-task run 创建位置。 | 未设置 |
| `--name NAME` | run 名称后缀。 | 未设置 |
| `--benchmark-command VALUE` | 在 workspace 中 patch 前后运行的命令。 | 未设置 |
| `--primary-metric NAME` | before/after verdict 使用的主指标。 | 未设置 |
| `--metric-direction METRIC=DIRECTION` | 指标方向：`higher`、`lower`、`resource` 或 `ignore`。 可重复。 | [] |
| `--env-mode CHOICE` | `current` 或 `external`。 可选：`current`, `external`. | 保存／配置策略；未设 current |
| `--python PATH` | `--env-mode external` 的 Python。 | 保存／配置解释器；external 策略需提供 |
| `--workspace-mode CHOICE` | CLI 接受 `auto`、`copy`、`git_worktree` 和 `sparse_copy`。`greenfield` 默认使用空工作区，也可通过 TOML 声明 `empty`；CLI 不接受 `empty`。 可选：`auto`, `copy`, `git_worktree`, `sparse_copy`. | 未设置 |
| `--workspace-include VALUE` | `sparse_copy` include pattern。 可重复。 | 未设置 |
| `--workspace-exclude VALUE` | `sparse_copy` 额外 exclude pattern。 可重复。 | 未设置 |
| `--workspace-reuse-source-venv` | 检测并复用 source `.venv` Python。 | 未设置 |
| `--max-file-bytes N` | copy/sparse 模式最大复制文件大小，`0` 表示禁用。 | 未设置 |

示例：

```bash
simple-ar code-task init --config code-task.toml
```


### `simple-ar code-task execute`

继续已有 CodeTask 的有界规划、审阅、编辑及基准。

```text
simple-ar code-task execute RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--config PATH` | 可选 TOML，用于模型路由、预算和运行设置。 | 未设置 |
| `--to-step CHOICE` | 最多运行到 `probe`、`baseline`、`work-plan`、`batch`、`plan`、`propose-edits`、`apply-edits`、`review`、`validate`、`run`、`analyze-failure` 或 `repair`。 可选：`probe`, `baseline`, `work-plan`, `batch`, `plan`, `propose-edits`, `apply-edits`, `review`, `validate`, `run`, `analyze-failure`, `repair`. | 配置值或 run |
| `--dry-run` | 只打印下一步动作，不写产物。 | 关闭 |
| `--model SELECTOR` | LLM 步骤模型覆盖。 | 未设置 |
| `--no-llm` | 尽可能使用 deterministic fallback。 | 关闭 |
| `--timeout N` | benchmark timeout。 | 60 |
| `--yes` | 普通 execute 模式下自动批准 inline 审核门；与 `--interactive` 一起使用时，自动继续 primitive prompts。只有明确接受审核风险、想自动化跑通时才使用。 | 关闭 |
| `--interactive` | 调试模式：逐个 primitive step 确认，而不是连续运行到下一个审核门。 | 关闭 |
| `--no-review-inline` | 禁用 inline 审核提示，在审核门直接停止。 | 关闭 |
| `--skip-validation` | 静态验证未通过时仍运行 benchmark。 | 关闭 |
| `--strict-validation` | 将较高风险 validation warning 视为 error。 | 关闭 |
| `--validation-max-file-bytes N` | 静态验证扫描文件大小上限。 | 500000 |
| `--apply-proposed-edits` | plan 批准后应用已审核的 `proposed_edits.json`。 | 关闭 |
| `--allow-large-edits` | 允许已审核、超过 normal 预算的较大 proposal。 | 关闭 |
| `--allow-planning-fallback` | 仅在已知完整响应格式失败后允许离线规划。 | 关闭 |
| `--planning-mode CHOICE` | 从零项目架构规划；tool_agent 是 compact 的弃用别名。 可选：`tool_agent`, `compact`. | 配置值或 compact |
| `--llm-retry-attempts N` | 完整响应纠正上限，含首轮；传输重试使用模型设置。 | 配置值或 3 |
| `--planning-review-rounds N` | 已弃用；非零只显示提示，不增加审阅调用。 | 配置值或 0；已退出 |
| `--repair-rounds N` | 失败后的 bounded repair proposal 轮数。 | 配置值或 0 |
| `--reuse-planning-from PATH` | 检查任务约束 hash 后，导入不可变的已接受 greenfield 规划；不导入生成代码、memory、审阅发现或修复历史。 | 未设置 |
| `--review-gate CHOICE` | strict 阻止所有 blocking 发现；runtime 阻止安全或直接可执行性问题，并保留包装和报告方面的缺口记录。 可选：`strict`, `runtime`. | 配置值或 strict |
| `--baseline-policy CHOICE` | 已有项目 baseline 策略：`auto`、`run`、`skip`、`provided` 或 `none`。昂贵 baseline 可用 `skip`/`none` 跳过，或用 `provided` 记录已有指标。 可选：`auto`, `run`, `skip`, `provided`, `none`. | 配置值或 auto |
| `--baseline-metrics-file PATH` | `--baseline-policy provided` 时读取的 JSON 或 `metric=0.82` 文本指标文件。 | 未设置 |
| `--max-files N` | LLM 步骤上下文文件预算。 | 8 |
| `--max-source-chars-per-file N` | 单文件 source 上下文预算。 | 4000 |
| `--env-mode CHOICE` | `current` 或 `external`。 可选：`current`, `external`. | 保存／配置策略；未设 current |
| `--python PATH` | external env mode 的 Python。 | 保存／配置解释器；external 策略需提供 |

执行在审阅关口停止。--yes 可批准 CodeTask 行内关口；原生 start 的进程批准规则不同。--no-review-inline 在关口停止；--interactive 在原语步骤前询问。应用编辑要求计划获批及 --apply-proposed-edits；较大编辑另需授权。离线规划降级仅限已知完整响应格式失败，不适用于传输／预算／截止时间／部分流失败。响应纠正含首轮；传输重试属于模型连接。基准完成不证明目标改善。

示例：

```bash
simple-ar code-task execute RUN_DIR --config code-task.toml --dry-run
simple-ar code-task execute RUN_DIR --config code-task.toml
```


### `simple-ar code-task decide-plan`

记录对当前补丁计划的人类决定。

```text
simple-ar code-task decide-plan RUN_DIR --decision CHOICE [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--decision CHOICE` | `approve`、`reject` 或 `revise`，必填。 可选：`approve`, `reject`, `revise`. | 必填 |
| `--note TEXT` | 可选审核备注。 | 空 |
| `--reviewer VALUE` | 审核人标签，默认 `user`。 | user |

示例：

```bash
simple-ar code-task decide-plan RUN_DIR --decision approve
```


### `simple-ar code-task probe`

检查运行时／环境，不安装依赖或运行基准。

```text
simple-ar code-task probe RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--env-mode CHOICE` | `current` 或 `external`。 可选：`current`, `external`. | 保存／配置策略；未设 current |
| `--python PATH` | external env mode 的 Python。 | 保存／配置解释器；external 策略需提供 |

示例：

```bash
simple-ar code-task probe RUN_DIR
```


### `simple-ar code-task map`

重建工作区仓库地图及索引。

```text
simple-ar code-task map RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--no-refresh-index` | 复用已有 `codebase_index.json`。 | 关闭 |
| `--show-summary` | 打印 `repo_map_summary.md`。 | 关闭 |

示例：

```bash
simple-ar code-task map RUN_DIR
```


### `simple-ar code-task locate`

对可能需要编辑的文件及只读证据排序。

```text
simple-ar code-task locate RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--query TEXT` | locate query，默认使用 `code_task/task.md`。 | 未设置 |
| `--top-k N` | 每组候选数量，默认 `8`。 | 8 |
| `--refresh-map` | 排序前重建 index 和 repo map。 | 关闭 |
| `--no-read-only` | 省略受保护的只读证据文件。 | 关闭 |
| `--show-summary` | 打印 `locate_results.md`。 | 关闭 |

示例：

```bash
simple-ar code-task locate RUN_DIR
```


### `simple-ar code-task context`

根据定位文件构建有界提示上下文包。

```text
simple-ar code-task context RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--query TEXT` | locate query，默认使用任务文件。 | 未设置 |
| `--top-k N` | locate 候选预算。 | 8 |
| `--max-files N` | 纳入 snippet 的文件上限。 | 8 |
| `--max-source-chars-per-file N` | 单文件 snippet 字符预算。 | 4000 |
| `--max-total-chars N` | 总 snippet 字符预算。 | 20000 |
| `--refresh-map` | 打包前重建 repo map。 | 关闭 |
| `--show-prompt` | 打印 `prompt_context.md`。 | 关闭 |

示例：

```bash
simple-ar code-task context RUN_DIR
```


### `simple-ar code-task work-plan`

生成面向批次的实现工作计划。

```text
simple-ar code-task work-plan RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--model SELECTOR` | 模型覆盖。 | 未设置 |
| `--no-llm` | 使用 fallback planning。 | 关闭 |
| `--force` | 重新生成已有 work plan。 | 关闭 |
| `--allow-planning-fallback` | 仅在已知完整响应格式失败后允许离线规划。 | 关闭 |
| `--llm-retry-attempts N` | 完整响应纠正上限，含首轮；传输重试使用模型设置。 | 1 |
| `--max-files N` | planning 上下文文件预算。 | 8 |
| `--max-source-chars-per-file N` | 单文件 source 上下文预算。 | 2500 |

示例：

```bash
simple-ar code-task work-plan RUN_DIR
```


### `simple-ar code-task batch`

为一个工作计划项创建尝试／批次。

```text
simple-ar code-task batch RUN_DIR --work-item NAME [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--work-item NAME` | work-plan item id，例如 `W1`，必填。 | 必填 |
| `--attempt-id NAME` | 可选 attempt id，例如 `attempt-001`。 | 未设置 |
| `--force` | 即使已有 batch，也创建新 batch。 | 关闭 |

示例：

```bash
simple-ar code-task batch RUN_DIR --work-item W1
```


### `simple-ar code-task plan`

生成供人类审阅的补丁计划。

```text
simple-ar code-task plan RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--model SELECTOR` | 模型覆盖。 | 未设置 |
| `--no-llm` | 使用 fallback plan。 | 关闭 |
| `--force` | 重新生成已有 plan。 | 关闭 |
| `--allow-planning-fallback` | 仅在已知完整响应格式失败后允许离线规划。 | 关闭 |
| `--llm-retry-attempts N` | 完整响应纠正上限，含首轮；传输重试使用模型设置。 | 1 |
| `--max-files N` | 上下文文件预算。 | 8 |
| `--max-source-chars-per-file N` | 单文件 source 上下文预算。 | 2500 |

示例：

```bash
simple-ar code-task plan RUN_DIR
```


### `simple-ar code-task propose-edits`

提出受控旧／新文本编辑，不应用。

```text
simple-ar code-task propose-edits RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--model SELECTOR` | 模型覆盖。 | 未设置 |
| `--no-llm` | 写入 deterministic empty proposal。 | 关闭 |
| `--force` | 重新生成已有 proposal。 | 关闭 |
| `--max-files N` | 可编辑上下文文件预算。 | 8 |
| `--max-source-chars-per-file N` | 单文件 source 上下文预算。 | 4000 |
| `--allow-large-edits` | 人工审核后接受较大但仍受限的 proposal。 | 关闭 |

示例：

```bash
simple-ar code-task propose-edits RUN_DIR
```


### `simple-ar code-task apply-edits`

在允许的工作区范围内应用已审阅编辑提案。

```text
simple-ar code-task apply-edits RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--edits-file PATH` | 指定要应用的 proposal 文件。 | 未设置 |
| `--allow-unapproved-plan` | 本地测试/demo 时绕过 plan approval。 | 关闭 |
| `--allow-large-edits` | 应用已审核、需要 large-edit approval 的 proposal。 | 关闭 |

检查路径、范围、旧文本唯一性及编辑预算。修复不自动应用：审阅后传其 --edits-file。--allow-unapproved-plan 绕过人类批准，仅用于本地测试／演示。

示例：

```bash
simple-ar code-task apply-edits RUN_DIR --edits-file PROPOSAL_JSON
```


### `simple-ar code-task validate`

运行轻量静态验证；通过不是运行证明。

```text
simple-ar code-task validate RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--strict` | 将较高风险 warning 视为 error。 | 关闭 |
| `--max-file-bytes N` | 扫描文件大小上限。 | 500000 |

示例：

```bash
simple-ar code-task validate RUN_DIR
```


### `simple-ar code-task baseline`

编辑前运行已保存的基准。

```text
simple-ar code-task baseline RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--command VALUE` | 带引号的基准命令字符串，覆盖已有命令。 | 未设置 |
| `--timeout N` | benchmark timeout。 | 60 |
| `--skip-validation` | 静态验证未通过时仍运行。 | 关闭 |
| `--env-mode CHOICE` | `current` 或 `external`。 可选：`current`, `external`. | 保存／配置策略；未设 current |
| `--python PATH` | external env mode 的 Python。 | 保存／配置解释器；external 策略需提供 |

示例：

```bash
simple-ar code-task baseline RUN_DIR
```


### `simple-ar code-task run`

编辑后运行已保存的基准，并比较已有基线指标。

```text
simple-ar code-task run RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--command VALUE` | 带引号的基准命令字符串，覆盖已有命令。 | 未设置 |
| `--timeout N` | benchmark timeout。 | 60 |
| `--skip-validation` | 静态验证未通过时仍运行。 | 关闭 |
| `--env-mode CHOICE` | `current` 或 `external`。 可选：`current`, `external`. | 保存／配置策略；未设 current |
| `--python PATH` | external env mode 的 Python。 | 保存／配置解释器；external 策略需提供 |

示例：

```bash
simple-ar code-task run RUN_DIR
```


### `simple-ar code-task analyze-failure`

确定性汇总最近验证／基准失败。

```text
simple-ar code-task analyze-failure RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |

示例：

```bash
simple-ar code-task analyze-failure RUN_DIR
```


### `simple-ar code-task repair`

提出有界修复编辑；另行审阅及应用。

```text
simple-ar code-task repair RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--model SELECTOR` | 模型覆盖。 | 未设置 |
| `--no-llm` | 写入 deterministic empty repair proposal。 | 关闭 |
| `--max-files N` | repair 上下文文件预算。 | 8 |
| `--max-source-chars-per-file N` | 单文件 source 上下文预算。 | 4000 |

仅生成提案；审阅后用 code-task apply-edits --edits-file 应用。

示例：

```bash
simple-ar code-task repair RUN_DIR
```

<a id="工具与-mcp"></a>

### `simple-ar tools schema`

向标准输出或文件导出已注册只读工具的 schema。

```text
simple-ar tools schema [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `--format CHOICE` | 工具 schema 导出格式。 可选：`mcp`, `openai`. | mcp |
| `--output PATH` | 本命令的输出目录／文件。 | 标准输出 |

示例：

```bash
simple-ar tools schema --format mcp --output schemas.json
```


### `simple-ar tools call`

用 JSON 对象调用一个已注册只读工具，并保存追踪。

```text
simple-ar tools call RUN_DIR TOOL_NAME [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `TOOL_NAME` | 已注册的只读运行工具名。 | 必填 |
| `--args-json VALUE` | JSON 对象工具参数；--args-file 可避免 shell 引号问题。 | {} |
| `--args-file PATH` | 从文件读取参数 JSON 对象，替代 --args-json。 | 未设置 |
| `--debug-payloads` | 在工具追踪中保留更大的载荷。 | 关闭 |

参数须为 JSON 对象。--args-file 优先。追加 RUN_DIR/tools/tool_trace.jsonl。

示例：

```bash
simple-ar tools call RUN_DIR TOOL_NAME --args-file arguments.json
```


### `simple-ar tools serve-mcp`

通过 MCP stdio 提供已注册只读运行工具；不授予 shell、写入、网络或安装能力。

```text
simple-ar tools serve-mcp RUN_DIR [OPTIONS]
```

| 参数 | 说明 | 默认值或使用条件 |
| --- | --- | --- |
| `RUN_DIR` | 已有运行／任务目录。 | 必填 |
| `--debug-payloads` | 在工具追踪中保留更大的载荷。 | 关闭 |

示例：

```bash
simple-ar tools serve-mcp RUN_DIR
```

兼容说明：`simple-ar run`、`simple-ar resume`、`simple-ar research-experiment`、`simple-ar research-code-task` 已退出并拒绝执行。目标任务用 start；显式合同用 research-session／code-task。status 可检查支持的历史结果。旧阶段／synthesis-file 参数不自动转换。
