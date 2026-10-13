# CLI 参考

[English version](CLI_REFERENCE.md)

本文用于查命令语法与操作效果。首次使用看[使用指南](USAGE_zh.md)，TOML 字段看[配置参考](CONFIG_REFERENCE_zh.md)。`simple-ar --help` 及各命令 `--help` 显示当前安装版本的选项。

## 原生任务入口（实验性）

```bash
simple-ar start "检查这些结果并解释比较" --source results/ --model profile:NAME --yes
simple-ar start --resume-task "SESSION" --approve-execution "TOKEN"
simple-ar status "SESSION"
simple-ar results "SESSION"
simple-ar start --resume-task SESSION "缩短解释，不重新运行"
simple-ar start --resume-task SESSION --retry-model
simple-ar start --resume-task SESSION "结合这些补充笔记" --source notes/ --allow-network --yes
simple-ar start --resume-task SESSION --authorize-remaining llm_requests=10 --authorize-remaining total_tokens=100000 --authorization-id continuation-1 --authorization-reason "完成保存的目标" --yes
```

裸 `start` 询问自然语言目标；位置参数需求、没有结构化参数的 `--goal` 也进入原生工具循环。
`--guided` 和普通 `--kind` 为同一原生所有者选择成果偏好：调研、写作、复现、改码、数据、图形和代码。
适配器只询问目标和常见只读材料/项目/数据路径；公开来源/进程提案访问须明确确认，
继续复用命名模型选择与任务摘要。偏好名称不授予访问。显式结构化协议保留兼容入口，其控制参数
不能混用原生访问/预算参数。可重复提供 `--source PATH`，
文件或目录以只读 `source0`、`source1` 等别名供调查；新文件保存在会话 `workspace/`。
新挂载须为普通文件或目录，不接受符号链接及 `.aws`、`.env` 等私有/运行时路径。
挂载内枚举、搜索及读取也按大小写无关的私有名称规则处理；项目调查在读取前过滤。
新建与追加材料共用准入检查；追加无效材料不会改变原挂载、权限或账本。
`--prepare-only` 只保存任务，不调用模型或执行进程，但原生创建仍预检连接和凭据。
`--max-turns`（30）与 `--max-tokens`（600000）
约束整个保存任务，包括反馈；恢复不刷新额度。目前接通本地调查、来源检索/获取、代码创建和成果解释；
出版导出也已接通；图像工具需要明确命名路由与提交额度。工具可调用不代表任务成功，
完整科研助手验收尚未通过。
从安装和秘密引用开始的完整操作见[使用流程](USAGE_zh.md#可复制的普通用户流程)。
`SESSION` 为打印的完整任务路径，`TOKEN` 为当前精确 `Approval token`。
`--approve-execution` 必须配合 `--resume-task`；`--yes` 接受摘要，不批准命令。
批准前检查生成源码和提案。`status`、`results` 只读取保存状态，不调用模型。
无反馈的 `--resume-task SESSION` 恢复保存边界；完成且没有待处理动作时读取保存终态，不重放工作。
反馈共享原预算，暂停时的旧交付记录不代表当前请求交付。
实验性 `focus_work` 允许模型以具体工作目标选择 TOML `code` 或可选 `writing` 路由，或选择 `task` 返回原任务连接。
它只改变下一轮的模型连接，不创建子任务；调查、编辑、验证继续使用同一历史、权限、批准和账本。
切换须单独请求，恢复从已保存结果读取焦点；不重新给额度，也不代表代码任务已完成。
两种专业焦点均可在同一行动循环调查原文、编辑、使用授权工具及交付；writing 未显式配置时
保持原任务 client。原生焦点要求 Chat 模式，不支持的协议明确拒绝；`compose_document`
本身仍保留既有 Responses 能力。
续接时可用 `--source`、`--allow-network`、`--allow-execution`、`--allow-vision` 明确追加访问，
经确认后生效，非交互使用须加 `--yes`。原挂载、模型绑定、解释器、已完成工作和预算保持不变，
进程命令仍须单独批准；已启动或用量未知的操作须先核对。续接中的 `--prepare-only` 只展示提案，
不应用授权；追加访问不能与 `--approve-execution` 合并。
已终止、无接受回复的失败原生模型决策，可用 `--retry-model` 明确允许一次新决策attempt，
沿保存的有限输出上限和原剩余额度继续。旧失败和有界未知用量仍记录并占额度，不重放工具或整个任务；
已有缓存回复沿原恢复消费，不新增请求。在途请求、未确认预留、无界未知用量及未决工具均拒绝。
此选项须与反馈、访问/资源授权、命令批准分开使用；普通恢复不自动替换失败请求，
`--prepare-only` 仍不改变状态或发请求。
若要替换该已结束失败决策的不稳定连接，在 `--retry-model` 后增加
`--model profile:NAME` 或 `--model route:NAME`。确认摘要展示文本、代码和写作的连接；
其实际绑定与替换记录随新决策身份一起保存，视觉保留原绑定。新连接必须使用Chat协议及相同的有限输出上限。
旧历史缺少新模型必需的推理字段时不伪造补齐，应选择兼容的非思考配置。缓存回复不能换连接，
没有 `--retry-model` 而显式指定不同 `--model` 会拒绝；省略 `--model` 时沿保存的选择器，
不会被当前环境默认值替换。
资源不足时可显式续接保存的目标，使用与 `research-session` 相同的可重复
`--authorize-remaining DIMENSION=AMOUNT`，并提供 `--authorization-id` 和
`--authorization-reason`。这些参数只能配合 `--resume-task`；没有额度而单独提供 ID 或原因会报错。
确认摘要展示已记录的累计用量和拟授权 remaining：原累计用量不清零，新 remaining 不是总历史限额。
`--yes` 可接受摘要；`--prepare-only` 只预览，不授权、不执行任务。相同授权 ID 重用不会补回已消耗额度；
普通 `--max-tokens`/`--max-turns` 在恢复时不能续额。模型绑定与权限保持不变，执行仍需精确批准。
资源续接不能与单次 `--approve-execution` 混用，须分开确认。已结束且保留有限费用上界的文本调用可在
明确的新额度下继续；旧未知用量及账本记录不结算、不释放。在途调用、无界用量及未确认副作用须先核对。
文件工具可使用短挂载定位，如 `source0/README.md`、`workspace/project`，也接受观察到的授权根内绝对路径。
生成代码及进程argv使用观察到的文件系统地址或真实工作目录下的相对路径；挂载名不是实际目录，不重写程序字符串。
绝对路径不扩大授权根，也不授予修改来源的权限。
每轮模型取得共享账本的剩余额度、未知用量维度及预计输入开销，用于调整范围；
这些信息不会增加额度，也不保证任务完成。
`inspect_project` 对授权目录及可选的明确 `data_paths` 复用只读项目准备调查，
返回依赖声明、入口/测试/说明候选、读取范围及完整 attempt 记录。
私有路径在建立索引前排除；派生观察不能当作论文原文、执行授权或任务解释器已就绪的证明，
不导入或运行项目代码。
`copy_project` 经同一 workspace 网关复用既有 CodeTask 复制所有者，将授权来源目录准备为可编辑项目。
可选 `archive_name` 为简单 `.zip` 文件名，例如 `project.zip`，在新副本内一并生成 ZIP，
仅包含实际复制的文件，使用相对路径。ZIP 沿同一复制操作、账本及恢复回执，
无需生成打包脚本或执行进程；继承下列复制排除规则与大小限制。
应检查 `copy` 报告中的遗漏及原因，再判断是否足够交付；返回的 `files` 包含 ZIP 的 SHA256。
归档名称与复制输入冲突时拒绝。恢复按保存回执核对已发布文件和 ZIP，不重新打包变化后的内容。
用户只需在普通自然语言目标或反馈中要求可搬迁 ZIP，无须自己调用内部工具。
生成 ZIP 本身不代表验证通过或正式交付。
`source` 指定来源目录，`path` 指定可写 workspace 内尚不存在的子目录；支持挂载定位和授权绝对路径。
例如工具参数 `{"source":"source0/project","path":"workspace/project"}` 复制普通源码、测试、配置、文档及二进制资源，
不使用 shell、网络或执行项目，原材料保持不变。
跳过缓存/构建目录、符号链接及原生私有/运行时路径，包括 `.git`、`.aws`、`.ssh`、`.codex`、`.agents`、
虚拟环境及所有 `.env*` 名称（也包括 `.env.example`）；单文件超限也跳过。
`copy` 报告包含复制数量、字节数及有数量上限的跳过项和原因；先核查报告，判断可编辑子集是否满足需求。
`max_files` 默认1000，范围1–4096；`max_total_bytes` 默认134217728（128 MiB），范围1–536870912；
`max_file_bytes` 默认2000000，范围1–67108864。路径不合法、无可复制文件、目标已存在或文件总数/总字节超限时拒绝，
不发布目标。成功数据包含 `absolute_path`、`source`、`copy` 及记录项目相对路径和 SHA256 的 `files`。
候选发布前，`attempts/IDENTITY/project-copy.json` 持久记录请求及输出摘要。
恢复要求请求相同且当前目标摘要匹配；同 ID 返回保存的结果，不再次复制，任何不匹配均阻止恢复。
这是可编辑子集，并非 Git 仓库完整复制，不保证无省略、依赖已安装、环境已准备或执行已验证。
后续执行仍沿既有 SessionController 唯一运行所有者，要求正常授权及批准。
`write_file` 可选 `delivery` 包含 `summary`、`artifacts` 和 `unresolved`。
必须引用本次写入的文件，其余产物须已存在于可写工作区；路径不合法时先拒绝，不写文件。
成功的最终写入复用既有交付与终态所有者；普通写入及独立 `deliver_result` 行为不变。
`run_process` 同样可显式指定 `delivery`，用于授权范围内的最后工作。
进程必须成功退出且未被停止，所声明的产物须在执行后实际存在。
进程回执绑定请求和产物摘要，中断恢复消费原绑定，不重跑命令；产物变化、缺少绑定、
命令失败或终态位置不合法时拒绝交付。交付声明包含在精确执行批准中；普通进程成功
不会自动结束任务。这是合并最终工作与明确交付，不是科学核验或自动打包。
产物也可指定工作区项目目录；沿已有清单展开为每目录最多200个可访问文件的明确快照，
排除私有路径、虚拟环境、Python字节码缓存及符号链接。空清单或超限拒绝，不悄悄截断。
这是实际文件记录，不是归档，也不证明项目可搬迁或内容正确。
输入历史软保留目标使用当前连接的 `context_input_tokens`（默认24000估计tokens），计入工具契约；有限余额扣除当前连接的生效输出预留，不按请求次数均摊或固定比例猜测。当前完整操作即使超过目标也保留；共享容量内继续保留连续完整的较早决策及匹配工具结果，`retained_turn_start`标明这段历史的起点。
遗漏前缀可补充版本核验原文及最多2000估计tokens的观察索引，不重复已保留完整轮次。`history_archive`标记省略的已接受轮次范围及历史失败数，不表示证据缺失或失败已解决。只读`read_history`按页回读原索引和内联取消记录，不调用模型、不另存记忆；字节续读须固定返回的`end_turn`并采用`next_offset_bytes`，attempt编号不是轮次编号。最新执行及最近非零执行的原始诊断在其他当前消息不可见时单独保留。旧推理保存在原记录，历史身份不是当前文件内容或已核实结论，原始记录不改。
容量不足时较早索引退出参数与观察正文，保留原记录定位、最近非零执行诊断和最后执行结果。
索引可使用无损列/行呈现减少重复字段；最终上下文元数据也计入输入估算。
历史失败是那次执行的事实，不是当前未解决问题或自动阻断声明。
它不是输出上限、精确分词保证或完成证明；受保护上下文仍可能超出目标或余额，请求须通过预算预检。
当前完整轮次本身已超软目标时，退出可选旧轮次、索引和原文窗口，仍不拆开当前完整操作。
不设输出上限时，预检只能预留估计输入，最后一次响应可能超过剩余估计；仍按实际用量
记录并阻止后续请求。任务token额度此时不是提供方侧的严格费用上限。
较早轮次退为索引时，最后执行及最近非零 `run_process` 的完整保存结果仍可见；已在近期完整读回的
同一记录不重复供给。它描述那次执行的退出码及输出，不代表当前文件通过；取消或被拒绝
的提案不能代替执行事实，也不改变原账本或解除未知操作的恢复限制。
原生任务的 `--project-python PATH` 为 `python`/`python3` 进程选择已有解释器并保留虚拟环境入口路径，
不安装依赖或创建环境；续接复用已保存的解释器，不能静默替换。该解释器目录置于过滤后的进程
`PATH` 前部，子命令可沿同一任务环境查找；Python 脚本建议用 `sys.executable` 调用子进程，
不创建别名垫片。命令批准绑定实际 argv 与过滤环境，变化须重新批准；已有执行回执恢复不重跑。

`list_directory` 可用 `recursive` 和 `file_glob` 按文件名发现材料，返回挂载路径及分页，
不读取文件正文；遍历最多4096项，达到边界报告 `inventory_truncated`，不能据此认定文件不存在。
`find_source_passages` 不提供查询时先查看原文记录的身份及保留文本覆盖，再按问题读段落，
不必为了辨认材料读取完整记录 JSON。

新原生 `edit_file` 工具声明统一使用 `replacements` 列表；修改须在同一原文件中唯一匹配且不重叠。
历史单处 `old_text`/`new_text` 参数仍接受，校验与写入边界保持不变。
提供当前文件摘要，全部匹配校验通过后才一次备份、原子写入，失败不留下部分修改。
`read_file` 可以按字节偏移，或按检索所得的 `start_line` 行号读取；行窗口保留摘要及字节续读位置，
受返回字节/行数限制，支持最多256000字节的文件；更大文件仍使用字节窗口。
字节窗口显式提供 `max_lines` 时也按行数限制，包括从偏移零开始读取；省略时保留仅按字节限制的行为。
同时提供时两种限制都生效，`max_bytes` 可能截在行内。续读使用返回的 `next_offset_bytes`，
不根据解码后的文字猜测字节位置。
较大的数据或图片可用 `read_file full_digest=true` 对最多64 MiB的授权普通文件流式计算全文摘要，
仍只返回指定文本窗口；默认摘要策略不变。`copy_file` 使用该源摘要逐字节复制文本或二进制文件，
上限相同，替换已有目标还须提供其当前摘要。复制与文本写入共享版本校验、备份及原子提交，
不解码源文件，也不把全部内容送入模型。目录复制保留独立的大小限制及遗漏说明。
原生 `search_files` 依赖执行主机的 ripgrep（`rg`），仅检索授权文件，明确扫描分页、续查和截断，
不等于全仓索引。只读检索仍消耗共享进程收据及额度。新任务每个模型轮次额度对应最多十次进程调用，
总进程墙钟预算不变；恢复保留原额度，包括旧任务。
它复用 `write_file` 的备份与原子写入路径；过期、歧义修改以及只读来源修改会被拒绝。

`--allow-network` 独立授权公开来源检索和原文获取，不授权执行代码。复用 arXiv、OpenAlex、
Semantic Scholar 和可选 Tavily（需要对应凭据）。搜索记录和取得的原文保存在只读 `evidence/`，
不进入可改写输出区；按问题查段落是本地操作，保留来源位置和截断范围。每次获取目前约束
一份文档、三次获取尝试、8 MiB 和60页 PDF，解析覆盖/失败仍记录。搜索摘录不是全文，
取得不等于核查通过。共享 `source_operations` 额度计提供方/适配器操作，不是HTTP请求数或供应商积分。
已保存文档记录也可从用户提供的只读挂载中查段落，不需要网络；查段落与起草按解析后的目录归属检查，
无论使用别名或绝对路径，都拒绝用工作区生成记录、执行历史冒充原文。
起草可在同次写作响应中提出首页可见的缺失书目字段，沿原文引句核对后装配；
不覆盖已提供元数据，也不等于独立验证了出版身份。

实验性 `compose_document` 工具使用选定的文档记录/chunk IDs 起草文章，也可根据旧 Markdown
稿生成反馈版本。配置 writing 路由时使用该连接，否则沿用任务文本连接；共用模型额度，保存带出版引用键的 `manuscript.md`、
书目及绑定记录，保留旧稿。查看、修改和导出使用同一正文，装配展示副本只留在工具记录，
不作为另一份可编辑稿。
新整篇文章明确绑定装配形式并保留作者的标题层级；历史稿件恢复仍使用其原装配方式。
不能通过修改已保存装配记录重新排版已完成的出版成果，应创建明确的新版本。
新起草从选定原段、材料窗口、已知书目、图形及可选旧稿完成一次整篇写作请求，
不再运行旧分节规划和审阅生命周期。原输入和响应先保存再校验，共享 LLM 客户端负责有界传输尝试及计账；工作单元不另建格式纠正或重试循环。
需要目录选材时仍单独计入原任务额度。结构化写作命令不变。
未明确指定材料行数时保持字节窗口语义；恢复沿原绑定的实际字节范围读取并核对内容，
不把历史序列化的默认值重新解释成当前读取限制。
工具不重新检索、执行实验或自动复审；调用任务仍须查看正文并核查重要主张。
完整且绑定原输入的整篇响应可零模型续接装配和缺失发布文件；历史Writer仍核对原checkpoint。
部分、未知或失败起草不重发；已修改的部分文件保留并拒绝覆盖。复用核对已保存正文、书目和图形内容；产物被修改、删除或旧记录缺少内容绑定时，
应检查当前文件或另创反馈版本，不自动重生成。接通此工具不代表普通入口写作验收已通过。
已保留的原始连接器身份直接供引用使用，不要求再次指定材料角色；未知出版字段保持未知，
旧稿和派生分析仍与原始参考来源区分。

本地材料目录可通过 `material_roots` 交给写作所有者选材，不必由协调层先列全 `materials`
窗口。选材与代码准备共用有界库存、私有路径过滤和版本绑定，模型调用计入原任务额度。
结果报告实际选中文件、未读范围及未选文件数量。`material_roots` 可与显式 `sources`、
图形和旧稿组合；只有显式 `materials` 窗口与 `material_roots` 互斥。
显式定位符使用授权绝对路径或`workspace/…`等挂载别名，在付费选材前检查；
已保存的混合选材请求保留原绑定的恢复语义，不能借恢复改变选择。
目录也可包含保存的 `document_bundle.v1` 原文，例如任务的 `evidence` 目录。写作所有者保留
其身份，选择相关来源问题，沿现有检索器取段后起草；生成工作区和执行历史不能成为原文证据。
来源声明和预览不代表核验通过，选材操作不搜索或取得新来源。
选材不等于全文阅读、统计验证或看图，输入变化和无终态选材不盲目重发。

写入、精确编辑与文本复制共用版本前置条件。冲突会返回已授权目标的当前摘要和调用提供的
预期摘要，不写文件、不回传正文；这只是观察快照，不是自动重试。下一次仍须显式提供匹配
摘要，随后变化仍会拒绝；只读或私有目标不泄漏冲突摘要。

原生写作、代码准备和视觉检查由同一任务所有者解析连接，共用保存的任务身份和账本。
新授权的视觉路由单独保存非秘密连接身份，不包含它不使用的代码路由；模型、命名连接或端点改变时，
在发送图片前拒绝调用。密钥轮换与运行超时/输出设置仍按已有兼容规则处理。
旧任务若已授权视觉但没有该绑定，保留旧解析语义，不补造历史身份；图像生成仍沿自身版本化提交/恢复所有者。

`--allow-vision` 单独授权 `inspect_images`：挂载的 PNG/JPEG/WebP 交给 TOML 的 `vision`
路由，使用同一任务的模型请求/令牌账本。缺少视觉路由会在创建前报错，不拿文本模型替代。
每次检查保存原图副本和反馈；已保存响应复用，未取得响应的中断请求不盲目重发。
不合响应契约时与连接失败分开报告，原回复保存在本地，不冒充有效意见或自动重发。
视觉意见不证明数值或科学正确。资料检索权限不授予视觉调用，视觉权限也不授予检索。

`--max-images N` 授权 TOML `image` 路由最多 N 次图像生成/编辑提交；默认零，不开放这些工具。
`create_image` 新建版本，不覆盖输入；`resume_image` 接受其确切版本，复用完成结果，或对已知
异步任务查询/下载，不重发未知提交。全部版本共用任务图像账本，恢复不刷新额度。
创建任务前检查路由、代理和凭据；生成位图不作为测量结果或可编辑矢量源文件。

`prepare_implementation` 可接受目标和 `project_path`，不要求协调层先列完文件。
代码所有者检查有界项目，用一次模型请求选择相关的既有/新文件，再将摘要绑定的
完整原文交给原有生成请求；选中但最终无需修改的既有文件可返回空替换，
保留原始字节，不要求为了满足文件清单制造改动。新建或已调查代码仍可显式提供 `files`。
选择与生成共用任务账本，选段覆盖、选择和输入存入原 attempt，不创建第二份计划或会话。
已完整返回但不符合响应契约的提案，可在剩余额度内纠正一次，原提案和纠正结果均保留；
本所有者不重试运输失败、未知用量或 Python 语法失败。中断提交不盲目重放；
候选生成不负责转入项目、执行、核验或交付。

`export_report` 将工作区 Markdown 及本地附件转换为可编辑 ACM 工程，可选编译 PDF，
复用既有导出器和共享进程收据。需要执行权限及明确批准；编译成功不证明文章质量或页数要求达标。
导出清单包含完整工程、图形与重建说明。新导出绑定实际文件摘要后才可复用，文件变化或缺失
不能沿用旧编译结果。历史仅绑定输入的回执仍可查看，但不追认当前字节或自动交付；缺少成功
输出绑定的中断导出不盲目重跑。原文与旧版本保留。
工作区 Markdown 拥有其当前作者图注；图形元数据只补空图注并提供可编辑变体，不覆盖已修订正文。
受控装配报告继续采用登记图注的原所有权。

`--allow-execution` 允许提出命令，不自动批准执行；需要终端确认，或恢复时提供显示的
`--approve-execution TOKEN`。`--yes` 仅确认创建任务。工作区不是操作系统沙箱：时限和日志
边界不提供 CPU、内存、文件系统或网络隔离。恢复沿保存的模型连接、材料、权限、收据与预算，
不能混用结构化设置或成果复用开关。交付清单检查文件存在，不证明科研内容正确；
未知的中断操作不自动重放。新原生任务保存非秘密模型连接绑定；同名模型、端点或协议改变时，
在新调用/执行前拒绝，需恢复原目录连接或另开任务。密钥轮换、超时和输出上限沿既有兼容规则处理。
没有这份快照的旧原生任务保留原解析行为，不能据当前配置重构或补造其历史连接。
反馈会取消尚未启动的待执行提案；已尝试操作必须先核对状态。
`simple-ar results SESSION` 显示当前交付文件、限制和原生续接命令。
修订暂停时旧交付仍在磁盘，但不显示为新反馈已完成。

多步工作可用 `update_work_plan` 保存可调整的简短方案，并与实际操作一起提出。
当前请求的最新计划在历史压缩和恢复后保留，不另调用规划模型或建立调度器。
新反馈不能自动沿用旧计划作为已接受安排；简单任务无需先规划。
步骤状态和证据定位只是模型声明，不授予执行许可，不证明结果正确，也不结束交付。

## 成果复用与概念图

需要继续检索的研究追问：
`simple-ar start --kind survey --from-session SESSION --reuse report --goal "新的问题"`。
新任务复用报告的原始来源，排除来源包内的旧稿及派生分析附件，默认在线检索。
重复来源按完整文本版本合并；来源仍须按新问题重新筛选并核查。
配置生成与执行均接受保存的来源包；提供本地来源不关闭明确要求的在线检索。
历史会话按已声明的报告材料归属排除无角色的本地旧稿，不凭文件名判断来源角色。

独立方法图/架构图使用 `simple-ar start --kind figure --goal "绘制观测→比较→证据的关系" --allow-execution`。
进入同一原生行动循环，不制造假数据或修 bug 工程；可用 `--document`、`--material`、
`--data-file` 挂载本地材料，要求交付源码/布局、PNG、可编辑矢量 SVG 和解释。
成果仍须实际检查，入口不证明语义或视觉质量通过。

结构化兼容代码任务默认允许一轮技术修复。未指定进程限额时，`research-session` 为首次验证及已配置
的修复复验预留额度；用户明确的限额（包括零）仍优先。修复额度不授权安装、改变科研协议
或无限重试。

`simple-ar start --from-session SESSION --reuse code_project --goal "调整布局"`
在新隔离任务中继续已验证的代码/绘图项目，保留登记的编辑范围、保护文件、验证命令和补丁策略，
不改原交付。此选择不能混用写作材料选择，也不能同时覆盖项目、编辑范围或验证命令。

`simple-ar results SESSION` 列出当前会话登记的可复用成果。
`simple-ar start --from-session SESSION --reuse data_analysis --goal "解释性能与开销的权衡"`
可直接用分析包写作；`--reuse report` 或 `summary` 复用相应成果，可重复选择。
`--reuse code_analysis` 复用已验证代码分析项目。新生成的登记交付包包含脚本、输入数据、
解释、JSON 结果和 PNG/矢量导出，可选正文图并保留完整链接附件；复制脚本输出不代表
本次独立数值复算。未登记交付包的旧会话仍只复用解释与 JSON。
这是新写作任务，不是中断恢复；原会话不修改、不重跑。报告携带已登记原文包、实验依据与引用身份，
保存的段落可继续读取，不重新下载；
引用元数据不等于原文或本次重新核验；旧正文单独保留为待修改草稿，不进入来源证据选择，
也不代表新测量结果。分析包沿用源数据复算。
保存的论文沿原连接器身份和引用别名使用，文档/段落存储ID不产生新参考文献；
链接说明仍为资料及其可读文本，不另生成重复的论文证据入口。

需要重组论证或章节结构时，用 `--reuse report --goal "重组论证与章节结构"` 新建自适应
写作任务，复用登记证据，不改原任务已接受的大纲；普通修订保留原大纲。
历史登记的分析/绘图项目以 `analysis.py` 为入口，辅助模块和可编辑布局可放入 `src/`，
该目录随脚本和数据进入可搬迁交付包。
显式兼容 CodeTask 工程保留large的文件数与总量限制，单源码块采用已有32,000字符绝对上限。
CodeTask的 `[budget]` 数值设置也贯通科研执行与恢复，不绕过绝对限制或保护路径。
对象形式的 `outputs/results.json` 可在 `figures` 中登记相对路径 `path` 和图注 `caption`，
登记、写作与导出保留来源图注；旧式路径列表仍按文件名回退，图注本身不代表核验通过。
已有来源身份可通过 `results.json` 的 `citation_map` 保留，沿用 `citation_map.v1` 格式。
写作复用这些记录，不猜测缺失的作者、日期或标识；书目元数据不等于原始证据。

`simple-ar image --config models.toml --prompt "方法概览" --output figures/v1`
使用命名目录的 `image` 路由；可用 `--model profile:NAME` 选择连接。
编辑时加 `--input figures/v1/image.png`，并选新输出目录，例如 `figures/v2`。
原图不覆盖，版本保存请求和单次调用账本；相同已完成请求恢复不再次调用。
失败或中断不自动重发。通用 `openai_images` 要求提供商返回 base64 图片，拒绝 URL-only 结果。
显式 `cctq_images_async` 在已有 task ID 时恢复同任务轮询，认证下载配置同 origin 的
`files/0`，不重复 POST；没有保存 ID 的请求不自动重提。该适配要求提供商支持相应异步协议，
不自动切换为其他 API 或模型。
已记录完成的任务直接恢复文件下载；可调整恢复的超时和 HTTP 协议，不改变生成内容、原请求记录或重新提交。
编辑连接须声明 `image_edit`；位图概念图不是科学测量或分层矢量源文件。

`simple-ar image-review --config models.toml --input figures/v1/image.png --goal "检查标签、箭头和多余文字" --output figures/review-v1`
通过独立 `vision` 路由检查实际图片；可用 `--model profile:NAME` 选择其他视觉连接，
重复 `--input` 提供最多四张图。保存源图副本、原始响应、`review.json` 和 `feedback.md`。
相同完成请求恢复不调用 API；已有原始响应可离线重新解析。输入改变须使用新输出版本，
未保存响应的中断请求不盲目重发。反馈可用于原代码项目或新 `image --input` 编辑，
审阅命令本身不改图，也不证明科学正确性。

代码分析/自定义绘图可用：

```bash
simple-ar start --kind data_analysis --scripted --data-file measurements.csv --goal "比较性能开销并做三面板图" --allow-execution
simple-ar start --kind code --material requirements.md --goal "创建可复用CSV验证器" --allow-execution
```

这些新任务直接使用已有原生行动循环、配置、账本和恢复，不生成占位工程、内部检查器，
不再改成 `bug_fix`。原目标完整保留，其后追加交付要求和用户声明；任务偏好不限制工具、
图型或多文件能力。脚本分析要求可运行源码、真实计算结果和解释，无需占位图。

`--document`、`--material`、`--project`、`--data-path`、`--data-file` 与 `--source` 都成为只读挂载，
保存用户声明的材料角色。data-file 保留普通文件、软链接/私密文件拒绝和20 MiB上限；
更大材料可用 `--source` 做有界调查。不制造输入快照或假数据；新源码和成果归任务 workspace。
用户提供的评价器也是只读材料，这不是操作系统沙箱。此适配器拒绝公开URL文档参数；
需要获取时把URL写入目标并单独授予 `--allow-network`。

明确的列、单位、来源声明、观测单位、配对、缺失处理及呈现选择原样写入目标；
未提供的预设默认值不锁定分析能力。声明不代表统计前提已成立，必要语义仍须澄清。
`--with-report` 要求基于真实材料/结果写报告。`--model`、`--project-python`、
`--output-root` 和原生预算/访问参数直接交给同一所有者，不新建环境或安装依赖。
功能名称不授权执行或联网；`--allow-execution` 只允许命令提案，实际命令仍逐项批准，
`--yes` 只接受启动摘要。`--prepare-only` 保存原生会话，不调用模型或进程。
恢复及反馈使用 `start --resume-task SESSION`，成果由 `results SESSION` 列出。

原生/共用/材料/声明参数之外的显式控制选择结构化兼容：来源/获取政策、报告模板/上限、
编辑范围/验证器、复现命令/指标/环境、物理表格上限与交互设置。
即使显式提供解析器默认值也选择该协议，不把严格字段静默改成偏好文字。
`--chat`/`--resume-setup` 保留技术设置及历史确认参数；普通写作的
`--from-session`/`--reuse` 将所选登记成果包挂载到原生任务，不重跑有效测量或导入旧账本。
显式模板、调研原文过滤及 `code_project` 的范围/验证仍保留结构化兼容合同。
这些兼容路线
保留登记范围和验证器。偏好取值为 `survey`、`bug_fix`、`reproduction`、`writing`、`data_analysis`、
`figure`、`code`；`--scripted` 必须配 `--kind data_analysis`，创建偏好不能用于 `--resume-task`。
data_analysis 不加 `--scripted` 且显式提供 `--value-column`、`--x-column` 或 `--paired-baseline`
时选择无模型 TableSpec 快捷入口；单独数据路径仍是普通原生偏好。
figure/scripted 与无法保留的严格控件组合在创建前拒绝。单独项目路径为只读原生挂载，
`--allow`/`--validate` 保留精确结构化编辑和执行语义。严格协议不能混用原生访问/预算控制。
直接 `research-session`、
`code-task init/execute` 仍是显式结构化接口；历史会话按原执行所有者继续，
不把旧规划、基线、审阅或测量参数假称已映射成自然语言任务。

## 引导入口

已验证的复现项目可用 `start --kind reproduction --from-session SESSION --reuse code_project
--document PAPER --goal "检查就绪并重复有限测量"` 生成隔离任务，继承单命令协议、输出指标选择器、
编辑范围、检查器和解释器，不重新创建已有适配器或安装依赖。执行前确认显示的范围。
配对或扩展协议继续用原会话恢复；要改变测量条件，应另建任务。

`simple-ar models [--config PATH] [--profile NAME --purpose text|code|writing|vision|image]`
本地检查命名连接和凭据是否配置，不调用 API、不显示密钥。
共享目录放在 `~/.config/simple-ar/models.toml`，或用 `SIMPLE_AR_MODELS_CONFIG` 选择其绝对路径。
TOML 保存路由和连接设置，`api_key_env` 引用环境/私有 `.env` 中的凭据。
终端中的 `start` 显示文本 profile 供选择；`--model env` 有目录时使用 text/default，
无目录时使用旧环境连接，`--model profile:NAME` 可明确指定。代码工作用 code 路由，
原生写作未设独立 writing 路由时沿用任务连接。原生决策要求 Chat。
启用与路由见[模型配置](CONFIG_REFERENCE_zh.md#命名模型连接)。

```bash
uv run simple-ar start
uv run simple-ar start --chat
uv run simple-ar start --kind writing --material notes.md --model env
```

普通 `start` 使用原生任务；明确的结构化协议仍由 `research-session` 执行。

复现对话在读取项目源码与命令依据后，可提出明确的新 Python 适配文件范围和独立验证命令。
用户确认后写入原 `allow` / `validate` 配置，不覆盖已明确给出的范围或命令；
提案本身不执行代码，也不代表适配器已经实现。
可明确选择任务 venv，在同一隔离工作区先准备授权依赖，再由同一解释器改码、独立验证
和测量；安装失败即停止。基础解释器只用于创建任务环境，不额外指定 `check-argv`。
授权准备时才在隔离工作区初始化明确的新文件，再建立索引；任务输出须放在作者项目外。
没有创建声明的普通 `--allow` 路径仍须已经存在。

在线调研在模型阅读发现明确缺口后，可在自动扩展开启时追加一轮搜索、取得与阅读，
沿用原有限额和已保存证据。`--fulltext` 仍决定远端原文权限；找到摘要不等于取得原文支持。
`start` 与 `research-session` 的 `--web-extract-backend direct|tavily_basic`
选择 HTML／文本原网页取得方式，不自行开启取得权限或改变 PDF 权限。
可选 Tavily 后端需要 `TAVILY_API_KEY`。
阅读可请求原文中的数据或项目资料链接，而非继续检索论文。关联资料重定向共用剩余获取
额度，每次取得最多四次GET，每跳检查公开地址；取得说明页不等于代码可运行、数据下载
成功或科学判断正确。

| 选项 | 适用范围 | 用途 |
| --- | --- | --- |
| `--guided`、`--kind`、`--goal`、`--output-root` | 新任务 | 可选成果偏好、目标与输出位置；严格控制选择兼容路径 |
| `--model` | 模型任务 | 命名连接或兼容环境方式 |
| `--interaction` | 结构化任务 | 参与策略；显式提供会选择兼容路径 |
| `--prepare-only`、`--yes` | 原生／结构化设置 | 仅保存、不执行/API；或接受展示的摘要，不增加执行权限 |
| `--chat`、`--resume-setup PATH` | 模型辅助设置 | 澄清任务或恢复设置草稿 |
| `--sources materials\|search`、`--document PATH`、`--fulltext`、`--max-cited-sources N` | 调研／改码 | 可选来源访问、可重复材料、全文与引用上限 |
| `--material PATH`、`--template` | 写作 | 可重复笔记/结果与报告形式 |
| `--project`、`--validate`、`--allow PATTERN`、`--project-python PATH` | 改码 / 显式复现 adapter | 项目、独立验证、可重复修改范围、已有解释器 |
| `--data-path PATH` | 改码/复现 | 可重复数据文件/目录，不是表格分析输入 |
| `--hypothesis`、`--dataset`、`--expected-outcome`、`--metric` | 复现 | 结论、条件、输出指标 |
| `--cwd`、`--timeout-sec`、`--output-files JSON` | 复现 | 目录、超时、登记输出文件 |
| `--metric-sources JSON` | 复现 | 从登记文件按 JSON 路径或 CSV/TSV 唯一行取指标，不隐式汇总 |
| `--environment current\|venv`、`--requirements PATH`、`--install-project` | 复现 | 明确依赖准备 |
| `--check-argv JSON` | 复现 | 明确的测量前短检查；不猜测命令，检查指标不进入测量结果 |
| `--command ARGV...` | 复现 | 实际命令，必须最后提供 |
| `--with-report` | 数据分析／改码 | 根据已保存的分析或代码验证结果附加写作，需要模型 |

作者输出参数可用字面量 `{output_dir}` 绑定本次运行目录，不经 shell 展开。
文件指标选择格式见[配置参考](CONFIG_REFERENCE_zh.md)。

偏好为 `survey`、`bug_fix`、`reproduction`、`writing`、`data_analysis`、`figure`、`code`。技术 `--chat` 保留受支持的结构化协议；figure/scripted 提案不能重建已退出的脚手架，会明确拒绝。结构化准备不调用模型；chat 设置本身需模型和终端，不能与 `--prepare-only` 同用。

改码对话可只读查看项目文本并提出带原文依据的验证 argv；用户确认后序列化原 CodeTask
验证命令，保留明确的 `--validate`。设置阶段不运行命令或安装依赖，显式结构化改码仍确认修改范围与执行。

改码可用 `--document PATH` 提供参考资料，或用 `--sources search` 显式授权在线检索；
`--fulltext` 需要在线来源权限。加 `--with-report` 可根据实际补丁和验证结果写说明，
不将代码检查当作科学测量。这些选项不会扩大 `--allow`，也不会更换独立验证命令。

改码默认当前解释器；`--project-python` 保留已有 venv 入口，不创建或安装环境。复现也可用它选择正式命令与短检的解释器；明确选择 `--environment venv` 时，它指定新任务环境的基础解释器。对话可经确认采用用户原话提供的已有解释器路径，不从源码说明中自行选择。

显式复现协议仍走固定命令路径。已有 Python 项目可显式组合 `--project`、可重复的
`--allow PATTERN` 和 `--validate "…"`（已调查的项目检查命令），授权限定范围的结果
adapter 或入口衔接。验证命令必须不同于最后提供的正式测量 `--command`；验证输出
不是科学测量。CodeTask 仅在隔离 worktree/copy 的允许范围内修改，保护测试、秘密文件
和已登记的项目内数据；验证通过后才在该项目中运行正式命令。若给 `--cwd`，须与
`--project` 相同。可使用 current Python 或已有 `--project-python` 解释器；选择
`--environment venv` 时，可在同一隔离工作区先安装明确授权的依赖/项目，再由同一解释器
验证和测量，而非在基础解释器中测量；不能添加额外 `--check-argv`。这不授权改变方法、划分或评价
条件，工作区隔离也不是 OS 沙箱；不代表任意论文自主准备或真实论文复现已验收。

对话可根据已读论文或项目材料的引文提议取得公开项目/数据/补充材料，每项须独立确认。
项目支持直接 ZIP 或 GitHub 仓库默认分支快照，不是固定 commit；数据保留单个下载文件，
不自动展开。最多3项资产，默认每项下载20 MiB，项目 ZIP 展开最多80 MiB、5000项。
对话启动前可用 `--asset-max-mb N` 显式选择项目/数据下载容量，ZIP展开上限为 `4*N` MiB；
确认时展示容量，获取记录保存实际采用的上限，恢复不重新下载。ZIP格式按实际归档检查，
不要求URL以`.zip`结尾。这个选项本身不授权下载、
安装或执行；补充文档仍使用独立的文档容量限制。
仅接受公开 HTTPS URL，保留论文标识、版本及下载选择等查询参数；拒绝携带凭据或签名的链接、URL 用户信息与片段。取得不授权执行或安装依赖，也不是安全沙箱或完整防 SSRF。
补充材料可为官方说明页或论文，复用文档预览和按问题补读；单次公开GET不跟随重定向，
PDF许可独立确认，同时计入资产/文档限额，保留出处与下载账本，不作为执行命令授权。
取得路径已实现，但完整真实复现验收仍未通过。

论文链接可用 `start --kind reproduction --chat --document https://… --goal "…"`。
先确认取得，再向模型提供带来源的摘录；PDF下载/原文件保留另需许可，或显式 `--fulltext`。
初次预览最多8000字符、PDF前3页，不代表读懂全文。需要后文方法或说明时，对话可对已提供的
文档或材料提出按问题只读补查。仅有论文名称或原页面不可用时，对话可提出公开资料检索，
不要求用户先找齐链接。确认展示的查询和提供方后，已配置`TAVILY_API_KEY`时使用Tavily与
OpenAlex，否则使用arXiv与OpenAlex。准备阶段最多两轮、每轮两条查询，每提供方每查询最多
五条结果。保存元数据和提供方失败；元数据不是已取得的论文或已核实的作者身份。恢复复用
已保存响应，中断请求不自动重发；下载和执行仍分别确认。
普通JSON/BibTeX笔记和结果应以`--material`提供，而非
`--document`；对话保留材料角色，不将声明当作已验证结果。复现准备可将这些说明保留为任务资产，
据此提出续接方案；这不把旧观察提升为新测量，数值复用仍须核查实际原始数据与条件。
扩大本地解析须确认，最多40页PDF、每文件20 MiB，只发送
有界命中片段。恢复复用保存的文档包与取得记录，不重复解析或取得；
恢复时可明确跳过失败论文资料，使用其他材料继续；失败收据和未确认消费保持保存，
失败资料不成为证据。新增结果适配代码可基于源码定义的调用，不强制要求README说明，
但静态依据不代表运行已验证。
普通 `--prepare-only` 不下载链接。仓库与执行建议仍须已读原文依据和分别确认。
公开资源提案可选择已读来源路径，由代码定位其中的字面URL；不凭模型推测的地址下载。
PDF解析保留页内HTTP链接目标。声明的锁文件可按需有界读取，默认不向模型发送其生成内容；
静态依赖记录不证明环境已安装或实验可运行。作者Python要求排除当前解释器时，
通过 `--project-python` 或确认具名Python资源绑定兼容解释器；文字假设不改变执行环境。
尚有选择时可逐项回答，或输入 `accept-proposal` 明确采用所展示设置与假设；未回答事实仍未知，
执行仍另行确认。已耗尽模型额度的设置可零调用复核已有有效方案，不清零账本。
复现设置中的 `--fulltext` 是资料获取许可，生成任务使用已取得／本地文件，不再开启一次检索。

### 已有数据分析（无需 API）

确定性快捷方式用 `start --kind data_analysis --data-file PATH --value-column NAME`；需要正文再加 `--with-report --model env`。未选列且无其他协议控制时，数据偏好走原生调查。表格参数与 `research-session` 共用：

| 参数组 | 用途 |
| --- | --- |
| `--data-file` | 输入表格 |
| `--group-column`、`--value-column` | 分组和可重复值列 |
| `--observation-unit`、`--value-unit`、`--data-attribution` | 声明语义 |
| `--data-mode observations\|values` | 默认聚合逐行观测，或保留已有数值 |
| `--data-plot bar\|box\|heatmap\|line\|scatter` | 图形类型，默认 `bar` |
| `--data-missing reject\|omit` | 默认拒绝缺失值，或明确选择遗漏策略 |
| `--paired-baseline COLUMN` | 与指定基线列计算同一行的配对差值 |
| `--x-column`、`--x-unit`、`--data-association none\|pearson` | 数值坐标与可选描述性相关，默认 `none` |
| `--series-layout separate\|shared`、`--figure-width column\|wide` | 坐标轴默认 `separate`，图宽默认 `wide` |
| `--data-max-mb`、`--data-max-points`、`--data-max-figures` | 输入/图形上限，默认 `20` MiB、`10000` 点和 `100` 图 |

附加报告的上下文在 `start` 中使用 `--material`、`--document`，
在 `research-session` 中使用 `--material`、`--local-document`；不改变数值变换。
单独分析忽略模型设置。选项组合要求见[数据字段](CONFIG_REFERENCE_zh.md#已有数据的描述分析)。

## 项目准备

```bash
uv run simple-ar project-info --project ./project --data-path ./data \
  --output runs/project-preparation-NEW
```

保存 `preparation.json` 和 `preparation.md`，含入口候选、依赖声明、摘录和指定数据位置。不执行、安装或下载项目代码/数据；静态依赖观察针对检查解释器，不代表目标环境已解析完成。

使用未占用目录；相对数据路径从项目根解析。说明可复用于写作或复现设置。

## 命令总览

| 命令 | 用途 |
| --- | --- |
| `simple-ar start` | 自然语言目标进入原生行动循环；新建图形/脚本/普通代码共享同一所有者，明确的结构化协议保留兼容。 |
| `simple-ar research-session` | 有界任务驱动 session 的正式入口；accepted plan 选择适用的研究与执行步骤。 |
| `simple-ar research-session-continue` | 重试 canonical 显式实验中的技术失败。 |
| `simple-ar research-session-migrate` | 从只读的 `session_manifest.v1` 创建 canonical 后继 session。 |
| `simple-ar research-report` | 从已完成的 research session 生成并审查报告。 |
| `simple-ar report-export` | 将已组装报告转换为新的可编辑 ACM 工程，可选本地 TeX 编译。 |
| `simple-ar research-brief` | 分段/开发接口：从主题或本地文献构建有证据支持的 research brief。 |
| `simple-ar status` | 查看 research run 或 code-task run 状态。 |
| `simple-ar tools ...` | 导出 tool schema、调用 run-local tool，或通过 MCP stdio 暴露只读 tools。 |
| `simple-ar inspect` | 为某次 run 构建本地 artifact index。 |
| `simple-ar search-artifacts` | 搜索已经索引的 run artifacts。 |
| `simple-ar clean` | 预览并清理某次 run 的可重建缓存。 |
| `simple-ar code-task ...` | 在隔离可编辑 workspace 中处理已有代码项目。 |

## Research 命令

### 报告控制参数

两个报告入口使用同一套设置，选项前缀不同：

| `research-session` | `research-report` | 用途 |
| --- | --- | --- |
| `--report-document-review` / `--no-report-document-review` | `--document-review` / `--no-document-review` | 开启或关闭整稿审阅 |
| `--report-review-scope section\|document` | `--review-scope section\|document` | 逐节审阅或正文完成后整稿审阅 |
| `--report-draft-scope section\|document\|auto` | `--draft-scope section\|document\|auto` | 分节、联合，或仅在完整定长计划不超过 2,000 词时联合起草 |
| `--report-outline-strategy auto\|template\|adaptive` | `--outline-strategy auto\|template\|adaptive` | 模板或证据驱动的章节规划 |
| `--report-data-tables linked\|full` | `--data-tables linked\|full` | 链接数据记录，或同时附摘要表 |
| `--max-document-review-prompt-chars N` | `--max-document-review-prompt-chars N` | 可选的完整审阅请求字符上限 |

`auto` 写作启用图形且存在已登记的资料图时，通过证据规划选择正文图及章节归属；
纯文字报告保留直接模板路径。显式 `template` 不改变用户选定的模板结构。

省略选项沿用配置或已保存设置。基础默认是分节起草/审阅、关闭整稿审阅、`auto` 规划、
`linked` 数据表及不额外限制完整审阅请求（`0`）；引导入口可能保存不同选择。
联合起草要求开启整稿审阅、选择整稿审阅范围和完整来源策略；依赖关系及持久化修订上限
见[配置参考](CONFIG_REFERENCE_zh.md)。

### 固定协议复现

固定协议论文结论检查使用 `research-session --config examples/conformal_reproduction/research.toml`，
先按案例 README 准备论文。`--task-kind reproduction` 要求本地材料和显式固定协议，
不代表任意论文的自动环境探索/代码实现。

### `simple-ar report-export`

```bash
uv run simple-ar report-export --report-dir 报告attempt目录 --output runs/acm-draft --compile
```

输入目录须包含保留引用键的 `report_body.md`；有引用时还需 `references.bib`。
Pandoc 转换已有正文，不调用模型或重跑实验。目录内的图复制到工程，SVG 转换需
`rsvg-convert`；不抓取远程图片。正文使用 PNG 时仍保留已登记的矢量导出。
可用 `--title` 指定面向读者的标题。
每次选新的输出目录，保留已有导出及人工编辑。

`source.md` 与 TeX 使用包内图路径，可搬迁整个目录；可编辑 SVG 与转换后的图一同保留。
指向外部本地证据的链接改为标签，原目标记录在 `export.json`；不会自动复制或重分发
数据集和来源产物。正文事实审计仍需单独查看。
代码块的字面内容保存在 `code/block-*.txt`，搬迁工程时需保留此目录。
TeX 使用 `fvextra` 换行代码、`seqsplit` 换行长标识符，不改规范正文、不执行代码内容。
`etoolbox` 保护标题/图注中的库调用，PDF 元数据保留字面文字。

输出为单栏 `acmart` 演示工程，含 `main.tex`、`body.tex`、文献、图和 `export.json`。
若已有图表清单，导出将登记 caption 绑定到对应图，仅移除紧邻且完全相同的重复图注段；
没有清单则保留输入图注，不自行编造内容。
生成的图优先就近放置（`htbp`），仍允许正常 LaTeX 浮动，不强制原地；编译通过不等于视觉验收。
它不替用户选择具体会议投稿选项，也不认证科研结论。需安装 Pandoc；加 `--compile`
还需 `pdflatex`、`bibtex`、acmart 依赖、`fvextra`、`seqsplit`、`etoolbox` 和可缩放的回退字体；Debian/Ubuntu 的
`cm-super-minimal` 只有 10pt 字体，应安装完整 `cm-super`。缺编译器或编译失败时返回非零状态，保留
源码和诊断；实际编译写入 `build.log`。当前使用 pdfLaTeX，中文等非拉丁文字需自行
调整可编辑工程的引擎和字体。


### `simple-ar research-brief`（分段/开发接口）

**一句话说明**：从主题或本地 Markdown/TXT 文献构建一个有证据支持的 research brief。

**语法用法**：

```bash
uv run simple-ar research-brief \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --output-root runs/research-brief
```

命令会创建带时间戳的 session，并把 plan、search、document ingest、read 和 synthesize
分别记录在独立 attempt 中。它不会静默 retry 或覆盖 attempt；`--query`、`--provider`、
`--max-results`、`--max-chunks` 和 `--idea-limit` 是这条路径保留的少量控制项。
后续 session 可以传入同一个可选的 `--cache-dir` 来复用已下载的全文；省略时缓存仍保留在当前 session 内。

省略 `--model` 时，这条入口使用可复现的 deterministic planning 和 evidence derivation；
如果希望真实调用共享 LLM transport 完成研究问题/查询规划和证据综合，可以显式传入模型：

```bash
uv run simple-ar research-brief \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --model "$SIMPLE_AR_MODEL"
```

LLM 模式仍使用正常的 `.env` provider 配置。缺少 key、模型请求失败或返回无效结果时，
对应 attempt 会明确失败，不会偷偷改用 deterministic 正文。

### 已退出的 `simple-ar research-experiment`

独立 design→experiment→analysis 创建器已删除；研究任务使用 `research-session`。
旧 `--synthesis-file` 参数不作隐式兼容。领域能力仍可在库级组合，历史产物保持可读。

### `simple-ar research-session`（任务驱动的 canonical 入口）

新报告默认复用整篇 Writer，继续沿规范装配、引用和确定性审计交付。
`--report-writing-method sections` 保留受控分节规划与审阅；未指定方法时，
显式分节/审阅参数自动选择它。明确选择 `article` 时，不兼容参数被拒绝而非忽略。
一次整篇起草不代表独立模型审阅，历史报告沿保存的 Writer/检查点所有者恢复。

LLM 任务计划校验失败时最多自动纠正一次，不静默改用固定计划。
返回提案与校验错误保存在该次 `attempts/plan-*/task_plan_proposals.json`；仍无有效计划时暂停，不执行后续动作。

可加 `--session-root PATH` 继续已保存的 accepted plan。未提供的新目标/outputs 保持不变；
显式提供新目标、outputs、本地文献或执行配置会修订当前 session。attempt 历史保留；只有
命令、结果 schema、协议、准备 lineage 和保护资产仍匹配的测量才复用，其余依赖步骤重新规划。
新增文献本身不会重跑有效实验；task kind 改变仍须新建 session。详见配置参考的续接说明。

进程被杀且还没保存结果时，确认旧进程已停止，再用
`--session-root PATH --recover-interrupted --model env` 恢复。旧 attempt 计入原预算，
保留此前证据和有效章节检查点，重试当前步骤；配置/输入修改另一次续跑处理。
已有失败结果的普通 API 故障使用常规续跑即可。
整稿检查未能完成时暂停写作并保留正文及修订候选；处理额度或传输问题后续跑，
写作内容未变则复用已保存工作，来源或测量输入变化仍会使其失效。
恢复时未显式提供的报告选项沿用已存设置；对已请求报告的 session，显式报告改动只重建报告产物。
尚未请求报告的前缀请使用 `research-report` 添加交付物。与已存 session 不匹配、
且不能安全修订的搜索/摄取设置会在执行前拒绝；要应用这些设置请新建 session。
`[research].max_iterations` 限制有界后续轮次；`0` 表示首轮分析后停止。

#### 参与策略与待处理决定

新 CLI 会话默认使用 `checkpoints`；用 `--interaction assisted|checkpoints|autonomous`
选择策略，也可在 TOML 的 `[research] interaction = "checkpoints"` 设置。
旧会话没有该字段时继续保持旧行为。三种模式都不会代替缺失的事实、资产或权限；这类条件仍须补充，
不能用 `accept` 绕过。

模式变更与 `--reanalyze` 或单独刷新报告须分次续接，不能组合后静默忽略模式。
交付决定的正式答复可以同时包含报告设置和模式变更。

- `assisted`：在首次执行协议、自动选择的分析报告等关键节点确认；也会询问补测或候选修订等科研选择。
- `checkpoints`：确认首次实验协议、实质方向修订和自动交付取舍；同协议内的有界补测仍按既有授权执行。
- `autonomous`：在有效选项与剩余轮次内自动继续；没有充分依据或前置条件时暂停并保留证据。

暂停时 Rich 展示决定 ID、问题、依据、选项和续接命令。正式答复仍走同一 `research-session`：

```bash
simple-ar research-session --session-root runs/research-session/<session> --topic "原研究目标" \
  --decision-id ID_FROM_RICH --decision-response accept

simple-ar research-session --session-root runs/research-session/<session> --topic "原研究目标" \
  --decision-id ID_FROM_RICH --decision-response revise \
  --decision-guidance "补充事实或修订后的研究方向"
```

`--decision-response` 可取 `accept`、`reject` 或 `revise`。修改交付选择时，`revise` 必须同时提供报告配置，
例如 `--report-template analysis_report`。TOML 可在 `[continuation]` 提供同名的 `decision_id`、
`decision_response`、`decision_guidance`。答复产物记录 brief、执行或报告修订值；若答复已落盘但输入尚未保存时
进程中断，使用相同决定和修订重放会恢复原修订，不同修订值会被拒绝。输入持久化后的精确重放不会重复已完成的工作，
相关输入改变后旧决定不再适用。Rich 只读显示，不会等待终端 stdin。

额度续接仍使用 `research-session`。可通过 `[continuation]` TOML 或 CLI 提供新额度：
`--authorize-remaining DIMENSION=AMOUNT` 可重复，表示授权后后续调用的新剩余额度，不会估算或
消除未知历史用量；`--additional-attempts` 与 `--additional-no-progress` 只增加持久化上限，不清零
计数。所有授权都需要稳定的 `--authorization-id` 和 `--authorization-reason`；相同 ID/条款重放
幂等，条款变化须使用新 ID。不得直接编辑账本或 manifest。

已完成会话若只追加额度而不修改输入，将保持 `completed`，不执行任何研究或报告动作。
刷新报告时可先用上述入口授权，再运行 `research-report --refresh` 或原报告配置修订命令。
完整报告刷新通常需要 writer、组装、审计三个 attempt，失败重试另计；纯报告不要求增加进程预算。

```bash
simple-ar research-session --config research.toml --session-root runs/research-session/<session> \
  --local-document new-paper.md \
  --authorization-id review-20260924-1 --authorization-reason "审查后有界续接" \
  --additional-attempts 2 --additional-no-progress 1 \
  --authorize-remaining total_tokens=50000 --authorize-remaining llm_requests=8
```

在已暂停或完成的分析检查点，可单独使用 `--reanalyze`（不与额度授权或输入修订合并）：复用已有测量重新分析，
再按结果继续研究决策。历史产物和已消耗预算保留，不增加论文交付要求；
后续若接受候选修订，仍会消耗原预算执行新实验。不能用它覆盖尚待执行的科研后续计划。

结束时的产物表按 accepted plan 展示最新实现、候选测量和分析；早期修订引用仍单独保留，便于追溯候选 lineage。

可使用 `simple-ar research-session --config examples/survey/research.toml`。
显式 CLI 覆盖文件值；`--outputs summary report experiments` 按需选择交付。
预算覆盖项为 `--total-tokens`、`--llm-requests`、`--max-output-tokens`、
`--process-invocations`、`--process-wall-seconds`。见[配置参考](CONFIG_REFERENCE_zh.md)。

**一句话说明**：运行一个有界 session，由 accepted plan 选择适合任务与已有资产的步骤。
提供实验命令或 `--code-task-config` 时，计划可包含准备、实现、测量和分析；两者都省略时，
session 保持 literature-only，不会创建执行请求或启动进程。

模型可用时，同一个 application 会继续进入 `report -> report_audit`；`--no-report` 仅用于调试
或只检查前缀 handoff。之后调用 `research-report` 可以只补齐报告交付物，不重新检索、分析或
运行已有实验；`research-session-continue` 只用于明确的恢复或修订决定。

**语法用法**（`--command` 必须放在最后）：

```bash
uv run simple-ar research-session \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.75')"
```

如果希望在同一个 session 中使用已有的 Code-Task 实现后端，可以省略 `--command`，
改为提供 Code-Task TOML 和模型：

```bash
uv run simple-ar research-session \
  --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --code-task-config examples/code_task_medium_review/configs/code_task.toml \
  --model "$SIMPLE_AR_MODEL" \
  --output-root runs/research-session
```

TOML 仍然是 Code-Task 项目、benchmark、workspace、baseline 和执行设置的来源。
生成的 code-task 产物会保留在 session 的 preparation/implementation attempt 下，并规范化为
同一份 Analysis 消费的 canonical result；不会新增第二套代码生成器。

canonical application 会在隔离的 preparation workspace 中复用既有 Code-Task 实现和验证能力。
如果 proposal 需要 `large` budget，检查 proposal 后必须在 Code-Task TOML 中显式设置
`[execute].allow_large_edits = true`；否则 session 会保留产物，并在审批边界停止。

可选的 `--cache-dir` 会传给 document ingest。后续 session 会复用有效的全文缓存文件；默认值仍是
session-local，以保持旧命令兼容。

它复用分段接口的 attempt-local 产物，但这些接口不再构成第二条完整主线，并且主线不隐式
retry 或 repair。若还没有准备好的可执行实验，`research-brief` 只能作为提前准备 handoff
的开发工具；实验继续由正式研究会话管理。
使用 `--no-report` 创建的 session 之后可用窄的 `simple-ar research-report` 命令补齐报告；
它只重新打开 canonical application 的报告动作，复用已持久化的 synthesis、测量和 analysis。
如果希望一次显式调用完成前缀和报告，可以在最后的 `--command` 之前加入
`--model NAME --with-report`；`--report-reviewer`、`--max-review-iterations` 和
`--max-section-tokens` 会进入同一份 application 配置；最后一个参数设为 `0` 时不设置章节
单次调用的 provider 输出上限。
Writer 执行和检查点统一由正式应用调用 `report/writing.py`。历史报告输入投影只读保留，
原来的独立 report-session 执行 API 已退出。

省略 `--model` 时，planning、reading、synthesis、design 选择和 analysis 都保持
deterministic；传入 `--model NAME` 后，同一个共享 client 会用于 planning、有界 reading/
screening 与 paper notes、synthesis、在已有研究方向中进行选择以及结果分析。provider 失败会
保留为可见错误，不会静默转换成离线输出。

如果只想查看已经持久化的 capability session，而不重新运行任何阶段，可以继续使用已有的
status 命令：

```bash
uv run simple-ar status runs/research-session/<session>
```

当目录包含 canonical `session_manifest.json`（schema `session_manifest.v2`）时，status 会显示
application 状态、当前 attempt、有限预算、各类 attempt 计数和最后一次决策；不会读取或改写 capability
产物。旧的 `session_manifest.v1` 只作为只读兼容输入；旧 pipeline 和 Code-Task 目录继续使用原来的
status 行为。

如果开放的 session 没有 active attempt，status 还可能显示
`Handoff: ready_for_report` 或 `Continuation: explicit ...`。这只是持久化的下一步提示，
不表示后台仍有进程运行；下一步必须由调用方显式执行。

### `simple-ar research-session-continue`

**一句话说明**：在已有 session 中用调用方提供的修正命令，显式重试一次技术失败的实验。
对于 canonical v2，它复用文献、research design 和失败的父 attempt，不重新检索；成对实验、数据准备
和 CodeTask 失败由各自的有界边界处理。科学负结果是证据，不会被静默重跑；旧 v1 session 不再原地执行。

**用法**（`--command` 必须放在最后）：

```bash
uv run simple-ar research-session-continue \
  --session-root runs/research-session/<session> \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.90')"
```

canonical session 会创建动态命名的新 experiment attempt，并把失败的候选记录为父节点，随后重新执行确定性
analysis；原 attempt 和文献产物不会被覆盖。只允许重试 `failed`/`timed_out` 技术失败，默认沿用原 result
schema，metric 参数可显式补充或覆盖。若请求了报告，成功后会显示下一步 report action。
旧会话可显式迁移证据，但迁移不等于继续执行旧阶段计划。

### `simple-ar research-session-migrate`

**一句话说明**：从旧的 `session_manifest.v1` 创建新的 canonical session，
不改写旧目录。不会猜测历史预算剩余量，也不会把未知旧产物冒充为当前状态；只有显式指定且可读取的小型产物才会迁入。

**用法**：

```bash
uv run simple-ar research-session-migrate \
  --source-root runs/legacy/<session> \
  --destination-root runs/research-session/<successor> \
  --artifact search \
  --requested-output report
```

命令会输出新 session 路径、`parent_session`、迁移 artifact、导入/跳过数量，
以及 `Historical budget: unknown_not_imported`。目标目录必须为空，且不能位于旧 session 目录内。

### `simple-ar research-report`

**一句话说明**：继续一个分析结果已经可以进入报告阶段的 `research-session`。该命令复用现有的 Writer/Reviewer、装配器和审查实现，不会重新检索文献或重新运行实验。

```bash
uv run simple-ar research-report \
  --session-root runs/research-session/<session> \
  --model "$SIMPLE_AR_MODEL"
```

可用 `--max-section-tokens N` 显式设置 Writer/Reviewer 每个章节的输出上限；设为 `0`
表示不添加该单次调用上限，这也是报告运行时配置的默认值。会话级 token 预算仍然有效。

报告和审查会作为新的 attempt 写入原 session。canonical session 已有报告时再次调用是幂等读取，
不会重新运行 Writer。显式添加 `--refresh` 会复用已保留的 analysis，仅创建新的报告/审查 attempts，
保留历史稿件，不重复测量或分析；模型会话使用模型解释假设，数值和执行状态仍由确定性分析负责。
刷新仍使用会话剩余预算。只有需要明确做 writer-only 对照时才使用 `--reviewer disabled`；最终 audit
仍会运行。历史 session 可读取，但不再由第二套报告执行器续写；损坏的正式 session 会明确失败，
不会回退到另一条流程。
未提供的 template/reviewer/limit 选项沿用 session 已存设置；显式选项只更新报告配置并重建报告产物。
持久化 attempt/no-progress 上限耗尽时，请通过 `research-session` 做显式授权续接；报告路径不绕过上限。

### 已退出的分段 CodeTask 命令

research-code-task 及其独立 session 执行器已退出。研究任务使用
research-session --code-task-config，纯编码任务使用独立 code-task；历史分段结果仍可读取。


### `simple-ar status`

**一句话说明**：查看 research run 或 code-task run 的紧凑状态。

**语法用法**：

```bash
uv run simple-ar status runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | research run 或 code-task run 目录。 |

**生成产物**：

- 无文件写入；只打印状态

**注意**：

对 code-task run，会显示环境、计划、补丁、验证、benchmark、指标对比和 repair 状态。

## Tool 与 MCP

### `simple-ar tools schema`

**一句话说明**：导出真实已注册 tool 的 MCP 或 OpenAI function-tool schema。

**语法用法**：

```bash
uv run simple-ar tools schema --format mcp
uv run simple-ar tools schema --format openai --output tool_schema.json
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `--format` | enum | `mcp` 或 `openai`，默认 `mcp`。 |
| `--output PATH` | path | 可选输出文件；省略时打印到 stdout。 |

### `simple-ar tools call`

**一句话说明**：调用一个 run-local 只读 tool，并写入紧凑 trace。

**语法用法**：

```bash
uv run simple-ar tools call runs/<run-id> list_experiment_artifacts
uv run simple-ar tools call runs/<run-id> search_generated_code --args-json '{"query":"run_experiment","max_matches":10}'
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | 已有 run 目录。 |
| `TOOL_NAME` | string | 已注册 tool 名称。 |
| `--args-json JSON` | object | JSON object 形式的 tool 参数。 |
| `--args-file PATH` | path | 从 JSON 文件读取 tool 参数。PowerShell 等 shell 中 inline JSON 不好转义时建议使用。 |
| `--debug-payloads` | flag | 保留更大的 trace payload；默认 trace 保持紧凑。 |

**生成产物**：

- stdout 上的 tool result JSON；
- `RUN_DIR/tools/tool_trace.jsonl`。

### `simple-ar tools serve-mcp`

**一句话说明**：通过 MCP stdio 暴露 run-local 只读 tools。

**语法用法**：

```bash
uv run simple-ar tools serve-mcp runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | tools 可检查的已有 run 目录。 |
| `--debug-payloads` | flag | 保留更大的 trace payload。 |

**注意**：

- 当前 server methods：`initialize`、`ping`、`tools/list`、`tools/call`；
- 默认只暴露真实注册的只读 experiment tools；
- 这个命令不会启用写文件、shell、network 或 dependency-install tool。

## Artifact Tools

### `simple-ar inspect`

**一句话说明**：索引并总结本地 run artifacts。

**语法用法**：

```bash
uv run simple-ar inspect runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | 要索引的 run 目录。 |

**生成产物**：

- `artifact_index.json`

**注意**：

只读取元数据和有界预览，不等于全文阅读；不再默认计算所有文件的哈希。
不追踪文件符号链接，也不打开命名管道等非普通文件。
`sha256` 默认为 null，Python 调用方可显式使用 `build_artifact_index(..., hash_files=True)`。
实验资产指纹与保护逻辑不变。

### `simple-ar search-artifacts`

**一句话说明**：使用 lexical retrieval 搜索已经索引的 run artifacts。

**语法用法**：

```bash
uv run simple-ar search-artifacts runs/<run-id> "accuracy" --top-k 5
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | run 目录。 |
| `QUERY` | string | 搜索 query。 |
| `--top-k N` | int | 返回结果数量，默认 `8`。 |
| `--include-operational` | flag | 同时搜索 manifest、runner metadata 等运行管理文件。 |

**生成产物**：

- 打印匹配片段和来源路径，并写入 `artifact_index.json`、`artifact_chunks.jsonl`、`artifact_search_results.json`。

**注意**：

搜索会自行建立当前索引，不必先运行 `inspect`。大于 8 MiB 的文件不参与全文分块，
在 `skipped_files` 和 CLI 中明确显示 `file_too_large`，不把文件前缀当完整来源。
二进制文件不分块。此限制只针对产物搜索，不改变表格分析或论文读取的限制；
需要搜索大日志时先缩小范围或导出所需日志。

### `simple-ar clean`

**一句话说明**：预览并清理某个 run 的可重建缓存，同时保留报告、manifest、paper metadata、read 阶段 Paper Brief、coverage report 和 `research_index/chunks.jsonl` 等审计产物。

**语法用法**：
```bash
uv run simple-ar clean runs/<run-id>
uv run simple-ar clean runs/<run-id> --yes
uv run simple-ar clean runs/<run-id> --all-caches
uv run simple-ar clean --shared-index
uv run simple-ar clean --shared-cache
```

**参数表**：
| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | 要清理的 run 目录。使用 `--shared-index` 或 `--shared-cache` 时可省略。 |
| `--yes` | flag | 跳过交互式 `yes` 确认，直接删除预览中列出的目标。 |
| `--all-caches` | flag | 在更强警告后，删除该 run 下所有已知可重建缓存、索引和 context artifacts。 |
| `--shared-index` | flag | 强清理：清空跨 run/test 共享的 research index store。 |
| `--shared-cache` | flag | 最强共享清理：同时清空共享 research index、literature provider cache 和外部 agent handoff archives。 |
| `--index-root PATH` | path | `--shared-index` / `--shared-cache` 使用的共享索引根目录；默认 `SIMPLE_AR_RESEARCH_INDEX_ROOT` 或 `.simple_ar_cache/research_index`。 |
| `--literature-cache-root PATH` | path | `--shared-cache` 使用的 literature cache 根目录；默认 `.simple_ar_cache/literature`。 |
| `--allow-external-index-root` | flag | 允许 shared cleanup 清理当前 workspace 外的路径。 |

**生成产物**：
- 先打印 Rich tree 预览：红色为将删除的缓存，绿色为会保留的审计产物。
- 当前会话按 `session_manifest.json`（schema `session_manifest.v2`）识别布局，清理 `cache/literature/` 中识别出的查询元数据 JSON 和 `artifact_search_results.json`；下载原文仍可能被保存的阅读直接引用，因此保留。
- 历史布局清理已知 `02-search/documents/fulltext_cache/`、`02-search/documents/extracted_text/` 等缓存。
- 如果 `index_meta.json` 指向当前 workspace 下的共享 SQLite research index，会删除该 run 对应的 SQLite rows。
- 使用 `--all-caches` 时，当前会话另清 artifact index/chunks；历史布局另清已知 research index、code-task repo map、locate outputs 和 context packs。

**注意**：
`clean` 不删除 run 目录本身。当前会话的 `attempts`、`outputs`、`documents`、下载原文及
未识别缓存继续保留，不追踪自定义外部缓存路径。
超过 1 MiB 检查边界的查询元数据会保留并列为未检查，不凭文件名删除。
历史布局保留报告、manifest、papers、解析审计、Paper Brief 和 synthesis brief；
其 `--all-caches` 会移除 portable 索引 chunks，
但仍保留最终报告、metadata、manifest 和 benchmark outputs。

`--shared-index` 比 `--all-caches` 更强：它会清空跨 run 共享的 SQLite/LanceDB 加速索引，后续运行需要重新构建索引状态。它不触碰 run 目录，因此 run-local 审计产物仍会保留。

`--shared-cache` 更强：它会同时清空共享 research index、`.simple_ar_cache/literature` 和 `.simple_ar_cache/agent_handoff_archives`。后续运行可能需要重新请求 literature provider、重新构建本地索引，并且不再保留旧的外部 agent handoff transcripts。

## Code Task Commands

Code-task 命令会把代码任务准备到 `runs/<run-id>/code_task/workspace`。已有项目默认 `auto`：优先创建 detached git worktree，Git 条件不满足时降级为受保护 copy；显式 `git_worktree` 失败时会给出可操作 checklist，而不是静默降级。greenfield 任务会从 empty workspace 开始生成项目。后续修改或生成只发生在隔离 workspace 的 project root 中，不会直接修改原始项目。

正常用户优先看“高级编排命令”。“底层原语命令”通常由 `execute` 自动调用，主要用于调试、学习或细粒度人工介入。

### 高级编排命令

已有项目的普通 `execute` 只使用一份 patch plan。通过 `--to-step work-plan` / `--to-step batch`
或已有 work plan 才进入分批路径，交互模式也相同；它们不是每次修改的必经阶段。

#### `simple-ar code-task init`

**一句话说明**：创建 code-task run，准备可编辑 workspace，并构建初始代码索引。

**语法用法**：

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task init --code-root path/to/project --task-file task.md --benchmark-command "python main.py"
uv run simple-ar code-task init --kind greenfield --task-file task.md --benchmark-command "python generated_project/main.py"
```

**参数表**：

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
| `--workspace-mode MODE` | enum | `auto`、`copy`、`git_worktree`、`sparse_copy` 或 `empty`。`greenfield` 默认 `empty`，已有项目默认 `auto`。 |
| `--workspace-include GLOB` | repeatable | `sparse_copy` include pattern。 |
| `--workspace-exclude GLOB` | repeatable | `sparse_copy` 额外 exclude pattern。 |
| `--workspace-reuse-source-venv` | flag | 检测并复用 source `.venv` Python。 |
| `--max-file-bytes N` | int | copy/sparse 模式最大复制文件大小，`0` 表示禁用。 |

**生成产物**：

- `code_task/manifest.json`
- `code_task/task.md`
- `code_task/workspace/`
- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

**注意**：

可复用设置建议写入 TOML，见 [配置参考](CONFIG_REFERENCE_zh.md#独立-code-task-config)。

#### `simple-ar code-task execute`

**一句话说明**：根据当前 run 产物推进到下一个安全停止点。

**语法用法**：

```bash
uv run simple-ar code-task execute runs/<run-id> --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute runs/<run-id> --to-step propose-edits
uv run simple-ar code-task execute runs/<run-id> --apply-proposed-edits --timeout 60
```

**参数表**：

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

**生成产物**：

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

**注意**：

`execute` 会保留审核点，但不强制每个审核门都另开一条命令。真实终端里，它会对
`patch_plan.md`、`proposed_edits.json` 或 large-edit approval 打印黄色 Rich 审核面板，
并询问是否继续；非交互 shell 中会干净停在审核门，除非显式传入 `--yes`。中断后重跑时，
已完成步骤会显示为 skipped。只有调试 primitive step 时才建议使用 `--interactive`，
并可搭配 `--yes` 自动继续这些 primitive prompts。普通 execute 模式下的 `--yes`
会自动批准审核门，只应在你明确想自动审批 plan/proposal 时使用。使用
`--no-review-inline` 可恢复“停住、下次再跑”的旧行为。完整运行流程见
[使用与配置](USAGE_zh.md#推荐路径toml--execute)。

如果 LLM work-plan 或 patch-plan 返回了无法解析的 JSON，`execute` 会停在
`llm_planning_failed`，并且不会写入 offline fallback plan。此时直接重跑同一条
`execute` 命令即可重新尝试模型调用；如果你明确接受 deterministic plan，再使用
`--no-llm` 或 `--allow-planning-fallback`。

补丁应用后，`execute` 会先运行结构化 reviewer，再进入静态验证；patched
benchmark 完成后还会再运行一次 post-run reviewer。阻塞性发现会写入
`code_task/memory/`，后续 repair 可以直接利用这些失败证据。

传入 `--config PATH` 时，`execute` 也会读取 standalone code-task 的
`[implementation]` 和 `[resource]`。Greenfield 任务正是通过这里选择本地
backend 或显式的 Codex / Claude Code / OpenCode handoff，而不是新增一套
provider-specific CLI 参数。

对于 greenfield 任务，`execute` 还会在 implementation planning 前写出
`code_task/meta/dependency_advice.json` 和 `.md`。JSON 会记录当前 Python 环境的
完整 installed distributions snapshot；终端输出和模型上下文只使用紧凑的任务相关子集。
这只是建议，不会自动安装依赖。

Greenfield 默认使用完整架构工作单元 `compact`。`tool_agent` 为弃用别名，
原四阶段规划及强制 planning review 已退役。`--planning-review-rounds` 弃用：
非零值显示说明，不授予额外调用；请设为 `0`。完整响应纠正、显式 fallback
仍沿既有预算和未知用量保护执行。
新尝试保存在 `code_task/meta/planning/architecture_unit/`。历史未生成任务进入
替代方法前，`planning/method_migration.json` 记录原方法（或 unknown）、planner
选择及保留产物摘要；旧阶段和失败原件保留，原可变计划另存至
`planning/retired_method/`。回执不认证旧流程成功，也不重置账本。
已生成项目跳过生成，不可变 snapshot 按保存内容导入、不重规划。
命名 planner 继续使用 `[models.code_task] planner = "profile:NAME"`；
退役阶段选择字段会明确拒绝，请移除并选择一个既有 planner profile。
后续 Writer、生成代码审阅及执行共用原实现。

明确离线生成只创建开发模板，不代表实验已实现。未实现的 runner 会报错停止，
不输出测量值；正式 benchmark 前应完成实现和验证，不能用预设分数满足结果 schema。
既有生成项目及其保存的执行记录不会被改写。

生成项目审查检查结构及脚手架风险，不证明任务验收通过。自然语言中的命令名、
输出路径和库偏好由上下文审阅处理，不再通过固定短语猜测并阻断。模型意见仍是
建议，实际验证和 benchmark 证据独立判断。审查契约变化时，续接先将旧报告保留在
`code_task/meta/review_history/`，再刷新当前报告。

有限的 greenfield 审阅/运行修复从实际诊断选择关联文件，复用代码所有者生成独立候选。
应用保留 API、静态检查和快照回滚，继续 review/validation 前同步
`code_task/meta/code_artifacts.json`。复用完成记录先核输入和项目字节；旧无绑定结果只作
历史记录，显式新轮次保留它并可继续工作。中断候选不盲目重放；问题仍阻塞时，执行
停住并保留文件、诊断和快照。

#### `simple-ar code-task decide-plan`

**一句话说明**：记录当前 patch plan 的人工审核决定。

**语法用法**：

```bash
uv run simple-ar code-task decide-plan runs/<run-id> --decision approve
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--decision VALUE` | enum | `approve`、`reject` 或 `revise`，必填。 |
| `--note TEXT` | string | 可选审核备注。 |
| `--reviewer TEXT` | string | 审核人标签，默认 `user`。 |

**生成产物**：

- 更新 `manifest.json` 中的 plan decision 状态

**注意**：

当 patch plan 不应继续生成 proposal 时，使用 `reject` 或 `revise`。

### 底层原语命令

以下命令通常由 `execute` 自动调用。需要手动控制或排查某一步时再直接运行。

#### `simple-ar code-task map`

**一句话说明**：从 editable workspace 重建 repo-map 产物。

**语法用法**：

```bash
uv run simple-ar code-task map runs/<run-id> --show-summary
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--no-refresh-index` | flag | 复用已有 `codebase_index.json`。 |
| `--show-summary` | flag | 打印 `repo_map_summary.md`。 |

**生成产物**：

- `code_task/meta/codebase_index.json`
- `code_task/meta/repo_map.json`
- `code_task/meta/repo_map_summary.md`

**注意**：

确定性命令，不调用 LLM。

#### `simple-ar code-task locate`

**一句话说明**：从 repo map 中排序可能相关的可编辑文件和只读证据文件。

**语法用法**：

```bash
uv run simple-ar code-task locate runs/<run-id> --query "improve classifier"
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--query TEXT` | string | locate query，默认使用 `code_task/task.md`。 |
| `--top-k N` | int | 每组候选数量，默认 `8`。 |
| `--refresh-map` | flag | 排序前重建 index 和 repo map。 |
| `--no-read-only` | flag | 省略受保护的只读证据文件。 |
| `--show-summary` | flag | 打印 `locate_results.md`。 |

**生成产物**：

- `code_task/meta/locate_results.json`
- `code_task/meta/locate_results.md`

**注意**：

tests 和 benchmark 可作为计划证据，但默认不可编辑。

#### `simple-ar code-task context`

**一句话说明**：构建受限的 prompt-ready context pack。

**语法用法**：

```bash
uv run simple-ar code-task context runs/<run-id> --max-files 8 --max-total-chars 20000
```

**参数表**：

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

**生成产物**：

- `code_task/context_packs/context-NNN/context_pack.json`
- `code_task/context_packs/context-NNN/prompt_context.md`
- `code_task/context_packs/context-NNN/selected_snippets.jsonl`

**注意**：

不调用 LLM，也不修改文件。

#### `simple-ar code-task probe`

**一句话说明**：检查 workspace runtime 和项目环境信号。

**语法用法**：

```bash
uv run simple-ar code-task probe runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

**生成产物**：

- `code_task/meta/environment_report.json`

**注意**：

`probe` 不安装依赖，也不运行项目 benchmark。

#### `simple-ar code-task baseline`

**一句话说明**：patch 前运行记录的 benchmark。

**语法用法**：

```bash
uv run simple-ar code-task baseline runs/<run-id> --timeout 60
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--command TEXT` | string | 覆盖本次 benchmark command。 |
| `--timeout N` | int | benchmark timeout。 |
| `--skip-validation` | flag | 静态验证未通过时仍运行。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

**生成产物**：

- `code_task/run/baseline/execution_report.json`
- `code_task/run/baseline/stdout.txt`
- `code_task/run/baseline/stderr.txt`
- `code_task/run/baseline/metrics.json`

**注意**：

benchmark command 在 `code_task/workspace` 中运行。

#### `simple-ar code-task work-plan`

**一句话说明**：生成面向批次执行的 implementation work plan。

**语法用法**：

```bash
uv run simple-ar code-task work-plan runs/<run-id>
```

**参数表**：

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

**生成产物**：

- `code_task/work_plan.json`
- `code_task/work_plan.md`

**注意**：

work-plan 中的 target files 会进入后续 edit-scope 检查。

#### `simple-ar code-task batch`

**一句话说明**：为某个 work-plan item 创建 attempt/batch 状态目录。

**语法用法**：

```bash
uv run simple-ar code-task batch runs/<run-id> --work-item W1
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--work-item ID` | string | work-plan item id，例如 `W1`，必填。 |
| `--attempt-id ID` | string | 可选 attempt id，例如 `attempt-001`。 |
| `--force` | flag | 即使已有 batch，也创建新 batch。 |

**生成产物**：

- `code_task/attempts/attempt-NNN/attempt_state.json`
- `code_task/attempts/attempt-NNN/batches/batch-NNN/batch_state.json`

**注意**：

`batch` 不调用 LLM，也不修改文件。

#### `simple-ar code-task plan`

**一句话说明**：为 active batch 生成可人工审核的 patch plan。

**语法用法**：

```bash
uv run simple-ar code-task plan runs/<run-id>
```

**参数表**：

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

**生成产物**：

- `code_task/patch_plan.md`

**注意**：

生成 edit proposal 前应先运行 `decide-plan`。

#### `simple-ar code-task propose-edits`

**一句话说明**：请求模型生成受控 old/new text edits。

**语法用法**：

```bash
uv run simple-ar code-task propose-edits runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 写入 deterministic empty proposal。 |
| `--force` | flag | 重新生成已有 proposal。 |
| `--max-files N` | int | 可编辑上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |
| `--allow-large-edits` | flag | 人工审核后接受较大但仍受限的 proposal。 |

**生成产物**：

- `code_task/meta/proposed_edits.json`
- `code_task/meta/proposal_warnings.json`
- 如果存在 active batch，也会写入 batch 级 proposal 产物

**注意**：

proposal 是结构化 JSON，不是 unified diff。

#### `simple-ar code-task apply-edits`

**一句话说明**：在 workspace 中安全应用受控 old/new edits。

`old` 为空仅用于创建不存在的文件，不能覆盖已有文件。模型提案的新文件须在显式
编辑范围或批次目标内；受保护路径仍只读，创建沿用替换的预算、快照和失败回滚。

**语法用法**：

```bash
uv run simple-ar code-task apply-edits runs/<run-id>
uv run simple-ar code-task apply-edits runs/<run-id> --edits-file runs/<run-id>/code_task/repairs/repair-001/proposed_edits.json
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--edits-file PATH` | path | 指定要应用的 proposal 文件。 |
| `--allow-unapproved-plan` | flag | 本地测试/demo 时绕过 plan approval。 |
| `--allow-large-edits` | flag | 应用已审核、需要 large-edit approval 的 proposal。 |

**生成产物**：

- 修改 `code_task/workspace` 下的文件
- `code_task/meta/applied_edits.json`
- `code_task/patch.diff`

**注意**：

写文件前会检查路径、edit scope、old text 匹配和 large-edit 限制。

#### `simple-ar code-task validate`

**一句话说明**：对 workspace 运行轻量静态验证。

**语法用法**：

```bash
uv run simple-ar code-task validate runs/<run-id> --strict
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--strict` | flag | 将较高风险 warning 视为 error。 |
| `--max-file-bytes N` | int | 扫描文件大小上限。 |

**生成产物**：

- `code_task/meta/validation_report.json`

**注意**：

静态验证是保守检查，不替代 benchmark。

#### `simple-ar code-task run`

**一句话说明**：patch 后运行 benchmark。

**语法用法**：

```bash
uv run simple-ar code-task run runs/<run-id> --timeout 60
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--command TEXT` | string | 覆盖记录的 benchmark command。 |
| `--timeout N` | int | benchmark timeout。 |
| `--skip-validation` | flag | 静态验证未通过时仍运行。 |
| `--env-mode MODE` | enum | `current` 或 `external`。 |
| `--python PATH` | path | external env mode 的 Python。 |

**生成产物**：

- `code_task/run/patched/execution_report.json`
- `code_task/run/patched/stdout.txt`
- `code_task/run/patched/stderr.txt`
- `code_task/run/patched/metrics.json`
- baseline metrics 存在时写入 `code_task/run/comparison.json`

**注意**：

指标比较会区分“benchmark 跑通”和“目标确实提升”。

#### `simple-ar code-task analyze-failure`

**一句话说明**：总结最近失败的 validation 或 benchmark。

**语法用法**：

```bash
uv run simple-ar code-task analyze-failure runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |

**生成产物**：

- `code_task/run/patched/failure_analysis.md` 或 `code_task/meta/failure_analysis.md`

**注意**：

确定性命令，不修改源文件。

#### `simple-ar code-task repair`

**一句话说明**：根据最近失败上下文提出受限 repair edits。

**语法用法**：

```bash
uv run simple-ar code-task repair runs/<run-id>
```

**参数表**：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `RUN_DIR` | path | code-task run 目录。 |
| `--model NAME` | string | 模型覆盖。 |
| `--no-llm` | flag | 写入 deterministic empty repair proposal。 |
| `--max-files N` | int | repair 上下文文件预算。 |
| `--max-source-chars-per-file N` | int | 单文件 source 上下文预算。 |

**生成产物**：

- `code_task/repairs/repair-NNN/proposed_edits.json`
- 更新 `code_task/summary.md`

**注意**：

repair proposal 不会自动应用。审核后使用 `apply-edits --edits-file ...`。
