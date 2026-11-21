# 常见科研任务配方

这里整理了 5 类最常见科研场景的即用命令模板。所有命令均使用同一个 `simple-ar start` 入口，按需组合权限即可；如果不习惯命令行，也可以直接在[桌面研究工作台](USAGE_zh.md#桌面研究工作台)中勾选对应开关。

[English](WORKFLOWS.md) · [开始使用](USAGE_zh.md) · [CLI 参考](CLI_REFERENCE_zh.md) · [成果展示](showcase/README_zh.md)

---

## 1. 公开文献调研与综述（联网检索 + BibTeX + PDF）

```bash
uv run simple-ar start \
  "调研大语言模型测试时计算扩展与过程奖励模型（PRM）路线对比：联网检索 8-12 篇真实论文并生成可核验的 references.bib，撰写带结构化对比表的中文综述并导出 XeLaTeX PDF。" \
  --model profile:daily --allow-network --allow-execution --yes
```

- 已有本地 PDF 论文时，去掉 `--allow-network`、改为 `--source ./papers` 即可做纯离线精读对比。

---

## 2. 科研代码实验、消融与绘图

```bash
uv run simple-ar start \
  "基于给定项目复现基线性能，设计并执行至少 3 组对照/消融实验，生成高分辨率结果对比图（PNG）并撰写包含量化表格与局限性的报告。" \
  --source ./my_project --model profile:daily --allow-execution --yes
```

- 在 Linux 服务器上若已安装 `bubblewrap`（`bwrap`），建议追加 `--sandbox-execution`，在隔离工作区内自动批准常规执行命令。

---

## 3. 已有数据/结果的分析与出版级绘图（不重做实验）

```bash
uv run simple-ar start \
  "基于保存的评估表完成量化分析，生成两张高分辨率对比图（PNG）与分析报告；不要重新训练或拟合模型，交付可复跑绘图脚本。" \
  --source ./saved_results --model profile:daily --allow-execution --yes
```

---

## 4. 使用官方会议模板导出英文论文 PDF（如 NeurIPS / EMNLP）

将官方 `.sty` / `.cls` 模板放入一个目录（如 `./neurips_template`）并通过 `--source` 只读挂载：

```bash
uv run simple-ar start \
  "Conduct a controlled study on this dataset, generate publication figures, write an English paper with verifiable BibTeX citations, and compile a PDF using the supplied neurips_2025.sty template." \
  --source ./neurips_template --source ./data \
  --model profile:daily --allow-network --allow-execution --yes
```

---

## 5. 定向反馈迭代、跨项目复用与预算续跑

```bash
# 查看会话交付物、账本与未解决项
uv run simple-ar results "SESSION_PATH"

# 沿原会话提反馈改图/改稿（保留初版，不重做无关计算）
uv run simple-ar start --resume-task "SESSION_PATH" \
  "保留初版 fig_v1.png，新增左右配对布局的 fig_v2.png 并在柱顶标注精确数值，同步更新报告解读。"

# 跨项目/跨数据集复用前序已交付的代码库与 SKILL.md 方法
uv run simple-ar start --from-session "SESSION_PATH" --reuse artifacts \
  --source ./new_dataset --model profile:daily --allow-execution \
  "复用前序会话交付的代码与方法，在新数据集上运行对比实验并总结结论迁移情况。"

# 预算触顶或网络瞬断后的无损续跑
uv run simple-ar start --resume-task "SESSION_PATH" \
  --authorize-remaining llm_requests=20 --authorize-remaining total_tokens=1000000 \
  --authorization-id continue-1 --authorization-reason "完成最终复核与交付" --yes
uv run simple-ar start --resume-task "SESSION_PATH" --retry-model --yes
```
