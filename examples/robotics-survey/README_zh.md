# VLA 与世界模型：一份框架生成的调研报告

[English](README.md) · [成果展示](../../docs/showcase/README_zh.md)

**可以直接阅读完整报告，也可以不调用模型重建可编辑源码。** 这份十页报告比较 VLA 策略与世界模型预测什么、预测如何进入控制，以及实验证据的适用范围，包含 16 项引用、两张说明图和两张表。

[阅读 PDF](report/survey.pdf) · [阅读 Markdown](report/manuscript.md) · [中文简报](report/briefing_zh.md)

![预测如何进入控制循环](report/figures/fig1_prediction_in_loop.png)

## 重建已交付报告

无需 API 密钥。下面重建的是已有正文，**不会重新检索或生成一份新综述**。先复制到新的工作目录：

```bash
mkdir -p runs/examples
cp -R examples/robotics-survey runs/examples/robotics-survey
cd runs/examples/robotics-survey/report
```

PowerShell 使用 `New-Item -ItemType Directory -Force runs/examples` 和 `Copy-Item examples/robotics-survey runs/examples/robotics-survey -Recurse`，再进入同一 `report` 子目录。不要复用以前运行的目标目录。

阅读 PDF 无需其他依赖。编译需要 Pandoc、pdfLaTeX、BibTeX 和 `survey.tex` 使用的宏包，包括 Latin Modern（`lmodern`）。Ubuntu 可安装 `pandoc`、`texlive-latex-extra`、`texlive-fonts-recommended` 和 `lmodern`。只编译可信输入；TeX 不是沙箱。

使用已有 PNG 图片重编译交付的 TeX：

```bash
pdflatex -no-shell-escape -halt-on-error survey.tex
bibtex survey
pdflatex -no-shell-escape -halt-on-error survey.tex
pdflatex -no-shell-escape -halt-on-error survey.tex
```

任一步失败就停止。若修改了 `manuscript.md`，先重新生成 TeX：

```bash
pandoc manuscript.md -s --natbib --bibliography=references.bib -V biblio-style=plainnat -V geometry:margin=2.2cm -V fontsize=10pt -V linkcolor=blue -V urlcolor=blue -V colorlinks=true --metadata lang=en -o survey.tex
```

然后再次执行上面的编译命令。可选的图形重绘只需 Python 标准库和 `rsvg-convert`（Ubuntu 包 `librsvg2-bin`）：

```bash
python3 make_figures.py
```

已检查 Linux 重建，未认证其他平台或 PDF 字节完全相同；TeX、Pandoc、字体和时间戳都可能影响导出文件。

## 尝试自己的调查

按[入门教程](../../docs/USAGE_zh.md)配置模型，资料获取方式见[任务指南](../../docs/WORKFLOWS_zh.md)。可以先请求一个范围明确的技术备忘录：

```bash
uv run simple-ar start "为机器人读书组比较VLA策略与世界模型，区分预测目标、预测进入控制的方式，以及真机和模拟证据。先交付简短技术备忘录、来源链接和比较表；说明全文缺口，不要编造跨论文性能排名。" --model profile:daily --allow-network --max-turns 20 --max-tokens 400000
```

这会启动新的付费调查，不是原报告重放，也不保证十页交付。用 `--source PATH` 添加可访问材料。代码执行、视觉检查和 PDF 编译另需相应授权与依赖，联网权限不等于这些权限。建议先审阅短稿，再要求扩展。

## 来源、成本与限制

正文、图形、书目和 TeX 均为框架原样导出。维护者筛选公开文件并编写本复跑教程；原编译日志、原工作区专用辅助脚本、论文全文快照和任务日志不随本例公开。源文件哈希见 `provenance.json`。

最后交付使用与 `d3b0721` 等价的代码；历史包含更早候选、连接切换、人工反馈和显式续接。人提供目标、授权、环境依赖和独立检查，没有代写科学正文或绘图代码。全阶段累计 **164 次已结算物理请求、6,086,900 个已结算 tokens**，未知用量另留。这是开发过程成本，不是无人干预的一次运行成绩或金额账单；最后写作用官方 `deepseek-flash`、低思考、流式。

原任务仍为 `partial`：16 篇材料是机会性选集，不是系统综述或完整的 2026 年调研；只有两篇来自 2026 年。PlayWorld 已取得全文，但正文仅使用摘要级信息。部分作者名单未核实，采用研究名称引用。图形是定性阅读辅助，不是新实验或可横向排名的性能图。最终十页另经独立逐页查看与搬迁编译，但没有完整的自动视觉逐页签收。

项目许可证尚未确定；使用前核对上游权利和原论文论断。本例不包含第三方论文全文。
