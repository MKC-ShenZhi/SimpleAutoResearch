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
使用 main 分支。
在终端执行：

```bash
git clone --branch main https://github.com/Wchanging/SimpleAutoResearch.git
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
无目录时，原生任务须在环境或当前目录 `.env` 设置 `SIMPLE_AR_LLM_API=chat`；
旧连接默认是 `responses`。Responses 不支持流式，结构化任务也不例外。

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
原生任务暂停时，`results` 不列出当前可复用交付；磁盘上仍可能保留反馈之前的旧文件。

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

## 配置一次，围绕成果继续

可以先生成带关键注释的原生任务配置，编辑后启动。生成配置不需要模型凭据，也不创建任务：

```bash
uv run simple-ar start "检查这些实验结果，解释稳定性与开销，按需要制作主图和结果说明" --source ./my-results --model profile:daily --max-processes 16 --max-process-seconds 600 --write-task-config task.toml
uv run simple-ar start --task-config task.toml
```

配置中的相对材料路径以配置文件所在目录为准，材料必须能由执行机器读取。
显式命令行值覆盖文件中的同名设置。确认摘要后输入 `y` 开始；模型和工具按目标选择工作。
`max_processes` 和 `max_process_seconds` 是进入实际账本的累计进程次数和累计墙钟秒数，
包括搜索后端；不是单命令超时。零次数禁止进程，允许提案仍需批准具体命令。
自然语言中的资源建议不自动成为硬限制，请核对摘要与这些显式设置。

查找已有任务和反馈时，无需先寻找内部 JSON：

```bash
uv run simple-ar start --tasks-in runs/assistant
uv run simple-ar start --tasks-in runs/assistant --select-task
uv run simple-ar start --tasks-in runs/assistant --resume-task EXACT_DIRECTORY_NAME "保留测量结果，调整主图并更新说明"
```

列表只读所给目录及其直接子目录，最多检查256项，不建立额外任务索引。交互选择需要终端；脚本使用列表中的精确目录名。
显示的 `partial` 保留未完成项；记录状态不代表成果质量已认证。
反馈沿原任务、材料和账本继续；再次用配置文件启动则会创建新任务。
继续时不能用创建参数悄悄重设进程额度，额外资源仍使用显式续额授权。

需要在没有待处理操作时更换工作连接，可显式记录变更，再单独提交反馈：

```bash
uv run simple-ar start --resume-task SESSION --switch-model profile:another --switch-id connection-1 --switch-reason "使用另一已配置服务"
uv run simple-ar start --resume-task SESSION "继续修改已有报告"
```

先核对显示的任务、code、writing端点与模型，确认后才保存。变更不调用模型、重跑工具或清空账本；已交付文件仍保留。
同一变更ID只能对应同一意图，重复旧ID不会撤销后来变更。任务有待处理/在途/未消费回复时先按原规则恢复；
已终止的失败模型决策仍使用 `--retry-model --model profile:another`，不要通过改连接规避未确认操作。

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

## 本地工作区预览（V2.10 分支）

开发分支提供仅绑定本机回环地址的薄浏览器界面：

```bash
uv run python -m simple_ar.ui --project-root /absolute/materials --task-root /absolute/native-tasks
```

从包含私有 `.env` 的目录启动，沿用全局模型目录。打开本进程打印的完整链接；
链接片段是本次本地会话的访问凭据，请勿分享或公开端口。
远程服务器使用 SSH 转发相同端口，不修改绑定地址。

页面列出任务根目录及其直接子目录中的原生任务，支持中文等非 ASCII 目录名。材料路径属于执行主机，
必须在项目根目录或显式重复指定的 `--read-root PATH` 内。
创建只保存已确认的连接、材料、权限和硬上限，点“继续执行”才开始工作。
界面明确区分保存成果浏览与本界面的实时工作线程；
反馈和精确命令批准沿原任务及账本继续。
“边界停止”等当前行动返回，不表示提供方请求或进程已取消；
请保持启动进程，直到它正常退出。

这是实验性单用户客户端，不是沙箱或多用户服务。支持文本及位图预览，
PDF 等支持的二进制文件下载后查看。浏览文件不认证科研质量。
可信用户 Pandoc 模板转换已可通过[出版导出](WORKFLOWS_zh.md)使用；
任意出版方模板适配及完整实时交互用户验收仍待完成。

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
