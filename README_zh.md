# SimpleAutoResearch

**持续科研助手：围绕同一任务组合资料、代码、数据、图形与可编辑报告。**

[English](README.md) · [开始使用](docs/USAGE_zh.md) · [任务指南](docs/WORKFLOWS_zh.md) · [成果展示](docs/showcase/README_zh.md) · [效果证据与限制](docs/EVALUATION_zh.md)

科研工作需要持续传递上下文：资料影响方法，代码落实计算，结果进入图表和正文，反馈又改变下一步。SimpleAutoResearch 将这些工作保存在可继续的任务中，让你检查证据、复用已有结果，并通过新指令推进工作。

你决定研究问题、资源和科学判断；助手在授权范围内调查材料、提出并调用工具，整理可供审阅的成果。

## 从你已有的工作开始

| 你的起点 | 可以提出的任务 | 可以检查的交付 |
| --- | --- | --- |
| 一个研究方向和约束 | 查找来源、比较路线，深入调查值得尝试的方案 | 来源引用、比较、报告和待解决问题 |
| 代码需求或已有项目 | 创建或修改代码、定位失败并验证行为 | 可编辑源码、修改、执行日志和未解决项 |
| 数据及字段说明 | 检查观测、分析结果并绘图 | 分析脚本、数值表、图形和解释 |
| 论文、实现和可用资源 | 确定并执行有限复现范围 | 实际协议、环境、测量与差异说明 |
| 已有结果、图形或初稿 | 修改图形、重组论证或装配报告 | 修订成果、保留的原版、引用及可选 ACM LaTeX/PDF |

这些能力可以在同一任务中组合。改图可以复用保存的数据，写作可以使用已有计算，缺少关键证据时可以回到调查。它们不是每项请求都必须走完的固定 pipeline。

## 看实际交付

可以先读完整报告，或直接复跑已交付图形。仓库包含源码、必要的保存结果和重建说明，不只是截图。以下是有人参与的开发案例，不是一键无人干预成果或 benchmark 成绩。

### 保存结果 → 分析、五面板图和报告

任务比较校准模型形式、更新策略、数据覆盖和记录的计算开销，经反馈修正图形与解释，交付六页 PDF、四张表、可编辑代码和冻结输入。报告阶段没有重新拟合模型。

![保存校准结果的五面板分析图](examples/saved-results-report/report/figures/monthly_update_panel.png)

[阅读报告](examples/saved-results-report/report/report.pdf) · [无需密钥复跑](examples/saved-results-report/README_zh.md) · [版本、人工参与和成本](docs/showcase/README_zh.md)

### 机器人方向 → 有出处的技术调研

十页报告比较 VLA 策略与世界模型的预测目标、控制角色和公开证据范围，包含 16 项引用、两张说明图、两张表及可编辑 Markdown/LaTeX。这是范围有限的阅读报告，不是完整的 2026 年综述。

![预测如何进入机器人控制](examples/robotics-survey/report/figures/fig1_prediction_in_loop.png)

[阅读调研 PDF](examples/robotics-survey/report/survey.pdf) · [中文简报](examples/robotics-survey/report/briefing_zh.md) · [重建报告](examples/robotics-survey/README_zh.md)

两份最终导出均在与 `d3b0721` 等价的代码上检查，该修复已纳入 main；完整任务历史跨越多个候选和显式续接，仍保留 `partial` 限定。正文与绘图代码由框架生成，人提供目标、反馈、授权和独立检查。[来源与开发成本](docs/showcase/README_zh.md)均公开说明。

### 不重做实验的图形反馈

较早的一项已核任务将保存结果的横版图改为配对视觉比较，保留八个显示值、单位与明确尺度。源码与数据已公开，可以不调用模型复跑。

![保存结果的反馈对比图](docs/showcase/assets/landscape-feedback-after.png)

[无需 API 密钥，使用源码和保存数据复跑](examples/figure-feedback/README_zh.md) · [前后对照与成本](docs/showcase/README_zh.md#横版比较图当前可复跑导出)

该有界反馈任务用 11 次模型请求、211774 记录 tokens，没有重拟合。[较早的 Bike Sharing 与方法图](docs/showcase/README_zh.md#较早成果)保留原版本说明；[效果证据与限制](docs/EVALUATION_zh.md)区分可用成果与科学、自动化主张。

## 一项任务怎样继续

1. **说明目标，提供材料。** 从问题、文件、数据或项目开始，给出影响选择的资源与约束。
2. **查看工作过程。** 助手从资料、代码、计算、图形和写作中选择下一步；保存的执行记录、模型用量和成果版本帮助你检查实际发生了什么。
3. **检查交付，提出反馈。** 打开打印的成果路径，核对证据和未解决项，再沿同一任务提出具体修改或补充材料。

命名 TOML profile 管理模型连接与用途路由。文本、代码、写作、视觉和图像生成可以使用不同后端；密钥通过环境变量或私有 `.env` 引用。配置方式见[模型配置参考](docs/CONFIG_REFERENCE_zh.md)。

## 快速开始

执行机器需要 Python 3.12+、Git、[uv](https://docs.astral.sh/uv/) 和 ripgrep（`rg`）。克隆 main 分支：

```bash
git clone --branch main https://github.com/MKC-ShenZhi/SimpleAutoResearch.git
cd SimpleAutoResearch
uv sync --locked
```

### 先试一个离线案例

```bash
uv run python examples/figure-feedback/build_figure_v2.py --output runs/examples/figure-feedback/replay
```

使用新的输出目录，打开其中的 `main.png` 或 `main.svg`。这会在保存结果上运行已交付源码，不需要密钥、联网或训练，也不是重新生成一次模型成果。完整图表与报告源码见[保存结果分析](examples/saved-results-report/README_zh.md)。

### 再配置模型，完成新任务

按[模型配置教程](docs/USAGE_zh.md)创建模型目录的 TOML 配置文件，定义名为 `daily` 的 profile，填写服务商实际端点、模型 ID 和凭据环境变量。教程包含默认文件位置、自定义路径和私有 `.env` 配置。

检查本地配置能否解析：

```bash
uv run simple-ar models --profile daily --purpose text
```

此命令检查本地配置与凭据是否可找到，不证明远端 API 可用。

### 用仓库已有材料启动任务

```bash
uv run simple-ar start "根据所给曲线数据与配置写一份简短 Markdown 说明。区分演示值与实测实验，列出证据限制并保留初版。" --source examples/data-curves --model profile:daily --yes
```

这是使用给定演示数据的模型辅助任务。`--yes` 接受展示的任务摘要。运行自己的任务时替换目标和材料路径；重复 `--source PATH` 可添加材料。

需要计算的数据分析任务可以请求执行工作：

```bash
uv run simple-ar start "分析这些结果。先检查字段和缺失值，再做一张多面板图和结果说明，交付可复跑源码。" --source ./results --model profile:daily --allow-execution
```

将 `./results` 换成执行机器上已存在的目录。`--allow-execution` 允许提出命令，每条命令仍需批准；需要获取公开来源时加 `--allow-network`。视觉、图像路由及其权限见[任务指南](docs/WORKFLOWS_zh.md)。

### 查看成果并反馈

将 `SESSION` 替换为终端打印的 `Task session` 路径：

```bash
uv run simple-ar results "SESSION"
uv run simple-ar start --resume-task "SESSION" "保留初版，增加一节缺失值处理与证据限制，交付独立反馈版本。"
uv run simple-ar results "SESSION"
```

打开 `results` 打印的实际路径，检查正文、来源、计算、验证状态和未解决项。反馈沿用保存的材料、连接、权限和预算；用 `--source PATH` 补充证据。批准、暂停与恢复方式见[开始使用](docs/USAGE_zh.md)。

### 可选：离线环境检查

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

此命令无需模型密钥，绘制给定的演示坐标。它检查确定性示例路径，不覆盖模型辅助的 `start` 任务。更多输入与依赖见[示例索引](examples/README.md)。

## 适用范围与审阅

项目目前为**源码可见预览**，原生 `start` 入口仍属实验性功能。当前重点是 Python 科研代码、常见表格数据、范围明确的调研与有限复现，以及有材料依据的报告。复杂环境准备、长篇调研、精细出版排版和稳定效率仍需改善。

来源原件只读挂载，你审阅命令提案并批准执行；任务工作区不是操作系统沙箱。科学解释与交付证据仍由你核验，执行或 PDF 编译成功本身不证明科学结论正确。模型、服务、数据和环境影响结果与成本；案例的具体条件见[成果展示](docs/showcase/README_zh.md)。

## 文档与贡献

| 你想做什么 | 阅读入口 |
| --- | --- |
| 安装、配置并完成第一个任务 | [开始使用](docs/USAGE_zh.md) |
| 选择输入并按任务配方操作 | [任务指南](docs/WORKFLOWS_zh.md) |
| 查询命令、批准与恢复 | [CLI 参考](docs/CLI_REFERENCE_zh.md) |
| 配置命名模型、路由和任务字段 | [配置参考](docs/CONFIG_REFERENCE_zh.md) |
| 查看展示成果及其条件 | [成果展示](docs/showcase/README_zh.md) |
| 理解代码与开发流程 | [开发指南](docs/DEVELOPMENT_zh.md) |
| 了解近期变化 | [更新记录](CHANGELOG_zh.md) |

欢迎提交可复现问题和脱敏诊断，请附目标、相关输入、命令、实际行为与预期结果。许可证尚未确定；代码贡献请先讨论，再提交 PR。

工作流设计参考了 [AutoResearchClaw](https://github.com/aiming-lab/AutoResearchClaw)、[OpenResearch](https://github.com/alphaXiv/OpenResearch) 和 [ARIS](https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep)。
