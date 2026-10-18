# 保存结果的图形反馈示例

[English](README.md) · [成果展示](../../docs/showcase/README_zh.md) · [效果证据与限制](../../docs/EVALUATION_zh.md)

这是**真实框架任务的可复跑导出**，不是新 benchmark 或模型训练示例。绘图源码、五份保存结果和图注均保持原交付字节。复跑不需要密钥、联网、下载原始观测或训练。

## 复跑已交付图形

在仓库根目录完成 `uv sync --locked` 后执行：

```bash
uv run python examples/figure-feedback/build_figure_v2.py --output runs/examples/figure-feedback/replay
```

每次使用新的输出目录；原脚本拒绝覆盖非空目录。得到 `main.svg`、`main.png`、`preview.png`、`panel_D_review.png` 和 `audit.json`。Matplotlib 已是核心依赖。已验收 Linux/Python 3.12，脚本单线程，并在支持时限制 120 CPU 秒/2 GiB 地址空间；这不是操作系统沙箱，也不是 Windows/macOS 认证。

![交付图形](../../docs/showcase/assets/landscape-feedback-after.png)

脚本核输入哈希及 354 项保存值/布局条件；独立验收另核 8 个显示值，并在相同环境搬迁后得到字节一致的两张 PNG。换字体或 Matplotlib 版本不保证像素一致。

## 尝试新的模型任务

先按[入门](../../docs/USAGE_zh.md)配置文本与 vision 连接。下面会启动有独立额度的付费任务，不是原模型对话的重放：

```bash
uv run simple-ar start "根据保存结果和初版图制作可编辑灰度横版图，让两类模型的 Frozen 与 Monthly 两指标真正可视比较；保留精确数值、单位，明确不同尺度和拟合/预测/评价角色，交付源码、SVG、PNG、图注及复跑说明。不得重新拟合或获取数据。" --source examples/figure-feedback/data --source docs/showcase/assets/landscape-feedback-before.png --model profile:daily --allow-execution --allow-vision
```

只批准已审阅的命令；文本连接本身不授权视觉。质量、成本、次数可能不同。后续沿[反馈与恢复](../../docs/USAGE_zh.md#反馈与恢复)继续自己的任务。

## 来源与边界

原反馈任务使用 `f45b596`、CCTQ/`gpt-6.1-sol`，11 次物理请求、211774 记录 tokens、一次渲染约 2.56 秒，无新 unknown 或续额。人提供反馈、批准执行和独立检查，没有代改交付源码或正文。更早失败的反馈任务仍保留失败和费用；本次独立有界任务只复用导出的初版材料，不继承其账本。

原观测来自 [Saverio Vito 的 UCI Air Quality](https://doi.org/10.24432/C59K5F)；本例只有保存的派生校准结果，不是重新独立验证。请引用数据集并核对上游使用条款。保存元数据含历史环境路径，仅是来源记录，不是依赖或凭据。

不声称新拟合、科学再验证、外站点性能或物理印样通过。两类模型使用明确不同的横轴尺度。原图注仍写 Left to right，但 A–C 实为左侧纵排，作为已知措辞瑕疵保留；原生 `partial` 不重标。项目许可证尚未选定，本示例不授予第三方材料的无限制使用权。
