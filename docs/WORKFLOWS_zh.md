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

标注 V2.10 的部分描述 `feat/v2.10-project-research-assistant` 上的开发工作，
不代表完整验收或正式发布。使用 main 的[安装方式](USAGE_zh.md#安装)，
并查看各案例的[证据与限制](showcase/README_zh.md#适用范围与当前限制)。
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

开发分支的项目工作还需检查保留清单。当前边界为最多编辑 12 份源码、保留 4,096 项，
单文件 64 MiB、总计 128 MiB；超量项目在生成前拒绝。未改的非运行时成员保留原字节，
包括二进制／大依赖，但不代表它们已被检查或已找到缺失依赖。
普通、已复制及已登记反馈项目均适用。审批和执行沿同一绑定清单，无需把各成员列为独立公开进程输入。
这不复制任务历史，整体项目持续性仍未完成验收。

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

### 继续保留的稿件（V2.10 开发中）

稿件没有通过检查时，可要求继续完成保留的最新候选。写作模块核对作者记录和原依据：检查未完成的，补完检查；已有完整修改意见的，直接据此修订，再核查修改结果，不退回旧稿重写。

工作候选与正式采用稿分开保存。即使某处已改好，仍须解决其余必改意见才能正式交付和出版。旧稿、候选、检查及用量记录均保留；缺少必要生产记录的旧稿不能自动获得这种续用资格。

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

V2.10 开发分支的原生任务可使用可信的 **Pandoc LaTeX 模板**替代默认 ACM 排版：

```bash
uv run simple-ar start "使用给定报告和可信模板，保留Markdown与参考文献，导出可编辑TeX工程和PDF并渲染各页供查看，说明未核查的投稿要求。" --source ./paper --allow-execution
```

材料目录只放稿件和所需模板文件，不挂载密钥或无关数据。
模板须含 `$body$`，并在导言区包含标准
`$for(header-includes)$$header-includes$$endfor$` 钩子。
渲染器接收已准备的可信模板。自主发现模板及满足全部会议要求仍需核查，
不要将成功编译当作投稿合规。

新任务若需准备模板，应给出官方入口、年份和阶段（初次匿名投稿或终稿），授权所需网络与执行。
任务可组合研究和代码能力获取规则、适配 Pandoc wrapper；这是待验证的请求，不是已验收流程：

```bash
uv run simple-ar start "按这个官方入口及其链接准备2025年初次匿名投稿的排版：https://iclr.cc/Conferences/2025/CallForPapers 。转换给定稿件，保留原文、引用、图表和数值；保存模板与规则来源及适配说明，交付可编辑工程、PDF和逐页预览。规则不满足时明确列出，不改科学内容凑合规。不重做实验，不安装环境。" --source ./paper --allow-network --allow-execution
```

准备模板时，请助手：

1. 核对官方年份和阶段，保留规则及模板作为材料。
2. 直接使用已有 Pandoc 模板，或将原生会议 TeX 适配为 wrapper。
   适配处理变量、包和布局，无需用户选择内部工具或修改任务 JSON。
   授权目录中的文件可按已观察到的完整 hash 复制。
3. 获取失败时保留失败原因，请求缺少的模板和规则，不猜其他年份或静默改用 ACM。

这由研究、代码和文件能力组合完成，渲染器不发现或下载模板；普通入口自主完成仍未验证。
原稿 Abstract／Summary 等章节保留在正文，不因标题自动填入 `$abstract$` 元数据。

独立 `report-export` 命令默认 ACM，也支持显式可信模板参数，见[导出选项](CLI_REFERENCE_zh.md#simple-ar-report-export)。
原生任务中说明所需标题层级，并提供精简模板包（最多 32 份平铺配套文件、共 16 MB）。
不支持模板片段导入、脚本和自定义构建命令。Abstract／Summary 标题保留在正文，
不自动填入模板元数据。

模板、图形字节及导出版本沿原审批和恢复合同。只使用可信 TeX；
禁用 shell escape 不等于安全沙箱。逐页检查并保留警告；字体、显式列宽、
页数和匿名要求仍需核查，不能把编译成功当合规认证。

需要页数或身份字符串检查时，在请求中提供精确规则。检查需要编译 PDF 并安装 Poppler 工具。
查看 `export.json` 与保存的诊断：失败或无法检查时保留工程，但阻止可选最终交付。
有限检查不认证匿名或会议合规。在任务中说明需求即可，普通使用无需填写底层工具参数。

**检查：** 逐页查看 PDF、警告、图和图注是否容纳、表格换行、字体、列宽，以及指定年份和阶段的规则。
不支持的复合／嵌套图片或无法容纳的图注会明确失败并保留源码。
给定图注与导出图片保持绑定；科学正文、编号、链接及引用须保留。
历史文稿仍使用保存时的图注规则。

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

按任务打印的续接方式操作。新原生任务和共同创建的结构化任务使用：

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "你的后续要求"
```

已保存的旧 ResearchApplication 会话保留原命令：

```bash
uv run simple-ar research-session --session-root "SESSION" --model env
```

### 在新任务复用材料（V2.10 开发中）

要为已有成果设置新目标和预算，在现有原生 TOML 的 `[task]` 表中加入 `from_session = "../saved-task"`、`reuse = ["artifacts"]`。也可选 `simple-ar results` 列出的成果组；组名不是文件路径，会话路径相对配置文件。复用核对材料字节，不继承原权限、额度或执行状态。继续原任务则用 `--resume-task`。

### 加入可信本地扩展（V2.10 开发中）

在原生 `[task]` 表中加入 `extensions = ["../my-skill"]`，用 `--task-config PATH` 启动。
只使用可信包：Python 拥有宿主 OS 权限，只读／计划声明不是沙箱；包缺失或变化会阻止继续。
设置、描述文件与恢复规则见[可信本地扩展](CONFIG_REFERENCE_zh.md#可信本地扩展)，
示例见[描述文件](../examples/local_extension/extension.toml)及[Python 工具](../examples/local_extension/notes.py)。

保留整份交付目录，避免只复制 PDF 而丢失图表、来源与重建材料。
暂停或失败时先查看原因；批准、重试和续额规则见[CLI 参考](CLI_REFERENCE_zh.md)。
不要通过删除记录、反复新建任务来“恢复”旧工作。
