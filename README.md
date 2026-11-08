# yhShell

一个用 **pywebview + xterm.js** 写的桌面 SSH 客户端，终端旁边内置一个可以自定义模型的 **AI 运维 Agent**：你描述要做什么，它提出命令，你一键确认后它就在远端执行，并把输出喂回模型继续推理。

![终端与 AI 助手](docs/screenshots/agent-answer.png)

## 功能

- **真正的交互式终端**：paramiko PTY + xterm.js，支持 vim / top / htop、窗口尺寸同步、中文与 UTF-8、复制粘贴、右键菜单、多标签会话。
- **主机管理**：保存主机、密码 / 私钥两种认证方式，密码与私钥口令用 Windows DPAPI 加密落盘。
- **内置指令集**：侧栏「指令集」菜单内置 Kubernetes / Docker / Linux 巡检 / 网络排查 / 日志文件 / 进程服务 / rz-sz / Git 等 128 条常用命令，变量统一写成 `<变量名>`；点一下只把命令输入到终端、**不自动回车**，填好变量、确认无误后再执行。
- **rz / sz 文件传输**：内置 ZMODEM 协议实现，直接在终端里执行 `sz 文件` 下载、执行 `rz` 上传；自动识别握手、显示进度与速度，默认直接接收到「下载\yhShell」，也可开启接收前确认。支持 CRC-16 / CRC-32、二进制与中文文件名、多文件与同名自动改名。
- **任意 OpenAI 兼容供应商**：DeepSeek、OpenAI、Moonshot、通义千问、智谱 GLM、OpenRouter、Ollama 本地模型都行；填一次 Base URL 与 Key，就能从上游 `/models` 自动拉取并加入**全部**模型，还能按间隔自动刷新同步，不用逐个添加或勾选。支持多套供应商、温度 / 最大 tokens、连接测试；对话框上方的「模型」下拉框按供应商分组，选一个即可开始对话。
- **Agent 对话执行命令**：模型通过 `run_command` 工具提出命令，界面展示命令、解释与风险标记，确认后**直接敲进你正在看的终端**执行，并把结果回传给模型，支持多步推理。也正因如此，跳板机（JumpServer 等）里进到目标机之后依然可用。
- **执行策略**：每条确认 / 仅安全命令自动执行 / 全部自动执行。危险命令（`rm -rf /`、`mkfs`、`shutdown` 等）无论哪种策略都强制确认。「仅安全」模式下，只读命令的**管道与命令链**（如 `ps -eo ... | head -n 6`、`df -h && free -m`）会按 `|`、`&&`、`||` 逐段检查：每一段都在只读白名单且不含 `;`、`>`、`<`、反引号等组合符号时才自动执行；`ps | sh`、`ls | xargs rm`、`df -h && env bash` 之类仍需手动确认（`;` 串联一律确认）。未自动执行时，命令卡片上会直接说明是哪一段、什么原因（例如「未自动执行：`&&` 第 2 段：`env` 可启动其他程序」）。
- **AI 对话按会话隔离**：每个 SSH 标签页有自己的 AI 上下文和历史记录。
- **本地对话留档**：每次对话自动保存到本机，重启后可查看、导出 Markdown 或继续对话。
- **Markdown 渲染**：代码块、有序/无序列表、表格（含对齐与横向滚动）、行内代码、链接与标题。
- **终端输出规则高亮**：错误 / 失败红色、警告 / 超时黄色、成功 / 运行状态绿色、IPv4 / URL 蓝色，支持中文日志和常见 Kubernetes 状态；保留远端原有颜色，设置里可整体或分类关闭。
- **明暗主题**、可调字号、命令超时、单轮最大步数等设置。
- **自绘标题栏**：无边框窗口，最小化 / 最大化 / 关闭按钮和整体 UI 统一；标题栏可拖动、双击最大化、四边与四角可缩放，贴边分屏等系统行为保持不变。

| Agent 提出命令 | 模型设置 |
| --- | --- |
| ![命令确认](docs/screenshots/agent-command.png) | ![模型设置](docs/screenshots/settings-models.png) |

## 环境要求

- Windows 10/11（依赖 WebView2 Runtime；Win11 自带，Win10 可安装 Evergreen Runtime）
- **64 位 Python 3.10+**（32 位 Python 缺少部分依赖的预编译包）
- 网络可访问你选择的模型服务

> 为什么必须 64 位：paramiko 依赖的 cryptography / PyNaCl 在 32 位 Windows 上通常没有可用 wheel，会退回源码编译并失败。

## 快速开始

```powershell
cd D:\code\yhShell
powershell -ExecutionPolicy Bypass -File setup.ps1
.\run.bat
```

`setup.ps1` 会寻找一个 64 位 Python（优先使用 `py -3` 启动器、常见安装路径，以及 DSH 运行时自带的 Python），创建 `.venv` 并安装依赖。也可以手动来：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

首次启动会生成配置目录 `%APPDATA%\yhShell`，并自带一个未填 Key、**未选模型**的 DeepSeek 供应商；填上 Key、点「获取模型」就会自动加入全部模型，之后按设定的间隔自动同步（不预置任何模型名，上游改名也不会留下失效条目）。

## 使用

### 连接主机

1. 点击「新建连接」，填写主机、端口、用户名与密码（或选择私钥认证）。
2. 勾选「保存到主机列表」后，密码会加密保存；之后从左侧「主机」进入主机列表页面，点击「连接」即可。
3. 每个会话是一个标签页，可同时开多个；右键终端可复制 / 粘贴 / 清屏 / 发送 Ctrl+C。
4. 右键终端标签可选择「复制新会话」「左右分屏」或「上下分屏」；标签内每个终端窗格右上角的菜单可继续递归分屏、切换单窗格视图或关闭当前窗格。一个标签可包含任意多个独立 SSH 会话窗格，分屏不会新增标签；「复制新会话」才会建立独立标签。分屏会使用原连接配置创建独立 SSH 会话（未保存的连接也支持），不会复制远端当前目录、跳板机内二次登录状态、终端输出或 AI 对话。点击窗格会切换当前操作目标，AI 助手、指令集和状态栏跟随当前终端；关闭窗格会保留布局中的其他窗格，切换标签会保存各标签自己的布局。

分屏间的分隔条支持拖拽调整宽度 / 高度，嵌套分屏各自独立调整；双击分隔条恢复均分。也可聚焦分隔条后用方向键微调（按住 Shift 加大步长，Home 恢复均分）。调整比例在切换标签、取消 / 恢复分屏后保留。

| 左右分屏 | 上下分屏 |
| --- | --- |
| ![同一标签内左右分屏](docs/screenshots/split-right.png) | ![同一标签内上下分屏](docs/screenshots/split-down.png) |

![递归多窗格分屏](docs/screenshots/multi-split.png)

### 终端输出高亮

打开「设置 → 常规」可启用或关闭「终端输出规则高亮」，也可分别控制四类内置规则，保存后立即应用到已打开的终端：

| 分类 | 颜色 | 示例 |
| --- | --- | --- |
| 错误 / 失败 | 红色 | `ERROR`、`fatal`、`failed`、`denied`、`NotReady`、`CrashLoopBackOff`、错误 / 失败 / 异常 |
| 警告 / 超时 | 黄色 | `WARN`、`warning`、`deprecated`、`Pending`、`timeout`、警告 / 超时 |
| 成功 / 运行状态 | 绿色 | `success`、`Running`、`Ready`、`active`、`Completed`、`OK`、成功 / 完成 |
| 网络地址 | 蓝色 | IPv4（含可选端口）、`http://` / `https://` URL |

英文关键词忽略大小写并按完整单词匹配。高亮仅作用于显示层，不修改终端文本、复制结果、SSH 数据或 AI 命令输出；已有前景色 / 背景色的字符不覆盖。深浅主题分别配色，支持分批输出、中文宽字符、自动换行和滚动回看。vim / top 等使用备用屏幕的全屏程序不应用规则。

### 侧边菜单

左侧仅保留导航菜单，点击后切换主区域页面，不再在菜单下方平铺列表：

- **终端**：打开的 SSH 会话用顶部标签页切换，不重复展示左侧会话列表。
- **主机**：独立的主机列表页面，保留名称与 `用户名@地址:端口`，支持新建连接、编辑和删除。
- **指令集**：独立的指令配置页面，支持分组、搜索、新建、编辑和删除指令。

切换页面不会关闭终端会话；当前菜单会记住。顶部「新建连接」与底部「设置」始终可用。

| 主机列表 | 指令配置 |
| --- | --- |
| ![主机列表页面](docs/screenshots/hosts.png) | ![指令集配置页面](docs/screenshots/snippet-config.png) |

### 指令集

「指令集」页面内置了一批常用命令，Kubernetes 放在最前面（查看 Pod / 节点 / 日志、`exec`、`port-forward`、发布回滚、节点维护等），另外还有 Docker、Linux 巡检、网络排查、日志文件、进程服务、ZMODEM 文件传输与 Git。

- **终端底部快捷栏**：连接会话后，终端底部、状态栏上方显示「指令集」栏，默认列出常用命令。下拉切换 Kubernetes / Docker 等分组，横向滚动选择命令；点「全部…」打开当前分组的完整指令面板。
- **其他打开方式**：在「指令集」页面点击命令的「插入」；也可以点终端右上角「指令集」，或按 `Ctrl+Shift+K` 打开命令面板，面板支持搜索、分组筛选和 `↑` / `↓` + `Enter` 选择。
- **只输入，不发送**：点击命令只会把文本写进当前终端的输入行，**不会自动回车**。你可以先改参数、确认无误后自己按 `Enter`；没有连接会话时会提示先连接。
- **变量**：命令里的参数写作 `<变量名>`，例如 `kubectl logs -f <Pod名> -n <命名空间>`。有变量的命令会先弹出填写框，实时预览拼好的命令；留空的变量会保留 `<变量名>` 占位符，点「插入占位符」则整条原样输入。
- **配置与搜索**：「指令集」页面支持按分组筛选，按名称、命令或说明搜索；可新建、重命名、删除自定义分组，以及增删改自定义指令。修改会保存到应用配置中。
- **内置指令**：可编辑或隐藏内置指令；「恢复内置」会撤销这些修改并恢复隐藏的内置指令，保留你创建的分组和指令。

| 指令面板 | 填写变量 |
| --- | --- |
| ![指令集](docs/screenshots/snippets.png) | ![填写变量](docs/screenshots/snippet-vars.png) |

### rz / sz 文件传输

远端装有 `lrzsz`（多数 Linux 发行版可 `apt install lrzsz` / `yum install lrzsz`）时，直接在终端里用它传文件，不需要另开 SFTP：

- **下载**：远端执行 `sz 文件名`（可跟多个文件，也支持通配符）。终端里出现 ZMODEM 握手后，右下角出现「正在接收文件」面板，默认存到「下载\yhShell」；如果想每次自己选目录，打开设置里的「接收前弹窗确认」。
- **上传**：远端执行 `rz`，面板会出现「发送文件」按钮，选择本地文件（可多选）后开始上传，文件落在远端 `rz` 的当前目录。
- **进度与取消**：面板显示当前文件、进度、速度与保存位置；点「取消」或按 `Ctrl+C` 会中止本次传输，远端 `sz`/`rz` 随即退出。
- **完成后**：点「打开文件夹」定位到下载目录；终端里会留一行 `[ZMODEM] 已接收 …` 记录。

| 接收前确认（可选） | 传输中 |
| --- | --- |
| ![接收确认](docs/screenshots/transfer-prompt.png) | ![传输进度](docs/screenshots/transfer-progress.png) |

设置项：接收目录、接收前是否弹窗确认、同名文件是否覆盖（默认自动改名为「文件 (1)」）。确认默认关闭，`sz` 会直接存到默认目录并显示进度；打开后每次接收前都会等你选目录（远端 `sz` 只等待约 20 秒，超时会失败）。

传输期间终端输入会被屏蔽（避免按键混进二进制流），需要中断时按 `Ctrl+C`。ZMODEM 会话结束后终端自动恢复，后续输出照常显示。

### 配置模型

侧栏「设置 → AI 模型 → 添加供应商」：

- 选择快速模板（OpenAI / DeepSeek / Kimi / 通义千问 / 智谱 GLM / OpenRouter / Ollama），或手动填写名称、Base URL 与 API Key。
- 新供应商默认不带任何模型；以前版本预置的 `deepseek-chat` 会在启动时自动移除。
- 点「获取模型」会向上游 `/models` 拉取该供应商的模型，并**自动全部加入**，保存后在对话框上方的「模型」下拉框里直接选即可，不需要逐个勾选。
- 设置页顶部的「自动同步模型」可以选择每 6 / 12 / 24 小时或每 3 天刷新一次；到期后后台自动向上游同步，新增模型会自动出现，上游下架的模型会自动消失。默认 24 小时，选「关闭」则只手动刷新。
- 设置页的「立即同步」可以随时刷新全部供应商；每个供应商行会显示「共 N 个模型 · X 分钟前同步」。
- 同一个供应商下的多个模型共用 Base URL 与 Key；对话框上方的「模型」下拉框按供应商分组列出全部模型，选一个即用于之后的对话，不用进设置。
- 模型被上游下架时，自动同步会把它移出下拉框；如果删掉的正好是当前模型，会自动回退到同供应商的第一个模型。
- 上游没有实现 `/models` 时，在搜索框输入模型 id，点「添加「…」」即可手动补一个（之后自动同步若成功会以上游列表为准）。
- **Base URL** 填到 `/v1` 或版本段即可，例如 `https://api.deepseek.com`、`https://dashscope.aliyuncs.com/compatible-mode/v1`、`http://127.0.0.1:11434/v1`，程序会自动补全 `/chat/completions` 与 `/models`；也可以直接填完整的 `/chat/completions` 地址。
- `http://` 的本地地址不强制填写 Key；`https://` 地址需要 Key。
- 供应商行上的「测试」会用该供应商的当前（或第一个）模型发一个最小请求，验证 Key、地址与模型是否可用。

![供应商配置](docs/screenshots/provider-modal.png)

### 和 AI 一起干活

1. 连接服务器，在对话框上方的「模型」下拉框里选择供应商与模型（默认是上次用的那个）。
2. 在右侧输入框描述需求，比如「看看磁盘和内存有没有异常」。
3. 模型流式输出分析，并给出带解释的命令卡片。
4. 点「执行」或「编辑」后执行；输出显示在卡片里，同时回传给模型继续分析。
5. 执行策略可在 AI 面板顶部随时切换，也可以在设置里调整命令超时与单轮最大步数。

AI 面板底部会显示当前连接状态与快捷键。每条会话的 AI 上下文相互隔离；对话会自动留档到本地，切换标签页不会串台。

### 历史对话

![历史对话](docs/screenshots/history.png)

- 点击 AI 面板右上角的时钟图标打开「历史对话」，按更新时间列出所有本地存档。
- **查看**：只读浏览，不会影响当前会话；如果主机匹配，顶部会出现「继续此对话」。
- **继续**：把该对话载入当前会话（要求连接的是同一台主机，否则按钮不可用），之后可以接着提问。
- **导出**：在该对话存档旁生成一份 Markdown 文件。
- **删除**：永久删除对应的本地存档。
- 「新对话」按钮只是开始一份新的对话，旧对话仍保留在历史记录里；需要删除请到历史列表操作。

## 版本更新

在「设置 → 关于」点击「检查更新」，查看最新稳定版本、发布日期和更新说明，并前往官方 [GitHub Releases](https://github.com/5YHao/yhShell/releases) 下载。当前只检测和提醒，**不会自动下载、执行安装程序、覆盖源码或中断 SSH 会话**。

- 默认启动约 15 秒后后台检查，成功结果缓存 24 小时；失败后至少退避 1 小时。持续运行时也会判断是否到期。
- 可以关闭自动检查，手动检查仍可使用。手动检查跳过本地缓存间隔，但遵守 GitHub 服务端限流冷却时间。
- 可忽略某个版本的自动提醒；更高版本仍会提示，手动检查仍可查看被忽略版本。
- 检查仅向 GitHub 请求公开 Release 信息，不携带 SSH 主机、模型 Key 或对话。自动检查失败不弹窗，手动检查显示错误。检查使用独立匿名连接，不读取 `.netrc` 登录凭据或环境变量中的代理；如果当前网络无法直连 GitHub，可关闭自动检查并在浏览器中访问发行页。
- 更新缓存单独保存在配置目录下的 `update-cache.json`，删除它不会影响主机和模型配置。
- 源码用户应先备份本地修改，退出应用后按安装步骤更新源码和依赖；应用不会自动执行 `git pull`。

### 发布稳定版本（维护者）

1. 在 `yhshell/__init__.py` 修改唯一应用版本号，使用 `主版本.次版本.修订号`（如 `0.2.0`）。
2. 运行测试，提交代码并创建对应标签（如 `v0.2.0`）。
3. 创建该标签的 GitHub Release，填写更新说明，上传准备好的发行包（如有），确认标签和应用版本一致。
4. 验证源码/发行包后正式发布。Draft 和 Pre-release 不参与稳定版检测；仅推送代码或标签不会触发更新。

当前更新源为 `5YHao/yhShell`，fork 后需修改 `yhshell/updater.py` 的仓库常量。尚未发布正式 Release 时显示「暂无正式版本」。已提供下述本机一键打包；未配置 GitHub Actions 自动发布。

更新专项测试不访问真实 GitHub：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_updater tests.test_update_api -v
# 前端交互回归（需 tests 下已安装 puppeteer-core 和本机 Edge）
node tests/test_updates_ui.mjs
```

## Windows 打包与发布

在 **Windows x64 + 64 位 x64 Python** 上运行，先完成 `setup.ps1`，并安装 [Inno Setup 6.3+](https://jrsoftware.org/isdl.php)（默认路径，或将 ISCC.exe 加入 PATH）。然后双击或执行：

```powershell
.\build.bat
# 仅构建便携 ZIP，不需要 Inno Setup
.\build.bat -SkipInstaller
# 指定自定义编译器路径（也支持 Inno Setup 7）
.\build.bat -IsccPath "C:\Tools\Inno Setup 6\ISCC.exe"
# 可选：先清理 dist；传入版本必须与代码一致
.\build.bat -Clean -Version 0.1.0
```

首次打包会在项目 `.venv` 内安装 PyInstaller。脚本按 `yhshell.__version__` 读取版本，不更改全局 Python；自定义逻辑在 `packaging/build.ps1`、`packaging/yhShell.spec` 和 `packaging/yhShell.iss`。缺少 Inno Setup 时会保留已生成 ZIP 并报错；不会下载编译器或静默安装工具。

产物位于 `dist/`（不加入 Git）：

- `yhShell-<版本>-windows-x64.zip`：解压整个压缩包后双击 `yhShell.exe`，不要只复制 EXE，必须保留 `_internal`。
- `yhShell-Setup-<版本>-windows-x64.exe`：按当前 Windows 用户安装，无需管理员权限；默认安装到 `%LOCALAPPDATA%\Programs\yhShell`，带开始菜单和可选桌面快捷方式。
- `SHA256SUMS.txt`：两个发行文件的 SHA256，用 `Get-FileHash -Algorithm SHA256 <文件>` 核对。

两种形式均内置 Python 运行时，用户无需安装 Python，但要求 **Windows 10/11 x64、.NET Framework 4.8+、WebView2 Evergreen Runtime**。安装器检测 WebView2，缺少时提示并提供官方网页入口，不内置/自动下载运行时，离线安装前请自行准备依赖。当前构建未做代码签名，Windows 可能提示未知发布者，校验和不等于发行签名。

**这里的“便携版”指免安装程序，不表示数据跟随 U 盘**：安装版和 ZIP 版默认共用 `%APPDATA%\yhShell`，密码由当前 Windows 用户 DPAPI 保护，不能复制到其他用户/机器后直接解密。升级前结束 SSH/Agent/文件传输并退出应用，再安装新版本或整体替换便携目录，避免新旧依赖混用。卸载保留主机、密钥和对话记录。无控制台版启动错误可查看 `%LOCALAPPDATA%\yhShell\logs\startup.log`。如果便携版所在路径被 Windows 认作网络/Internet 区域，.NET 可能拒绝加载 DLL；应解压到可信本地目录，不建议修改系统安全策略。

本机验证命令（会打开并关闭测试窗口；安装测试会临时登记当前用户的应用卸载项，请勿在已有正式安装时运行）：

```powershell
.venv\Scripts\python.exe packaging\smoke.py "C:\本地测试目录\yhShell.exe"
.venv\Scripts\python.exe packaging\verify_installer.py
```

发布时，先在本机测试，再到仓库 **Releases → Draft a new release**，创建与代码一致的 `v<版本>` 标签，填写更新说明，将上述 ZIP、安装包和校验文件作为附件上传，再发布为正式稳定版。不要把 `dist/yhShell` 目录或个人配置上传到源码仓库；GitHub 自动生成的 Source code ZIP 不是可运行程序。

## 快捷键

| 快捷键 | 功能 |
| --- | --- |
| `Ctrl+Shift+T` | 新建连接 |
| `Ctrl+Shift+W` | 关闭当前会话 |
| `Ctrl+Shift+C` | 复制终端选中内容 |
| `Ctrl+Shift+V` | 粘贴到终端 |
| `Ctrl+Shift+A` | 全选终端 |
| `Ctrl+Shift+K` | 打开指令集面板 |
| `Ctrl+Shift+,` | 打开设置 |
| `Enter` / `Shift+Enter` | AI 输入框发送 / 换行 |
| `Esc` | 关闭最上层弹窗 / 菜单 |

## 数据与安全

- 配置保存在 `%APPDATA%\yhShell\config.json`（可用环境变量 `YHSELL_HOME` 覆盖）。
- AI 对话存档保存在 `%APPDATA%\yhShell\conversations\`，每条对话一个 JSON 文件，导出的 Markdown 也在同一目录。
- SSH 密码、私钥口令、模型 API Key 使用 **Windows DPAPI** 加密（当前用户范围），界面上只显示是否存在以及末四位提示，不会回传明文。
- 远端主机密钥：会加载系统 / 用户 `known_hosts`；未知主机采用首次信任并显示指纹，密钥变更会报错。
- 模型 API Key 仅在你配置的 Base URL 请求中使用，不经过任何第三方中转。
- **注意**：Agent 执行命令得到的输出（以及主机名、用户名等上下文）会发送给你配置的模型服务。如果输出包含密钥、隐私数据，请确认模型服务方的数据策略后再使用。
- 对话存档包含主机信息、命令与输出，以明文 JSON 保存在本机用户目录（受 Windows 用户账户权限保护）；如果介意，请在历史记录里删除或定期清理该目录。

## 设计说明

Agent 和你用**同一个终端**：它把命令直接敲进你正在看的那个 shell，然后从同一条字节流里把结果读回来。这样做的原因是跳板机（JumpServer / Teleport 等）——真正的目标机 shell 只存在于这条交互会话里，另开 channel 只会回到堡垒机的资产菜单，所以只有「共用终端」这一条路。

具体做法：

- 发送前先确认终端停在提示符（空闲、非全屏程序、你不在输入中），否则拒绝发送并说明原因；
- 命令用 `__YHS_<token>_START__` / `__YHS_<token>_END__ <退出码>` 包住，标记在敲入的原文里不连续出现，因此不会被命令回显误判；
- 这些标记行只从 **UI 流**里过滤掉，你看到的仍是「命令 + 输出」；`cd`、`export` 天然在命令之间保持状态；
- Agent 执行期间终端输入被锁定（右下角有提示），避免按键和命令交错；命令超时发送 `Ctrl-C` 并返回超时状态。

代价是：命令会真的写进你的终端，回滚缓冲里会留下痕迹；提示符识别依赖 `#`、`$`、`%`、`❯` 结尾的常见 shell 提示符。

前端通过 `window.pywebview.api` 调用 Python 后端；后端以 30ms 轮询把 SSH 输出与 Agent 事件批量推给 xterm.js 和聊天面板。

## 已知限制

- Agent 执行命令依赖远端的 **POSIX shell**（bash / sh / zsh）和可识别的提示符。Windows OpenSSH 的 cmd / PowerShell 远端、以及停在菜单界面的跳板机会被拒绝执行并给出提示。
- Agent 自动执行依赖模型对 **OpenAI function calling / tool calls** 的支持；不支持工具调用的模型可能无法完成「提出命令」这一步。
- 需要交互输入的命令（`sudo` 密码、`vim`、`less`）会一直等待直至超时；请让 Agent 使用 `sudo -n`、`--no-pager` 之类的非交互写法。
- 执行期间你无法在终端里打字（输入被锁定）；如果你正停在命令中间或全屏程序里，Agent 会拒绝执行。
- 引号未闭合、以 `&&` / `|` 结尾等会让 shell 进入续行状态（`>` 提示符）的命令会被本地拦下，需要修正后重试。
- 继续历史对话要求当前连接的主机、端口、用户名与存档一致，避免把旧命令误发到另一台机器。
- 危险命令识别基于模式匹配，只能降低误操作风险，不能替代人工判断；「全部自动执行」请谨慎开启。
- 「仅安全」模式偏保守：引号内的 `|` / `&&`（如 `grep -E "a|b"`）、`;` 串联、`env`、`find -exec`、`awk system()` 等会执行其他程序的写法一律需要确认；宁可多点一次，也不自动放行。
- 目前是单窗口应用，不支持会话在关闭标签后保活 / 重连。
- 终端暂不包含 URL 点击、搜索；支持在同一标签内递归创建任意多个左右 / 上下分屏窗格。
- ZMODEM 传输依赖远端 `lrzsz`（或兼容的 `rz`/`sz`）；传输是整文件级的，不支持断点续传与目录递归，远端 `rz` 若对已存在文件提问会阻塞（用 `rz -y` 覆盖）。

## 开发与测试

```powershell
# 下载一次 BusyBox for Windows（约 700KB）
Invoke-WebRequest https://frippery.org/files/busybox/busybox64.exe -OutFile "$env:TEMP\busybox64.exe"

# 本地假 SSH 服务（tester/test，端口 2222，需要 busybox 提供 POSIX shell）
$env:YHS_BUSYBOX="$env:TEMP\busybox64.exe"
.venv\Scripts\python.exe tests\fake_sshd.py

# 假 OpenAI 兼容流式服务（端口 8900，先返回工具调用，再返回最终回答）
.venv\Scripts\python.exe tests\fake_llm.py
```

`tests/fake_sshd.py` 用 [BusyBox for Windows](https://frippery.org/busybox/) 作为远端 shell，因此不需要 WSL / Docker 就能本地联调终端与 Agent 命令通道。

单元测试覆盖供应商增删改、旧配置迁移与 `/models` 解析，只使用临时目录和本地假上游，不访问外网：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_providers -v
```

内置指令集另有单元测试，校验分组 / 指令 ID 唯一、命令为单行、`<变量名>` 提取与填空替换：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_snippets -v
```

ZMODEM 单独有一套测试：线格式（CRC、转义、帧头/子包）、本机收发互通、会话内检测与确认流程，以及与 [zmodem.js](https://github.com/FGasper/zmodem.js)（Node，独立实现）的双向互通测试：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_zmodem tests.test_transfer_session -v
cd tests; npm install; cd ..
.venv\Scripts\python.exe -m unittest tests.test_zmodem_interop -v
```

互通测试会启动 `tests/zmodem_peer.mjs`（用 zmodem.js 扮演远端 `sz`/`rz`），通过管道交换真实的 ZMODEM 字节；没安装 Node 依赖时自动跳过。

终端高亮专项回归使用真实 xterm.js 与无头 Edge，覆盖分批输出、中文列位置、ANSI 原色、分类开关、主题、备用屏幕、换行与滚动回看（需在 `tests` 下安装 `puppeteer-core`）：

```powershell
node tests/test_terminal_highlight.mjs
.venv\Scripts\python.exe -m unittest tests.test_highlight_settings -v
```

UI 视觉/交互回归用无头 Edge 渲染（Mock 桥接，不启动 pywebview），需要 `puppeteer-core` 与本机 Edge：

```powershell
cd tests
npm install
node ui_mock.mjs
```

脚本会模拟连接、AI 工具调用、执行确认、设置弹窗、添加供应商并获取模型、指令集插入（含变量与「不回车」校验）、双会话切换及递归分屏等流程，截图输出到 `.shots/`（可用 `YHS_SHOT_DIR` 改到别的目录），并以退出码报告前端异常。`puppeteer-core` 装在别处时，可用 `YHS_PUPPETEER` 指向它的 ESM 入口。

更新 README 配图时，在项目根目录运行：

```powershell
node tests/ui_mock.mjs --update-docs
```

只有整套 UI 回归通过后，脚本才会将对应截图同步到 `docs/screenshots/`。所有配图均由当前前端和 Mock 桥接生成，使用演示主机、模型与对话数据，不连接真实 SSH 或模型服务，也不读取本机账户配置。

## 目录结构

```
app.py                 # 入口：创建 pywebview 窗口
yhshell/
  api.py               # 暴露给前端的 js_api
  ssh.py               # paramiko 会话、PTY、终端内 Agent 执行、ZMODEM 字节流接入
  zmodem.py            # ZMODEM 协议：帧头 / 子包 / 转义 / CRC，收发两端
  transfer.py          # 一次 rz/sz 传输任务：确认、进度、结果
  agent.py             # OpenAI 兼容流式客户端、工具调用与 Agent 循环
  history.py           # 本地对话存档（查看 / 继续 / 导出 / 删除）
  snippets.py          # 内置指令集：Kubernetes / Docker / Linux / 网络等
  store.py             # 配置读写、主机 / 供应商 / 模型管理
  security.py          # Windows DPAPI 加解密
  window.py            # 无边框窗口：标题栏拖拽 / 缩放 / 最大化 / DWM 边框
web/
  index.html
  css/app.css
  js/app.js
  vendor/              # xterm.js + fit addon
tests/
  test_providers.py    # 供应商存储与 /models 解析的单元测试
  test_zmodem.py       # ZMODEM 编解码与收发互通测试
  test_zmodem_interop.py  # 与 zmodem.js 的双向互通测试
  test_transfer_session.py # 会话内检测、确认与恢复终端
  test_terminal_shell.py   # 终端内执行：标记捕获、退出码、锁定与超时
  test_snippets.py     # 内置指令集：ID、单行命令、变量提取与替换
  zmodem_peer.mjs      # 用 zmodem.js 实现的测试对端
  fake_sshd.py         # 本地假 SSH 服务（BusyBox）
  fake_llm.py          # 本地假模型服务
docs/screenshots/
```
