# 工作流与产物

[English version](WORKFLOWS.md)

普通 `start` 将目标、只读材料和约束交给 `ToolApplication`，由同一行动循环经
`CommonToolGateway` 组合来源调查、代码/执行、分析、写作、视觉/图像与出版，复用现有
专业模块、`SessionController`、产物和预算账本。新建偏好表达成果要求，不强制选择内部
流水线，也不授予访问或执行权限。网络、视觉、进程提案及资源各有明确边界，进程仍需
精确命令批准；工作区隔离不等于操作系统沙箱。

精确表格、固定代码范围/验证及复现等结构化协议仍沿 TOML/`research-session` 与
`ResearchApplication` 执行；它们也服务公开新任务，不只读取历史。普通原生任务通过
`start --resume-task` 沿原记录接受反馈和恢复，结构化会话保留自己的恢复入口。恢复不把
旧任务重建成新默认，不把半截响应当成果，也不重放已完成副作用。以下 accepted plan、
分段 Writer 和 capability 流程说明适用于相应结构化消费者及已保存任务；模块复用不代表
所有组合、图形反馈、科学能力或可靠性交付已经通过验收。

新默认研究计划只在明确请求摘要（或摘要是默认交付）时生成独立摘要。
已有本地原文、明确仅用材料且只请求报告的调研/研究任务，可摄入后直接写作并查询
保存原文；接受的计划仍可选择阅读笔记与综合。明确请求摘要、评估、设计或实验时
保留相应证据依赖。减少中间处理不保证理解或论文质量；恢复沿保存计划，不重写旧任务。

接受计划既不检索也不执行证据阅读时，来源交接由既有确定性规划生成，不再为查询规划增加一次
模型调用；Writer 仍可读取相关原文。一篇论文可以报告多个方法、对照或消融，比较结论限于
实际测试条件，不能把来源数量当成方法数量，也不能据此补造不存在的对照。

材料写作的提纲补读与作者缺口补读是共享工具额度下的独立有界批次，前者不再关闭作者
后续查找遗漏段落的机会。检查点保存两者结果，历史任务不重放已消费批次。
写作、格式恢复与分节审阅保留全部已供给补读，由工具额度和会话 token 预算控制成本，
不再额外只取最后六项。审阅直接用
实际 JSON Pointer 引用供给字段；定位和归属不等于科学支持，引文及旧保存引用仍按原规则验证。
有界原文概览保留所选章节中排名最高的实际命中；章节开头只在余量允许时补入，
不替代检索到的结果。这些窗口仍是部分阅读，不代表已读全文。

### 修改对象与审查覆盖

纯 Python 局部补丁的补充上下文按已有索引的导入依赖与直接调用者选择，包含条件/函数内及
相对导入，而不是按全仓角色补入无关数据或文档。这是静态关联，不包含动态加载或完整传递调用图；
整体项目审查、非 Python 修改和没有导入观察的旧索引保持较广范围。确定性检查仍覆盖原项目。

现有项目审查在原分组/文件额度内优先选择修改文件，再补角色背景；额度无法覆盖
较大修改集时，metadata的`changed_files_outside_review_clusters`明确列出未入组文件。
选入不等于全文已审或补丁有效。审查和修复共用diff/失败行附近的当前源码窗口，
标明位置与部分覆盖；生成项目无diff时仍有分离的首尾窗口，不拼成假连续源码。
受保护源码只读，审查不增加修改授权、不替代行为验证，也不新增模型轮数。

模型疑虑保留为建议，即使多组措辞相似也不升级为阻断；重复意见不是独立失败证据。
范围越界、确定缺失接口及已记录验证失败仍阻断，执行只能使用已授权的命令。
确定的阻断错误不等待模型调用即保存诊断。批次限制筛选可改文件，但保留其已定位源码窗口；
只有未取得窗口的目标才补读文件开头。警告或静态通过不证明行为正确，恢复也不把旧失败审查重写为成功。

### 初始源码问题与新增问题

CodeTask准备不只是复制源码，还绑定任务与修改契约。目标、约束或修改范围修订时，
新任务重新准备，不复用旧冻结计划；旧产物保留，沿同一会话预算累计。
只有呈现偏好变化时不重新准备工作区。
任务快照保留声明的源码与执行设置；实际目录留在准备产物，执行时叠加使用。
修订失效和检查点确认比较任务声明，不用已覆盖为旧工作区的配置代替声明。

CodeTask初始化在原工作区清单中只保留语法失败文件的已有索引校验值，不建第二份
源码快照或全文件完整性系统。非严格静态验证把内容未变的初始失败保留为警告；
修改/新增错误与严格模式仍失败。刷新索引不重设初始证据，旧复制任务没有该证据时
保持保守，不能把当前源码回填成初始化基线；已有冻结Git基线仍可使用。

这不证明初始错误是故意构造的、不证明项目可运行，也不证明补丁有效。应阅读警告
并执行声明的行为检查；静态passed不是运行成功。

## 可选自然语言任务澄清

`start --chat` 位于精确结构化配置/会话入口之前。澄清草稿保存用户原始回复、方案、材料授权和
沿既有 BudgetLedger 记录的此次对话用量；材料预览有界，截断/未读明确标出。模型建议语义，
不授予执行权限。确认后同一序列化器校验并保存 TOML。`--resume-setup` 复用待确认方案和
账本；真正执行仍由原会话控制器恢复。结构化设置和描述分析本身无需 API，选择 chat 才为
澄清调用已配置模型。

复现沿同一适配器读取提供项目的说明/入口并有界补读索引内文本，建议带出处的命令与
协议，确认后交原配置/执行器；不把建议当运行事实，不让模型授权目录、时限或安装。
准备笔记与科学来源分开，不为对话新建执行运行时或后台安装器。
重复 `--data-path` 文件/目录沿同一 `assets.data` 进入准备观察和会话只读资产；
不当论文摄入、不增加数据注册表，不改写命令。直接测量保持输入原位置；CodeTask 准备
复制声明的项目内数据，沿原编辑范围保护副本。外部数据不搬迁，旧完成工作区不补填。

## 明确选择的任务依赖准备

确认的 `[execution.environment]` 虚拟环境或当前环境检查配置，在原测量前加入 `prepare_execution`。
原准备 attempt 保存环境、`environment_setup.json`、进程记录与日志；创建、所选依赖
及显式项目包安装和 `pip check` 均由原执行后端及会话账本计费；项目与依赖同次解析。
默认不安装项目，构建可能写源目录元数据。仍输出原 `prepared_execution.v1`
的 `execution.json`，只绑定命令的 Python 解释器，没有第二套环境运行时。
准备失败不启动科学命令；`status RUN` 查看，明确续跑失败任务则新建 attempt、不清零旧用量。
完成恢复复用已准备命令与结果。虚拟环境不是 OS 沙箱或可搬迁环境镜像，安装不认证科学条件。
这条单命令路径不改变 CodeTask 的 current/external 环境策略。
可选 `check_command` 在 venv 准备后执行，也可在 current 模式单独执行而不安装依赖。
它共用原后端、额度与日志；失败或超时停止测量。不缩短正式命令，检查输出留在准备记录中，
不进入测量结果。检查由用户明确提供，不从文件名推测“安全”的试跑命令。

## 已有数据分析与绘图（无需模型）

需要**同一会话分析、绘图并写报告**时，在下面命令加 `--with-report --model env`；
也可以在 `start --chat` 中明确要求报告并确认提议。分析本身仍不调用 API，
随后已有材料写作能力消费保存的结果与图形，执行审阅和审计；不添加联网调研或训练。
专家 TOML 使用 `outputs = ["data_analysis", "report"]` 并配置模型。恢复使用打印的
session 路径和原模型，不要重新 start 或另开写作任务。

新引导的数据报告联合起草各节，再整稿审阅与有界修订；已有任务配置保留已接受的范围。

请求报告时，可加 `--material README.md` 提供数据字典/笔记，用
`--document reference.pdf` 提供参考论文。两类只读本地来源沿已有文档包保存，
恢复后仍可供写作使用。目标放在 `--goal`，说明放在材料文件，原始表格放在
`--data-file`；说明不会覆盖已确认的分析设置，也不会变成实测证据。
仅分析不消费这些额外材料。图注说明实际绘制的量，详细来源和重建记录保留在链接的分析包中。

```bash
uv run simple-ar start --kind data_analysis --goal "描述测量结果" \
  --data-file ./observations.csv --value-column score --group-column method \
  --observation-unit "一次运行" --interaction autonomous --yes
```

明确选择列与每行含义，不猜 ID/指标。默认 `observations` 分组计算 count/mean/sample std；
保存任务前先按输入上限检查表格结构与列名；选错列会显示实际可用名称。
该预检不计算测量或认证数值，正式摄入仍检查数值并固化实际字节。
已有均值等汇总表的条形图用 `--data-mode values`，分组标签必须唯一，不再次平均、不自动造误差条。
缺失默认拒绝，`--data-missing omit` 才按列省略并报告数量；非有限值、非数值、重复列、空表、
不规则行、嵌套 JSON 都明确失败。输入支持 UTF-8 CSV/TSV、同构 JSON records。

沿同一 SessionController 先固化输入及设置，再生成 `analysis.json`、`analysis.md`、数据副本与
可编辑 SVG、矢量 PDF 与 300 dpi PNG 预览。Matplotlib 随包安装，不需要 GUI 或额外绘图设置；
三种格式仍算一张图。不调用 API、不训练、不伪造 experiment 状态。计算完成不证明采集、单位、独立性、
显著性或因果关系；分享前检查数据敏感性。物理限制默认 20 MiB 输入、100 页图；
`--data-max-mb`、`--data-max-figures` 可明确调整，超出报错而非截掉数据。
不同指标分轴，类别分页而非截掉。`--figure-width column|wide` 为通用 3.5/7 英寸，不保证会议版式。
数值与视觉检查分别记录，图的初始视觉状态是 `not_performed`。

同一行的匹配观察可指定 `--paired-baseline baseline`，同时选择基线与候选列。
用户需确认共同量/单位及每行配对含义；`omit` 在两列共同非缺失行上计算差值，
保存平均差、样本标准差与标准误，少于两对不造误差。附加差值图显示 ±1 标准误，
假定各对独立，不是置信区间或显著性检验。搬迁重建及写作导入会按数据副本复算。
已声明共同量的基线/候选柱图使用同一尺度；配对差值图单独标轴，避免分别自动缩放掩盖差异。
见[完整无需 API 案例](../examples/data-paired/README_zh.md)。

数值曲线/坐标对使用 `--data-mode values --data-plot line|scatter --x-column step`，
可选 `--group-column` 分开类别系列，不表示配对或聚合；多个 `--value-column` 默认分别绘图，`--x-unit` 记录横轴声明单位。
折线按组内数值 x 排序并要求组内 x 唯一，散点保留重复 x。不拟合、不平滑、不平均重复点、不造误差条。
折线 x 必须完整，明确 `--data-missing omit` 时保留缺失 y 并断线；散点保留缺失坐标行，但不绘制不可用位置。类别图例分页使用相同轴，不丢类别。
供写作消费的包还包含全部完整坐标对的组内计数与边际最小值、四分位数、中位数、最大值；
四分位数按 `(n−1)*p` 插值。这不是联合关系、显著性或原生图像阅读，也不替换原图点。
明确请求 `--data-association pearson` 可另算各组完整 x/y 对的描述性相关，而非配对差值
或自动混池。常量轴/不足两对明确未定义；两对非恒定值 r 必为 ±1，不推断显著性或因果。
数值附件、写作共同投影和搬迁重建沿同一复制输入携带此结果，不改变图点。
行级 `observations` 分析也保留每组/列全部非缺失行的五数分布与计数，贯通写作及数值附件。
它不把均值柱图变成箱线图、不冒充配对差值；旧包从复制输入重算，原包不重写。
`--data-max-points` 默认每张坐标图最多 10000 行，可明确调整，超出报错而非抽样。
仍使用同一输入固化、恢复及重建路径；可运行[完整坐标绘图案例](../examples/data-curves/README.md)。

恢复使用 `research-session --session-root PATH`，原文件更新/删除不替换已固化数据。
交付目录可以搬迁，在安装项目包的环境中执行 `python -m simple_ar.result_analysis.table analysis.json`
重建 SVG，并同步更新 `analysis.json` 的图注/编码及 `analysis.md`；普通已完成会话恢复不改
历史交付。计算记录不一致则拒绝重建，不宣称全量文件完整性认证。缺失策略与全量记录的实际
使用数量分开呈现，`reject` 不等于已删除行。密集散点以较小半透明点减少遮挡，保留全部完整
坐标及原记录顺序，不抽样、抖动或平滑。后续写作可将完成的
`analysis.json` 作为 `--material`，重新核对数值并附带可编辑图；保留其同目录的数据副本，
不要把原始表格 JSON 当作写作分析包。复杂统计仍不自动推断。

服务器数值验收：`python scripts/validate_table_nist.py --output-root runs/nist-NEW`。
对照两份 NIST StRD 官方认证数值，评价器另用 Decimal 独立计算；仅验证算术和 CLI 交付，
不算 Agent/论文复现/完整 NIST 成绩。下载失败时可将未修改的公开 `.dat` 放到该目录的 `inputs/` 后继续。

## 从已有材料直接写作

```bash
uv run simple-ar start --kind writing --goal "说明已有结果、条件和局限" \
  --material ./notes.md --interaction autonomous --yes
```

草稿、笔记、结果说明用可重复的 `--material`；另有参考论文则用 `--document`。
材料写作预留有界窗口展示明确摘要段，其余片段沿既有词面检索按任务选择。
长摘要可跨多个窗口，各窗保留精确 chunk 位置和省略情况；这不代表全文已读或语义已核实。
无标题的已解析笔记保留为正文，不按长度猜成摘要；没有识别到标题不代表原文没有结果。
支持 Markdown、文本、HTML、PDF、普通 JSON 或已完成的 `table_analysis.v1` 分析包，不把原始数值表当成已验证的实验结果。提取文本后直接进入共享的
Writer、Reviewer、装配和审计，不需要检索、创新候选、空综合产物或重新实验。
用户提供的结果仍是外部陈述，不会因此成为本会话独立测量。
新建引导写作使用内置模板时启用 adaptive 文章规划：按保存的请求和证据安排简短标题、
章节职责、篇幅与已有数据图的位置。自定义模板保留其结构，已有 TOML/检查点不改变。
完整章节篇幅与登记数据包的装配图注、表格、来源限定传入写作及审阅，共用装配正文构建函数，
不改保存稿；不完整旧包明确预览未知。共同的渲染前文本统计还包含标题、规范化章节标题、
所引参考文献与实验附录，不为统计在各提示里重复整篇正文。这些是规划辅助，不认证论文质量；
尚未生成的图及附件复算仍须核最终稿。先清草稿References再追加必需数据说明/图注。
章节指标明细按论点规划的指标引用选择，不随标题措辞或语言改变；审阅补入当前草稿引用
的指标，没有论点规划的历史任务保留完整紧凑概览。
章节核验的预览用当前候选替换同节旧采用稿；尚未起草的章节按冻结标题保留其附件并记录
待起草归属，不发明未来正文。未知归属使预览不可用。全文审阅共用同一canonical总计，
章节与附件的原始计数保留为组成部分，不再成为竞争的全稿总计。
全文检查覆盖已有章节；逐节编辑的目标额度来自冻结论证计划，旧计划保留两处目标。
显式 `report.draft_scope=document` 的跨节修订形成共同候选，先核旧意见、再独立检查整稿，
全部合格才共同采用，否则原稿不变。候选、原请求、已完成核验和已耗轮数进入原检查点；
  默认逐节编辑与待完成的旧候选继续旧路径。额度外意见保留为未解决。
  联合 Writer 在概览缺少必要条件或段落时可主动请求一批登记原文补读，仍与审阅共享
  只读工具/额度，不另开阅读流水线。未确认的读取或词面未命中不能证明原文不存在。
保存的来源简报使用有界证据投影，不改原记录，原文仍可授权补读。
单节核验与最终交付是不同门槛。候选保护拒绝完整合规稿因修订而超出冻结长度范围，
但不要求每个局部编辑独自修好本已超限的整稿；即使模型通过该节，最终审计仍记录未满足要求。
`status: passed` 不是语义认证，应分别查看 `semantic_review_status`、未解问题和实际正文。
已消费的编辑额度不会因恢复而自动增加。
固定和自定义模板保留正文要求，而不只使用标题。内置 adaptive 规划仅接收模板用途，
不再收到默认章节职责；采用计划提供实际写作职责。已有材料写作在选源预算内保留
用户材料并穿插参考论文，不会因增加参考论文而挤掉任务本身的结果。

分析后写作：将分析任务打印的 `analysis.json` 路径传给 `--material`，也可同时提供笔记。
同目录必须保留 `input.csv`、`input.tsv` 或 `input.json` 副本。系统用副本重新计算并核对记录，
固化分析包，重新生成原生 SVG；不执行附带脚本、不信任外部图链接。缺数据、路径逃逸、
无效包或数值不一致会在写作前失败。写作导入的 JSON/数据文件各限 20 MiB；独立重建
仍使用分析时明确配置的输入上限，不受写作导入上限限制。

报告附确定性的描述数值表和相对路径 `analyses/`，内含数据副本、分析 JSON/Markdown、SVG。
搬迁时复制整个报告目录。`report.figures.enabled = false` 或 `mode = "off"` 只关闭正文插图，
不丢弃证据包；显式总图数上限超出则报错，不悄悄丢图。分享前核查数据隐私。
复算只检查算术，不证明采集、语义、独立性、显著性或科学有效性，也不是本次重新实验。

默认输出材料报告（`material_report`）；`--template experiment` 请求诚实的论文体草稿，不授权实验，
也不保证论文质量。本地论文书目信息可能不完整，需核对，不能编造。
仅用已有来源、只交付报告的调研/研究任务同样复用材料到报告的路由，不调用模型重排流程；
文章规划与审阅仍保留，历史已接受路线不改。
引导生成的材料报告、分析报告和来源比较默认联合起草、整稿审阅；长论文和自定义模板
保留分节默认方式，明确 TOML 设置及已有会话保留其选定方式。
文章计划可从已登记分析图中选择正文用图；未选图、可编辑导出和数值记录仍留在链接的
分析包中。没有选图记录的旧计划保留整包放置行为。
观测分析的各列摘要可能使用不同非缺失行，配对差值只使用完整配对行。写作同时接收
这些定义和保存的计数；合表须标明各自口径，否则分表。算术复核不证明解释正确。
引导时检查模板与审阅文件；专家自定义模板/criteria 路径通过普通研究 TOML 设置。

失败或中断后用打印的路径执行 `research-session --session-root PATH --model env`。
复用已保存的提取文本，修改原文件不会悄悄替换当前依据；这不是跨会话记忆服务。
联合、分节与格式恢复的 Writer 共用一处装配内容输入，邻文上下文不重复携带附件预览；
装配仍负责数据图注、附件与引用，Writer 负责正文解释，独立审阅保持其原有证据输入。

本文说明 SimpleAutoResearch 内部在做什么：任务驱动能力、artifact 归属、恢复边界和模块职责。它不重复完整文件手册；具体命令和文件树见 [使用与配置](USAGE_zh.md)，命令参数见 [CLI 参考](CLI_REFERENCE_zh.md)，TOML 字段见 [配置参考](CONFIG_REFERENCE_zh.md)。

## 任务驱动的执行与恢复

逐篇笔记在原有片段预算内，同时保留章节概览和用户问题的词面匹配段落；
先检索已保留的实质正文，再采样，相关邻段也占用同一预算，不增加模型调用轮次。
新笔记的 `reading_coverage` 保存可用块数、实际展示的块 ID，以及
`semantic_verification = not_performed`。没有新增向量索引或论文名特判。
词面匹配是定位工具，不证明结论、不保证找到所有反证；同义改写、跨语言问题和
缺失的全文仍需定向补读。运行完成不等于原文或笔记的语义质量通过。

全部材料均明确要求阅读时，默认跳过重复粗筛/重排；检索或混合来源及显式模型筛选保留原选择。
默认PDF解析器安装pypdf官方字体扩展，用于内嵌CFF字体解码，不新增自造字形规则。
分节保留书目中的编号出版物标题，不把末尾连续数值单元当隐式标题；正文标题提及
references 不等于书目。明确的 Markdown 章节或附录可以恢复正文边界，原文仍按区间保存。
这不是OCR或布局恢复，也不保证公式、表格与科学对象正确；抽取文本仍须核查。
模型笔记可提出最多两条具体补读查询。Reader 搜索该来源已保存的完整材料，包括参考文献和附录；
初始概览仍优先正文，查询段落保留原章节标签，不把书目条目当实验依据。
明确编号的图表、定理等对象共用原文定义/标题导航，不把定位当数学验证。
正常原生来源调研不再要求旧survey契约才进入现有文章规划；显式 `outline_strategy="template"`、
自定义模板和已冻结计划保留原拓扑，不因升级重规划旧稿。
联合修订可协调必改意见精确引用的当前邻节，但不复制意见身份；建议/仅核验意见、
未知节或不存在的引文不扩展目标。恢复保留已保存请求的范围，不增加模型轮次。
每篇最多补读一轮、六个有界窗口；邻段计入预算。仅有新文本时才再调用一次模型修订笔记，
窗口在问题之间共享，可包含分散的不同命中；被预算省略的候选明确记录，不把省略当原文缺失。
没有请求或没有新窗口不增加调用。查询、实际段落和未解决问题进入综合与写作，
旧笔记留作修订记录，不再混入当前综合依据。未命中不等于论文没有相关信息；
仍有问题时保留 partial，不循环搜索或重新下载。该步骤按现有 read attempt 恢复，
没有新增逐篇 API 中断检查点，失败后可能重新执行本次阅读。

精确结构化协议的 `start` 保存任务输入后交给 `research-session`，适配器不新增规划器/生命周期；只准备
配置不会调用模型或进程。阅读传给综合时保留局限、开放问题、置信度和引用，并说明
卡片裁剪；用户执行约束不再静默截尾。这不代表语义理解已经得到保证。
报告规划、写作、格式纠正及两级审阅使用已保存的problem/goal任务要求，不以短记忆
摘要替代。不同输入保留，完全重复文本只传一次；缺原始请求的旧快照沿用既有objective。
完成恢复不重写历史正文。全文请求超过既有审阅窗口时明确未完成，不能静默丢掉要求后
声称全面审阅。这是传递保障，不是模型一定正确落实全部要求的保证。
最终审计只复用冻结计划中有原任务引文的整稿字数契约，统计实际report含标题/参考文献，
不以正文或预测代替。超限保留修订要求，非法契约/缺最终文本要求核查；旧计划无契约不猜造。
空白分词不是语言无关字数统计，符合长度也不认证语义或论文质量。
只读来源的调研和固定协议复现，综合只整理证据，不凭空生成创新候选；任务明确要求候选评估/设计时
仍采用研究综合。阅读笔记是模型解释，不冒充原文。Writer、逐节 Reviewer 和整篇 Reviewer
接收同一份有标识、有界的原文片段与来源可读状态；截断显式标记，片段没有不等于原论文没有。
交付的六个原文窗口由保存的精确补读命中和笔记/主张引用的章节概览共用，预留最多两个
补读名额，两类可借用空余名额。选中的晚段保留原偏移；满额补读不再挤掉全部早期方法依据。
省略的窗口仍需定向回读；这不是语义验证，也不改写保存笔记。
初读概览和任务词面选择按同文档保留的行位置排序，不把正文优先入库当原文次序。
缺失/混合来源位置保留同源内部顺序，同一行的并列片段不靠ID猜次序；命中邻段不跨文件，
保留文本之间仍可能有缺口。任务命中片段计入文档和章节覆盖；余下窗口先覆盖未读章节，
再在各章节内分散选段，不按整篇距离让长章节占满窗口。窗口足够时覆盖章节边界；窗口不足
仍可能遗漏定义或限定，不能认证都已读到。已选中正文不会导致参考文献重新挤入窗口。
这仍是有界采样，不是完整阅读或主张核实。
审阅器可用 `search_source_chunks` 在已登记来源的保存正文中定位遗漏段落，再补读邻段。
这是有界词面检索，不是语义验证或重新下载；未命中不能证明原文不存在该信息。
“通过但仍请求证据”的审阅只是暂定：保存补读结果后，对同一稿件再审一次才能接受。
仍有待补证据、回读关闭或预算耗尽时，明确保留来源核查缺口，不将它称为已核实或直接断言主张为假。
逐节和全文审阅复用已有工具与修订预算；有界上下文优先保留最近取得的结果，并记录省略数量。
恢复从保存的工具结果还原已用调用次数，不重置回读额度。
同时补证与改文也在调用Writer前保存已完成读取；恢复后没有新补读时，原证据仍传给Writer
及候选审阅。复用上下文不重复计作工具调用，也不认证这些材料支持正文结论。
没有补读请求的审阅不增加模型调用。
逐节和全文独立检查默认使用原文窗口，不自动继承阅读笔记结论；保留来源、截断及记录的
阅读范围。笔记可显式请求，但仍属派生解释；Writer和保存记录不变。未显示不等于原文没有，
阅读范围标记也不是事实验证，不能据此认定论文质量已通过。
全文检查不自动把早先角色取得的综合工具文字重放为原始证据，仍可显式请求；综合文字标为
保存的未核实解释，匹配来源句柄保留各自身份。检查已保存请求结果后中断，恢复复用该结果
再判断，不重新取得。历史意见仍需解决，上下文分离不能认证模型判断正确。
首次独立检查不把旧审阅意见当来源事实。历史意见另行核验，回答结构由实际旧意见目标
投影，不同时要求新发现或修订指令；完整当前稿仍可跨节取证，多余/迁移的问题核验仍拒绝。
不同意见复用模型ID时使用请求内的核验编号，原ID只作历史元数据；关闭对应精确原记录，
不能关闭同原ID的其他意见。恢复从保存请求顺序还原编号，仅完全相同的重复记录去重；
行动要求、严重度、证据或身份变化仍各自保留。这不认证模型语义判断。
关闭须有明确理由、当前章节身份及精确引文；跨节依据显式标记各自来源，问题本身不迁移，
旧字符串只匹配目标节。
遗漏或失败仍保留问题。控制器失败不交给模型关闭。被结构/引文校验拒绝的
已解析回答和理由保存于原迭代记录，不将整批当有效意见；后续核验中断不丢失已完成的新检查。
若整个回答的目标、身份及角色契约无歧义，逐条仍按原严格证据规则验证；原一次格式纠正后，
保存的有效子集可沿原额度继续。错误意见及混合无效章节的笼统修改指令不进入Writer或关闭问题；
完整审阅未完成项保留。恢复子集不重发已消费纠正，部分可用不是完整审阅通过。
采用修订只关闭原修订契约的问题，不清掉同章节其他问题。这不是自动科学认证。
有效全文检查/旧意见核验与被拒回答的补读共用原事件中的分配函数：执行前保存无源文本的blocked分配结果，
完成后替换。两种结果恢复均计次数、不重发；未确认读取不是证据，核验仍可未完成。不授予额外模型调用。
同节尚未启动的修订合并独立检查要求与核验后未解旧意见，一次起草并按合并契约验证。
已保存候选恢复仍用原契约；不增加章节/目标额度，不关闭被遗漏的历史意见。
恢复检查发现的同节新问题另存待解决清单和检查记录，不覆盖候选契约，也不因采用候选消失。
新的必要事实性意见须引用当前正文；确定的指标冲突还须定位非派生反证，观测缺失应请求核验，
不能编造冲突。`evidence_quotes` 可用`anchor: "executor_record:0"`选择本请求
`evidence_locator.pointers_by_role`中直接列出的键与路径，不要求模型自行数数组索引。
旧保存请求的零基数组仍可读取；不存在的键不猜测、不偏移纠正。省略quote时不要求模型复制原文，
共享默认回答样例仅展示anchor选择；直接Pointer和严格逐字引用继续可选兼容。
控制器保存实际Pointer、原值、归属及`mode: "field_reference"`，短引用不进入检查点或在恢复时重绑。
这是字段选择，不是模型逐字引文或语义认证。可选Pointer/role必须相符；提供quote仍严格匹配。
也可直接提供本次实际JSON Pointer并省略quote，选择可见且归属合法的标量字段，包括导航未列项；
共用原值/归属保存，不另建索引。提供引文时仍须精确引用并声明归属，非法引文不自动转换成字段选择；旧记录形状不变。
只有同一登记原文段落（`evidence_passages`、`chunks`、`source_front_matter`的text）允许ASCII排版空白差异，
例如PDF换行写为空格；词、数字、标点、大小写和断词不改，不拼接分开的字段/窗口。其他标量包括路径/身份仍精确匹配。
原文与响应不重写，这只处理排版，不认证语义。
逐节与全文请求附有界`evidence_locator`路径导航，按记录归属分组，不复制原文、不认证语义。
适用时直接采用实际完整路径并引用对应字段，导航本身不能作证据；原文路径先于元数据，省略数明确显示。
未列路径但本次可见的字段仍可引用，省略不证明原文缺信息。全文接近窗口上限时缩减或省略导航，
不删原证据、不放宽角色和引文校验。
不能将声明值标成执行器观测，或把旧意见当源证据。逐节补读结果位于同一可寻址请求内。
旧意见可继续读取，不补造历史引文；新回答提供的证据定位均核对。错位置返回有界父结构反馈，
不自动修引用；起草/恢复同样区分准备条件与执行观测。定位/归属通过不等于语义正确。
已解析的全文审阅回答校验失败后允许一次格式纠正；保持问题目标、精确引文和有界证据契约，
被拒回答不是来源证据。纠正一旦开始即消耗机会，即使结果未知；完成后恢复复用已验证回答。
纠正须保留问题身份，不须保留被拒绝的错误判断。旧建议不作为核验者必须执行的指令，
严重度/行动要求不证明问题成立。独立全文核验保留原文、书目和阅读覆盖，早先模型阅读卡
只在显式请求时提供；Writer 输入与来源/工具历史不改，缺原文仍须补读或保留缺口。
已保存上下文按原独立检查、旧意见核验或最终复审角色继续，不重取；传输、解码和额度失败
不触发格式纠正，原证据窗口上限仍适用。相同措辞的控制器错误保持各自操作身份，
实际完成对应操作后才能清除自身旧运行错误，不清除其他错误或正文问题。
无效意见不阻断单独合法的读取请求：回答目标必须全部唯一且属于当前角色，工具必须注册且仅只读。
原gateway仍检查参数、注册来源和累计次数；被拒意见/改稿指令不采用，取得材料进入原单次纠正，
不另起修订周期。读取前保存分配，完成后保存结果；中断读取明确未确认，恢复不重读。
取回原文只提供依据，不认证模型的科学判断。
逐篇笔记也接收任务目标，且与来源证据分开。默认保留入库块的长度，进一步缩短须标记；
核对笔记身份和声明引用是否属于当前文档。这防止串源，不保证主张的语义正确，也不将有限选段当作全文核查。
报告输入还保留原始任务和材料可读范围（正文已解析，或仅有元数据/摘要）。显式设置
引用来源上限时，不提前截断检索和阅读候选，终稿核对不同引用数。未解决的重大事实性审阅意见或超出
显式上限会让审计失败，失败产物仍可检查，会话暂停交付；普通风格意见仍是警告。
机械审计通过也不等于最终语义已经获得保证。
Writer 与逐节 Reviewer 共享已采用正文的有界窗口与冻结的章节职责；格式恢复保留该视图，
续跑从现有章节重建，不增加摘要存储。窗口记录首尾位置与省略字符，只辅助连贯性、不证明来源支持。
章节职责标为规划意图，不是当前正文已作出的断言。起草、格式恢复与候选核验把审阅指控和建议
视为可能有误的派生上下文：先核原稿及来源，不能为了服从意见而补入原先不存在的断言。
全文检查不再自动传入旧起草目的清单，保留原任务、当前稿、事实/风格准则与证据；保存计划和
Writer可见性不改。派生篇幅预测仍与实际canonical计数分开显示。
角色标注不认证语义、不改保存计划，也不自动关闭旧问题。
新建自适应计划可在 `document_plan.length_budget` 保存显式全稿词数要求及原任务精确引文。
复用canonical预览，先预留已知标题/章节标题、数据附件和实验附录，再分配近似正文份额；
份额不是最低字数，原要求与当前修订优先。未选参考文献和未来图形文字是未定开销，不当作零。
完成交付仍须检查，空白分词不能认证所有语言或字数惯例。页数、字符数、排除正文范围不静默换算；
不增规划调用，旧冻结计划恢复不升级。引文定位有效不等于模型对要求的解释已正确。
候选核验共用同一检查：完整原稿符合范围、单节修订使交付越界时，模型pass不能采用。
继续修订/恢复仍沿原额度；不完整预览或原稿本已越界不猜单节补救，最后仍审计实际装配稿。
这防止该类回退，不保证全部长度问题或语义错误已解决。
综述规划同样读取完整原任务；无论是否指定词数，2–12个可用提案章节均不再按关键词
插入覆盖章节或填充小节，也不因通用标题拒绝方案。宽综述覆盖指导、来源路由、用户
模板及冻结计划仍保留。保存提案不等于已经回答任务或具有充分的科学覆盖。
主张视图随当前采用稿重建；不同章节复用 ID 不互相覆盖，被拒修订不替换当前正文依据。
修订验证同时接收原始问题、修改要求、原稿有界窗口和来源证据。允许删除无依据或重复的正文，
保留有依据的事实与必要限定，而不是保住原字数。检查点保存修改要求和全文编辑候选稿；续跑复用
它们及已消耗的修订/补读额度，不重新生成一套额度。这些仍是模型辅助检查，不认证所有重要主张或论文质量。
普通写作与格式恢复共用有效修改要求：显式指令加必需修改建议；旧记录整体修订请求仍执行。
可选/仅核证建议不静默变成改稿，原意见仍可见。可改范围只含当前单节正文、显示标题及有依据的元数据，
冻结计划、其他正文、登记数据/观测和装配文字只读；不可执行的所有者修正保留待办，不能声称Writer已改。
核验给出原稿/候选章节计数及差值；这些是派生观察，可支持style，不是测量事实的反证。
补充必要事实可能变长，revised状态本身不证明修订成功，也不要求所有候选必须缩短。
单节响应显式指向其他章节时拒绝，沿原一次格式纠正处理；缺省、null或旧空ID绑定当前节，
不迁移已保存记录，不借响应打开新的编辑目标。
内置自适应规划成功后，综述和实验报告均由冻结计划拥有章节结构，不再叠加默认起草顺序。
独立审阅保留事实检查，只省去明确分开的 `Default Structure` 回退块；自定义准则和旧冻结
准则恢复时不改写。项目目录的 `templates/report` 仍优先于安装包默认资源；检查 wheel
默认模板时应使用没有该覆盖目录的工作位置。
旧意见核验省去写作准则/章节目的，但保留完整任务、当前稿与证据；消费者拒绝该角色的
新findings或改稿指令，沿原一次格式纠正保留拒绝回答和旧问题。独立审查仍负责新发现，
不是删审阅或把未知意见当已解决。
核验投影不发送旧意见的严重度、行动要求和建议；原记录及编辑者的修订优先级/契约仍保留。
回答结构只描述一次，实际问题身份与目标仅由核验列表提供，原文引文、当前稿及严格绑定不变。
这减少判断暗示和重复结构，不等于模型语义或实际费用已经改善。
JSON表示层仅为字符串非法转义保留实际反斜杠，不另发请求；合法转义含义不变，不猜补Unicode/
控制字符/缺失结构，不从损坏外层取内部对象冒充回答。解码后仍核目标、引文和值，不认证内容。
当前限制与待解问题沿冻结输入和采用稿重建，不累加每版解释。既有检查点所有者在恢复首个
请求前及采用修订后更新视图；历史稿元数据用于退出已替换、待审和被拒稿的备注，原始约束
及无法追溯来源的旧备注保留。历史不改，备注也不等于已独立核实的来源事实。

报告审阅问题可记录 `required_action`：`advisory` 为可选建议，`revise` 要求有界修订，
`verify` 要求取证或限定/删除无依据断言。影响严重度与行动要求分开，不能因为问题标为
minor 就忽略必需工作。仅核证时沿既有工具取证、重判现有稿件，不强制重写；同时需要
改正文时走原取证→修订→检查路径。不因此授权新实验，不在恢复时刷新工具或修订额度。
必需工作未解决时审计保留 warning 或更差状态；旧记录缺字段仍沿原严重度/类型策略，
显式建议也不能降级既有事实保护。这是控制契约，不是模型理解能力的认证。
Agent 结果/检查点顶层的 `reviewer_findings` 保留历史意见，包含暂定或已解决问题；
当前未解决项在 `memory.reviewer_findings`。判断缺陷须看关联审阅/工具事件，
不能把历史意见直接当作终态问题。
仅核验的旧意见未解决可保持待查，不自动改稿。取证先由核验者处理，待补证即使模型提议关闭
也不关闭原意见；不证明旧判断成立或无效。明确的有限修订仍执行，其他修订不能清除它。
当前断言的来源保护不变，必需工作未完成仍影响审计。

新冻结论证计划允许选择其章节集合，旧计划仍最多两处目标；每处共用既有
`max_review_iterations` 上限，被拒候选也计入。恢复保留原计划范围、已用额度与问题，不重置次数。
已有日期、DOI 和作者名单覆盖说明随文档交接保留；写作、参考文献、BibTeX 和 `citation_map.json`
共用同一元数据投影，缺项明确显示而不推断。服务商元数据不等于论文身份或版本核实，同名或字段齐全仍可能有误。
投影提示不可能的 ISO 日期、明确的 DOI/arXiv 定位符不一致和版本冲突，不按标题或最早年份选记录。
识别 DOI 包装格式但不联网认证、不覆盖原记录。审计提示已引用的冲突；缺项或未使用的冲突来源本身
不阻断交付，字段一致也仍是未独立核实。
解析后第一个已识别标题之前的文字保留为前置信息；有界块预算优先正文，极小预算可能不将前置信息编入块。
原有模型阅读调用会得到有界的前置信息视图，可提议缺失的本地引用字段，不覆盖已记录元数据。
自适应材料写作在原提纲调用中使用同一保存视图，不必先制造阅读阶段。
同源匹配的提议沿原提纲检查点保存，恢复后贯通写作、装配、引用映射和 BibTeX。
固定自定义提纲及没有提议的旧计划不自动补入新元数据。
文件名占位题名按缺项请求；可保留准确的短署名子集并标明不完整，不用模糊匹配放过
错误的长名单转写。ISO 或完整英文月份日期校验日历后提供年份，原文本不改；歧义
数字日期和猜测年份不接收。这只校验所给字段的格式，不认证日期确实属于发表而非
投稿/接收，也不认证版本归属。
`get_paper_brief` 同时返回已记录元数据与保存的前置信息，即使块上限未索引署名页；不打开实时路径、不猜补旧保存包缺失的前置信息，明确显示截断和缺失。
本地来源缺失的题名/作者/日期/DOI/公开链接，可凭同源已保存的前置信息定位和匹配引句补入报告投影；
题名只能替换文件名默认值，不改原始记录或用户/检索源已有元数据，不完整署名明确标注。
引用映射、Markdown 与 BibTeX 共用该投影；不联网查找，不从文件名猜测，不认证身份或版本。
未读到或不可取得的字段仍未知，旧阅读笔记保持较窄的同源标题匹配行为。
前置信息只是解析标签，不保证仅包含署名；未识别明确摘要时，开头文本仍可参与
任务相关选读，不整段丢弃，不冒充摘要或全文已读。

报告示意图必须有正文实际标签，不填通用节点、不推断箭头；信息不足就跳过。
配对图按输入指标顺序选取（默认最多四图），不偏向持续学习指标名；复用无界面 Matplotlib
渲染器，交付 SVG、PDF 和 PNG。已接受论点规划将实测指标唯一归属到一个已写章节时，
图形放入该章节，不依赖标题语言；否则保留对照附录。连线表示记录的配对，不是置信区间；
绘图不等于科学验证。

结构化研究入口是 `research-session`，在同一会话中维护 attempt、产物、报告与审计，
由 `ResearchApplication` 根据任务、已有材料和已接受的执行约束选择需要的能力；对用户没有
固定阶段序列。

```text
任务 + 材料 + 约束
  -> 短 accepted plan
  -> 能力执行并记录观察产物
  -> 应用层决定下一动作、修订、交付或停止
  -> 从持久化引用显式恢复，已完成的有效副作用不重复
```

应用层负责计划接受、能力顺序、可比性决定和交付选择；每个能力负责自己的输入合同与
attempt 输出；`SessionController` 负责 attempt、预算、lineage 和产物持久化。恢复时读取
这些事实并重建下一项已接受动作，不重放已完成副作用，也不让 core 自行发明领域阶段。

公开入口保持精简：

```text
simple-ar start                  # 普通原生目标驱动任务
simple-ar start --resume-task PATH # 原生反馈与恢复
simple-ar research-session       # 精确结构化协议与已保存会话恢复
simple-ar research-brief         # 兼容请求/结果适配器
simple-ar status RUN_DIR         # 只读展示存档
```

## Capability 运行

在这个任务驱动入口之外，`simple_ar.core` 为新的可替换能力提供了一层可选
边界。能力通过 `CapabilityContext` 接收已经声明的输入引用，通过 attempt-local
的 `ArtifactStore` 写出结果，并返回 `CapabilityResult`。`SessionController` 可以
持久化一个有界 attempt 和对应 decision，但不会把应用变成不受限制的
任务图。

这层边界是组合式的：它不会调度任意动作，也不会改变现有命令和 adapter 依赖
的产物路径。`tests/fixtures/capability_package_minimal/` 提供最小离线 handoff 示例；
具体领域的 schema 应属于对应 capability，不应继续堆进共享 core。

session 还可以选择一个可选的 lifecycle profile，限制本次 session 可以执行的
capability。内置范围包括 `research_brief`、`survey`、`experiment`、`paper_audit`
和 `full_research`；它们只是 allow-list，不是自动运行器。无法识别的 profile 名称
仍按旧调用方式兼容处理。
新建的已知 profile session 如果没有显式预算，controller 会按每个声明能力一次 attempt
再加两次有界恢复机会分配默认预算；调用方可用 `BudgetState` 覆盖，加载旧 manifest 时则
继续使用其中持久化的预算。

如果调用方需要串起多个 capability，应由 application 层按明确顺序调用
`SessionController.execute_attempt()`。它持久化执行事实；是否停止、继续或完成研究由应用决定。
进程恢复时应先加载 session、查看状态和 attempt lineage，再显式构造下一次调用；core
不会静默重跑中断的 attempt，也不会替领域规则选择所谓最佳结果。
如果已经人工确认发生了中断，调用方可以使用
`SessionController.recover_interrupted()`，先把遗留的 running attempt 收束为明确失败，
再显式构造 retry 或 repair attempt。该方法不会自动重试，也不会覆盖已有的 result envelope。
只要前一个 attempt 仍标记为 `running`，controller 就会拒绝新 attempt，避免恢复前悄悄形成第二条活动分支。
Core 在创建 attempt 前检查能力范围、预算和输入 artifact；研究顺序归应用负责，
执行边界不再施加第二套固定阶段转移规则。

如果后一个 capability 需要使用前一个 attempt 的已声明输出，应调用
`SessionController.attempt_output_refs()`。它只把 attempt 内的相对路径转换成
session 根目录引用，不复制或合并产物；使用哪个 attempt、哪个输出仍由调用方决定。
如果需要从较早的 `completed` 或 `failed` attempt 开始另一条比较路径，可以在
`SessionController.execute_attempt()` 中传入它的 `parent_attempt_id`。controller 校验并记录该
父节点；不传时仍沿用当前 attempt 的线性行为。
controller 不会自行推断分支，也不会替调用方选择结果。需要展示某个节点的父链时可使用
`attempt_lineage()`；它只读取持久化的 attempt manifest，不合并产物或调度新工作。

历史研究会话读取器展示记录过的 `next_capability` 和执行证据，不再用已退出的策略
重新推算下一步；结构化研究的动作选择归 ResearchApplication 负责。

如果库调用方只想得到一个内存中的聚合值，可以使用
`research.brief.build_research_brief()` 只保留为内存中的兼容视图。默认 registry 刻意不再
暴露聚合的 `research_brief` capability：session 分别持久化 `read` 与 `synthesize`。
历史 `research_brief.v1` handoff 仍可读取，但不会再成为第二条可执行 lifecycle。

下面的 `research-brief` 是分段/开发入口，不是普通用户的完整任务入口。面向普通用户的流程应
优先使用 `simple-ar start`；需要只构建研究 handoff、调试阅读或从已有 handoff
开始时，才使用这些较小的组合入口。`research-brief` 现在只是参数/返回值适配器，
请求同一个 `ResearchApplication` 生成文献摘要，采用其生命周期及默认请求/token 预算，
不再维护独立编排器。其研究能力顺序为：

```text
plan -> search -> document_ingest -> read -> synthesize
```

主题检索可以直接运行：

```bash
uv run simple-ar research-brief --topic "reliable agents"
```

如果希望使用可复现的本地输入，可以重复提供 Markdown/TXT 文件：

```bash
uv run simple-ar research-brief --topic "reliable agents" \
  --local-document tests/fixtures/research/reliable_agents.md \
  --output-root runs/research-brief
```

命令会在输出目录下创建带时间戳的 v2 session。每次交接保留在动态命名的独立 attempt 中，
实际路径从 `session_manifest.json.state_refs` 读取；下游命令使用 CLI 输出的
`Synthesis handoff` 路径，不再拼接固定 attempt ID。规范输出分别是
`research_plan.json`、`search_result.json`、`document_bundle.json`、`read_result.json` 和
`synthesis_result.json`。能力结果与 attempt manifest 会记录状态和 lineage。该入口不会
静默重试或覆盖旧 attempt；`--query`、`--provider`、`--max-results`、`--max-chunks` 和
`--idea-limit` 是这条路径保留的少量控制项，更复杂的策略仍由上层应用负责。旧的
`research_brief.v1` 聚合格式仍可作为输入交给后续入口。

这条 standalone 路径会明确区分模型模式。省略 `--model` 时，它是离线/确定性组合：搜索、
解析、card derivation 和结构化方向提取只使用已有输入；传入 `--model NAME` 后，使用现有
LLM client 完成研究规划、有界 Read 筛选/重排、paper notes 和综合，并在 handoff 中记录
Read provenance、`planner: llm` 与 `generation_mode: llm`。缺少凭据、传输失败或模型返回格式
错误时，对应 attempt 会失败，不会静默伪造模型结果。

分段 `research-experiment` 创建器已退出。实验与分析能力继续由正式应用复用，
不再为旧 synthesis 文件另建一套会话生命周期。

如果需要在同一个结构化 session 中组合这些 handoff，可使用
`simple-ar research-session`。它复用相同的
`plan -> search -> document_ingest -> read -> synthesize` 前缀，记录一个
`research_design.v1` handoff，再用一个明确提供的 `ExperimentRequest` 进入现有 Analysis
capability。默认实验命令仍由调用方给出；传入 `--code-task-config` 时，experiment attempt
会改用已有的 project-style Code-Task backend，项目、benchmark、workspace、baseline 和
执行设置仍由 TOML 管理，最终输出会规范化为同一份 canonical result。这仍是受控组合，
不是不受限制的研究循环。

如果既没有提供实验命令，也没有传入 `--code-task-config`，同一个入口也支持 literature-only
组合：在有证据支持的 summary 处结束，不创建 execution 请求；提供模型时，可以继续进入
research-only 报告路径。这是明确支持的无实验形态，不会隐式生成占位实验。

如果 session 的实验失败但仍保留了 design 和 analysis handoff，可以显式追加一次恢复实验，
复用已有文献和研究设计，不重新检索：

```bash
uv run simple-ar research-session-continue \
  --session-root runs/research-session/<session> \
  --cwd tests/fixtures/research \
  --primary-metric accuracy \
  --metric-direction accuracy=higher \
  --command python -c "print('accuracy: 0.90')"
```

对于 canonical `session_manifest.v2`，它会创建动态命名的新 experiment attempt，把失败候选记录为父节点，
然后复用已有文献和 design 做确定性 analysis。修正后的命令由调用方提供；不会重复 search 或 design，
原有 attempt 也不会被覆盖。这个边界只处理普通显式实验的技术失败；成对实验、数据准备和 CodeTask
使用各自的有界恢复路径。科学负结果是证据，不会被静默重跑。旧 v1 session 仍保留固定的
`experiment-002`/`analysis-002` 兼容行为。

使用 `--no-report` 创建的正式 session 可通过 `research-report` 补齐报告动作，复用既有证据
和测量。Writer 与检查点统一在 `report/writing.py`，组装和审计通过同一应用生命周期完成。

写作与两级审阅共同区分协议声明、执行记录和独立实现检查。当前会话登记的文档产物
可供审阅者按引用 chunk 回读有界原文；只有缓存片段时不能冒称重新读了原文。
迭代轨迹保留修订正文和全文修订的采用决定，验证失败也不丢失诊断。
生成报告不等于论文已核查，仍须查看未解决意见和审计状态。

已组装报告可独立使用 `report-export`：保存的引用键正文、文献和图转换为可编辑 ACM
演示工程，不重复阅读、写作或测量。导出/编译状态与内容审计状态分别记录；命令见
[CLI 参考](CLI_REFERENCE_zh.md#simple-ar-report-export)。

pdfLaTeX 演示对常见希腊/数学 Unicode 符号采用导出层固定声明，不修改原 Markdown。
这不等于通用多语言字体支持；不支持的字符或缺 TeX 包时，保留源工程和编译诊断，
不悄悄改正文，也不冒称 PDF 交付成功。

普通斜杠串在导出层增加换行机会，不改字体/数值，数学与URL目标不重写。
编译成功不等于页面/字体检查通过。缺书目字段可能产生嵌套ACM/natbib标签；
内置编译器保护生成标签，不编造作者/日期，导出README说明相应手工构建限制。
缺书目信息和字体警告仍须保留。

历史 session 可检查和读取，但不再由第二套 Writer/report/audit 执行器续写。
`build_research_session_report_inputs()` 和 `build_code_task_report_inputs()` 只投影已有证据，
不执行或修改会话。分段 `research-code-task` 创建入口已退出；完整任务使用
`research-session --code-task-config`。

如果应用需要使用内置适配器，也可以调用
`research.register_research_capabilities(registry, names=...)`。不传
`names` 时注册完整适配器集合，传入时只注册当前路径需要的能力；注册仍然是
显式操作，不会创建调度器。该 helper 覆盖确定性的 research planning、Search、
Document Ingest、Read、Synthesis、Research Design、Experiment、Analysis、Report、Report Audit
和 Research Brief。
独立结果分析能力的规范名称是 `analysis`；为兼容旧调用方，显式选择时仍保留
`analyze` 这个 registry/session 别名。

其中 `plan` 适配器复用已有的问题、查询和来源预算 builder，写出一个
`research_plan.v1` handoff；默认使用确定性路径，调用方显式传入
`use_llm=True` 和共享 client 时才会得到规范化的模型辅助计划。它不替调用方选择下一能力。
窄的 `research_design` 适配器接收持久化的 synthesis，默认选择调用方指定的研究方向；
调用方显式提供共享 LLM 时，它也可以只在已有候选方向中选择一个，并写出包含已有
`ResearchExperimentContract` 的 `research_design.v1` handoff。它只检查契约是否具备最小可执行
字段，不会自行创造 command、metric value、实验矩阵、代码或执行计划；领域专属的代码生成和
执行实现仍由调用方提供。
如果要把该计划交给已有的 `SearchRequest`，可以使用
`research.planning.search_request_from_plan()` 这个内存适配器；它不会调用 provider，
也不增加 retry、去重或候选选择策略。

如果调用方已经拥有输入，也可以分别注册
`research.evidence.reader.run_read_capability()` 或 `research.synthesis.run_synthesis_capability()`：前者
接收 `DocumentBundle` 并写出 `read_result.json`，后者接收 expanded evidence pack 并写出
`synthesis_result.json`。前者不会自行下载文档或调用 LLM；后者默认使用确定性结构推导，
只有显式传入 client 才调用 LLM。两者都不会决定阶段转移。

如果 session 从检索开始，也可以显式注册
`research.sources.run_search_capability()`。它会在 attempt 目录写出一个
`search_result.json`，包含规范化论文行以及 provider/query 的响应状态。这只是交接产物，
不替代旧 Search projection，也不改变候选选择策略。

如果调用方希望从证据综合直接进入独立的执行适配器，有限 recipe 允许
`synthesize -> experiment -> analysis`。调用方仍需提供 `ExperimentRequest`、执行 backend
和下一步 decision；core 不会替调用方推断 design 或 repair 策略。
若该请求来自持久化的 `synthesis_result.v1`，可以使用
`research.experiment_request_from_synthesis()` 转移已有的 research-level 实验契约；
`RunRequest`、result schema 和是否执行仍由调用方显式提供。该 helper 不批准
`needs_review`，也不隐式执行、重试或选择下一阶段。

如果下一步需要全文资源，可以使用 `DocumentIngestRequest` 显式注册
`research.documents.run_document_ingest_capability()`。它会写出一份可恢复的
`document_bundle.json`，包含文档记录、section、chunk 和 extraction 状态。后续 Read attempt
可以通过 `DocumentBundle.from_handoff_dict()` 加载这份声明过的产物；ingest 本身不选择论文，
也不调用 LLM。

Read attempt 完成后，可以用 `ReadResult.from_handoff_dict(payload, bundle=bundle)`
恢复 typed cards；调用方必须显式提供原始 document bundle，因此 Read 产物不会再次复制
source chunk 原文。如果调用方要直接组合 Read 与 Synthesis，可使用
`research.brief.evidence_pack_from_read()` 这个小型适配器，把 cards 转成 Synthesis 所需的
最小输入。
Read 在生成和恢复时会检查 cards 的 `evidence_refs` 是否仍指向 bundle 中的 chunk；失效引用
会记录诊断并将结果标记为 `partial`，但不会扫描其他文件或阻断 metadata-only 的兼容读取。

执行切片遵循同一规则：session 需要运行 `RunRequest` 时，显式注册
`research.experiment.run_experiment_capability()`。它把现有 canonical result 暴露为
`results.json`，并把捕获到的 stdout/stderr 以同一 attempt 下的
`execution/stdout.txt`、`execution/stderr.txt` 声明。分析步骤可以显式注册
`research.analysis.analyze_experiment_capability()` 读取该引用；失败或超时执行不会被
转换为成功 capability，诊断日志仍可供后续 capability 使用。分析缺少必要证据时返回
`partial`，只有分析状态明确为 `passed` 才返回 `completed`。持久化的
`analysis_handoff.v1` 可通过 `AnalysisHandoff.from_handoff_dict()` 恢复；恢复只验证结构
并保留 execution ref，不会重新执行实验。
如果已有两份完成的结果 mapping，可以使用
`research.analysis.compare_experiment_results()` 生成状态与指标比较，并作为
`ExperimentRequest.comparisons` 传入；方向未知或证据不足时保持为 `inconclusive`，后续
是否继续实验仍由调用方决定。
对应的 `AnalysisResult` 还提供保守的证据状态：`passed`、`failed`、`blocked`、
`incomplete` 或 `metric_below_target`。它要求显式的 execution handoff，不会自动安排
retry 或阶段转移；持久化的独立分析还会写出 `analysis_status.json`。
应用直接消费分析证据并负责下一动作。

如果 session 只需要审查已经组装好的报告，可以显式注册
`report.audit.run_report_audit_capability()`。调用方传入报告 artifact 引用和 typed report
状态；适配器保持现有 `report_audit.json` 格式，并把 warning 报告为 partial，不会静默当成
干净通过。

如果调用方已经拥有完成的 section draft，可以先显式注册
`report.capability.run_report_capability()`。它复用现有 report assembler、可选标题编号和
可选的计划图表 renderer，在 attempt 目录生成 `report.md`；生成的图文件也会作为同一 attempt
的 `figure` 输出引用登记，figure manifest 只是索引，缺失图文件会报告为 `partial`。它不会
调用 writer，也不会生成 audit。随后把声明出的 report 引用传给独立的 audit capability。

### 1. Research Report：文献优先（分段/高级用例）

适合想要 literature review、survey 或 DeepResearch-like report，而不强调实验执行的场景。
普通用户的完整研究任务使用 `start`；这里描述的是可复用的分段能力边界。

概念流程：

```text
plan -> search -> read -> synthesize -> report
```

在文献优先的结构化路径中，未提供执行命令或 CodeTask 配置时，`research-session` 使用纯文献路径，不启动实验。
报告属于同一生命周期；只有显式 `--no-report` 才省略该交付。

### 2. Code Task：已有代码库

普通 `execute` 默认只生成 patch plan；显式执行 `--to-step work-plan` / `--to-step batch`
或已有 work plan 时才使用分批路径。交互执行遵循同一规则，分批任务的作用域、批准与恢复记录保留。

适合已经有代码，希望进行有目标的修改、优化、修复或 benchmark improvement。

概念流程：

```text
init workspace -> index code -> map repo -> probe environment
-> apply baseline policy -> plan patch -> approve -> propose edits -> apply edits
-> review changes -> validate -> run patched benchmark -> post-run review
-> compare results
-> analyze failure -> repair proposal
```

关键边界：

- 源项目会准备到 `code_task/workspace`。已有项目默认 `auto`：优先为已有 commit 的 Git 项目创建 detached `git_worktree`，如果 Git 条件不满足则降级为受保护的 `copy`，并记录原因与下一步建议。monorepo 场景下会在仓库根创建 worktree，并把对应项目子目录作为可编辑 project root。实验性 `sparse_copy` 只复制配置的 include patterns，并始终排除 data/model/cache/secret-like 路径。原始代码不会被修改。
- Patch application 必须经过显式人工 approval gate。
- Edit proposal 是保守 old/new replacement，不是自由形式重写。
- 受控补丁提案与应用直接调用实现，重复编辑适配层已退出；产物保留 `controlled_patch` 来源标记。外部 Harness 接入属于后续工作。
- 同一个文件可以有多个有序 edit，但每个 `old` block 必须保持唯一匹配；无效 proposal 会在写文件前停止。
- `code-task execute` 可以推进下一步，但会在 plan approval 和 proposal review 处停下，除非用户显式继续。
- Work-plan item 应该是可执行的 implementation batch。executor 在选择第一个 active batch 时会跳过明显的纯分析 item，因此 LLM 生成的“先 inspect 项目”不会意外限制后续 edit 阶段。
- 如果多个已审核 work-plan item 形成小型串行依赖链，且必须一起落地才可运行，比如 feature producer、model consumer 和 config switch，active batch 可以把它们合并。拆分后的计划仍然可见，`batch_state.json.work_item.source_work_item_ids` 和合并后的 `target_files` 会记录实际执行范围。
- benchmark 通过的 repair 不自动等于任务成功。最终是否 improved 要看 `code_task/run/comparison.json`；如果 patched 指标仍低于 baseline，只能说明流程恢复到可运行或超过 benchmark floor，还没有真正完成“提升”目标。
- baseline 运行是策略，不是无条件成本。`auto`/`run` 会记录未修改指标，`skip`/`none` 会继续执行但不做 comparison，`provided` 会把用户提供的指标写入 artifacts 并标注来源。
- 当前执行有 workspace isolation 和明确 interpreter policy。支持 `current` 和 `external`；自动创建环境留到后续。`workspace.reuse_source_venv` 可以把 worktree/copy/sparse run 指向 source 项目已有 `.venv` Python，但不会安装依赖。

内置示例：

- `scripts/research_session_smoke.py`：正式研究应用 smoke；纯文献报告也使用同一应用，不再提供旧 pipeline 配置。
- `examples/code_task_medium_review/`：standalone code-task 流程，目标是一个多模块 review classifier，入口是 `main.py`，使用 JSON config，运行时有进度输出，任务自然涉及 feature extraction、model scoring 和配置文件之间的联动。
- `examples/code_task_digits_mlp/`：独立 CodeTask 的轻量 NumPy MLP benchmark，适合无 GPU 的真实 CPU 测量。

### 3. Research With Experiment：研究衔接实验

使用 `research-session`，显式提供执行命令或 `--code-task-config`。
应用负责研究生命周期；CodeTask 在准备好的工作区实现修改，experiment 能力负责实测。

概念流程：

```text
plan -> search -> document ingest -> read -> synthesize -> design
-> 按请求准备/实现 -> experiment -> analysis -> report -> audit
```

- 研究目标与限制通过 research handoff 传给 CodeTask。普通修改只需一份批准后的 patch plan；
  显式请求或已有 work plan 的任务保留分批路径。
- 实现产出冻结的 patch、validation、review 和计划证据；通过这些检查不等于科学上有提升。
- 实验结果和对照保留执行来源。失败进程不构成有效测量，合法负结果也不意味着应无限修复。
- 报告使用已记录的文献、实现和实验证据。字面可见性检查在同一行寻找登记指标名和值，不推断表格别名、行列关系或舍入；未命中明确要求核验，不直接判漏报或要求改稿，未解清单保留。框架生成的测量表另与持久化结果逐行核对，对调 baseline/candidate 数值仍失败。这是结果一致性检查，不是原始测量独立核验或论文质量认证（`semantic_review_status` 仍为 `semantic_unchecked`）。
- 终端交付分别显示最近一次实测候选的方法证据状态、代码验证状态与报告审计状态。配对运行会交叉核对各候选测量与汇总的实现引用；来源混杂或缺失时显示不可判定。`not independently checked` 表示尚无独立机制核验，不是实验失败，也不是默许方法已获证实。
- 恢复报告不能重跑已完成实验。入口参见上方会话命令与 `scripts/research_session_smoke.py`；
  离线 smoke 使用 fixture，不是真实科研验收。

## 历史八阶段产物

旧八阶段执行器已经删除。`run`、`resume`、`research-code-task` 和
`research-experiment` 拒绝执行并提示使用 `research-session`；旧模板参数不再代表可执行工作流。

现有阶段目录存档不会被删除或改写。`status RUN_DIR` 可以展示其中记录的 manifest 和 pipeline state。
私有接口 `_legacy.documents.load_search_document_bundle(search_dir)` 可只读加载历史 Search
目录，不创建运行时 Context。历史产物名称描述的是存档证据，不是另一套可执行 pipeline。

## Search 与 LLM 边界

Search 检索记录并保留来源；文档摄取处理本地或允许访问的远程正文，Read 选择和分析证据，
Synthesis 综合证据，Design 提出实验方案。缺少全文应保留为明确限制，不能声称已完成全文阅读。

有界阅读笔记可保留 `claim_scopes`：主张的对象、性质、适用条件、证据类型及同源段落引用。
缺字段保持未知；引用无法解析或跨来源时阅读保留 partial 诊断。综合与报告共享这些范围，
但它们是模型解读，不是独立语义认证。来源已解析、存在有界模型笔记及笔记覆盖数量分别显示，
均不等于全文理解。采用正文的上下文另展示带原位置与省略数量的有界表格行，不另建测量事实或记忆库。

LLM idea 与本地新颖性检查只是研究建议，不是原创性证明；离线 fixture 输出不是模型完成的科研分析。
具体输入输出见上方 capability 入口。

## Artifact 归属概览

实验报告装配将读者正文与登记证据分开：`data_tables=linked` 追加简短链接，`full` 另外内嵌摘要表。
两种模式都保留本地 JSON/Markdown 记录包和明确登记的来源副本；规划、预览与实际装配共用受保护正文块。
原生全文检查及旧意见核验（含纠正/补读路径）也实际收到该块，而不只是统计它的篇幅。
审计仍查正文必要指标、包内投影值/可读文本及声明副本是否存在，不独立认证实验或检测副本事后篡改。
重新装配和 ACM 导出不改原任务/结果，导出只携带该原生包，不扫描任意本地链接或源目录。

报告装配负责标题呈现与最终参考文献表，不改正文含义。不同的首个小标题保留，
仅移除与章节标签完全相同的重复标题；围栏代码里的标题和字面的 `References`
不是文档边界。引用清理与编号共用边界规则。写作最终检查点将当前未解意见与
历史审阅记录分开保存，关闭整稿审阅时也不遗漏。重新装配不重跑阅读、模型或实验，
也不独立认证内容质量。

- `session_manifest.json` 记录会话和 attempt 状态；应用选择研究动作，共享 budget ledger 记录用量。
- `attempts/` 下各次执行拥有其声明产物；通过引用而非固定阶段编号连接检索、阅读、综合、实现、
  实验、分析、写作与审计。
- 实现冻结所测版本的 patch 与检查证据；实验执行拥有实测指标，分析模块负责解释。
- 报告写作、组装、审计保留各自的 attempt 产物。进程完成、工作流完成和论文达到发表质量是不同结论。
- 历史编号阶段目录仅作为存档。可重建缓存不是结果的事实来源，不能替代原始产物。

## Code Task Artifact 边界

Standalone code task 和 research-session 中的 CodeTask 使用相同的概念布局。重点不是记住每个文件名，而是理解每组 artifact 的职责：

- `workspace/`：隔离后的可编辑项目副本、worktree 或 sparse subset。
- `meta/`：环境报告、repo map、locate results、edit proposals、validation reports、applied-edit summaries 和 LLM usage。
- `context_packs/`：从候选可编辑文件和受保护只读证据中组装出来的有界 prompt context。
- `attempts/`：多步骤实现和 repair loop 的 work-plan / batch state。
- `run/`：baseline/patched benchmark 日志、metrics、execution reports、failure analysis 和 before/after comparison。
- `repairs/`：按 repair attempt 分组的有界修复 proposal。

tests、benchmarks、环境文件、secrets 和用户配置的 protected paths 默认作为只读证据被索引，不应被 proposal、repair 或 apply 步骤修改。Edit scope 行为和完整 artifact 路径见 [使用与配置](USAGE_zh.md) 与 [配置参考](CONFIG_REFERENCE_zh.md)。

## Code-Task 环境策略

环境处理和源码隔离是两件事：

- 源码隔离：用户代码会先准备到 `code_task/workspace`，再应用任何补丁。默认 `auto` 通常为已提交的 Git 项目创建 detached worktree，Git 不可用时降级为受保护 copy；monorepo 子目录会成为实际可编辑 project root。`sparse_copy` 是实验性 allowlist copy。
- 执行隔离：benchmark 使用选择的 Python/runtime 环境运行。

今天 code-task 已经有第一类隔离，并通过 `meta/environment_report.json` 记录环境信号。它可以选择当前 SimpleAutoResearch Python，也可以选择用户提供的 external interpreter。它还不会自动创建 venv 或安装依赖。

计划中的环境模式：

- `current`：使用当前 SimpleAutoResearch Python。已支持。
- `external`：使用用户提供的 Python 或 Conda interpreter。已支持。
- `project-venv`：在 run 目录内创建 per-run 环境。计划中。
- `shared-env-cache`：按 dependency-file 和 platform hash 复用环境。计划中。
- `docker`：需要更强隔离时在容器中运行。计划中。

默认应保持保守：依赖安装必须显式、可审核，并且不应默默把用户项目包安装进 SimpleAutoResearch 自己的环境。

## 为什么内部要拆分能力

内部拆分能力并不等于把普通用户暴露到多条并行主线。它的作用是避免实现变成一个无法维护的
大 pipeline，同时让正式入口保持简单：

- 用户只想写 survey 时，不应强制运行代码阶段。
- 用户只想优化已有代码时，文献阶段应可选。
- `research-session` 可以按任务配置选择是否接入准备好的代码实验，但生命周期仍保持有界。
- 测试、恢复、开发者和未来 workflow 可以组合模块；普通用户不需要理解内部组合细节。
- 每个模块可以独立升级，但不得形成第二套 session 状态、artifact 或报告核心。

这也来自 AutoResearchClaw 的一个实践启发：复杂行为如果暴露成 workflow modes 和 capabilities，会比塞进一条不断膨胀的 flag 序列更可控。
