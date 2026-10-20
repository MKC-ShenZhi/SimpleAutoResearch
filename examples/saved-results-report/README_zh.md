# 从保存结果到分析图与报告

[English](README.md) · [成果展示](../../docs/showcase/README_zh.md)

**不需要 API 密钥，也不需要训练模型。** 这里提供框架实际生成的六页报告、五面板图、四张表、绘图源码和五份冻结结果文件。

[阅读 PDF](report/report.pdf) · [阅读正文](report/manuscript.md) · [查看主图](report/figures/monthly_update_panel.png)

## 复跑分析

在仓库根目录安装锁定依赖，再把案例复制到新的输出目录。原始生成脚本会写入自身所在目录，因此不要直接在案例原件上运行。

```bash
uv sync --locked
```

Linux/macOS：

```bash
mkdir -p runs/examples
cp -R examples/saved-results-report runs/examples/saved-results-report
uv run python runs/examples/saved-results-report/report/make_figure_tables_v2.py
```

PowerShell：

```powershell
New-Item -ItemType Directory -Force runs/examples | Out-Null
Copy-Item examples/saved-results-report runs/examples/saved-results-report -Recurse
uv run python runs/examples/saved-results-report/report/make_figure_tables_v2.py
```

每次使用新的目标目录。打开副本下的 `report/figures/monthly_update_panel.png`；脚本还会生成四份 CSV 表和 `report/derived_metrics.json`，一致性检查失败会非零退出。
这只是从保存结果重新汇总，**不是重新开展实验**。已检查 Linux/Python 3.12 复跑，未认证其他平台或字体环境。

### 可选：重建 PDF

直接阅读现有 PDF 不需要 TeX。若要编译可编辑工程，先安装 pdfLaTeX 及 `report/latex/main.tex` 使用的宏包，再执行：

```bash
cd runs/examples/saved-results-report/report/latex
pdflatex -no-shell-escape -halt-on-error main.tex
pdflatex -no-shell-escape -halt-on-error main.tex
```

得到 `main.pdf`。这条路径重编译交付的 TeX，不会从 Markdown 重新生成正文，也不会自动纳入刚修改的绘图脚本。需要协调改图和改正文时，请新建模型辅助任务。

## 让助手完成新任务

按[入门教程](../../docs/USAGE_zh.md)配置模型后，从仓库根目录执行：

```bash
uv run simple-ar start "检查这些保存的校准结果，比较模型形式、更新策略、覆盖情况和记录的计算开销。交付多面板图、可编辑源码与简短Markdown报告。不得重新拟合；区分实测计时和年度折算情景，不得虚构应用领域。" --source examples/saved-results-report/report/inputs --model profile:daily --allow-execution --max-turns 20 --max-tokens 400000
```

这会启动**新的付费任务**，不是原对话重放，也不保证得到相同报告。审阅命令后再批准执行；保存终端打印的任务路径，用于[反馈和恢复](../../docs/USAGE_zh.md)。额度是上限，不是费用估计，也不保证额度内一定完成。

## 来源、成本与限制

科学正文、绘图源码、数值和 TeX 均为原生框架任务的原样导出；维护者只筛选公开文件、编写本教程。最终交付使用与 `d3b0721` 等价的代码；完整开发过程跨越早期候选和多个模型连接。人提供目标、缺陷反馈、授权、环境准备和独立检查，没有代写科学正文或绘图实现。

完整开发历史累计 **180 次已结算物理请求、5,871,880 个已结算 tokens**，未知用量另留。这不是一次成功运行的成本，也不是金额账单。最后写作用官方 `deepseek-flash`、低思考、流式；离线复跑不调用模型。

独立核查覆盖 72 项数值条件及搬迁重建，最终六页另经逐页查看。原任务仍为 `partial`：没有区间估计、只有一个测试季、计时依赖机器、TeX 有少量溢出警告，最终图没有成功完成新一轮付费视觉复核。输入也不足以确定物理应用领域或目标单位。

**计时范围勘误：** 原报告称特征构造等工作不在计时内，这一表述过窄。[保存记录](report/inputs/recorded_results.json)的 `timing_scope` 明确说明，拟合计时包含流水线构造、预处理和索引，预测计时包含批次索引与变换；不包含导入、磁盘、绘图和调参。各月中位数之和也不是整条工作流的一次实测耗时。这里保留原始生成稿，不手工改写报告或 PDF；此前数值与构建检查不等于全文语义认证。

`provenance.json` 记录所选文件与原交付的对应关系。原失败和账本私下保留，不随本例公开；元数据中的历史环境路径不是运行依赖。本例不包含第三方论文全文、密钥或任务日志。项目许可证仍未确定，不额外授予上游材料使用权。
