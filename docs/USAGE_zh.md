# 开始使用

[English version](USAGE.md)

本教程带你完成安装、模型配置、本地材料调查、成果检查，以及同一任务的反馈与续跑。
具体任务见[任务指南](WORKFLOWS_zh.md)；
选项与字段见 [CLI 参考](CLI_REFERENCE_zh.md)和[配置参考](CONFIG_REFERENCE_zh.md)。

## 第一个任务

示例使用仓库内可直接取得的数据曲线材料。
先请求说明，需要计算时再提出进程。
原生 `start` 入口仍属实验性功能；使用成果前应检查正文与证据限制。

### 安装

执行主机需要 Python 3.12+、Git、uv 和 ripgrep（`rg`）。
使用文档所列预览分支；远端已发布文件可能与本地未发布改动不同。
在终端执行：

```bash
git clone --branch feat/v2.9-task-driven-research https://github.com/Wchanging/SimpleAutoResearch.git
cd SimpleAutoResearch
uv sync
uv run simple-ar start --help
rg --version
```

后续示例都从仓库目录执行。
Windows 同样使用 `uv run simple-ar` 入口。

### 配置模型连接

将以下内容保存为 `~/.config/simple-ar/models.toml`，或使用下文变量指定其他位置。
替换端点和模型 ID 为服务商实际支持的值。
原生任务需要 Chat 连接；`code` 路由供后续代码工作使用。

```toml
version = 1

[profiles.daily]
api = "openai_chat"
base_url = "https://your-provider.example/v1"
model = "your_model_id"
api_key_env = "RESEARCH_API_KEY"
capabilities = ["text", "code"]
stream = true

[routes]
default = "daily"
code = "daily"
```

此示例启用 Chat 流式输出。请确认服务商支持所选模型与能力；
输出上限、超时等设置可在模型目录中配置。

在命令当前目录创建或编辑私有 `.env`，保留已有条目。
加入引用凭据的实际值：

```dotenv
RESEARCH_API_KEY=your_actual_secret
```

默认位置中的 `~` 是当前用户目录；Windows 也使用用户目录下的 `.config/simple-ar/`。
如果文件放在其他位置，再设置配置文件路径变量，也可将该变量写入 `.env`：

```bash
export SIMPLE_AR_MODELS_CONFIG=/absolute/private/path/models.toml
```

```powershell
$env:SIMPLE_AR_MODELS_CONFIG = 'C:/private/models.toml'
```

凭据来自环境或当前目录的 `.env`，已有环境变量优先。
两个文件都应保密，不作为来源挂载。
换终端或目录时，确保仍可找到配置与凭据；无需重复设置全局模型和端点。

本地检查所选连接：

```bash
uv run simple-ar models --profile daily --purpose text
uv run simple-ar models --profile daily --purpose code
```

`credential=set` 只表示找到凭据，不证明远端 API 可用。
可选 writing、vision、image 路由、传输设置及旧环境变量回退见
[命名连接](CONFIG_REFERENCE_zh.md#命名模型连接)。
有目录时，`--model env` 使用 text/default 路由。

### 提供可访问的材料

仓库中的数据曲线示例包含 README、TOML 和 CSV：

```bash
uv run simple-ar start "根据所给曲线数据与配置写一份简短 Markdown 说明。区分演示值与实测实验，列出证据限制并保留初版。" --source examples/data-curves --model profile:daily --yes
```

自己的任务可将 `examples/data-curves` 换成执行主机上的真实文件或目录。
重复 `--source PATH` 可添加材料。
挂载原件只读；目标和选中的材料内容会发送给模型。
不要挂载凭据、私有配置或无关数据。

### 从一个任务开始

`--yes` 接受展示的任务摘要。
上面的示例未授予公开来源获取或进程提案权限；按需要追加：

| 需要 | 原生选项 | 边界 |
| --- | --- | --- |
| 公开检索与来源获取 | `--allow-network` | 不授权安装或命令 |
| 提出计算等进程 | `--allow-execution` | 每条命令仍须批准 |
| 检查渲染图像 | `--allow-vision` | 将图像发送给配置的 vision 路由 |
| 生成或编辑栅格概念图 | `--max-images N` | 需要 image 路由，额度保留 |

反馈与恢复沿用访问权限和预算。
`--max-turns`、`--max-tokens` 限制模型工作；剩余请求次数不保证下一次请求能放入 token 余额。
具体限额见[原生入口](CLI_REFERENCE_zh.md#原生任务入口实验性)。

需要进程时，用打印的 `Task session` 路径续跑：

```bash
uv run simple-ar start --resume-task "SESSION" "计算所需数值，先提出命令并保留原说明。" --allow-execution --yes
```

在终端批准前，检查提案命令、源码、路径和副作用。
没有交互批准或拒绝时，任务暂停并打印 `Approval token`。
审核后使用当前精确 token：

```bash
uv run simple-ar start --resume-task "SESSION" --approve-execution "TOKEN"
```

`SESSION`、`TOKEN` 是占位符；每条新命令可能需要新批准。
工作区隔离不是操作系统沙箱。

### 检查成果

```bash
uv run simple-ar status "SESSION"
uv run simple-ar results "SESSION"
```

打开 `results` 打印的实际路径。
按目标检查正文、来源引用、计算结果、验证状态及未解决项。
文件或交付记录存在不认证质量。
暂停时，旧交付可能属于当前反馈之前的请求。

进一步检查产物：

```bash
uv run simple-ar inspect "SESSION"
uv run simple-ar search-artifacts "SESSION" "evidence"
```

### 反馈与恢复

记录初版路径，再请求独立反馈版本：

```bash
uv run simple-ar start --resume-task "SESSION" "保留初版，增加一节缺失值处理与证据限制，交付独立反馈版本。"
uv run simple-ar results "SESSION"
```

反馈可继续调用模型或提出命令，沿用同一预算、材料、连接及批准规则。
在同一任务补充证据时，在反馈命令中加入 `--source PATH`。

不提交新反馈，只恢复保存边界：

```bash
uv run simple-ar start --resume-task "SESSION"
```

已完成且无待处理动作的任务返回保存终态。
中断后先确认旧工作进程已停止；在途操作或未知副作用不能盲目重放。
符合条件的已终止模型失败，可单独使用 `--resume-task SESSION --retry-model`，
不要同时提交反馈或新授权。失败费用保留，并使用剩余预算；
见[恢复条件](CLI_REFERENCE_zh.md#原生任务入口实验性)。
追加额度与更多恢复选项见 CLI 参考。

## 选择任务与最小输入

[六项工作流配方](WORKFLOWS_zh.md)覆盖调研、代码、数据、写作、复现和导出。
`start --guided` 提供原生成果偏好。

需要固定表格列、验证命令或复现协议时，可使用结构化配置。
它与普通任务的恢复命令不同，详见[CLI 参考](CLI_REFERENCE_zh.md)。

首次无 API 凭据计算：

```bash
uv run simple-ar research-session --config examples/data-curves/research.toml
```

该案例绘制已有演示坐标，不是实验。
原生 `--prepare-only` 仍检查连接与凭据，但不调用模型或进程；
结构化准备可在无凭据时保存 TOML。

## 排错

| 现象 | 检查方法 |
| --- | --- |
| 找不到 profile/key 或端点错误 | 执行 `models`，检查目录路径、`api_key_env`、当前目录和环境变量优先级 |
| 服务商超时或断流 | 查看保存的用量及完成诊断；命名 profile 使用自身超时/流式/重试设置，不用旧 `SIMPLE_AR_LLM_*` 设置 |
| 本机联网但执行主机不通 | 在执行主机配置可达代理；它的 `127.0.0.1` 指向它自己 |
| 任务停在命令批准 | 检查当前提案，批准其精确 token |
| 恢复拒绝在途或未知操作 | 确认旧进程结束，按对应入口的恢复规则处理 |
| 图形中文等字符缺失 | 安装适合字体并配置 Matplotlib 的 `font.family`，检查 PNG/PDF 标签 |
| `uv run` 报 cache 权限 | 修复本地缓存权限，或在 Windows 使用 `.\.venv\Scripts\simple-ar.exe` |

传输超时不是整项任务截止时间，重试可能延长总耗时。
排错时保留会话和用量记录。
固定代码任务与验证见[代码配方](WORKFLOWS_zh.md#代码工作)。
