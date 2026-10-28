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

这些配方组合已有能力，不选择固定执行流程。一项任务可以查资料、改代码、执行计算，再写报告。
已有成果可以继续使用，无须为了写作重新运行实验。按需授予权限；
命令仍须批准，模型及其他 API 调用可能产生费用。

本页对应当前分支的命令行为。请让文档与安装版本保持一致；这些是任务请求示例，
不表示每个目标都已通过端到端验收。可查看已有案例的[证据与限制](showcase/README_zh.md#适用范围与当前限制)。
启动前[检查有效设置](USAGE_zh.md#配置一次围绕成果继续)：偏好指导交付，权限和预算控制访问及资源。

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

需要预先固定编辑范围、验证命令和基线时，参考[多文件修改案例](../examples/code_task_medium_review/README.md)，
并查阅 [CodeTask 配置](CONFIG_REFERENCE_zh.md)。普通代码任务不必先填写这些专家设置。

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

稿件未完成时，先查看任务状态及保留文件，再提出具体修改要求。候选稿不等于正式交付；
应核对本轮改动是否同时反映到正文和图表。反馈沿用原会话的材料、权限和剩余额度。

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

### 使用提供的模板

已有可信的 Pandoc LaTeX 模板时，将稿件、参考文献、图表和模板放入材料目录：

```bash
uv run simple-ar start "使用给定报告和可信模板，保留正文、参考文献与图表，导出可编辑TeX工程和PDF。说明未核查的投稿要求。" --source ./paper --allow-execution
```

模板格式、配套文件和编译依赖见[导出命令](CLI_REFERENCE_zh.md#simple-ar-report-export)。
这里只提供用户所需操作；自主查找会议模板及完整满足投稿要求仍未验收。
若需要准备模板，请提供官方入口、年份和投稿阶段，不要仅给会议简称。

**检查：** 逐页查看图表、图注、换行和字体，并核对页数及匿名要求。
编译成功不代表投稿合规。仅使用可信 TeX，执行隔离不等于安全沙箱。
模型看图另需配置 vision 路由并授予 `--allow-vision`。

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

按任务打印的续接方式操作。新原生任务和共同创建的结构化任务使用：

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "你的后续要求"
```

已保存的旧 ResearchApplication 会话保留原命令：

```bash
uv run simple-ar research-session --session-root "SESSION" --model env
```

要为已有成果设置新目标和预算，使用 `--from-session` 与 `--reuse`，见[CLI 参考](CLI_REFERENCE_zh.md)。
继续原任务使用 `--resume-task`；两种操作不要混用。

保留整份交付目录，避免只复制 PDF 而丢失图表、来源与重建材料。
暂停或失败时先查看原因，再按打印的命令继续；不要删除记录或反复新建任务来恢复旧工作。
