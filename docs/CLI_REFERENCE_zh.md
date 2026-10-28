# CLI 参考

[English version](CLI_REFERENCE.md) · [使用指南](USAGE_zh.md) · [配置参考](CONFIG_REFERENCE_zh.md)

本文用于查阅命令语法、参数和恢复规则。从源码目录运行时，在示例命令前加 `uv run`。`SESSION` 是打印出的完整会话路径，`RUN_DIR` 是运行目录。`--command ARGV...` 必须放在最后：它后面的所有参数都属于子进程命令，不经过 shell 解释。

## 日常使用

通常只需要以下四个命令：

| 要做什么 | 命令 |
| --- | --- |
| 新建任务、继续任务或提出反馈 | [start](#引导入口) |
| 查看成果及继续方式 | [results](#simple-ar-status) |
| 查看暂停原因与已保留的工作 | [status](#simple-ar-status) |
| 查看本地模型配置 | [models](#simple-ar-models) |

```bash
simple-ar start "解释这些结果" --source ./results --model profile:daily
simple-ar status "SESSION"
simple-ar results "SESSION"
simple-ar start --resume-task "SESSION" "修改图例，保留原版"
```

首次配置见[入门教程](USAGE_zh.md)；需要时再查[恢复与授权](#原生任务恢复)。
模型本地检查不会发送测试请求，实际任务可能产生费用。

## 独立工具与高级接口

这些命令用于明确的单项操作、自动化或兼容任务，不是每次研究都要选择的流程。

| 需要 | 查阅 |
| --- | --- |
| 复用已有交付 | [成果复用](#成果复用) |
| 图像生成、编辑或视觉检查 | [图像工具](#概念图) |
| 检查项目入口与依赖 | [项目准备](#项目准备) |
| 导出已有稿件 | [report-export](#simple-ar-report-export) |
| 精确研究协议或旧研究会话 | [Research 命令](#research-命令) |
| 指定范围的代码任务 | [CodeTask 命令](#codetask-命令) |
| 搜索成果、检查索引或清理缓存 | [成果工具](#成果工具) |
| 工具 schema、只读调用与 MCP | [工具与 MCP](#工具与-mcp) |
| 旧命令迁移 | [历史与开发命令](#历史与开发命令) |

各命令的 `--help` 对应当前安装版本。原生工具的参数由工具目录提供；
集成开发从[代码归属与工具接口](DEVELOPMENT_zh.md#实施一项改动)开始，不需手工编排工具 JSON。

## 引导入口

~~~bash
simple-ar start
simple-ar start --guided
simple-ar start "解释这些结果" --source results/ --model profile:daily
simple-ar start --kind writing --material notes.md --prepare-only
~~~

裸 `start` 会询问目标。位置参数需求或普通 `--goal` 会进入原生工具循环。`--guided` 和普通 `--kind` 只为同一循环选择成果偏好，不授予访问权限。

### 原生任务入口（实验性）

#### 目标与材料

| 参数 | 含义或默认值 |
| --- | --- |
| `REQUEST`、`--goal TEXT` | 自然语言目标。 |
| `--guided`、`--kind KIND` | 偏好包括 `survey`、`bug_fix`、`reproduction`、`writing`、`data_analysis`、`figure` 和 `code`。 |
| `--model SELECTOR` | 默认 `env`；可选择模型目录或旧连接。原生决策需要 Chat 连接。 |
| `--output-root PATH` | 默认 `runs/assistant`。 |
| `--source PATH` | 可重复提供只读普通文件或目录。 |
| `--document PATH`、`--material PATH`、`--project PATH`、`--data-path PATH`、`--data-file PATH` | 普通偏好输入会成为带角色声明的只读挂载。 |
| `--project-python PATH` | 已有进程解释器；不会安装依赖或创建环境。 |

#### 访问与额度

| 参数 | 含义或默认值 |
| --- | --- |
| `--allow-network` | 允许公开来源检索和获取，与进程权限分开。 |
| `--allow-execution` | 允许提出进程命令；每条命令仍须审批。 |
| `--allow-vision` | 允许将挂载中的 PNG、JPEG 或 WebP 发给配置的 vision 路由。 |
| `--max-images N` | 图像生成或编辑提交额度，默认 0；不取消单独授权的视觉检查。 |
| `--max-turns N`、`--max-tokens N` | 保存任务的总额度，默认 30 次模型请求和 600000 token。 |
| `--prepare-only` | 只保存或预览，不调用模型或执行进程；新原生任务仍会预检连接和凭据。 |
| `--yes` | 接受展示的任务、访问和资源摘要，不等于命令审批。 |

#### 已保存任务与续接

| 参数 | 含义或默认值 |
| --- | --- |
| `--resume-task SESSION` | 恢复保存的原生任务，可同时提供反馈。 |
| `--tasks-in PATH`、`--select-task` | 只读列出目录及直接子目录中的原生任务；可交互选择，或配合 `--resume-task 精确目录名` 继续。 |
| `--write-task-config PATH`、`--task-config PATH` | 导出带注释的原生创建配置并退出；编辑后加载，显式命令行值优先。配置不能用于重设已有任务。 |
| `--max-processes N`、`--max-process-seconds N` | 新原生任务累计进程次数和墙钟秒数上限，包含搜索后端；零值有效。恢复沿原账本，续额另行授权。 |
| `--supply-task PATH` | 配合原生恢复，确认 TOML 中尚缺的合同；只记录，不执行工作。 |
| `--approve-execution TOKEN` | 提交当前打印的精确审批 token；必须与原生恢复一起使用。 |
| `--decision-id ID`、`--decision-response accept\|reject\|revise`、`--decision-guidance TEXT` | 恢复时回复冻结设计；guidance用于revise，不代表执行授权。 |
| `--retry-model` | 显式重试已关闭的失败模型决策，规则见下文。 |
| `--switch-model profile:NAME`、`--switch-id ID`、`--switch-reason TEXT` | 配合原生恢复，只记录安全时点的显式命名文本连接变更，不发请求/工具/反馈；旧交付和账本保留。独立于retry/反馈/授权使用。 |
| `--authorize-remaining DIMENSION=AMOUNT` | 恢复时可重复声明新的剩余额度。 |
| `--authorization-id ID`、`--authorization-reason TEXT` | 改变额度必须提供稳定 ID 和原因。 |

新挂载不接受符号链接或私有、运行时路径。输出写在 `workspace/` 中。偏好适配器的 `--data-file` 仍有 20 MiB 上限；更大的材料可用 `--source` 进行有界调查。普通偏好适配器不接受公开文档 URL；请将 URL 写入目标，并另行授权网络获取。

### 目标驱动的代码、分析与图形

~~~bash
simple-ar start --kind figure --document method.md --goal "画出观察、比较与证据之间的关系" --allow-execution
simple-ar start --kind code --material requirements.md --goal "建立可复用 CSV 校验器" --allow-execution
simple-ar start --kind data_analysis --scripted --data-file measurements.csv --goal "比较成本与质量" --allow-execution
~~~

图形偏好要求布局或绘图代码、PNG、可编辑 SVG 和解释。脚本分析偏好要求可运行源码、计算结果和解释。偏好不会限制工具或图形类型，不会授予权限、制造数据，也不证明任务完成。

显式提供的列、单位、配对关系和展示要求会保留在目标中。没有提供的预设默认值不会约束原生任务。`--scripted` 仅适用于 `data_analysis`。

### 原生任务恢复

先用 `status` 和 `results` 确认保存边界，再选择对应操作。
中断后先确认旧工作进程已停止；在途请求、尚未接纳的结果和未知进程副作用须先核对。

#### 恢复与反馈

```bash
simple-ar status SESSION
simple-ar results SESSION
simple-ar start --resume-task SESSION
simple-ar start --resume-task SESSION "缩短解释，不重新运行"
simple-ar start --resume-task SESSION --approve-execution TOKEN
simple-ar start --resume-task SESSION --source notes/ --allow-network --yes
```

普通恢复沿用保存边界。已完成且没有待办的任务返回终态，不重新执行。
反馈共用原预算，只取消尚未开始的提案；修改暂停期间的旧交付不代表新请求已完成。

追加来源或网络、进程、视觉权限需要确认。无效追加不改变挂载、权限和账本，
保存的模型、解释器、工作和预算继续保留。恢复时 `--prepare-only` 只预览，
不应用访问或资源变更。权限确认与具体命令的精确审批分开。

#### 补充剩余额度

需要可重复的 `--authorize-remaining DIMENSION=AMOUNT`、稳定的
`--authorization-id ID` 及 `--authorization-reason TEXT`：

```bash
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "完成保存的目标" --yes
```

新额度是未来容量，不改写历史总量。只提供 ID 和原因、没有额度项会失败。
重放同一 ID 不补回已消耗额度；创建参数 `--max-turns`、`--max-tokens` 不能用于恢复续额。
原费用留在账本。

模型操作确认已结束并进入任务记录后，可显式授权新的剩余额度，历史账单仍可未知。
当前额度内的未知用量仍阻止继续消耗。

#### 切换已配置连接

在已核对的边界先记录变更，再单独恢复：

```bash
simple-ar start --resume-task SESSION --switch-model profile:another --switch-id connection-1 --switch-reason "使用另一已配置服务"
simple-ar start --resume-task SESSION
```

切换改变文本、代码、写作绑定，不发请求、不重放工具、不追加反馈或额度。
vision 绑定、交付和账本保留。已关闭的专业调用可继续保留历史未知费用。
同一切换 ID 只能对应同一意图；重放旧 ID 不撤销后续变更。
已终止的失败决策使用下面的重试操作。

#### 重试失败的模型决策

```bash
simple-ar start --resume-task SESSION --retry-model
simple-ar start --resume-task SESSION --retry-model --model profile:another
```

重试要求请求已终止失败、没有已接受回复，并沿用保存的有限输出上限和剩余容量。
只重新决策，不重放工具或整个任务。缓存回复走普通恢复；在途请求、未确认预留、
无界消耗及待执行工具都会拒绝重试。重试与反馈、权限追加、命令审批分开；
`--prepare-only` 不重试或修改状态。

仅这种已关闭失败可用 `--model profile:NAME` 或 `route:NAME` 更换 Chat 连接，
输出上限保持相同。文本、代码、写作绑定与切换记录一起保存，vision 不变。
替换在同任务内开始新的提供方原生对话；旧回复保留在记录和 `read_history` 中，
已授权且版本有效的材料可复用，不伪造缺失 reasoning。
缓存回复不能切换；不带重试却指定不同模型会被拒绝，省略则沿用保存选择。

#### 补齐或修订合同

`--supply-task PATH` 确认 TOML 中给调用方原生任务补充的条件，只记录、不执行工作。
首次绑定模型的示例：

```toml
supply_id = "first-model"
reason = "基于已保存材料继续工作"

[changes]
model = "profile:daily"
allow_execution = true

[resource_allowances]
llm_requests = 20
total_tokens = 600000
process_invocations = 8
process_wall_seconds = 600
```

```bash
simple-ar start --resume-task SESSION --supply-task supply.toml --prepare-only
simple-ar start --resume-task SESSION --supply-task supply.toml --yes
simple-ar start --resume-task SESSION
```

补齐共用原生创建校验。`changes` 还接受 `sources`、`execution_contract`、`code_task`、
`research_policy`、`table_analysis`、`report_requirement` 和 `max_output_tokens`。

- 默认只填缺项。替换已有合同须在顶层写 `replace_existing = true`，提供新 ID 和原因。
  仅列出的字段会改变；替换模式中的 `sources` 是完整别名表，不是合并。
- 来源路径相对补齐文件解析；执行 cwd 须为已有绝对目录，argv 不做 shell 展开。
  需要确认的协议改变后重新接受，实际执行仍须独立精确批准。
- 首次绑定模型须提供正数请求和 token 额度；仅续额不授予模型访问。
  后续模型变更使用独立恢复控制。
- 原记录和费用保留；只改报告时保留有效测量。旧确认和证据不代表新条件已验收。

相同 ID 与参数重放幂等，也覆盖已记额度、尚未完成配置提交的中断。
须先完成原补齐才能继续其他工作。补齐与反馈、重试、独立续额和命令审批分开，
不修改保存的 legacy ResearchApplication 协议。

### 结构化兼容入口

大部分显式协议控制参数会选择结构化输入适配，即使显式值恰好等于解析器默认值；新任务仍使用共同任务所有者。来源策略、检索边界、报告模板、复现命令、指标、环境、表格物理限制和参与策略仍保留精确语义，不能与原生访问或预算参数混用。

新建 `--kind bug_fix` 同时提供 `--project`、`--allow`、`--validate` 时，使用原生会话和精确 CodeTask 所有者。执行前批准工作、原源码、验证器及环境；冻结合同下不能另用普通工作区写入或进程绕开范围。反馈可引用已验证候选，重新批准后修订。不能承接的额外协议参数会明确拒绝，不会忽略；已登记的旧项目复用保留原编辑及预算策略。

图形或脚本分析请求若带不兼容的严格控制参数，会在创建前失败。创建时的偏好也不能改变恢复中的原生任务。

`--chat` 需要终端和模型，使用结构化技术设置。`--resume-setup PATH` 恢复设置草稿，不恢复执行。不要将 chat 与 prepare-only 合用。设置过程可以检查材料和提议参数，但不会执行项目或安装依赖；显式用户参数始终优先。

| 结构化 start 参数 | 含义 |
| --- | --- |
| `--sources materials\|search`、`--document PATH`、`--fulltext`、`--web-extract-backend direct\|tavily_basic`、`--max-cited-sources N` | 来源策略、可重复论文路径、全文获取、网页获取后端和最终引用上限。 |
| `--material PATH`、`--template NAME_OR_PATH` | 可重复提供笔记、草稿或结果，并选择报告形式。 |
| `--project PATH`、`--allow PATTERN`、`--validate TEXT` | 项目、可重复的精确编辑范围和独立校验命令。 |
| `--data-path PATH`、`--project-python PATH` | 可重复执行输入位置和已有解释器。 |
| `--hypothesis TEXT`、`--dataset TEXT`、`--expected-outcome TEXT`、`--metric NAME` | 复现结论、条件、预期结果和可重复指标。 |
| `--cwd PATH`、`--timeout-sec N` | 复现目录和单进程超时，默认 300 秒。 |
| `--output-files JSON`、`--metric-sources JSON` | 登记产出文件，声明 JSON 或唯一行的数值选择器。 |
| `--environment current\|venv`、`--requirements PATH`、`--install-project` | 显式准备依赖，可重复提供 requirements。 |
| `--check-argv JSON` | 测量前的独立短检查；不推断命令或科学指标。 |
| `--command ARGV...` | 正式命令，必须是最后一个选项。 |
| `--interaction assisted\|checkpoints\|autonomous` | 新任务的协议/设计确认策略；legacy 里程碑按保存的所有者执行。 |
| `--with-report` | 从保存的数据或修复结果写报告，需要模型。 |
| `--asset-max-mb N` | chat 项目或数据下载容量，默认 20 MiB；ZIP 展开容量为四倍，不等于下载授权。 |

#### 受限复现适配

项目适配必须同时声明项目、编辑范围和独立校验器。解释器解析后，校验命令必须不同于正式命令；工作目录必须与项目一致。受保护的测试、秘密和数据保持只读，编辑发生在隔离 worktree 或副本中，测量前先校验。

可以使用当前或已有 Python，也可以显式准备任务 venv；安装失败会停止工作。CodeTask 已有校验时，不要再加第二个 `--check-argv`。这些操作不授权改变科学方法、数据划分或实验条件。

chat 可以提议精确的新 Python 适配器路径和有来源依据的校验器，确认后才保存。输出应放在作者项目之外。没有创建声明时，allow 路径必须已经存在。`--project-python` 选择已有解释器；显式准备 venv 时，它可作为基础解释器。设置过程不能依照不可信来源中的指令选择解释器。

#### 资产和材料获取

chat 的资产提案需要单独确认，并使用已检查的字面 URL。仅接受公开 HTTPS；带 userinfo、fragment、凭据或签名的链接会被拒绝。项目资产支持直接 ZIP 和 GitHub 默认分支快照，不支持固定 commit。数据只下载为一个文件，不解包。

默认最多三个资产，每个 20 MiB；项目展开最多 80 MiB、5000 个条目。ZIP 格式按字节检查，配置的限制会保留到恢复过程。

辅助材料使用一次不跟随重定向的公开 GET，PDF 需要单独授权。预览最多 8000 字符或三页 PDF。扩大本地提取需要确认，且最多 40 页 PDF、20 MiB。问答只发送有界片段。

设置发现最多两轮，每轮两个查询，每个查询和 provider 最多五个结果。有 Tavily key 时使用 Tavily 和 OpenAlex，否则使用 arXiv 和 OpenAlex。已保存响应和获取凭据会复用；未知中断不会重新发送。失败论文可显式跳过，但其凭据和未知消耗仍保留。accept-proposal 只接受展示的假设，不补造事实，也不授予执行权限。

### 已有数据分析（无需 API）

~~~bash
simple-ar start --kind data_analysis --data-file observations.csv --value-column score --observation-unit "一次运行"
~~~

在没有 `--scripted` 时，显式 `--value-column`、`--x-column` 或 `--paired-baseline` 会选择确定性 TableSpec 分析。只提供数据路径仍是原生偏好。加 `--with-report` 和模型可写报告。以下参数也适用于 `research-session`。

| 参数 | 默认值或用途 |
| --- | --- |
| `--data-file PATH` | UTF-8 CSV、TSV 或 JSON 记录。 |
| `--value-column NAME`、`--group-column NAME` | 可重复数值列，可选分组列。 |
| `--observation-unit TEXT`、`--value-unit TEXT`、`--data-attribution TEXT` | 必填的行语义，以及声明的单位和来源。 |
| `--data-mode observations\|values`、`--data-plot bar\|box\|heatmap\|line\|scatter` | 默认 `observations` 和 `bar`。 |
| `--data-missing reject\|omit` | 默认 `reject`。 |
| `--paired-baseline COLUMN` | 同一行内进行候选值减基线值的配对比较。 |
| `--x-column NAME`、`--x-unit TEXT`、`--data-association none\|pearson` | 坐标和关联分析；关联默认 `none`。 |
| `--series-layout separate\|shared`、`--figure-width column\|wide` | 默认 `separate` 和 `wide`。 |
| `--data-max-mb N`、`--data-max-points N`、`--data-max-figures N` | 默认 20 MiB、10000 个位置或单元格、100 张图；超限不抽样。 |

报告上下文在 start 使用 `--material` 和 `--document`，在 research-session 使用 `--material` 和 `--local-document`。只做分析时忽略模型设置。有效参数组合和统计含义见[已有数据描述分析](CONFIG_REFERENCE_zh.md#已有数据的描述分析)。

### `simple-ar models`

~~~bash
simple-ar models --config models.toml
simple-ar models --config models.toml --profile daily --purpose writing
~~~

此命令只在本地检查命名连接，不调用 API，也不显示凭据。

| 参数 | 用途 |
| --- | --- |
| `--config PATH` | 模型目录 TOML；省略时使用 `SIMPLE_AR_MODELS_CONFIG` 或用户配置。 |
| `--profile NAME` | 查看一个命名 profile。 |
| `--purpose text\|code\|writing\|vision\|image` | 连接用途，默认 `text`。 |

字段和路由规则见[命名模型连接](CONFIG_REFERENCE_zh.md#命名模型连接)。

## 成果复用

~~~bash
simple-ar results SESSION
simple-ar start --from-session SESSION --reuse data_analysis --goal "解释取舍"
simple-ar start --from-session SESSION --reuse report --goal "重组讨论"
simple-ar start --from-session SESSION --reuse code_project --goal "修改实现并实际验证"
~~~

`--reuse` 可以重复，写作选择包括 `data_analysis`、`report`、`summary` 和 `code_analysis`。普通写作会用登记的成果包创建新的原生任务，不继承旧账本。原交付保留，有效测量不重新运行。

报告保留来源片段、引用身份和证据；旧正文单独作为草稿。分析包会重新检查复制的数据。元数据不是一手证据，也不构成独立验证。

`code_analysis` 包保留脚本、数据、结果、PNG 和矢量导出。`analysis.py` 是入口，`src` 辅助代码和布局源文件随包保留。结果可以登记带包内相对路径和图注的图形，以及 `citation_map.v1`。已写图注和已知参考文献会保留，不编造缺失书目。没有成果包的旧交付仍可复用文本或 JSON。

`code_project` 会开始隔离修订，保留登记的编辑范围、保护文件、校验器、解释器和补丁策略。普通修订进入原生任务，不再生成中间 TOML 或旧研究计划；执行仍须批准。结构化成果和当前已正式交付、具有固定范围与校验器的原生 CodeTask 工程都可选择，改动或未完成的候选不提供。原任务和账本不变；不能与写作选择混用，也不能覆盖项目、范围或校验器。

经过校验的单命令复现项目可结合 `--kind reproduction`、`--document` 和目标，重复保存的协议、解释器和输出选择器，但仍须确认执行。科研复现及显式技术控制继续遵循结构化合同；配对或扩展协议应在原会话恢复。

`--kind survey` 加 `--reuse report` 会使用原始来源包，排除草稿和派生产物，并默认在线搜索。不同文本版本不合并，来源仍须重新检查相关性。成果复用不是中断执行恢复。

## 概念图

~~~bash
simple-ar image --config models.toml --prompt "方法概览" --output figures/v1
simple-ar image --config models.toml --input figures/v1/image.png --prompt "保留布局，改为绿色" --output figures/v2
~~~

| image 参数 | 用途 |
| --- | --- |
| `--prompt TEXT`、`--output PATH` | 必填提示词和新版本目录。 |
| `--input PATH` | 可选编辑源图，原图保留。 |
| `--config PATH`、`--model SELECTOR` | 模型目录或显式 profile；默认 image 路由。 |

连接能力必须支持所请求的生成或编辑。同一已完成请求会直接恢复，不再调用；失败或结果未知的提交不会重新发送。`openai_images` 要求 base64 输出，不接受仅 URL 的结果。

显式 `cctq_images_async` 会保存 task ID，通过带认证的 GET 恢复同源 `files/0`，不会再次 POST，也不会回退到其他 API 或 provider。已有完成 ID 可直接下载；恢复时可以调整超时和 HTTP 设置。没有 ID 的未知提交不会重发。

栅格概念图不是测量结果，也不是可编辑矢量图。

### 检查已渲染图形

~~~bash
simple-ar image-review --config models.toml --input figures/v1/image.png --goal "检查标签与箭头" --output figures/review-v1
~~~

必须提供输入、目标和输出目录；输入可重复，最多四张。可选 `--config` 和 `--model` 选择 vision 路由。静态 PNG、JPEG 和 WebP 的总大小不超过 20 MiB，每张不超过 4,194,304 像素。

检查使用连接的输出上限；未配置时回退为 3000 token。缺失 usage 会记录为未知。原图、原始响应、`review.json` 和 `feedback.md` 会保存；无效响应保留在 `rejected-response.json`。

相同的已完成请求会复用，已保存原始响应可以离线规范化。输入改变需要新版本。没有响应的中断请求不会重发。检查不修改图形，也不认证其正确性。

## 项目准备

`simple-ar project-info --project DIR [--data-path PATH ...] --output NEW_DIR` 保存 `preparation.json` 和 `preparation.md`，记录索引条目、依赖、片段和声明的数据位置。相对数据路径以项目根目录为基准。

此命令不导入或运行项目代码，不安装或下载。对解释器的观察不证明目标环境已经准备好。输出必须使用未占用的目录。

## Research 命令

<a id="simple-ar-research-session任务驱动的-canonical-入口"></a>

### `simple-ar research-session`（结构化与兼容入口）

~~~bash
simple-ar research-session --config research.toml
simple-ar research-session --config research.toml --session-root SESSION --model env
~~~

这是精确的结构化接口。显式 CLI 覆盖研究 TOML；直接新建省略模型时为确定性模式，配置文件默认 `env`。模型失败仍是失败。所有新任务使用共同行动所有者，材料、简报、代码和实验按声明合同组合。读取来源本身不要求科学设计。不支持的声明在创建前报错，不回退旧控制器。表格可不调用模型，成文报告需要模型。无模型的实验请求可先保存材料，保留尚未完成的请求后暂停。沿打印的 `start --resume-task SESSION` 继续同一任务，并通过[明确补齐条件](#原生任务恢复)添加缺失合同或模型授权；收集材料不等于实验或报告完成。已有历史会话保留原所有者。

设计回复使用 `start --resume-task SESSION --decision-id ID --decision-response accept|reject|revise`，revise可附 `--decision-guidance TEXT`。ID绑定实际设计/协议字节；确认不会运行或授权进程。恢复保留冻结合同；文字修改使用普通原生反馈，不重复传新建参数。报告反馈可复用未变的表格结果，无需重算。新旧确认范围见[参与策略](#参与策略与待处理决定)。

| 参数组 | 选项和默认值 |
| --- | --- |
| 任务 | `--config PATH`、`--topic TEXT`、`--task-kind KIND`（`auto`）、`--outputs OUTPUT ...`、`--output-root PATH`（`runs/research-session`）。 |
| 模型和 API 预算 | `--model SELECTOR`、`--feasibility-review-model NAME`、`--total-tokens N`、`--llm-requests N`、`--max-output-tokens N`。 |
| 进程预算 | `--process-invocations N`、`--process-wall-seconds N`。 |
| 来源 | 可重复 `--local-document PATH`、`--material PATH`、`--data-path PATH`、`--query TEXT`、`--provider NAME`；`--cache-dir PATH`。 |
| 检索边界 | `--max-results N`（10）、`--max-chunks N`（300）、`--idea-limit N`（3）、`--max-fulltext-documents N`、`--max-pdf-mb N`、`--web-extract-backend direct\|tavily_basic`。 |
| 研究 | `--max-research-iterations N`（1）、`--interaction POLICY`（`checkpoints`）。 |
| 执行 | `--cwd PATH`（`.`）、`--timeout-sec N`、`--label TEXT`（`research-session`）、`--primary-metric NAME`；可重复 `--metric NAME` 和 `--metric-direction NAME=DIRECTION`；`--code-task-config PATH` 或末尾的 `--command ARGV...`。 |
| 报告 | `--with-report` 或 `--no-report`；二者都不能与显式 outputs 合用。模型任务通常请求报告，数据分析除外，需要显式请求。 |
| 恢复和决定 | `--session-root PATH`、`--recover-interrupted`、`--reanalyze`、`--decision-id ID`、`--decision-response accept\|reject\|revise`、`--decision-guidance TEXT`。 |
| 续接 | `--authorization-id ID`、`--authorization-reason TEXT`；可重复 `--authorize-remaining DIMENSION=AMOUNT`；`--additional-attempts N`、`--additional-no-progress N`。 |

数据参数与前面的表格共用。全文授权、网页提取、配对协议、输出附件和环境准备通过[研究 TOML](CONFIG_REFERENCE_zh.md#研究-toml分区与默认值) 配置，不应编造对应 CLI 参数。

历史结构化会话保留有界计划纠正及 `attempts/plan-*/task_plan_proposals.json`，新任务使用共同动作记录。CodeTask 保留项目、工作区、基准及编辑范围；经审阅的大改动需要 `execute.allow_large_edits=true`。默认进程额度由实际执行所有者计算，含准备及修复复验，显式限制包括零始终优先。

结构化修复省略进程限制时，会为校验及配置的修复和复测预留容量；显式限制始终优先，包括零。

#### 恢复与重分析

对于由 ResearchApplication 承接的已保存 legacy 会话，恢复保留历史。只有命令、schema、协议、准备状态和保护资产一致时，才复用测量。显式修改目标、输出、本地论文或执行设置，会重新规划相关工作。改变 task-kind 需要新会话。不兼容的搜索或摄取修改会失败。

省略报告参数会保留原设置；改变报告设置只重建报告输出。`--recover-interrupted` 确认旧 worker 已停止，并在同一账本内关闭仍为 running 且没有结果的尝试。输入修改必须另行提交。已经保存的 API 失败用普通恢复处理。

这些 legacy 会话的 `--reanalyze` 必须与输入修改和授权分开。它重新考虑保存的测量，可能引出已授权的后续实验，但不请求报告。已有待续接工作必须先完成。新共同任务使用[原生任务恢复](#原生任务恢复)。

### 报告控制参数

| research-session | research-report | 含义 |
| --- | --- | --- |
| `--report-writing-method article\|sections` | 保留保存的方法 | 新任务默认 article；显式分段控制选择 sections；不兼容 article 参数会失败。 |
| `--report-template NAME_OR_PATH` | `--template NAME_OR_PATH` | 结构，session 默认 auto；报告恢复省略时保留原值。 |
| `--report-reviewer llm\|disabled` | `--reviewer llm\|disabled` | 明确整稿审阅/范围后，0轮只检查，正数轮次允许有界完整候选纠正。 |
| `--max-review-iterations N` | 同左 | 候选/纠正上限，请求时CLI默认1；失败提案仍计入轮次。 |
| `--max-section-tokens N` | 同左 | 0 或未设置表示没有单次调用上限；正数限制当前写作单元。 |
| `--report-document-review`、`--no-report-document-review` | `--document-review`、`--no-document-review` | 全文审阅。 |
| `--report-review-scope section\|document` | `--review-scope section\|document` | 审阅时机。 |
| `--report-draft-scope section\|document\|auto` | `--draft-scope section\|document\|auto` | 按节、联合起草，或计划不超过 2000 词时联合起草。 |
| `--report-outline-strategy auto\|template\|adaptive` | `--outline-strategy auto\|template\|adaptive` | 提纲选择。 |
| `--report-data-tables linked\|full` | `--data-tables linked\|full` | 链接记录，或同时包含数据表。 |
| `--max-document-review-prompt-chars N` | 同左 | 可选的完整审阅请求长度上限。 |

sections 默认按节起草和审阅，不做全文审阅；提纲为 auto，数据表为 linked，没有额外审阅长度上限。联合起草要求全文审阅、document scope 和 full 来源策略。历史方法和 checkpoint 会保留。

article 默认进行确定性组装和审计；明确的整稿独立检查及有界纠正由共同写作所有者承接。详细约束见[已有材料写作](CONFIG_REFERENCE_zh.md#已有材料写作)。

#### 参与策略与待处理决定

新共同任务的 `interaction` 只控制协议/设计确认：`assisted` 和 `checkpoints` 等待对生成设计的回复，`autonomous` 在保存合同内接受有效生成设计。它不增加阶段或交付里程碑，也不授予进程权限。缺事实、资产或权限仍会阻止相关工作。

固定测量新建在显式提供 `--interaction checkpoints` 或 `assisted` 时要求协议确认。共同任务冻结调用方实际协议并打印 decision ID。先按打印的 `start --resume-task ... --decision-id ... --decision-response accept` 接受协议，再用精确 token 单独批准待执行进程。接受保留原命令队列，不执行命令、不伪造研究设计；交付后重放相同调用方回复幂等。

仍由 ResearchApplication 续接的已保存 legacy 会话保留里程碑策略：`assisted` 确认协议、交付及补充/修订研究选择；`checkpoints` 确认首个协议、候选修订及交付变化，同时允许有界的同协议补充；`autonomous` 在保存边界内选择有效选项。下列旧会话回复示例及交付/模式修改规则仅适用于这些会话：

~~~bash
simple-ar research-session --session-root SESSION --model env --decision-id ID --decision-response accept
simple-ar research-session --session-root SESSION --model env --decision-id ID --decision-response revise --decision-guidance "修订方向"
~~~

Rich 会显示决定，不等待 stdin。修改交付还需要报告设置。答案与提案分开保存；精确重放会恢复已保存的中断修订，改变条款或输入则不能复用旧决定。

模式修改须与重分析或单独报告刷新分开，但交付答案可以同时修改报告或模式。

legacy 续接额度必须有稳定 ID 和原因，既有使用、未知消耗和计数器保留。同一 ID 和条款具有幂等性，不补回已消耗容量。尝试次数和无进展次数增量会提高对应上限。

单独授权不重开已完成工作。报告刷新前先授权；通常写作、组装和审计需要三次尝试，失败另计。只改报告不需要增加进程容量。不要手改账本或 manifest。

### 固定协议复现

使用 `--task-kind reproduction`、本地来源和固定协议进行复现。measurement 则原样运行一个已有命令，仅做实验。二者都不代表能自动发现任意论文环境，也不保证科学成功。

所有measurement新建均进入共同任务；固定条件、来源、环境准备和报告按各自合同承接。纯调用方字面命令只携带初次执行意图，确认协议或研究要求不自动授予进程权限；重测及重试仍需批准。恢复沿打印的 `start --resume-task`，未完成工作不作为成功退出。不支持的新声明在创建前报错，不回退旧调度；已有会话保留原恢复所有者。

独立校验、适配器创建、解释器和指标选择器由配置声明，见[执行与报告](CONFIG_REFERENCE_zh.md#执行与报告)。

#### 单候选修复与复现

明确的单候选复现若有 CodeTask、独立声明且不同于正式测量的 validator，以及平直正式命令，
使用共同任务，可同时冻结报告要求。验证、正式指标／协议和实际候选分别绑定；
项目内绝对路径指向隔离候选，外部输入和选定解释器保持身份。反馈可只改报告、不重测。
兼容的已交付单命令成果可用 `--reuse code_project --kind reproduction` 复用，不导入旧账本。

显式环境准备和适配文件由同一所有者承接；需要比较时选择 auto，而非严格 reproduction 快捷入口。
普通开放任务不被缩成这个专家合同。

#### 固定测量条件

没有 CodeTask 的 `measurement` 新建，支持通过共同任务执行平直单命令或明确的baseline-run比较（pairs，或带声明seed_flag的紧凑种子配置）。每条字面条件只授权初始一次，保留原cwd、argv和继承环境；明确的任务venv配置另绑定实际解释器。协议只有seeds、没有argv绑定时仍是内部批处理，不增加进程。完整声明不能被静默缩减或换配；完成交付要求实际条件及对应确定性分析，缺测、失败或条件不可比须明确保留。

普通 CodeTask benchmark 即使 validator 与正式 argv 相同也可迁共同 owner，但仍须独立物理调用和记账，校验结果不算测量；严格 reproduction 仍要求不同命令。

声明条件可附带必需新 Python 文件和一次 current/venv 准备，不必更换任务类别；精确范围及来源检查保留。默认预算含准备步骤，显式限制不改；venv 的 Python 别名交准备 owner 选择真实解释器。

greenfield `copy`/`sparse_copy` 有真实 source 项目时可测源码 baseline，条件显式标记 `source="preparation"`、`before_implementation=true`：先 prepare，完成全部声明基线，再调用 `implement(prepared_result)` 并传实际成功基线事实。失败或缺测阻止生成。empty/无source项目不能虚构未改源码基线，须提供真实源码或已核外部测量证据。

精确argv明确选中的外部已有文件沿单文件只读来源传递，并在批准时绑定hash，也支持`--option=FILE`。不会挂载其父目录或推断目录授权，数据/材料目录须显式提供。域摘要缺失时，只在产物和候选核验后复用完整已存观察，不重跑模型或校验器。

有模型时，解释和后续工具共用原任务/账本，需要Chat连接及有限请求/token额度。无模型可完成调用方已声明测量和确定性分析，但缺少模型、设计或报告的工作仍待续。沿打印的start --resume-task恢复，未知操作不重放。

测量加写作可用`--task-kind measurement --outputs experiments report --model SELECTOR`及既有报告选项。不要求先修改代码：Writer消费真实测量和对应确定性分析；参考材料沿local-document/material输入。反馈要求新的规范写作回执，原结果、稿件与账本保留，不重跑测量。缺分析或旧稿不能算完整报告交付。

#### 准备测量环境

测量可带 `execution.environment`：当前环境检查，或任务venv、声明的依赖/项目安装及短检查。

冻结多条件共享一个真实attempt的准备回执，要求相同cwd/profile及兼容解释器，后续条件不重复安装。准备前核全部条件与准备的有限额度，显式限制包括零始终优先；来源或准备变化、中断与未核清进程均阻断复用。检查指标不是正式测量。安装可能执行构建代码、使用网络，并非OS沙箱；不能替换CodeTask自有环境。

### `simple-ar research-report`

对于已保存 ResearchApplication 会话，`simple-ar research-report --session-root SESSION --model SELECTOR [--refresh]` 为已准备好的分析交付补写报告。报告控制见上表；省略参数保留保存的设置。

已完成报告会幂等返回。refresh 基于保存证据创建新的报告和审计尝试，不重新测量或分析。尝试或无进展上限耗尽时，必须显式授权 session。禁用 reviewer 不会禁用 audit。

legacy v1 记录保持只读；会话无效时不会回退到另一个执行器。新共同任务通过[原生反馈或补齐条件](#原生任务恢复)请求写作，并由同一所有者继续。

### `simple-ar report-export`

将已组装报告转换为可移动 LaTeX 工程，可选编译 PDF。
输入须有 `report_body.md`，有引用时还须有书目。只转换已有文本，不调用模型或实验，
不是任意 Markdown 转换器。

| 参数 | 含义／默认值 |
| --- | --- |
| `--report-dir PATH`、`--output PATH` | 必填：组装后的输入目录和新输出目录 |
| `--title TEXT`、`--compile` | 可选标题及 PDF 编译 |
| `--latex-template PATH` | 相对报告目录的可信 Pandoc LaTeX 模板；否则使用单栏 ACM |
| `--template-asset FILE` | 可重复：模板旁的平铺配套文件；要求 `--latex-template` |
| `--tex-engine pdflatex\|xelatex` | 默认 pdflatex；显式指定要求 `--latex-template` |
| `--citation-style numeric\|template` | 可选引用样式；省略保留历史渲染 |

```bash
simple-ar report-export --report-dir REPORT_ATTEMPT --output NEW_DIR --title "研究报告" --compile
simple-ar report-export --report-dir REPORT_ATTEMPT --output NEW_DIR --latex-template layout/article.latex --citation-style numeric --compile
```

#### 模板与引用要求

模板须在 `\begin{document}` 后包含 `$body$`，导言区包含
`$for(header-includes)$$header-includes$$endfor$`。文档类、正文布局及字面书目样式由模板控制。
有界模板包最多 32 份平铺配套文件、共 16 MB，不支持片段导入、脚本或自定义构建命令。
导出器不发现模板或下载依赖；可信 TeX 不是沙箱。

`numeric` 在模板设置后配置 natbib，使用方括号数字，包括叙述式及省略作者的引用。
模板须加载 natbib；已加载时只配置，不重复传入冲突包选项。无作者条目显示数字而非来源 ID。
citekey 和 BibTeX 条目保持原样，不推测作者或年份。

`template` 保留模板默认样式，内置 ACM 使用 `acmnumeric`。
`$biblio-style$` 默认 plainnat，显式 numeric 时使用 **plainurl**。
plainurl 需要 TeX Live／MiKTeX 的 **urlbst** 包，保留 URL／DOI 而不打印无作者记录的内部 key；
不回退到 plain。模板字面指定的自定义样式仍控制自己的格式。

仅显式选择才在 `export.json` 增加 `citation_style`。原生 `export_report` 也可在模板包之外接受
`citation_style: "numeric" | "template" | null`，可依据普通出版目标选择样式，无需用户输入 hash。
省略／null 不进入序列化请求与输入绑定，保留历史 hash。显式选择绑定审批、请求和 manifest；
换样式须使用新导出标识／输出。恢复不为旧产物换样式或重新编译。

#### 文件与编译

保留整个输出：`main.tex`、`body.tex`、`source.md`、书目、图形、`export.json`，
以及保存字面代码的 `code/block-*.txt`。本地图形及登记矢量版本会复制；SVG 转换需要
`rsvg-convert`。不获取远程图片；外部证据链接转为标签，在 `export.json` 记录目标，不复制数据集。

登记图注绑定导出，只移除紧邻的完全重复图注；无 manifest 时保留图注。
图形使用 `htbp`，不强制固定位置。

编译需要 Pandoc、`pdflatex`／BibTeX／`acmart`、`fvextra`、`seqsplit`、`etoolbox`
及可缩放回退字体（Debian／Ubuntu 的完整 `cm-super`）。缺工具或编译失败返回非零，
保留源码，并在适用时写入 `diagnostics/build.log`。非拉丁文本可能需要调整引擎／字体。
编译前安装缺少的依赖。逐页查看 PDF；编译不认证质量、页数或投稿选项。每次导出使用新目录。

## CodeTask 命令

CodeTask 准备隔离工作区，不直接编辑源项目。已有项目默认 `auto`，可行时优先使用 detached worktree，并记录复制回退。显式 `git_worktree` 失败时不会回退。greenfield 默认从空工作区开始生成。隔离工作区不是操作系统级沙箱。

### 高级编排命令

已有项目的 execute 通常使用一个补丁计划。只有显式选择 to-step 或已有 work plan 时，才使用工作计划和批次路径。

交互终端会在计划、提案和大改动关卡询问。非交互执行会停止，除非用户明确使用 `--yes`。execute 的 `--yes` 自动批准审阅关卡，与原生 start 的 `--yes` 不同。`--no-review-inline` 在关卡停止；`--interactive` 逐个确认底层步骤，便于调试。

#### 规划失败与方法迁移

规划失败会保留产物，完整响应的修正次数有界。离线回退需要显式 `--no-llm` 或 `--allow-planning-fallback`。传输失败、结果未知或超过 deadline，不会被当成回退成功。

greenfield 的 compact 方法替代已退出的分阶段生成和审阅。`tool_agent` 和非零 `--planning-review-rounds` 会显示迁移提示，不增加调用。已退出的模型阶段路由会失败。

已生成项目和不可变的已接受规划会复用，不重新生成。旧规划与失败保留在 `planning/method_migration.json` 和 `retired_method` 中。

#### 审阅与修复

依赖建议只用于检查，不会安装。应用后和运行后的审阅将发现写入 memory，不代表验收通过。改变审阅约束会保留历史。

有界修复检查输入和文件字节，保留快照及回滚记录，并同步 `code_artifacts.json`。中断候选不会盲目重放。离线 scaffold 会在生成假指标之前失败。

#### `simple-ar code-task init`

创建 code-task run，准备可编辑 workspace，并构建初始代码索引。

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task init --code-root path/to/project --task-file task.md --benchmark-command "python main.py"
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `--config PATH` | path | init 设置 TOML；CLI 参数覆盖配置值。 |
| `--kind MODE` | enum | `existing_project` 表示已有项目 patch；`greenfield` 表示从零生成项目。 |
| `--code-root DIR` | path | 源项目。`existing_project` 必填；`greenfield` 仅在需要 scaffold/source root 时填写。 |
| `--task-file PATH` | path | Markdown/text 任务描述。除非配置中已设置，否则必填。 |
| `--output-root DIR` | path | code-task run 创建位置。 |
| `--name TEXT` | string | run 名称后缀。 |
| `--benchmark-command TEXT` | string | 在 workspace 中 patch 前后运行的命令。 |
| `--primary-metric NAME` | string | before/after verdict 使用的主指标。 |
| `--metric-direction NAME=DIRECTION` | repeatable | 指标方向：`higher`、`lower`、`resource` 或 `ignore`。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | `--env-mode external` 的 Python。 |
| `--workspace-mode MODE` | enum | CLI 接受 `auto`、`copy`、`git_worktree` 和 `sparse_copy`。`greenfield` 默认使用空工作区，也可通过 TOML 声明 `empty`；CLI 不接受 `empty`。 |
| `--workspace-include GLOB` | repeatable | `sparse_copy` include pattern。 |
| `--workspace-exclude GLOB` | repeatable | `sparse_copy` 额外 exclude pattern。 |
| `--workspace-reuse-source-venv` | flag | 检测并复用 source `.venv` Python。 |
| `--max-file-bytes N` | int | copy/sparse 模式最大复制文件大小，`0` 表示禁用。 |

产物：

- `code_task/manifest.json`
- `code_task/task.md`
- `code_task/workspace/`
- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

#### `simple-ar code-task execute`

根据当前 run 产物推进到下一个安全停止点。

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute runs/<run-id> --to-step propose-edits
uv run simple-ar code-task execute runs/<run-id> --apply-proposed-edits --timeout 60
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--config PATH` | path | 可选 TOML，用于模型路由、预算和运行设置。 |
| `--to-step STEP` | enum | 最多运行到 `probe`、`baseline`、`work-plan`、`batch`、`plan`、`propose-edits`、`apply-edits`、`review`、`validate`、`run`、`analyze-failure` 或 `repair`。 |
| `--dry-run` | flag | 只打印下一步动作，不写产物。 |
| `--model NAME` | string | LLM 步骤模型覆盖。 |
| `--no-llm` | flag | 尽可能使用 deterministic fallback。 |
| `--timeout N` | int | benchmark timeout。 |
| `--baseline-policy MODE` | enum | 已有项目 baseline 策略：`auto`、`run`、`skip`、`provided` 或 `none`。昂贵 baseline 可用 `skip`/`none` 跳过，或用 `provided` 记录已有指标。 |
| `--baseline-metrics-file PATH` | path | `--baseline-policy provided` 时读取的 JSON 或 `metric=0.82` 文本指标文件。 |
| `--reuse-planning-from PATH` | path | 检查任务约束 hash 后，导入不可变的已接受 greenfield 规划；不导入生成代码、memory、审阅发现或修复历史。 |
| `--review-gate strict\|runtime` | enum | strict 阻止所有 blocking 发现；runtime 阻止安全或直接可执行性问题，并保留包装和报告方面的缺口记录。 |
| `--planning-review-rounds N` | int | 已弃用，应使用 0；非零不授予额外调用。 |
| `--planning-mode MODE` | enum | Greenfield 完整架构工作单元，默认 `compact`；`tool_agent` 为弃用别名，进入替代方法前显示迁移说明。 |
| `--yes` | flag | 普通 execute 模式下自动批准 inline 审核门；与 `--interactive` 一起使用时，自动继续 primitive prompts。只有明确接受审核风险、想自动化跑通时才使用。 |
| `--interactive` | flag | 调试模式：逐个 primitive step 确认，而不是连续运行到下一个审核门。 |
| `--no-review-inline` | flag | 禁用 inline 审核提示，在审核门直接停止。 |
| `--skip-validation` | flag | 静态验证未通过时仍运行 benchmark。 |
| `--strict-validation` | flag | 将较高风险 validation warning 视为 error。 |
| `--validation-max-file-bytes N` | int | 静态验证扫描文件大小上限。 |
| `--apply-proposed-edits` | flag | plan 批准后应用已审核的 `proposed_edits.json`。 |
| `--allow-large-edits` | flag | 允许已审核、超过 normal 预算的较大 proposal。 |
| `--allow-planning-fallback` | flag | 允许明确离线规划或完整且用量已知的响应格式失败后 fallback；不在提供方、预算、时限或未完成流失败后使用。 |
| `--llm-retry-attempts N` | int | work/patch 规划和 greenfield 生成的完整响应纠正上限，含首次；运输重试由共享连接负责，未核清操作阻止纠正与 fallback。 |
| `--repair-rounds N` | int | 失败后的 bounded repair proposal 轮数。 |
| `--max-files N` | int | LLM 步骤上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

产物：

- `code_task/work_plan.md` 和 `work_plan.json`
- `code_task/attempts/attempt-*/batches/batch-*/batch_state.json`
- `code_task/patch_plan.md`
- `code_task/meta/proposed_edits.json`
- `code_task/meta/applied_edits.json`
- `code_task/meta/review_report.json` 和 `review_report_post_run.json`
- `code_task/meta/validation_report.json`
- `probe` 后的 `code_task/meta/resource_probe.json` 和 `resource_decision.json`
- `code_task/memory/task_memory.md`、`compressed_memory.md` 和 `review_findings.jsonl`
- `code_task/run/baseline/`、`code_task/run/patched/`、`code_task/run/comparison.json`
- `code_task/summary.md`

#### `simple-ar code-task decide-plan`

记录当前 patch plan 的人工审核决定。

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--decision VALUE` | enum | `approve`、`reject` 或 `revise`，必填。 |
| `--note TEXT` | string | 可选审核备注。 |
| `--reviewer TEXT` | string | 审核人标签，默认 `user`。 |

产物：

- 更新 `manifest.json` 中的 plan decision 状态

### 底层原语命令

这些命令接受 `RUN_DIR`，通常由 execute 管理。规划和提案命令共用模型、`--no-llm` 和有界上下文设置。支持 `--force` 的命令可以重新生成已有提案。基准运行成功不等于目标改善。

#### `simple-ar code-task map`

从 editable workspace 重建 repo-map 产物。

```bash
uv run simple-ar code-task map runs/<run-id> --show-summary
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--no-refresh-index` | flag | 复用已有 `codebase_index.json`。 |
| `--show-summary` | flag | 打印 `repo_map_summary.md`。 |

产物：

- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

#### `simple-ar code-task locate`

从 repo map 中排序可能相关的可编辑文件和只读证据文件。

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve classifier"
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--query TEXT` | string | locate query，默认使用 `code_task/task.md`。 |
| `--top-k N` | int | 每组候选数量，默认 `8`。 |
| `--refresh-map` | flag | 排序前重建 index 和 repo map。 |
| `--no-read-only` | flag | 省略受保护的只读证据文件。 |
| `--show-summary` | flag | 打印 `locate_results.md`。 |

产物：

- `code_task/meta/locate_results.json`
- `code_task/meta/locate_results.md`

#### `simple-ar code-task context`

构建受限的 prompt-ready context pack。

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--query TEXT` | string | locate query，默认使用任务文件。 |
| `--top-k N` | int | locate 候选预算。 |
| `--max-files N` | int | 纳入 snippet 的文件上限。 |
| `--max-source-chars-per-file N` | int | 单文件 snippet 字符预算。 |
| `--max-total-chars N` | int | 总 snippet 字符预算。 |
| `--refresh-map` | flag | 打包前重建 repo map。 |
| `--show-prompt` | flag | 打印 `prompt_context.md`。 |

产物：

- `code_task/context_packs/context-NNN/context_pack.json`
- `code_task/context_packs/context-NNN/prompt_context.md`
- `code_task/context_packs/context-NNN/selected_snippets.jsonl`

#### `simple-ar code-task probe`

检查 workspace runtime 和项目环境信号。

```bash
uv run simple-ar code-task probe runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

产物：

- `code_task/meta/environment_report.json`

#### `simple-ar code-task baseline`

patch 前运行记录的 benchmark。

```bash
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--command TEXT` | string | 覆盖本次 benchmark command。 |
| `--timeout N` | int | benchmark timeout。 |
| `--skip-validation` | flag | 静态验证未通过时仍运行。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

产物：

- `code_task/run/baseline/execution_report.json`
- `code_task/run/baseline/stdout.txt`
- `code_task/run/baseline/stderr.txt`
- `code_task/run/baseline/metrics.json`

#### `simple-ar code-task work-plan`

生成面向批次执行的 implementation work plan。

```bash
uv run simple-ar code-task work-plan runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 使用 fallback planning。 |
| `--force` | flag | 重新生成已有 work plan。 |
| `--allow-planning-fallback` | flag | 完整且用量已知的 work-plan 响应格式失败后允许 fallback，不用于运输失败。 |
| `--llm-retry-attempts N` | int | work planning 的完整响应纠正上限，含首次，不另建运输重试层。 |
| `--max-files N` | int | planning 上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |

产物：

- `code_task/work_plan.json`
- `code_task/work_plan.md`

#### `simple-ar code-task batch`

为某个 work-plan item 创建 attempt/batch 状态目录。

```bash
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--work-item ID` | string | work-plan item id，例如 `W1`，必填。 |
| `--attempt-id ID` | string | 可选 attempt id，例如 `attempt-001`。 |
| `--force` | flag | 即使已有 batch，也创建新 batch。 |

产物：

- `code_task/attempts/attempt-NNN/attempt_state.json`
- `code_task/attempts/attempt-NNN/batches/batch-NNN/batch_state.json`

#### `simple-ar code-task plan`

为 active batch 生成可人工审核的 patch plan。

```bash
uv run simple-ar code-task plan runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 使用 fallback plan。 |
| `--force` | flag | 重新生成已有 plan。 |
| `--allow-planning-fallback` | flag | 完整且用量已知的 patch-plan 响应格式失败后允许 fallback，不用于运输失败。 |
| `--llm-retry-attempts N` | int | patch planning 的完整响应纠正上限，含首次，不另建运输重试层。 |
| `--max-files N` | int | 上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |

产物：

- `code_task/patch_plan.md`

#### `simple-ar code-task propose-edits`

请求模型生成受控 old/new text edits。

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 写入 deterministic empty proposal。 |
| `--force` | flag | 重新生成已有 proposal。 |
| `--max-files N` | int | 可编辑上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |
| `--allow-large-edits` | flag | 人工审核后接受较大但仍受限的 proposal。 |

产物：

- `code_task/meta/proposed_edits.json`
- `code_task/meta/proposal_warnings.json`
- 如果存在 active batch，也会写入 batch 级 proposal 产物

#### `simple-ar code-task apply-edits`

在 workspace 中安全应用受控 old/new edits。

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--edits-file PATH` | path | 指定要应用的 proposal 文件。 |
| `--allow-unapproved-plan` | flag | 本地测试/demo 时绕过 plan approval。 |
| `--allow-large-edits` | flag | 应用已审核、需要 large-edit approval 的 proposal。 |

产物：

- 修改 `code_task/workspace` 下的文件
- `code_task/meta/applied_edits.json`
- `code_task/patch.diff`

#### `simple-ar code-task validate`

对 workspace 运行轻量静态验证。

```bash
uv run simple-ar code-task validate runs/<run-id> --strict
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--strict` | flag | 将较高风险 warning 视为 error。 |
| `--max-file-bytes N` | int | 扫描文件大小上限。 |

产物：

- `code_task/meta/validation_report.json`

#### `simple-ar code-task run`

patch 后运行 benchmark。

```bash
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--command TEXT` | string | 覆盖记录的 benchmark command。 |
| `--timeout N` | int | benchmark timeout。 |
| `--skip-validation` | flag | 静态验证未通过时仍运行。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

产物：

- `code_task/run/patched/execution_report.json`
- `code_task/run/patched/stdout.txt`
- `code_task/run/patched/stderr.txt`
- `code_task/run/patched/metrics.json`
- baseline metrics 存在时写入 `code_task/run/comparison.json`

#### `simple-ar code-task analyze-failure`

总结最近失败的 validation 或 benchmark。

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |

产物：

- `code_task/run/patched/failure_analysis.md` 或 `code_task/meta/failure_analysis.md`

#### `simple-ar code-task repair`

根据最近失败上下文提出受限 repair edits。

```bash
uv run simple-ar code-task repair runs/<run-id>
```

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 写入 deterministic empty repair proposal。 |
| `--max-files N` | int | repair 上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |

产物：

- `code_task/repairs/repair-NNN/proposed_edits.json`
- 更新 `code_task/summary.md`

apply-edits 写入前会检查路径、范围、旧文本的唯一匹配和预算。空 old 文本只在允许但不存在的路径创建文件，不替换已有文件。新文件需要显式范围或批次目标，并使用相同的快照和回滚机制。

repair 提案不会自动应用。请审阅后用 `--edits-file` 指定应用。静态校验采用保守规则，不证明运行正确。probe 不安装依赖，也不运行基准。详细编辑约束见[配置参考](CONFIG_REFERENCE_zh.md#编辑范围规则)。

## 成果工具

### `simple-ar inspect`

`simple-ar inspect RUN_DIR` 写入 `artifact_index.json`，包含元数据和有界预览。它不跟随符号链接或特殊文件，默认 `sha256` 为 null。Python 调用者可显式启用 `hash_files`。这不是全文读取。

### `simple-ar search-artifacts`

`simple-ar search-artifacts RUN_DIR QUERY [--top-k N] [--include-operational]` 会建立自己的当前索引。`--top-k` 默认 8；操作记录默认排除，需显式包含。

命令打印片段和路径，并写入 `artifact_index.json`、`artifact_chunks.jsonl` 和 `artifact_search_results.json`。二进制文件不切分。大于 8 MiB 的文件会报告 `file_too_large`，不会把前缀搜索冒充为全文搜索。

### `simple-ar clean`

~~~bash
simple-ar clean RUN_DIR
simple-ar clean RUN_DIR --all-caches
simple-ar clean --shared-index
simple-ar clean --shared-cache
~~~

| 参数 | 作用 |
| --- | --- |
| `RUN_DIR` | 只有共享清理时可省略。 |
| `--yes` | 无需交互确认，删除预览列出的目标。 |
| `--all-caches` | 扩大运行目录内可重建的缓存、索引和上下文清理范围。 |
| `--shared-index` | 清理跨运行的历史 SQLite 和 LanceDB 索引。 |
| `--shared-cache` | 还清理文献 provider 缓存和 `agent_handoff_archives`。 |
| `--index-root PATH` | 使用环境索引根目录或默认 `.simple_ar_cache/research_index`。 |
| `--literature-cache-root PATH` | 默认 `.simple_ar_cache/literature`。 |
| `--allow-external-index-root` | 允许清理工作区之外的共享索引。 |

清理会预览目标，保留运行目录本身、报告、manifest 和证据。当前 v2 清理识别出的缓存、文献查询元数据和 `artifact_search_results`；all-caches 还清理成果索引和片段。

尝试、输出、文档、全文和未识别文件会保留；不会跟随自定义外部缓存。超过 1 MiB 的查询元数据保持未检查状态，不会只凭文件名删除。可通过 `index_meta` 清理工作区内共享 SQLite 行。

旧布局会清理识别出的全文和提取缓存。all-caches 可以清理可移动的 `research_index/chunks.jsonl`，以及 repo-map、locate 和 context 缓存；最终报告、元数据、基准和审计成果仍保留。

共享清理不删除运行目录内成果。历史持久索引已不参与资料检索；清理 provider 缓存后可能需要重新查询。shared-cache 还会删除保留的外部 agent 对话记录。

### `simple-ar status`

`simple-ar status RUN_DIR` 和 `simple-ar results SESSION` 不调用模型，只读取保存状态。status 支持原生任务、research、CodeTask 和旧布局。canonical v2 显示状态、当前尝试、预算、计数器和最近决定；v1 只读。

交接或续接提示不代表后台 worker 正在运行。results 列出当前交付文件、局限和恢复命令。反馈修订暂停时，不会把旧交付当作当前请求已完成。

## 工具与 MCP

### `simple-ar tools schema`

`simple-ar tools schema [--format mcp|openai] [--output PATH]` 导出登记工具的 schema。默认格式为 mcp，省略输出路径时写到 stdout。

### `simple-ar tools call`

`simple-ar tools call RUN_DIR TOOL_NAME [--args-json JSON | --args-file PATH] [--debug-payloads]` 调用登记的只读运行工具。参数必须是 JSON 对象；args-file 可避免 shell 引号问题。

命令打印 JSON，并写入 `RUN_DIR/tools/tool_trace.jsonl`。debug 会保留更大的 payload。

### `simple-ar tools serve-mcp`

`simple-ar tools serve-mcp RUN_DIR [--debug-payloads]` 通过 stdio 提供只读实验工具。支持 `initialize`、`ping`、`tools/list` 和 `tools/call`，不启用写入、shell、网络或安装能力。

## 历史与开发命令

### `simple-ar research-brief`（兼容别名）

`research-brief` 是仅输出简报的兼容别名，保留默认目录 `runs/research-brief` 与可选模型。新建进入共同任务，保留原文、阅读/综合和简报产物，不另建控制器或强制调用规划。覆盖不足保持限定交付。不选择模型是确定性模式，不是模型失败回退。按打印的 `start --resume-task SESSION` 继续；`research-session --session-root SESSION` 也会识别已保存的所有者。历史尝试和账本不改写。

可重复 `--local-document`、`--query` 和 `--provider`。默认 `--max-results=10`、`--max-chunks=300`、`--idea-limit=3`；可选 `--cache-dir` 共享全文缓存。

省略模型时使用确定性流程，不把模型失败改为离线成功。已保存会话用 `research-session --session-root SESSION` 继续；历史尝试和账本不变。

### `simple-ar research-session-continue`

~~~bash
simple-ar research-session-continue --session-root SESSION --parent-attempt-id ID --model SELECTOR --cwd PATH --timeout-sec 300 --label research-session-recovery --primary-metric NAME --metric NAME --metric-direction NAME=DIRECTION --command ARGV...
~~~

`--session-root` 和末尾命令必填，其他示例选项可省略，`--metric` 和 `--metric-direction` 可重复。

此兼容命令使用保存的 ResearchApplication 所有者（canonical v2），只允许在技术候选 failed 或 timed_out 后，补交一次修正的简单显式实验。科学上的负结果仍是证据。cwd 默认 `.`，超时默认 300 秒，label 默认 `research-session-recovery`。

未显式覆盖时，保留最近候选和 schema。父尝试及文献保留，确定性分析重新运行。配对、已准备数据或 CodeTask 的失败使用各自有界路径。v1 执行已退出；新共同任务使用原生恢复。

### `simple-ar research-session-migrate`

~~~bash
simple-ar research-session-migrate --source-root OLD --destination-root NEW --artifact NAME --requested-output NAME --max-artifact-bytes 2097152
~~~

`--source-root` 和 `--destination-root` 必填。`--artifact` 和 `--requested-output` 可重复且可省略，`--max-artifact-bytes` 默认 2 MiB。

命令创建共同工具后继会话，不改变 v1。只复制显式命名的小文件。目标必须为空且位于旧根目录之外。记录父会话、导入及跳过数量，历史预算记为 `unknown_not_imported`，不猜测。

### 已退出的 `simple-ar research-experiment`

请使用 research-session；旧 `synthesis-file` 不自动转换。历史成果仍可读，库能力仍可组合。

### 已退出的分段 CodeTask 命令

`research-code-task` 和其独立执行器已退出。请使用带 `--code-task-config` 的 research-session，或独立 code-task。历史结果仍可读。
