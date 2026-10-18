# 任务指南

[English](WORKFLOWS.md) · [开始使用](USAGE_zh.md) · [CLI 参考](CLI_REFERENCE_zh.md)

这里从你要完成的工作出发，说明该提供什么、如何启动、怎样检查交付。
先完成[模型配置](USAGE_zh.md)。命令中的 `SESSION` 和示例路径需替换为执行机器上的实际位置。

## 选择起点

| 我想做什么 | 阅读 |
| --- | --- |
| 给一个方向，调查有哪些路线 | [方向调研](#方向调研) |
| 修复项目，或从需求创建代码 | [代码工作](#代码工作) |
| 分析实验结果，绘制主图 | [分析与数据图](#分析与数据图) |
| 画方法图或概念插画 | [方法图与概念图](#方法图与概念图) |
| 根据材料写报告或论文初稿 | [写作与出版](#写作与出版) |
| 检查论文中的一项结论 | [有限复现](#有限复现) |

这些是起点，不是互斥模式。一项任务可以查资料、改代码、执行计算，再写报告。
已有成果可以继续使用，无须为了写作重新运行实验。

## 方向调研

**提供：** 方向、用途，以及真正影响选择的条件，如时间范围、硬件、关注的方法。
有指定论文或笔记时用 `--source` 提供；没有也可以从方向开始。

```bash
uv run simple-ar start "调查低资源表格学习的近期路线。我只有一张24GB GPU。比较公开数据、代码、评估条件与成本，给出有出处的建议和待验证问题。" --allow-network
```

**查看：** 来源、路线比较、报告及未解决的问题。报告应区分原文事实和推断；
跨论文数字只有在条件可比时才适合直接比较。摘要或不可获取的全文不能当作完整阅读。

**继续：**

```bash
uv run simple-ar start --resume-task "SESSION" "补查最值得尝试的两条路线，比较数据许可与训练要求，并更新报告。"
```

网络授权用于公开资料搜索与获取，不授权安装或执行代码。
检索服务和密钥见[配置参考](CONFIG_REFERENCE_zh.md)。
需要预先固定查询、来源数或报告参数，可使用[结构化调研示例](../examples/survey/README.md)。

## 代码工作

**已有项目：** 给出项目路径、失败现象、预期行为，以及可用的验证方式；
不必预先指定要改哪些文件。

```bash
uv run simple-ar start "项目读取含缺失值的输入时崩溃。定位原因，修复相关代码并验证，交付修改说明。" --source ./my-project --allow-execution
```

**从零创建：** 描述输入、输出和约束。明确需要执行验证时同样加执行授权。

```bash
uv run simple-ar start "创建一个Python命令行程序，汇总目录内CSV的指定指标。处理缺列和空文件，提供示例、源码和可运行验证。" --allow-execution
```

`--source` 挂载原件只读；检查交付的工作副本或代码包，不要假定原项目已被修改。
系统提出进程命令后仍需逐条批准；审查代码和命令的副作用再执行。

**查看：** 源码或补丁、实际验证日志、结果及未解决问题。
模型审查与静态检查不能代替行为验证。反馈时说明仍失败的输入或新要求，沿原会话继续。

### 固定修改范围与验证

项目、编辑范围和独立验证命令已明确时，也可以留在普通任务会话中：

```bash
uv run simple-ar start --kind bug_fix --goal "修复缺失值导致的报错" --project ./my-project --allow "src/**" --validate "python tests/check.py"
```

执行前确认展示的工作和源码身份。此合同的代码修改与验证由 CodeTask 承担，
普通工作区写入或进程工具不能绕开范围。反馈可以基于已验证候选创建新版本，
继续使用原范围、验证器及剩余预算；原项目和前一候选不覆盖。
工作未验证时应说明未完成，不能用无关文件作为已完成修复交付。

需要明确基线、环境或分阶段批准控制时，仍可使用独立 CodeTask：

```bash
uv run simple-ar code-task init --config examples/code_task_medium_review/configs/code_task.toml
uv run simple-ar code-task execute "RUN" --config examples/code_task_medium_review/configs/code_task.toml
```

`RUN` 使用第一条命令打印的目录。遵循计划与补丁确认提示。
完整说明见[多文件修改案例](../examples/code_task_medium_review/README.md)；
工作区、环境、基线和执行设置见[配置参考](CONFIG_REFERENCE_zh.md)。

## 分析与数据图

**提供：** CSV/TSV/JSON 数据、字段解释，以及每行代表什么。
比较稳定性时说明重复运行、配对关系和开销单位；无法确定时让系统先澄清，不要猜测独立性。

```bash
uv run simple-ar start "分析这些运行结果，比较性能稳定性与时间开销。先检查字段和缺失值，再做一张多面板主图和结果说明；交付可复跑源码。" --source ./results --allow-execution
```

数据目录可包含数据字典或 README。使用 `--allow-vision` 并配置 vision 路由，
可以授权模型查看渲染图像；不启用时仍应自己打开图检查标签和布局。

**查看：** 分析方法、纳入与排除的行数、数值表、图及重建材料。
程序运行成功不证明统计假设合理；重点核对全量与子集、配对与独立样本是否混用。

**反馈：**

```bash
uv run simple-ar start --resume-task "SESSION" "保留计算结果，将主图改成双栏宽度，统一图例，并使颜色在灰度下仍可区分。"
```

### 已知列的无需 API 快捷路径

只需要确定性的描述统计或给定坐标绘图，可以直接运行现成 TOML：

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

见[坐标图](../examples/data-curves/README.md)和[配对观测](../examples/data-paired/README_zh.md)。
它们使用演示数据，不是科学实验。字段、缺失值和图形选项见[配置参考](CONFIG_REFERENCE_zh.md)。

这条路径的 `analysis.json`、数据副本和图应一并保留。移动整个分析目录后，
可在安装本项目的环境中重建：

```bash
uv run python -m simple_ar.result_analysis.table /path/to/analysis.json
```

自定义代码分析则按其交付的脚本与说明复跑，不套用上述分析包命令。

## 方法图与概念图

方法图应描述组件、关系和要表达的重点，而不只是说“画得好看”。
提供草稿可以帮助确定符号和术语：

```bash
uv run simple-ar start "根据草稿绘制方法概览图，区分训练和推理，保留可编辑布局源码并导出SVG和PNG。" --source ./draft.md --allow-execution
```

**查看：** 箭头、分组和标签是否符合真实方法；打开导出图检查遮挡、裁切与字号。
反馈沿原会话指定需要修改的对象。

Teaser 或概念插画使用独立的 Images 连接。配置 image 路由后：

```bash
uv run simple-ar image --prompt "为科研报告绘制一张概念插画，表达检索、实验和反馈循环，不含虚构性能数字。" --output runs/concept-v1
uv run simple-ar image --prompt "保留主要构图，减少背景元素，为标题留白。" --input runs/concept-v1/IMAGE_FILE --output runs/concept-v2
```

第二条中的 `IMAGE_FILE` 换成首次命令实际输出的图片文件。
生成与编辑均调用图像 API；原图保留在旧目录中。
位图不等于可编辑矢量图，生成的标注、结构和科学细节需要人工核对。

若希望同一原生任务调用图像生成，可显式给出 `--max-images N`。
图像连接、视觉检查和参数见[CLI 参考](CLI_REFERENCE_zh.md)。

## 写作与出版

**提供：** 论文、笔记、数据说明或已有成果；说明读者、体裁、语言和期望篇幅。
如果只需整理已有结果，就明确不重新实验。

```bash
uv run simple-ar start "根据现有材料写一份技术报告，组织问题、证据、比较与局限。保留引用和已有图，不重新运行实验，交付可编辑正文。" --source ./materials
```

**查看：** 正文、参考文献、图表与未完成项。特别核对作者、年份、数值、条件和引用归属。
材料中没有的实验不能写成已经完成的结果。

修改文章时说明是局部润色还是结构重组：

```bash
uv run simple-ar start --resume-task "SESSION" "保留初稿，将结构改为问题、路线比较、证据限制、建议；不要改变已有结果。"
```

### 导出 ACM LaTeX 与 PDF

导出器接收已装配的报告目录，其中有 `report_body.md` 和 `references.bib`；
不是任意 Markdown 转换器。

```bash
uv run simple-ar report-export --report-dir /path/to/assembled-report --output runs/paper-export --compile
```

需要 Pandoc；编译还需 `pdflatex`、BibTeX 和 `acmart`。
SVG 图形按需使用 `rsvg-convert`。不加 `--compile` 时交付 LaTeX 工程。
输出目录须为新目录；保留整个工程、图形与编译日志，逐页检查 PDF。
编译成功不代表符合所有会议要求。具体文件与依赖见[CLI 参考](CLI_REFERENCE_zh.md)。

### 使用提供的模板（V2.10 开发版）

原生任务可使用可信的 **Pandoc LaTeX 模板**替代默认 ACM 排版：

```bash
uv run simple-ar start "使用给定报告和可信模板，保留Markdown与参考文献，导出可编辑TeX工程和PDF并渲染各页供查看，说明未核查的投稿要求。" --source ./paper --allow-execution
```

材料目录只放稿件和所需模板文件，不挂载密钥或无关数据。
模板须含 `$body$`，并在导言区包含标准
`$for(header-includes)$$header-includes$$endfor$` 钩子。
这不是任意会议 TeX 的自动转换，也不自动发现官方模板。

共同出版工具 `export_report` 接受可选 `template`：`path` 相对 Markdown 所在目录，
`assets` 明确列出同目录平铺配套文件，`engine` 为 `pdflatex` 或已安装的 `xelatex`，
`top_level_division` 可选 `section`、`chapter`、`part`。
可让助手检查模板后选择正文层级；代码不按类名猜分支，也不因此重组科学内容。
配套文件最多32份、总计16 MB，不支持模板片段导入、脚本或自定义构建命令。
独立 `report-export` 命令仍保持 ACM 默认。

模板、图形字节及导出版本沿原审批和恢复合同。只使用可信 TeX；
禁用 shell escape 不等于安全沙箱。逐页检查并保留警告；字体、显式列宽、
页数和匿名要求仍需核查，不能把编译成功当合规认证。

对于用户明确给定的有限规则，`export_report` 还接受
`"checks": {"max_pages": 4, "forbidden_identifiers": ["Example Author", "某研究所"]}`。
两项可各自省略，但对象至少声明一项。`max_pages` 为正整数，统计 PDF 全部页面，
包括参考文献和附录。身份提示词为1–32个互不重复的非空白 literal 字符串，
每个最多200个 Unicode 字符，不含控制字符。按大小写敏感的精确子串匹配，
不做正则、归一化、跨空白拼接或 OCR；必须由用户给定，不能推断作者身份或会议规则。

检查要求 `compile_pdf=true`，使用已安装的 Poppler `pdfinfo` 读取页数，
和/或 `pdftotext -enc UTF-8` 提取文本；各自沿原批准 runner 和进程账本，
每工具最多30秒，PDF最多32 MB，页数信息最多64 KB，UTF-8文本最多1 MB。
不安装工具、字体或依赖。`export.json` 的 `checks` 分别记录页数和文本结果，
接受的输出保存在 `checks/pdfinfo.txt`、`checks/text.txt`，记录证据及受检 PDF 的哈希。
可沿这些文件与原进程回执审计。无可提取文本、替换字符、缺工具、超限、
输出截断或规则不通过均阻止可选 final delivery，保留工程和诊断；
不会自动修稿、删事实、隐去身份或增加编译尝试。

未命中仅表示给定 literal 未在提取文本出现，绝不认证匿名；图像、PDF元数据和
间接身份均未覆盖，视觉质量和官方合规仍未核实。
显式检查的成功和失败工程均沿原产物字节绑定；重读保存结果不重跑检查，
文件变化须保留旧版本并用新导出身份。未声明 checks 的请求、清单与历史绑定保持原样，
不为旧成果补造检查。独立 `report-export` CLI 不新增参数，该声明属于共同出版工具。

模型视觉检查还需配置 vision 连接并显式授予 `--allow-vision`；渲染本身不授予视觉访问。

## 有限复现

**提供：** 论文、目标结论、机器资源、已有实现和数据位置。
先要求调查条件、提出可承担的范围，再批准准备和执行。

```bash
uv run simple-ar start "检查论文的一项主要结论。先调查给定实现、数据与依赖，提出CPU可承担的范围和短运行方案，再经批准执行并解释差异。" --source ./paper.pdf --source ./implementation --allow-network --allow-execution
```

缺少实现、数据许可或资源时，可能需要用户参与准备，不保证从任意论文自动完成复现。

**查看：** 原论文条件、实际执行协议、环境与数据说明、短运行及测量日志、结果差异。
有限范围或合成改编必须明确标注，不能冒充原论文完整复现。

已有固定协议时，从[低成本结论检查案例](../examples/conformal_reproduction/README.md)开始。
结构化环境配置见[配置参考](CONFIG_REFERENCE_zh.md)；准备失败不能算正式测量。
虚拟环境提供依赖隔离，不是安全沙箱。

## 查看成果与继续任务

原生任务统一使用：

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "你的后续要求"
```

结构化 `research-session` 使用它打印的会话路径：

```bash
uv run simple-ar research-session --session-root "SESSION" --model env
```

保留整份交付目录，避免只复制 PDF 而丢失图表、来源与重建材料。
暂停或失败时先查看原因；批准、重试和续额规则见[CLI 参考](CLI_REFERENCE_zh.md)。
不要通过删除记录、反复新建任务来“恢复”旧工作。
