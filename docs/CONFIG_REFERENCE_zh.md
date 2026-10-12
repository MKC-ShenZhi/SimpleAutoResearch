# 配置参考

[English version](CONFIG_REFERENCE.md)

本文是任务配置字段参考。不需要专家设置时先看[使用指南](USAGE_zh.md)；命令见 [CLI](CLI_REFERENCE_zh.md)。

## 引导设置

`start` 保存普通研究 TOML，改码另引用 CodeTask TOML。对话和结构化入口共用序列化；设置草稿不是执行会话。

| 设置 | 位置 |
| --- | --- |
| 命名连接和用途路由 | 可选模型目录 TOML |
| 连接密钥／旧单连接配置 | 环境变量或私有 `.env` |
| 目标、输入、交付、资源限制 | 研究 TOML |
| 编辑项目、保护、项目解释器 | 引用的 CodeTask TOML |

优先级为默认值 → TOML → 明确 CLI；CLI 列表替换文件列表。路径按对应字段从配置位置解析，命令 argv 保持字面含义；对话不能暗改用户明确选择。

### 命名模型连接

复制[模型目录示例](../examples/model-profiles/models.toml)到私有位置，在环境或私有 `.env`
设置 `SIMPLE_AR_MODELS_CONFIG` 为绝对路径，另配置 `CCTQ_API_KEY`（或自己指定的变量）。
也可使用 `~/.config/simple-ar/models.toml`；一次只加载一个目录，不合并多份文件。
TOML 保存名称、URL、模型及密钥变量名，不保存密钥本身。

交互式 `start` 在模型任务开始前列出文本连接，可按名称或编号选择；展示实际模型、端点、
流式、超时及密钥是否已配置，并将 `profile:名称` 写入任务配置。明确指定的 `--model`
不会被替换；纯描述统计不要求模型。有目录时无需在 `.env` 重复填写模型和端点，只需保留
被引用的密钥，以及不使用全局路径时的 `SIMPLE_AR_MODELS_CONFIG`。没有目录会明确提示
正在使用旧环境配置；`--prepare-only` 允许在尚未设置密钥时先保存任务。

```bash
simple-ar models                       # 本地检查，不调用 API
simple-ar models --config /path/models.toml
simple-ar start --chat --model profile:daily
```

`models --config` 只检查指定文件，不会替后续命令启用它。`--model env` 选 `text` 路由，
没有时选 `default`；CodeTask 选 `code`，再回到能力匹配的 `default`。
`profile:名称` 选择完整连接，`route:用途` 选择路由，任务 TOML 的 `[model].name` 也接受这两种值。
嵌套代码任务默认使用代码路由；任务明确指定的代码连接会保留给子调用。
原生 `compose_document` 可用独立的 `writing` 路由处理选材、起草和反馈，共用原任务账本。
该连接声明 `text` 能力即可，不新增模型能力类型。没有此路由时保持所选任务连接，
包括显式选择的 profile；可让日常协调与科学表达使用不同的模型或思考设置，
但配置本身不保证理解正确。命名任务快照绑定明确的写作路由，模型或端点改变时
需创建新任务，不能在恢复时静默换连接。
同一可选路由也可由 `focus_work` 通过 `purpose="writing"` 和具体目标选择，
在既有行动循环中调查、修订及交付。该消费者要求 `openai_chat`；Responses 写作连接
仍可服务 `compose_document`，但原生工作焦点会明确拒绝它。连接选择不自动提高
思考强度、工具权限或共享预算。

新整篇起草只发起一次逻辑 `ask_json` 调用，采用所选连接的共享有界重试、API 协议及
JSON 政策；物理尝试沿原账本和额度计账。JSON 解析或文章内容格式失败保留响应，
不自动重写文章；恢复使用冻结输入和已保存响应，不盲目重发。历史 Writer 检查点保持原契约。

每个 profile 必填 `api`、`base_url`、`model`、`api_key_env`、`capabilities`。
文本接口支持 `openai_chat`、`openai_responses`，目前仅 Chat 支持 `stream=true`。
可选 `http2=true` 使用 SDK 的 HTTP/2 协商；先安装 `pip install 'simple-autoresearch[http2]'`
（源码使用 `uv sync --extra http2`）。默认关闭，不切换服务商，也不能保证消除网关卡顿；
选择前在运行机器验证长输入和长输出。修改已保存任务的此选项属于连接变化；省略或false
仍兼容此前HTTP/1.1绑定。
可选字段：`request_timeout_sec`、`max_output_tokens`、`context_input_tokens`、`retry_attempts`、
`retry_base_delay_sec`、`retry_max_delay_sec`、`reasoning_effort`、`thinking_mode`、
`reasoning_output_tokens`、`json_response_format`、`chat_token_limit_param`、
`input_price_per_million`、`output_price_per_million`。
超时、等待和显式 token 上限须为正值；重试次数包含首次请求，单次调用仍可指定输出上限。
推理、JSON 等参数是否被服务商接受需要实测，不由模型名称保证。
`context_input_tokens` 是原生行动循环的输入工作集目标，默认24,000，包含工具契约、
思考历史和观察。它只控制本地呈现，不是服务商请求参数，也不代表模型的真实上下文上限。
较长任务可在命名连接中设置如 `context_input_tokens = 64000`，先核对提供方的输入输出
合计限制和任务总预算。当前工作连接拥有该设置；剩余tokens扣除输出预留后仍可缩小它。
增大输入会增加重复输入费用，不保证判断更好。已保存命名任务可调整此设置而不改变
模型身份、权限或账本；已耗尽的合同不会因此恢复额度。
`reasoning_effort="low"` 只降低思考强度，不等于关闭服务商思考。
思考档位并非跨提供方等价：[DeepSeek 将 `medium` 映射为 `high`](https://api-docs.deepseek.com/guides/thinking_mode/)。
其思考模式工具调用还会将此前的 `reasoning_content` 带入后续输入，因此提高档位可能同时增加
思考输出及后续输入成本，不代表每次目录调查、编辑或验证决策都会更好。必要时按工作责任使用
不同命名连接，先检查完整交付及反馈收益，再决定是否作为默认配置。
支持该开关的 Chat 提供方可使用 `thinking_mode="disabled"`，并将 effort 留空或
设为 `reasoning_effort="none"`；关闭思考与启用思考强度的冲突配置会拒绝。
调用或 profile 的显式输出上限优先；`reasoning_output_tokens` 备用上限仅用于
启用的 effort，不用于 `none` 或关闭思考。提供方可能将思考与最终文本一起计入
输出上限；只有思考、`finish_reason="length"` 而无最终文本不是完成的结果。
应核对提供方语义，不能只延长超时。
SDK Chat 流式且设置有限超时时，提交与正文读取同时受总时长约束，随后最多两秒本地清理。
未收到完成标记而到期，保留未知用量；有输出上限的纯文本请求可在 retry_attempts 内
沿同一 API 重试，每次失败保留完整预算预留。无上限或视觉请求停止，预算不足也停止。
这不代表服务商已停止或不计费。
`retry_attempts = 1` 会关闭自动重试，单纯延长超时不会改变此策略。
示例文本连接使用600秒时限及最多三次请求；若要求未知用量的文本重试仍受有限任务预算约束，
应选择服务商支持的明确输出上限。不要无意中把图像连接的单次提交设置复制给文本连接。
非流式、Responses 和 LiteLLM 保留其传输超时语义。SDK 请求设置正有限超时时，TCP/TLS
建连最多等待20秒，配置时限更短则取该时限；读取、写入及连接池等待仍用配置值。
因此600秒生成时限不会让单次建连也等待十分钟。SDK内部重试仍关闭，新请求沿既有
`retry_attempts`及任务预算计账。已有 asyncio 事件循环中调用该同步接口，
使用 `await asyncio.to_thread(client.ask, ...)`，不嵌套新事件循环。

`json_response_format="off"` 只是不发送服务商原生 JSON 参数，`ask_json` 仍解析并检查 JSON。
它用于接口兼容，不是过载修复。HTTP 连接成功后，流中仍可能返回上游错误；立即返回的
`overloaded` 不能靠延长超时解决。有界重试逐次计入物理调用并保留未知用量，不重置旧任务额度。
切换路由前，应在实际运行机器对比 SDK 和框架请求，并验证较长响应，而不只测试一句话。
流诊断只保留SDK异常类型、可取得的HTTP状态及白名单协议错误码/类型，不保存服务商任意正文、
请求ID或地址。未取得错误码意味着未知，不证明超时；不接受未完成的半段输出。
恢复任务可调整超时、输出上限和重试设置，模型、端点及代码路由身份仍绑定；不改写旧快照。

能力声明不是探针结果。`simple-ar image` 使用 `openai_images` 连接生成或编辑；
可选 `image_size`、`image_quality` 传给提供商，须验证其实际支持。
Images 不能用作文本客户端，
也不能继承文本的流式、token、JSON 或 token 单价参数。
图像连接设为 `http2=true` 时，实际执行环境须安装 `simple-autoresearch[http2]`。
缺依赖在预留额度和登记提交前拒绝；完整且未变的旧图片仍可离线复用。
历史未知提交不据此改账或自动重发。

`simple-ar image-review` 使用独立 `vision` 路由，连接须声明 `vision` 能力并显式选择
`openai_chat` 或 `openai_responses`，不自动切换 API 模式。发送实际图片像素，不是文件名；
支持最多四张静态 PNG/JPEG/WebP，总计不超过 20 MiB，每张不超过 4,194,304 像素。
服务商提供用量时据实登记，未提供时保留未知而不是零。反馈检查可见问题，不证明科学正确性。
超出本地图片限制时返回明确校验说明，可缩小检查副本，原图保持不变。
审阅沿命名连接的输出上限；未配置时使用 3,000 tokens 的有界默认值。
无法解析的最终响应保留在审阅目录的 `rejected-response.json`，不作为反馈发布，也不自动重发。

原生任务的 `--allow-vision` 授权 `inspect_images`；看图共用模型调用和 token 额度，
不占图像生成额度。因此 `--max-images 0` 禁用生成/编辑，不禁用已授权的看图。
任务上下文区分工具可用、剩余额度与提供方通路是否正常。

cctq 官方异步图像任务协议须显式选择 `api = "cctq_images_async"`；
`openai_images` 保留通用同步接口。异步路径使用配置的 origin，保存同一 task ID，
轮询该任务并认证下载同 origin 的 `files/0`，不硬编码主机、不跨服务商或接口 fallback。
`request_timeout_sec` 约束提交、轮询和下载的整段调用，随后最多两秒本地清理。
已保存任务 ID 的恢复只发 GET；中断且未保存 ID 的提交不重放。
已通过用户配置的代理验证旧任务结果恢复和一次反馈编辑；不代表服务器直连已稳定。
此前同步请求的超时仍保留为失败，不能算成功。

可选 `proxy_env = "RESEARCH_API_PROXY"` 引用环境变量中的 HTTP(S) 代理 URL，
只影响此 SDK/图像连接，不影响文献检索或其他 profile，也不是服务商 fallback。
当前不接受 URL 内的用户名密码、query 或 fragment；变量缺失或无效在付费调用前失败。
实际代理 URL 不进入任务快照或 provider 请求正文。未配置时沿用客户端标准环境路由。

关联公共资料下载可单独配置部署路由，不改变模型连接：
`SIMPLE_AR_DOCUMENT_ROUTE_HOSTS` 填逗号分隔的准确主机名，
`SIMPLE_AR_DOCUMENT_PROXY_ENV` 填保存 HTTP(S) 代理 URL 的环境变量名；
可选 `SIMPLE_AR_DOCUMENT_CA_BUNDLE` 指向已有可信 CA 文件。例如只路由 `github.com`，
不接受通配符；未列出的主机仍直连，TLS 校验始终启用。这些设置仅影响关联补充资料下载，
不影响文献检索、模型调用、论文 PDF 传输或 ZIP 获取。未配置时此下载器不使用环境代理。
应在执行机器显式设置；模型代理可用不代表作者仓库也能访问。

文献连接器读取独立可选的 `OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY` 环境变量，
不借用模型连接密钥；通过认证请求头发送，不放在查询参数里。缺密钥仍可匿名访问，
受提供商额度限制；配置密钥不代表来源完整，也不取消限流。
项目文档、仓库与数据集网页可在 `research_sources` 中显式加入 `"web"`，
或在论文提供方之外追加 `--provider web`。此可选连接器读取执行主机 `.env` 的
`TAVILY_API_KEY`，不复用模型凭据。仅使用基础搜索，不自动升级、生成答案或自动重试；
检索响应记录提供方返回的 credits。缺密钥明确失败，不自动换服务；默认来源与旧计划不变。
网页保留 URL 与 `source="web"`；摘要只用于发现资料，不冒充原文、已核实书目或运行证据。
原网页沿已有文档路径获取。明确限定出版年份时，无日期网页仍不符合范围，不以访问时间填年份。

HTML／文本原网页可显式设置 `[research] web_extract_backend = "tavily_basic"`，
或使用 `start --fulltext --web-extract-backend tavily_basic`。取得后端独立于搜索来源，
仍需原文获取许可；默认 `direct` 不变，PDF 继续沿现有下载路径及权限处理。
取得文本与搜索摘要分开缓存，保存原 URL、提供方请求 ID 和报告用量；缓存复用不再请求提取。
空结果或逐 URL 失败仍记为失败，不自动升级或跨后端回退。提供方提取网页不等于已核实论文全文，
返回零 credits 也不代表提取永久免费。

匿名额度可能由同一服务器 IP 的用户共用。收到 HTTP 429 后，当前检索批次停止
实时请求该提供方，仍检查各查询显式启用的缓存，并继续其他已配置来源。
缓存沿原出处标记；该处理不补齐来源覆盖，也不代办密钥。

启用目录后，旧环境中的模型、URL、密钥等设置不覆盖 profile。未知名称、缺失密钥、
能力不匹配会报错，不自动切换服务商；不同的裸模型名覆盖也会拒绝，请另建 profile。
没有目录时旧 `.env` 方式不变；不会自动迁移或删除用户配置。

新研究会话和对话草稿在已有快照中保存非秘密连接绑定；恢复时检查当前连接及嵌套代码路由。
同一变量中的密钥可轮换，超时、输出上限和重试可调；更换模型或端点需恢复原配置或开始新任务。
添加无关图像连接不影响文本任务。
旧会话和独立 CodeTask 操作没有追溯补建的连接冻结，请在运行期间保持目录不变。

迁移顺序：复制示例→填写当前非秘密连接→保留 `.env` 密钥→本地检查→用小型新任务验证。
确认后才手工清理不用的旧变量。目前没有交互式连接登记或自动迁移命令。

改码 `--project-python PATH` 在 CodeTask 写入 `[environment] mode="external"` 和 `python="..."`，省略仍 current；只选择已有解释器，不安装依赖。

复现默认以 `--project` 提供只读准备和 cwd；`--data-path` 登记数据，`--output-files JSON` 写入 `execution.output_files`。仅这些选项不授权改码，也不从任意路径猜结果。

显式组合 `--project`、`--allow` 和独立 `--validate` 可另行选择限定范围的 CodeTask
adapter 准备。CodeTask 的 current/external Python 同时用于验证和正式测量；
`--project-python` 选择已有解释器。也可明确选择任务 venv，在同一隔离工作区先安装
已授权的依赖/项目，再由该解释器执行改码、独立验证及正式测量，共用进程额度。
不能同时指定 external 解释器或第二条 `check_command`；安装失败不进入实现或测量，
已完成准备的恢复不重新安装。契约见下文复现段落。

单 Python 命令的可选依赖准备：

```toml
[execution.environment]
mode = "venv"
requirements = ["requirements.txt"] # 相对 execution.cwd
install_project = false
timeout_sec = 300
# python_executable = "/path/to/base/python"
```

默认仍当前环境、不安装。准备共用进程额度与日志，失败不进入测量；构建可能执行代码和联网，不是 OS 沙箱或通用环境准备。见[工作流](WORKFLOWS_zh.md#明确选择的任务依赖准备)。

明确选择的短运行检查可以直接使用当前环境：

```toml
[execution.environment]
mode = "current"
check_command = ["python", "check_inputs.py"] # 项目实际提供的检查，不是内置脚本
timeout_sec = 30
```

复现引导可在最后的 `--command` 前添加
`--check-argv '["python", "check_inputs.py"]'`。venv 配置也可添加
`check_command`，裸 Python 别名使用准备好的解释器。显式 baseline 命令中的裸 Python 别名
也使用同一任务 venv；其参数和明确指定的其他解释器保持不变。current 保持两条命令原样，
不安装或替换解释器。非零退出或超时会停止正式测量并保留日志，检查输出不进入科研指标。
成功只证明该检查正常退出，不保证完整实验成功。专家配置需为检查分配进程额度，引导自动计入。
复现对话也可依据已查看的项目说明，在执行建议中展示可选检查；确认后由同一序列化入口
保存。设置对话不执行这两条命令，也不替换用户明确指定的 argv。

来源访问中，`materials_only` 禁止在线检索；`--fulltext` 允许在线调研尽力获取远程全文。不可得或截断材料仍需说明。

## 已有材料写作

字数和文体要求写在任务目标中，不放入额外环境变量。新自适应文章规划区分
“约 1200 词”（软目标）与“1000–1200 词”（硬范围），不臆造容差、不把页数
换算为词数，也不静默修改保存计划。目标用于指导组织，不意味着过短或内容不全
也能交付；审阅仍检查原要求、证据和完整规范装配稿，不需要新增 TOML 字段。
可明确要求包含全部交付、仅排除参考文献，或另排除标题和章节名；这些范围分别计数，
不要将正文范围等同于含书目的全交付。保存计划沿原口径，不自动重解释旧任务。

`start --kind writing --goal "说明已有结果和局限" --material notes.md --prepare-only`
自动保存普通配置，不必先写 TOML。高级配置使用 `task.kind = "writing"`、
`task.outputs = ["report"]`、`assets.materials = ["notes.md"]`、`model.name = "env"`。
`assets.papers` 可另提供论文；不要把同一文件重复标成论文和笔记。
材料路径相对配置文件解析，支持 Markdown、文本和 PDF。
`assets.materials` 也接受普通 JSON 对象/数组，作为未独立验证的用户文本，不自动复算。
JSON 语法错误会在引导保存前失败；声明 `table_analysis.*` 的文件必须通过既有加载器，
不能因损坏而悄悄降为普通材料。已完成的 `table_analysis.v1` `analysis.json` 同目录保留数据副本。
沿用同一配置字段，不新增运行时：重新计算核对数值、生成同源 SVG/PDF/PNG，报告附可搬迁数据表与分析包。
分析包导入的 JSON/数据文件各限 20 MiB；缺项或数值不一致会在写作前失败。
复算不证明采集或科学主张；原始表格应先使用 `data_analysis`，不能直接冒充分析包。

默认 `report.template = "material_report"`，整理已有材料，不要求不存在的实验或失败目标；
`"analysis_report"` 保留为目标未达成/不确定的实验分析，`"experiment"` 请求论文体草稿，而不授权实验。
已保存的显式模板继续有效；已经开始写作的自动模板沿用原输入快照的解析结果，不因升级重选。
引导写作默认开启 `report.document_review = true`。执行配置、在线搜索及研究创新不属于此任务。
专家可加 `review_scope = "document"`，先完成多节正文，再沿原整稿审阅/修订执行，
不强制先逐节重复审阅；必须同时开启 `document_review = true`。默认 `section` 保持旧顺序，
单节稿仍审阅该节。检查点、证据工具和每节修订上限共用原机制，草稿不认证为已逐节审阅；
已有任务通过明确的报告配置/刷新入口更改，不直接改检查点。
联合起草独立于审阅时机。新引导的数据报告（`start --kind data_analysis --with-report`，
也可由对话确认）沿用联合起草和整体审阅；新建引导调研与有限复现同样保留此默认。
自动起草粒度仅供显式试用，尚不是新入口默认。
已有配置和自定义写作模板保留原选择，专家可自行选择：

```toml
[report]
document_review = true
review_scope = "document"
draft_scope = "document" # 显式实验性选择："auto"
```

`auto` 在所有章节都有正数目标词数、整份计划合计不超过 2,000 词时联合起草；
较长或未定长度的计划逐节保存，完整正文再统一审阅。这是起草启发式，不是 token
保证，也不改变输出上限。显式 `document` 则始终按冻结计划与共同证据联合组织待写章节。
要求默认的 `full` 来源策略，不兼容 `batch_refine`；默认 `draft_scope = "section"`
仍分次起草，并保持旧默认检查点身份。原 `max_section_tokens` 是单次调用上限，
正数会限制整次联合回答。完整章节集验证后保存在同一检查点，恢复已保存正文不重写。
此显式模式及 `auto` 的跨节修订仍形成完整候选；自动起草不会拆分大型联合修订。
一次完整候选检查核对未解决意见与当前全文，合格才一起采用，
否则保留原各节。原 `max_review_iterations` 限制联合候选轮数；恢复保留已消耗的旧逐节轮数
  与待完成的旧修订，不因升级获得新额度。候选及原修订请求保存在原迭代/检查点中。
  `allow_source_backtracking = true` 时，联合 Writer 可在初次组织或每轮共同修订前请求
  一批只读补证，受 `max_backtracking_calls` 与原六结果提示窗口约束，并与审阅共享
  工具级总额度。请求/结果在读取前后保存，恢复不重放已分配读取；设为 false 也关闭
  Writer 补读。不会新增在线检索、任意路径读取或执行权限。
不认证科学质量，也不保证消除服务商故障。
提取后的文本随会话保存，恢复不重新读取修改过的原文件；更换材料应明确修订或新建任务。
提供的结果仍是外部陈述，不能称为本次独立测量；缺证据和缺书目信息必须保留，审阅通过不保证论文正确。

引导入口在线调研默认读取摘要和已提供的本地材料。加入 `--fulltext --sources search`
才允许远程全文抓取/PDF 下载；仍沿用现有材料流水线，获取失败会记录，不能当成读过全文。
本地材料模式不通过此选项开启网络。

## 已有材料写作的章节规划

完整分析包与正文展示分开：默认 `[report].data_tables = "linked"` 链接全部数值记录，
`"full"` 另外将逐行表格放入正文。两种设置都保留完整输入和复算结果，不抽样。
有唯一冻结章节来源归属时，图和数据链接放入该节；歧义时保留独立描述性数据节。
显式adaptive规划还可在同一文章计划中给已有图指定一个展示章节，即使其他章节也引用该来源。
只接受实际登记的数据图，不编数据/路径；无效来源和冲突归属使用原有有界规划纠正，不新增调用阶段。
命令行对应 `research-session --report-data-tables linked|full` 和
`research-report --data-tables linked|full`。已完成的历史报告不被静默重写。
原生实验报告复用同一选项：`linked` 在正文放简短记录链接，`full` 另外内嵌登记结果摘要表。
两种模式都交付 `experiment_evidence.json`、可读的 `.md` 和明确登记的来源副本，不抽样测量。
篇幅预览和实际装配共用这份文本，正文仍须呈现必要结果。附件审计核投影值、可读文本和声明副本是否存在，
不认证科学有效性，也不检测副本生成后的逐字节篡改。ACM 导出携带该原生包，不扫描或复制任意链接文件。

写作可选 `[report].outline_strategy = "adaptive"`：内置非综述模板会先按目的、已有主张、
测量和限制组织章节。`auto` 保持原模板路径，`template` 关闭自适应；自定义 Markdown
模板不改结构。新建 `start --kind writing` 内置模板任务会明确保存 `adaptive`，
不改变已有配置中 `auto` 的含义。新增一次规划调用，最多纠正一次，不检索、不新增实验；未知来源指针被拒绝，
模板回退沿已有显式 fallback 设置。已开始稿件恢复时复用 checkpoint 计划。
非综述规划成功后，写作保留内置模板的用途边界，不再把旧章节列表和起草顺序与冻结计划一起传入。
章节请求复用这些冻结职责，不重复发送；减少的是重复上下文，不是保存的证据或审阅要求。
命令行对应 `research-session --report-outline-strategy adaptive`；请求另写报告时可用
`research-report --outline-strategy adaptive`。计划组织正确不等于事实认证或投稿质量。

## 已有数据的描述分析

```toml
[task]
goal = "描述已有测量"
kind = "data_analysis"
outputs = ["data_analysis"]

[analysis]
file = "observations.csv"
value_columns = ["score"]
group_column = "method"
observation_unit = "一次运行"
value_unit = "秒"
```

`file` 相对 TOML 解析；`value_columns` 与非空 `observation_unit` 必填。
可选 `attribution = "数据名称、版本、署名或公开URL"` 随固化分析包及后续写作保存。
这是用户声明，不下载URL、不核验采集，也不虚构文献元数据；未知时直接省略，不是必填项。
`group_column`、`value_unit` 默认空（不分组、单位未知）。可选：`mode = "observations"`
将行聚合为组内 count/mean/sample std 与经验四分位数；`"values"` 保留输入值，柱图要求唯一标签，
折线/散点则保留每个完整坐标对，包括原始个体观测。模式名表示转换操作，不表示数据是否
属于观测研究。`missing = "reject"`
或明确 `"omit"`；`width = "wide"` 或 `"column"`；物理限制 `max_mb = 20`、`max_figures = 100`
为可调整正整数。缺失不填零，非有限/非数值报错；不自动造误差条，不作显著性/因果结论。
任务必须输出 `data_analysis`，可加 `report`，不接受执行或联网调研。
配置 `outputs = ["data_analysis", "report"]` 和 `[model].name = "env"` 可在同会话
分析、绘图并写作，完成包进入已有材料写作和审计。报告可附带
`[assets].materials = ["README.md"]` 数据说明/笔记及
`[assets].papers = ["reference.pdf"]` 参考来源，仅接受本地文本/Markdown/HTML/PDF，
不是第二份原始表格或分析 JSON。保存的原文和复算结果分开，说明不覆盖
`[analysis]` 设置、不授权搜索。默认仅分析只读表格并忽略模型设置；
加入报告需要模型，继续使用原会话预算，不另建写作任务。
`plot` 默认 `"bar"`（均值）；`"box"` 要求逐行 observations，展示 Q1–Q3、中位数与
实际 min–max 须线，不是置信区间或 Tukey 异常值界。四分位数按 `(n−1)p` 插值，
全部有效观测参与，标签保留计数/遗漏，同一列各分页共用尺度。已有汇总值不能恢复
四分位数；箱线图不接受坐标或 Pearson 选项。
`"heatmap"` 要求 `mode = "values"` 和唯一行标签 `group_column`，所选列应表达用户
声明的兼容量/单位。原值共用全局色标，行/列分页也相同；不隐式归一化、排序、聚类或
计算相关。明确 `missing = "omit"` 时，缺失格保留灰色/NA，包括全缺失列；全缺失矩阵
无法着色。`max_points` 计每页单元格（含缺失格），`max_figures` 计页数，不抽样。
完整标签和原值进入分析包及后续写作；不接受坐标、Pearson、配对或共享坐标布局选项。

`"line"`/`"scatter"` 要求 `mode = "values"` 和数值 `x_column`，
可选 `group_column` 标识不同系列，不表示配对或重复测量聚合；`x_unit` 默认空（单位未知），多个数值列默认分别绘图。
显式 `series_layout = "shared"` 可让折线/散点的所选系列共用坐标轴与图例，
使用共同声明的 `value_unit`，不自动归一化，也不独立认证单位兼容。
共享图将所有系列的位置计入 `max_points`，超出不丢系列；默认 `"separate"` 保持独立轴。
折线要求每组内 x 唯一并按 x 排序，缺失 y 断线；散点保留重复 x，不聚合、不拟合。
散点明确选择 `missing = "omit"` 时保留缺失坐标行，但不绘制对应位置；折线仍要求 x 完整。类别图例分页，不丢组，同一图的各组分页使用相同坐标尺度。坐标图物理上限 `max_points = 10000` 为可调整正整数，
超出报错而非抽样。见[完整案例](../examples/data-curves/README.md)。
新任务保存前按 `max_mb` 预检表格结构与所选列；正式摄入仍校验数值，续跑不重读原文件。
摄入时固化原始字节与设置，续跑复用；更改列/聚合设置需新任务。产物与重建见[工作流](WORKFLOWS_zh.md)。

需要同一行的配对比较时，设置 `paired_baseline = "baseline"`，在 `value_columns`
同时选择基线与候选列。仅用于 `mode = "observations"`、`plot = "bar"` 或 `"box"`；用户需确认
各列表示同一种量、共同单位，每行是一个匹配对，可另按类别分组。结果保留候选减基线
的逐对差值、有效对数/缺失对数、平均差、样本标准差与标准误。`missing = "omit"`
按两个值联合筛选，不对分别过滤后的均值相减。至少两对时差值图显示 ±1 标准误，
假定各对独立；不是置信区间、显著性检验或“改进”判断。写作导入分析包会复算这些值。
`plot = "box"` 将各方法的边际分布画在共同坐标范围内，配对均值差与标准误仍单独画图。
边际分布使用各列自己的非缺失值，差值只使用完整匹配对。图形记录其实际表达的统计量；
计算过标准差，不等于图上已经展示标准差或误差棒。

若问题是两个数值的线性关联，而不是“候选减基线”，在显式 x/y 坐标的折线/散点任务中
设 `association = "pearson"`；默认 `"none"` 不计算相关。各组/数值列只使用同一行共同
非缺失坐标，不混池、不改变原点或图形。分析包和写作提供 r、完整/缺失对数及未定义原因；
少于两对或常量坐标不造系数，两对非恒定数值的 r 必为 ±1。这不是回归、显著性、置信
区间、因果或总体推断，小 r 也不排除非线性关系。坐标 `mode = "values"` 表示保留输入值，
不认证每行是独立实验。不增加模型调用或依赖。

## 全局 `.env`

`.env` 由 LLM 集成读取，用于凭据、endpoint、模型和传输设置；它被 Git
忽略，不应放任务资源、源码根目录、数据集或解释器路径。任务专属路径和执行条件
必须写在研究 TOML 或其引用的 CodeTask TOML 中。

## 研究 TOML：分区与默认值

研究入口的 `report.template` 默认 `auto`：仅调研且最多只有一份可引用来源时，使用简短的单来源证据审阅；多来源才使用综述结构，不凭单篇材料虚构方法谱系或跨论文结论。独立供材写作自动选 `material_report`，具体恢复语义见上文。实验任务依据分析中的目标判断选择实验论文、复现报告或简短实验分析报告。执行 `passed` 不等于科研目标达成；目标不明、未达成或因轮次限制停止时，默认不写成成功论文。显式 `source_review`、`survey`、`material_report`、`experiment`、`reproduction`、`analysis_report` 或自定义模板优先，但不能覆盖测量事实。旧会话保存的显式模板不会被新默认值更换；需要时通过现有报告配置续接改为 `auto`。结构自动选择不代表论文语义质量已验收。

新研究会话不设置 `budget.total_tokens`、`budget.llm_requests` 时，框架不限制 API 总 token 和请求次数；
需要限制时设置正整数。无限额仍记录用量，不等于免费调用或无限重试。
进程资源、科研轮次、attempt/no-progress 和供应商单次输出限制独立保留。
恢复旧会话仍沿用其持久化额度；省略配置不会清除旧限额或清零用量。服务商报错时保留产物并暂停受影响工作，以便恢复。

- 优先级：内置默认 → 研究 TOML → 显式 CLI 覆盖。CLI 列表覆盖整份文件列表。
- 研究文件中的相对路径以 TOML 所在目录为基准；命令 argv 原样传给实验进程。

### 任务、模型与预算

| 分区 | 字段 | 默认值 / 必填与条件约束 |
| --- | --- | --- |
| `[task]` | `goal`、`kind`、`outputs`、`output_root`、`selected_idea_id` | 新 session 必须有 `goal`。kind：默认 `auto`，或 `survey`、`bug_fix`、`measurement`、`reproduction`、`writing`、`data_analysis`。`measurement` 只输出 experiments；`reproduction` 含 experiments，可加 report；`writing` 只接受 report；`data_analysis` 必须有同名输出，可加 report。其他输出：summary/report/experiments/bug_fix。`output_root` 默认 runs/research-session；可选 `selected_idea_id` 必须选已有、具有依据的候选。 |
| `[model]` | `name`、`feasibility_review_model`、`max_output_tokens` | 文件配置默认 `name = "env"`，读取 `.env` 的 `SIMPLE_AR_MODEL`；`name = ""` 选择不调用 LLM 的确定性处理。可选 `feasibility_review_model` 仅让另一模型审核源码支持的实现可行性，仍使用同一服务商与会话预算；选择会随会话保存，恢复时不可更改。`max_output_tokens` 可省略；凭据始终留在环境中。 |
| `[budget]` | `total_tokens`、`llm_requests`、`process_invocations`、`process_wall_seconds` | 新 session 的 token/request 上限可省略（该维度不设框架上限）；进程值按任务形态在入口推导，要求执行时应显式设置。恢复沿用已存账本，不清零用量。 |

独立可行性审查可在 CodeTask 或训练前质疑方案机制；它仍是模型判断，不能替代可执行的机制验证或改进证据。省略时由主模型审查；续接时可省略该字段以沿用存档选择。

### 研究输入与行为

| 分区 | 字段 | 默认值 / 必填与条件约束 |
| --- | --- | --- |
| `[research]` | `providers`、`queries`、`max_results`、`max_chunks`、`max_pdf_pages`、`read_max_shortlist`、`idea_limit`、`cache_dir` | 列表可省略；CLI 默认 `max_results = 10`、`max_chunks = 300`、`idea_limit = 3`。不设置 `max_pdf_pages` 默认提取全部 PDF 页面；显式正整数限制提取页数并记录截断。更改后应创建新会话，不能把已冻结的阅读证据当成新版本。`read_max_shortlist` 可选，显式提供的论文优先保留；若数量超过上限则显式报错。`cache_dir` 可选，未持久化，不能作为安全的恢复变更。 |
| `[research]` | `use_fulltext`、`allow_pdf_download`、`keep_raw_pdf`、`max_fulltext_documents`、`max_pdf_mb`、`materials_only` | 开关默认 false，可选上限默认不设。`materials_only = true` 使用本地输入（`assets.papers`，或写作的 `assets.materials`）并禁用 search，仍允许模型阅读；writing 始终仅用本地输入。引导入口的 `--fulltext --sources search` 会允许缓存 PDF，默认最多 6 份远程资源、每份 20 MiB；专家可在 TOML 中调整正整数上限。自动补查会在既有总额内保留首轮取材名额，不额外下载。远程 PDF 需要下载许可与缓存许可；获取失败仍明确标注只读摘要或不可用。 |
| `[research]` | `max_iterations`、`interaction` | `max_iterations` 默认 `1`，`0` 表示首轮分析后停止。`interaction` 新 CLI 默认 `checkpoints`，可选 `assisted`、`checkpoints`、`autonomous`；硬事实和权限缺口在任何模式下都是阻塞。 |
| `[assets]` | `papers`、`materials`、`data` | 路径相对 TOML 所在目录解析。`papers` 表示书目来源；`materials` 用于 `writing` 的草稿、笔记、外部结果说明或附有数据副本的完整 `table_analysis.v1` 分析包，不当成本次实测实验指标。写作至少需要一份输入，不接受重复文件或同一文件兼任两种角色。`data` 表示执行输入：隔离 CodeTask 准备复制声明的项目数据，直接/外部输入保持原位置；不核验论文划分或改写 argv。 |

模型检索计划保留用户显式查询；未配置时采用聚焦主题词，不把整段任务强制作首条检索。
补充条件供筛选和原文阅读，不全部拼成一条必需条件。arXiv连接器明确用AND连接普通关键词，
显式高级连接器表达式保持原样。文档额度内兼顾不同聚焦查询；取得材料不代表语义相关或覆盖充分。

本地已有PDF也会尽力解析，即使关闭远程全文获取。省略页数上限提取全部页；
观察到的总页/提取页、截断和空文本页会保留并传入下游。阅读窗口仍独立有界，
全部提取不代表OCR或完整理解，旧无页覆盖记录不能追认。
该页数上限由基础PDF解析器实现；可选unstructured后端无法执行显式PDF上限时会报错，
不静默忽略用户限制。外部parser应声明自己观察到的覆盖。

未知分区/字段和类型错误会显式拒绝。accepted plan 是短顺序计划，动作唯一且输入由
能力适配函数绑定，不是任意模型调度器。只调研不会创建实验进程；`bug_fix` 必须提供
`execution.code_task_config`，产出 `bug_fix` 且不运行 baseline；`auto` 不意味着自动发现
仓库或安装依赖。

### 执行与报告

| 分区 | 字段 | 默认值 / 必填与条件约束 |
| --- | --- | --- |
| `[execution]` | `command`、`cwd`、`timeout_sec`、`code_task_config` | 通常选择 literal argv `command` 加已存在的绝对 `cwd`，或 CodeTask TOML 引用。复现可显式同时提供，先授权 adapter 准备，再运行独立正式命令。只调研时两者都省略；`timeout_sec` 在 CLI/应用边界提供默认值。 |
| `[execution.environment]` | `mode`、`requirements`、`install_project`、`python_executable`、`timeout_sec`、`check_command` | 可选的单命令准备：venv 创建任务环境；current 必须明确检查 argv，不安装或替换 Python。依赖列表默认空，项目安装默认 false，venv 基础 Python 默认当前运行时，每步超时默认 300 秒；可选检查在准备后执行。省略保持普通当前环境执行，专家 TOML 预留准备额度，引导计入。 |
| `[execution]` | `primary_metric`、`metrics`、`metric_directions` | 可选测量 schema；方向为 `higher`、`lower`、`resource` 或 `ignore`。 |
| `[execution]` | `output_files` | 可选映射，附件名称对应进程 `SIMPLE_AR_OUTPUT_DIR` 内的相对 POSIX 文件路径。仅登记每个不超过 2 MiB 的 UTF-8 普通文件，提供有界预览与读取句柄；缺失或不可读附件独立于执行成功状态记录。 |
| `[execution]` | `pairs`、`seeds`、`seed_flag`、`seed_count` | 可选的显式比较输入。`pairs` 每行包含唯一整数 `seed` 与 literal `baseline_command`/`candidate_command`；compact seed 必须有 literal command 和显式 seed flag/count，不解析自然语言 seed。 |
| `[execution]` | `baseline_policy`、`baseline_ref`、`protocol` | policy 为 `run`、`skip` 或 `reuse`；`reuse` 要求当前 session 中通过且命令、schema、协议条件、保护资产和准备 lineage 都匹配的产物。`protocol` 复用已有实验合同，但不证明数据内容。 |
| `[report]` | `template`、`reviewer`、`max_review_iterations`、`document_review`、`max_section_tokens`、`max_cited_sources`、`figures` | `template` 默认 `auto`，`reviewer` 默认 `llm`，CLI 修订次数默认 `1`。可选 `document_review = true` 增加整稿审查：新论证计划可纠正冻结的各章节，旧计划保留两处目标；每处最多修订 `max_review_iterations` 次，被拒候选计入额度，恢复不重置。整稿审查默认关闭。`max_section_tokens = 0` 取消单次输出上限；正整数 `max_cited_sources` 限制最终不同引用数，不提前截断阅读池，超出在终审记录；省略即不设此上限。图表默认确定性生成，可设 `[report.figures].enabled = false` 或 `mode = "off"`。 |
| `[report]` | `max_document_review_prompt_chars` | 专家可选的完整审阅请求字符上限，默认 `0` 不添加隐藏的拼接请求限制。已有检索窗口、60,000 字符稿件窗口、模型容量及累计会话预算仍有效。正数上限拒绝超量审阅，不截断证据、不假称已经审完。 |

`report.figures.max_figures = 0` 选择自动生成，不表示纯文字。自适应文章只生成
计划中受支持且有证据的概念图，不为凑数量造图；已有用户图片可以与之共存。
正整数是报告总上限，包含导入的分析图，超出明确失败而不丢弃用户数据。
关闭生成用 `enabled = false` 或 `mode = "off"`。原生图是可编辑概念概览，
不是任意机制插图或图像生成。

单条固定命令只写 `seed_flag` 会记录当前 seed，但不授权增加新 seed。若希望先只运行 seed 0、以后允许按证据决定是否补测，可同时写 `seeds = [0]` 和 `seed_flag = "--seed"`；这不会默认多跑种子。补测仍须分析提出理由、运行同种子的 baseline/candidate 配对、通过剩余进程预算检查并由既定交互模式接受。

只在 `execution.protocol.comparison_conditions.seeds` 声明的列表是协议条件，不是命令展开授权；没有 `execution.seed_flag` 时保持单次原命令，可由评估器内部处理多个种子。声明不证明种子都被实际测量。顶层 `execution.seeds`/`seed_count` 请求命令展开，仍须显式 flag。

`[[execution.protocol.protected_assets]]` 中每个文件须有唯一 `asset_id` 和 `path`。运行前后会检查这些文件是否发生改动。相对路径按实验 `cwd` 解析；只核对显式列出的文件，不递归校验整个数据集，也不因此证明科研结论正确。

显式 `outputs` 不能与 `--with-report`/`--no-report` 同时使用。报告结构选择不能覆盖
测量事实或证明科研成功；恢复时变更搜索/摄取设置若与存档不符会被拒绝，显式报告变更只
失效 writer/report/audit 产物，不重跑研究或测量。已有前缀可用 `research-report` 补齐报告。

`task.kind = "reproduction"` 是**已准备好环境与命令的固定协议复现**：读取本地论文、
综合来源证据、执行声明的命令、分析实测值，可选复现报告。要求 `research.materials_only = true`、
`assets.papers`、`execution.command`，以及至少包含 `hypothesis`、`dataset`、`expected_outcome`
的 `execution.protocol`；使用 `baseline_policy = "skip"` 和有限进程超时。
它默认不改代码，不提出创新、不扩种子，也不寻找缺失环境；依赖安装仅在明确选择上述
`execution.environment` 虚拟环境设置后进行。已有项目可另行显式配置
`execution.code_task_config`，只准备授权范围内的结果 adapter 或入口衔接：引用配置的
`[benchmark].command` 成为独立 `validation_command`，`[execute].timeout_sec` 成为正值
`validation_timeout_sec`；`execution.command`、`execution.timeout_sec` 仍负责正式测量。
验证 argv 在解释器解析后也须与测量不同。隔离项目准备、限定实现和验证在测量之前完成；
保护路径继续生效，授权不包含改变声明的科学协议；验证通过本身不证明科学等价。
新适配器可用 `execution.initial_files = ["adapters/export.py"]` 明确声明创建意图；
路径必须是明确允许、尚不存在且不受保护的 Python 文件。初始化只在隔离项目创建
执行即报未实现的占位文件，再走原索引、编辑和验证；仅 `--allow` 不会创建文件。
准备阶段消费该声明，恢复不覆盖已实现文件。
CodeTask 默认使用 current/external Python；也可明确组合 `execution.environment` 的 venv
准备，在隔离工作区创建环境后绑定实际解释器。不能同时指定 external Python，也不能
叠加 current/check 或第二条检查命令；独立验证仍由 CodeTask 执行。配对对照仍走普通研究路径。
这不是任意论文自主准备、OS
沙箱或真实论文复现验收通过。报告使用 `template = "reproduction"`，区分原论文结果、改编检查和本地实测。
完整低开销案例见 [conformal_reproduction](../examples/conformal_reproduction/README.md)。

`task.kind = "measurement"` 仅用于原样运行并分析一条已提供的命令，不做文献检索、
候选设计、CodeTask 改码或 baseline 对照。它要求 `outputs = ["experiments"]`、
显式 `execution.command` 和有限进程额度。需要依据证据提出新候选或做对比实验时，
使用 `auto`/科研路径；单次测量不证明科研改进。

- code-task init --config PATH 读取初始化配置。
- code-task execute --config PATH 读取执行、模型与预算配置。
- research-session --code-task-config PATH 使用同一个 CodeTask 解析器。
- 显式 CLI 参数覆盖对应 TOML 选项。
- `[continuation]` 除额度授权外，还接受 `decision_id`、`decision_response` 和 `decision_guidance`，
  用于通过正式续接入口答复 Rich 显示的待处理决定。
- 安装与命令说明：[使用手册](USAGE_zh.md)、[CLI参考](CLI_REFERENCE_zh.md)。

### 研究会话补齐条件与续接

执行附件可在任务 TOML 中声明，不放进全局 `.env`：

```toml
[execution.output_files]
paired_observations = "tables/observations.csv"
runtime = "runtime.json"
```

子命令将文件写入本次进程自动提供的 `SIMPLE_AR_OUTPUT_DIR` 目录（先自行建目录）。
作者程序已有输出参数时，可在 `execution.command` 中使用字面量 `{output_dir}`，例如
`--output={output_dir}/runtime.json`。本地执行后端替换为本次调用目录，不经 shell。
最多声明八个相对 POSIX 路径；不扫描 cwd、不解析 stdout 中的路径、不自动复制外部目录。
UTF-8 普通文件单个不超过 2 MiB；链接、越界、缺失、不可读或超大分别记录。
`results.json` 的 `output_evidence` 保存来源、预览和截断范围，原写作/审阅工具可按登记句柄
补读字符窗口。文件名和用途不证明内容真实；附件缺失不推翻已有效的指标，也不能支撑事实主张。
补读支持文件内词面定位，或按最多四个实际字段/标量条件筛选 JSON 记录数组、CSV/TSV 行，
避免盲猜字符位置。返回原始记录及匹配数量/截断状态，不汇总、不推断缺失值；词面命中不等于语义支撑。
生产结果按登记名称归属，不强迫套论文引用键或编造参考文献。
改变此契约属于执行输入修订，而非仅刷新报告；旧完成会话不静默补文件或重跑。

直接从作者 JSON/CSV 结果提取指标，而非只作写作附件：

```toml
[execution.metric_sources]
elapsed = { output = "runtime", path = ["elapsed_seconds"] }
coverage = { output = "paired_observations", column = "coverage", match = { method = "candidate" } }
```

JSON 路径明确选择字段或数组下标；CSV/TSV 条件必须唯一选中一行，值须为有限数值。
缺失、歧义或 stdout 与文件数值冲突会使提取失败，不隐式汇总、不猜列。
规范结果保留文件及选择位置；文件存在不会把失败或超时进程变成成功测量。

```bash
simple-ar research-session --config research.toml --session-root runs/research-session/<session>
```

省略的目标和 outputs 沿用原值；显式提供的新目标、outputs、本地文献或执行配置会修订现有
session。accepted plan 重新生成，attempt 历史保留；只有命令、结果 schema、协议、准备 lineage
和保护资产匹配的测量才复用。仅增加文献不会重跑有效实验；改变 task kind 仍需新建 session。

待处理的科研决定通过同一 CLI 入口答复；无需交互式 stdin：

```bash
simple-ar research-session --session-root runs/research-session/<session> --topic "原研究目标" \
  --decision-id ID_FROM_RICH --decision-response accept

simple-ar research-session --session-root runs/research-session/<session> --topic "原研究目标" \
  --decision-id ID_FROM_RICH --decision-response revise \
  --decision-guidance "补充事实或修订后的方向"
```

`decision_response` 为 `accept`、`reject` 或 `revise`。修订自动交付选择时，还要显式提供报告设置，
例如 `--report-template analysis_report`。TOML 答复写法：

```toml
[continuation]
decision_id = "ID_FROM_RICH"
decision_response = "revise"
decision_guidance = "补充事实或修订后的方向"
```

精确重放同一答复不会重复已完成工作；提案和答复作为分开的决策产物保留。改变目标或协议后，
旧决定不会沿用。`assisted` 会询问科研后续选择，`checkpoints` 只暂停在首次协议、实质方向/交付节点，
`autonomous` 在有依据且仍有授权轮次时自动选择；它们都不能越过缺失事实或权限。

### 显式续接额度

在同一 `research-session --session-root` 命令中可用 TOML 授权：

```toml
[continuation]
authorization_id = "review-20260924-1"
reason = "审查后的有界续接"
additional_attempts = 2
additional_no_progress = 1
remaining = { total_tokens = 50000, llm_requests = 8 }
```

`remaining` 对原账本已有的资源维度开启一份后续调用的新剩余额度；它不会估算或消除未知历史用量，
旧 ledger entries 和 attempt 计数仍保留。`additional_attempts`、`additional_no_progress` 增加持久化
上限，不清零已用量。CLI 等价选项是 `--authorization-id`、`--authorization-reason`、可重复的
`--authorize-remaining DIMENSION=AMOUNT`、`--additional-attempts`、`--additional-no-progress`。
相同 ID 只可重放完全相同的条款；需要新授权时使用新 ID，不要手改 ledger/manifest。
已完成会话仅追加额度时保持完成状态，不新增 attempt 或修改产物；授权不等于执行。
可先补资源额度，再补 attempt 额度；执行仍检查所有实际需要的上限。
重放相同条款不会补回已消耗额度；若上次续接在账本写入中断，会幂等补完该账本项。
报告刷新先完成授权，再调用 `research-report --refresh`；纯报告无需扩大进程权限。

保存的研究/报告设置继续以 session runtime_config 为准，除非通过入口显式修订当前支持的输入。
首次启动时仍应声明允许的 `process_invocations`/`process_wall_seconds`；续接只接受此会话账本已配置的
资源维度。自动寻找、下载和配置仓库仍未实现。

研究准备复用 CodeTask 的 auto/copy/git_worktree 选项及自定义保护路径。
auto 对干净仓库使用 worktree；有未提交源码或无法创建 worktree 时使用 copy 并记录原因。
显式 git_worktree 使用已提交 HEAD，不包含未提交修改。准备产物记录 Git 版本和隔离位置；
这不是每轮候选自动提交 Git。sparse_copy/empty 仍限独立 CodeTask 使用。

## CodeTask TOML 字段参考

CodeTask TOML 旧有的相对路径仍以运行时 cwd 为基准，不会被静默改写。若要让案例随仓库
直接运行，可在路径字段和 `[benchmark].command` 中用 `{config_dir}` 引用该 TOML
所在目录；机器拥有的项目、数据集、划分或解释器则写显式绝对路径。命令参数中的路径
可能含空格时须加引号。可用 `[environment].required_paths` 在配置加载时检查额外
数据/划分文件是否存在；这不验证数据内容。完整用法见[持续学习案例](../examples/continual_learning/README.md)。

| 字段 | 含义 |
| --- | --- |
| `[code_task].kind` | `existing_project` 表示已有源码项目 patch；`greenfield` 从 empty workspace 开始，并在 `code_task/workspace/generated_project` 下生成项目。 |
| `[code_task].code_root` | `existing_project` 的源项目路径；`greenfield` 仅在需要 scaffold/source root 时填写。原始项目不会被直接修改。 |
| `[code_task].task_file` | 独立 CodeTask 的任务描述；research-session 通过正式应用准备 implementation handoff。 |
| `[benchmark].command` | 在 `code_task/workspace` 中 patch 前后运行的命令。建议输出 `accuracy: 0.82` 这类可解析指标。 |
| `[benchmark].primary_metric` | objective verdict 使用的主指标。未知指标仍会记录，但最好声明方向。 |
| `[benchmark.metric_directions]` | 指标方向表：`higher`、`lower`、`resource` 或 `ignore`。 |
| `[environment].mode` | `current` 使用当前 SimpleAutoResearch Python；`external` 使用 `[environment].python` 或 `[environment].python_executable`。不会自动安装依赖。 |
| `[environment].python_executable` | `mode = "external"` 时必填；会把 benchmark argv 开头的裸 `python`/`python3` 解析为该解释器。已经是绝对路径的命令和非 Python 命令保持不变。 |
| `[environment].required_paths` | 可选的文件/目录列表；加载 CodeTask 配置时检查是否存在，可用于数据和固定划分。存在性不等于数据内容已验证。 |
| `[workspace].mode` | workspace 策略：`auto`、`copy`、`git_worktree` 或 `sparse_copy`。已有项目默认 `auto`，会优先尝试 git worktree，失败时降级为 copy 并记录原因。 |
| `[workspace].reuse_source_venv` | 检测到 source `.venv` 或 `venv` 时，是否记录并使用其中 Python。 |
| `[implementation].provider` | code-task 实现 backend。`local` 使用 SimpleAutoResearch 进程内路径；`fake` 用于确定性测试；`local_llm` 使用当前 LLM；`codex`、`claude_code`、`opencode` 和 `external_cli` 会在显式启用时走外部 agent handoff 边界。 |
| `[implementation].agent_mode` | backend 实现模式：`model` 表示 SimpleAutoResearch 仍拥有 harness；`handoff` 会从外部 agent package ingest candidate files；`delegated_workspace` 目前只识别并显式失败，等 snapshot/diff/rollback 执行边界完成后再开放。 |
| `[implementation].allow_external_agent` | 外部 CLI backend 启动前必须显式设为 true。外部输出仍需通过 SimpleAutoResearch 的 review、validation、benchmark 或 result guard 才能被接受。 |
| `[implementation].agent_model` / `.agent_binary` / `.agent_args` / `.agent_timeout_sec` | 可选外部 backend 启动设置。除非确认外部 CLI/账号支持某个模型名，否则建议让 `agent_model` 留空。 |
| `[implementation].max_repair_attempts` | implementation 侧修复尝试上限，也覆盖 greenfield validation 前的 review repair。 |
| `[resource].max_runtime_sec` | 暴露给 greenfield planning 和 external-agent handoff 的运行预算。 |
| `[resource].max_files` / `.max_generated_lines` | code-task 生成预算。`code-task execute` 会优先使用 `[execute].max_files` 和 `[execute].max_generated_lines`，缺省时再读取这里。 |
| `[resource].max_memory_mb` / `.allow_gpu` | 暴露给规划阶段的内存/GPU 约束，只描述允许的资源 profile，不会自动安装依赖。 |
| `[edit_scope].allowed_patterns` | automated edits 可以修改的 workspace-relative glob allowlist。空列表表示所有 normalized、非 protected workspace 路径都可编辑。 |
| `[edit_scope].protected_patterns` | 额外只读路径。tests、benchmark、`.env`、secret/credential-like 路径等默认 protected patterns 始终保留。 |
| `[edit_scope].mode` | 可选标签，写入 `manifest.json` 供审计使用；它本身不改变行为。 |
| `[safety].max_file_bytes` | copy/sparse 模式最大复制文件大小，避免误复制大模型、数据或 checkpoint。 |
| `[safety].validation_max_file_bytes` | 静态 validation 扫描文件大小上限。 |

静态检查遵守 Python 源码编码规则。Git worktree 中，确认与记录的冻结提交字节一致的
既有语法缺陷仍显示为警告；strict 模式不降级。新建/改坏文件与无法核实的基线仍报错。
这不是测试目录豁免，也不代表运行或受保护验证成功。

### Execute 与 Budget 字段

| 字段 | 含义 |
| --- | --- |
| `[execute].to_step` | 状态感知 executor 最多推进到哪一步。例如设为 `propose-edits` 可停在应用补丁之前，设为 `review` 可停在应用补丁后的结构化 review 之后。 |
| `[execute].use_llm` | 是否启用 LLM 支持的 work-plan、patch-plan、edit-proposal 和 repair 步骤。 |
| `[execute].timeout_sec` | executor 管理的 baseline 和 patched benchmark timeout。省略时会依次回退到 `[benchmark].timeout` 和 `[resource].max_runtime_sec`。 |
| `[execute].stream_benchmark_output` | 实时 benchmark log 模式：`off`、`line`、`auto` 或 `summary`。有 tqdm 这类进度条时建议 `auto`。 |
| `[execute].baseline_policy` | 已有项目 baseline 策略：`auto`/`run` 会运行未修改 benchmark；`skip`/`none` 不做 baseline comparison；`provided` 从 `[execute].baseline_metrics_file` 记录用户已有指标。 |
| `[execute].baseline_metrics_file` | `baseline_policy = "provided"` 时使用的 JSON 或 `metric=0.82` 文本指标文件。JSON 支持 `{"accuracy": 0.8}`、`{"metrics": {...}}` 和 `{"metric_values": {...}}`。 |
| `[execute].apply_proposed_edits` | 允许 execute 应用已经审核过的 proposal。review-first 流程建议保持 false。 |
| `[execute].allow_large_edits` | 允许应用超过 normal 预算但落在 large 预算内的已审核 proposal。 |
| `[execute].allow_planning_fallback` | 允许明确的离线规划，以及完整、用量已知的响应格式失败后的 greenfield fallback；不把提供方、预算、时限或未完成流失败替换成离线成功。要求模型实施的任务保持 false。 |
| `[execute].planning_mode` | Greenfield 规划：`tool_agent` 分需求、架构、接口、文件计划及有限审阅，正常至少五次调用；`compact` 在重试前仅一次架构调用。两者共用文件生成和执行，调用数不代表研究质量。 |
| `[execute].planning_review_rounds` | standalone code-task 中 greenfield planning reviewer 可触发的最大回修轮数。默认 `2`；轻量 smoke example 可设为 `1`，大型服务器任务可按需调大。 |
| `[execute].llm_retry_attempts` | work/patch 规划和 greenfield 架构/文件生成的完整响应纠正上限，含首次尝试。运输重试和等待只由共享连接的 `retry_attempts`（旧环境变量 `SIMPLE_AR_LLM_RETRY_ATTEMPTS`）负责，不再由阶段乘倍。账本有未核清操作时不纠正或 fallback，即使用量维度已记全。 |
| `[execute].repair_rounds` | 技术性验证/运行失败后的有限修复次数（非负整数，默认 `0`）。带 CodeTask 项目的 `research-session` 也用它限制单条件科研修订失败后的修复/复测；复测沿用既定协议与进程预算，不授权科研方法变更、增种子或配对修订的自动修复。修复仍需审核。 |
| `[execute].max_files` | plan/proposal/repair 步骤纳入 LLM 上下文的最大文件数，也限制新 greenfield 规划的文件数，包括补入的 `main.py`。完整规划超限时明确拒绝，不静默丢弃模块或依赖；已保存规划不按新上限重建。 |
| `[execute].max_source_chars_per_file` | LLM 上下文中单个文件的 source snippet 字符预算。 |
| `[execute].max_generated_lines` | greenfield 生成行数预算。省略时会回退到 `[resource].max_generated_lines`，再回退到保守默认值。 |
| `[models.code_task].planner` | work-plan 和 patch-plan 使用的模型。 |
| `[models.code_task].editor` | edit proposal 使用的模型。 |
| `[models.code_task].repair` | 失败后 repair proposal 使用的模型。 |
| `[budget].profile` | 当前 edit budget profile。`normal` 支持最多四个允许文件中的紧凑修改（总编辑字符 12,000）；`large` 用于经明确批准的较大修改。 |
| `[budget].max_batches` | 一个 code task 中 executor 最多创建多少个实现批次。 |
| `[budget].cost_cap_usd` | provider usage 返回费用估计时可用的成本上限。 |
| `[budget.*].max_files` | 单个 edit proposal 最多修改多少个文件。 |
| `[budget.*].max_edits` | 单个 proposal 最多包含多少个 old/new replacement。 |
| `[budget.*].max_old_chars` / `[budget.*].max_new_chars` | 单个 edit 的 old/new 文本字符限制。 |
| `[budget.*].max_total_edit_chars` | 全部 edits 的总字符预算。 |
| `[budget.*].max_proposal_chars` | 序列化 proposal 的总字符预算。 |

## Workspace Mode Variants

### `auto`

`auto` 是已有项目推荐默认模式。它会在 `code_root` 位于本地 Git 仓库且仓库至少有一次 commit 时优先创建 detached `git_worktree`；如果 Git 条件不满足，则降级为受保护的 `copy`，并在 manifest 与终端输出中记录 `fallback_reason` 和 `user_next_steps`。

```toml
[workspace]
mode = "auto"
reuse_source_venv = false
```

如果 `code_root` 是较大仓库中的项目子目录，SimpleAutoResearch 会在仓库根创建 worktree，并把对应子目录作为后续索引、修改和运行的 project root。

### `copy`

`copy` 会在 `code_task/workspace` 下创建受保护的物理复制。它适合非 Git 项目、尚未提交但希望包含当前文件状态的实验，或者你明确不想使用 Git worktree 的场景。

```toml
[workspace]
mode = "copy"
reuse_source_venv = false

[safety]
max_file_bytes = 2000000
```

### `git_worktree`

`git_worktree` 会创建 detached worktree。显式选择该模式时，如果 Git 隔离不可用会直接失败并给出修复提示，而不会静默降级。`code_root` 可以是仓库根目录，也可以是仓库中的项目子目录；源项目必须位于本地 git 仓库中，并至少有一次 commit；不需要连接远程 GitHub 仓库。

```toml
[workspace]
mode = "git_worktree"
reuse_source_venv = true
```

### `sparse_copy`

`sparse_copy` 只复制 allowlist 路径，并始终应用内置排除规则：`.git`、虚拟环境、`runs`、cache/build 目录、data/model 目录、`.env` 和 secret-like 路径。

```toml
[workspace]
mode = "sparse_copy"
include = ["src/**", "tests/**", "configs/**", "main.py", "pyproject.toml"]
exclude = ["data/**", "models/**", "checkpoints/**"]
reuse_source_venv = false
```

只有在你理解项目依赖关系时才建议使用 `sparse_copy`；它可能漏掉运行时 import 需要的文件。

## 独立 Code-Task Config

用于 `simple-ar code-task init --config PATH`，后续也可传给
`simple-ar code-task execute RUN_DIR --config PATH`。

```toml
[code_task]
kind = "existing_project"     # existing_project | greenfield
code_root = "path/to/project"
task_file = "tasks/improve_model.md"
output_root = "runs"
name = "my-code-task"

[benchmark]
command = "python main.py --config configs/experiment.json"
primary_metric = "accuracy"

[benchmark.metric_directions]
accuracy = "higher"
macro_f1 = "higher"
latency_ms = "resource"
loss = "lower"

[environment]
mode = "current"
# mode = "external"
# python = ".venv/Scripts/python.exe"

[workspace]
mode = "copy"
reuse_source_venv = false

[safety]
max_file_bytes = 2000000

[execute]
to_step = "run"
use_llm = true
timeout_sec = 120
repair_rounds = 1
stream_benchmark_output = "auto"
baseline_policy = "auto"
apply_proposed_edits = false
allow_large_edits = false
allow_planning_fallback = false
planning_mode = "tool_agent"
planning_review_rounds = 2
llm_retry_attempts = 3
max_files = 8
max_source_chars_per_file = 4000
max_generated_lines = 1600

[implementation]
provider = "local"            # local | fake | local_llm | codex | claude_code | opencode | external_cli
agent_mode = "model"          # model | handoff | delegated_workspace
allow_external_agent = false
agent_model = ""              # 留空表示使用外部 CLI / 账号默认模型
agent_binary = ""
agent_args = []
agent_timeout_sec = 600

[resource]
max_runtime_sec = 120
max_files = 8
max_generated_lines = 1600
max_memory_mb = 4096
allow_gpu = false

[budget]
profile = "normal"
max_batches = 3

[budget.normal]
max_files = 2
max_edits = 4
max_old_chars = 3000
max_new_chars = 4000
max_total_edit_chars = 12000
max_proposal_chars = 24000
```

独立 greenfield code-task 不需要填写 `code_root`，除非你有意从 scaffold/template
目录开始。workspace 默认使用 `empty`，`execute` 会把生成项目写到
`code_task/workspace/generated_project`：

```toml
[code_task]
kind = "greenfield"
task_file = "tasks/build_new_project.md"
name = "greenfield-project"

[benchmark]
command = "python generated_project/main.py"
primary_metric = "accuracy"

[workspace]
mode = "empty"

[execute]
to_step = "run"
max_files = 16
max_generated_lines = 4000
baseline_policy = "none"

[implementation]
provider = "local"
agent_mode = "model"
allow_external_agent = false

[resource]
max_runtime_sec = 600
max_files = 16
max_generated_lines = 4000
allow_gpu = false
```

如果要用同一个 greenfield 任务测试 Codex、Claude Code 或 OpenCode handoff，
保留 `[code_task]`、`[benchmark]` 和 `[resource]`，只切换实现 backend：

```toml
[implementation]
provider = "codex"
agent_mode = "handoff"
allow_external_agent = true
agent_model = ""          # 使用外部 CLI / 账号当前配置的模型
agent_timeout_sec = 1800
```

外部 agent 写出的仍然只是未信任候选文件。SimpleAutoResearch 会先 ingest，
再复制到 `code_task/workspace/generated_project`，之后继续走 review、validation、
benchmark、guard、memory 和 repair 路径。

## 在研究会话中使用 CodeTask

通过 research-session --code-task-config 提供现有项目 CodeTask TOML。
CodeTask 负责编辑与验证，应用负责实验测量和报告交付。

## Execute 与 Budget

`execute` 是状态感知调度器。下面配置控制它最多推进到哪一步、使用哪些模型、纳入多少上下文，以及允许多大的 edit proposal。

```toml
[execute]
to_step = "run"
use_llm = true
timeout_sec = 60
repair_rounds = 1
max_files = 8
max_source_chars_per_file = 4000
stream_benchmark_output = "auto"
baseline_policy = "auto"
apply_proposed_edits = false
allow_large_edits = false
allow_planning_fallback = false
planning_review_rounds = 2
llm_retry_attempts = 3

[execute.ablation]

[models.code_task]
planner = "gpt-4o-mini"
writer = "gpt-4o-mini"
reviewer = "gpt-4o-mini"
editor = "gpt-4o-mini"
repair = "gpt-4o-mini"

[budget]
profile = "normal"
max_batches = 3
cost_cap_usd = 2.0

[budget.normal]
max_files = 2
max_edits = 4
max_old_chars = 3000
max_new_chars = 4000
max_total_edit_chars = 12000
max_proposal_chars = 24000
```

`stream_benchmark_output` 取值：

| 值 | 含义 |
| --- | --- |
| `off` / `false` | 不实时转发 benchmark logs。 |
| `line` | 转发普通换行日志。 |
| `auto` / `true` | 同时处理普通日志和 `tqdm` 这类 carriage-return 进度输出。 |
| `summary` | benchmark 结束后只打印尾部摘要。 |

`baseline_policy` 取值：

| 值 | 含义 |
| --- | --- |
| `auto` | 默认行为；需要比较时运行未修改 baseline。 |
| `run` | 强制运行未修改 baseline。 |
| `skip` | 跳过未修改 baseline，但仍允许代码理解、review、validation 和最终 benchmark。 |
| `provided` | 从 `baseline_metrics_file` 记录用户已有指标；summary 会标注这些指标不是本次复测得到。 |
| `none` | 任务没有有意义的 baseline comparison，适合纯生成或验收式任务。 |

## Edit Scope Behavior

`[edit_scope]` 已经是 code-task 的真实安全契约，而不是只写给模型看的提示词。它会在多个位置重复生效：

- `init` 会把 `allowed_patterns`、`protected_patterns` 和可选 `mode` 写入 `code_task/manifest.json`；
- repo map、locate、context、work-plan、patch-plan、edit proposal 和 repair 会把 allowlist 之外的路径标为只读证据；
- `apply-edits` 写文件前还会再次校验 workspace-relative path、allowed patterns、protected patterns、当前 batch target files 和 old-text 精确匹配；
- 默认 protected patterns 始终保留，因此 tests、benchmark 文件、`.env`、secret/credential-like 路径等不会因为用户配置了 allowlist 而变成可写；
- `protected_patterns` 是在默认规则之外额外添加只读路径，不是替换默认规则。

常见配置如下：

```toml
[edit_scope]
# 允许模型改应用代码和必要的实验配置；测试、benchmark 与数据仍可作为 evidence 读取。
allowed_patterns = ["review_pipeline/**", "main.py", "configs/experiment.json"]

# 此示例不额外锁定配置；内置规则仍会保护 tests、数据、secret 和 credential-like 路径。
protected_patterns = []
```

如果 `allowed_patterns` 为空，含义是“允许所有非 protected 的 workspace 文件作为候选”。如果需要最严格的生产级流程，建议显式声明 allowlist，并把数据、配置、测试和 benchmark 放进 protected 范围。
