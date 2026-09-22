# mcp-remote-control

通过 **MCP** 管理本机 / SSH / WinRM 远端主机；业务在 **Core**，MCP / CLI 只做薄壳。会话与 endpoint 均为**进程内**状态。

| 入口 | 作用 |
|------|------|
| **`mcp-remote-control`** | MCP stdio 服务（Claude Code / Cursor / Claude Desktop 等 Host） |
| **`mcp-remote-control-cli`** | 同构 CLI harness（doctor / 调试 / 与 MCP 共用 Core） |

**MCP 工具（7）：**

| 类 | 工具 |
|----|------|
| 主机 | `endpoint` · `exec` · `fs` · `screen` · `ps` |
| 设备 | `console`（串口，非 PTY/SSH） |
| 自助配置 | `config`（Agent 写 `MRC_HOME`，少让人手改 TOML） |

**要求：** Python **≥ 3.11** · 依赖 `asyncssh` · `pyte` · `pypsrp` · `mcp` · `pyserial`

| 名称 | 值 |
|------|-----|
| 发行包 / MCP 服务名 | `mcp-remote-control` |
| import | `import mcp_remote_control` |
| 配置根（默认） | `~/.config/mcp-remote-control`（`MRC_HOME`） |
| **Git / 工程根** | **本目录**（含 `pyproject.toml`；`uvx --from git+…` **不要**加 `#subdirectory=`） |
| **许可证** | **Apache-2.0**（可商用；再分发须保留版权/许可与 `NOTICE` 归属声明，**非 GPL 传染**） |

---

## 目录

1. [Agent 输出形态（MCP wire）](#agent-输出形态mcp-wire)
2. [三种装法（先选场景）](#三种装法先选场景)
3. [快速部署：uvx + Git](#快速部署推荐-uvx--git)
4. [本地开发（必读：`--with-editable`）](#本地开发必读-with-editable)
5. [MCP Host 配置](#mcp-host-配置)
6. [配置 `MRC_HOME`](#配置-mrc_home)
7. [工具面](#工具面给-agent)
8. [CLI](#cli)
9. [开发与 harness](#开发与-harness)
10. [故障排查](#故障排查)
11. [安全](#安全提示)
12. [许可证](#许可证)

---

## Agent 输出形态（MCP wire）

规范上 `tools/call` 的 `content` 以 **Agent 文本** 为主，例如：

```json
{
  "content": [{ "type": "text", "text": "@exec ok ep=lab exit=0 …\n\n$ whoami\n…" }],
  "isError": false
}
```

本项目默认 **Agent 轨**（语义文本，见设计 002）：

```text
@exec ok ep=lab exit=0 ms=12 form=command cwd=/home/deploy

$ whoami
deploy
```

**不是**默认把正文塞进：

```json
{ "result": "@exec ok …" }
```

那一层 `{"result":…}` 来自 MCP SDK 对 `-> str` 的自动 structured 包装（v1 FastMCP / v2 `MCPServer`）。本仓库在 tool 注册时使用 **`structured_output=False`**，只暴露 Agent 文本，避免 Host UI 显示 JSON 外壳。要求 **`mcp` SDK ≥ 2**（`MCPServer`）。

CLI 默认同样是 Agent 文本；加 **`--json`** 才走机器轨。

`fs read` 读到完整 PNG / JPEG / GIF / WebP 时，会在文本块后再附一个 `image` 块：文本仍是路径、大小、MIME 等元信息，**不含**图片 Base64。默认读取上限 **1 MiB**（可用 `max_bytes=` 加大）。超出上限的图片返回错误、不附图片。CLI 只显示元信息。

---

## 三种装法（先选场景）

| 你想做什么 | 命令形态 | 改 `src/` 会生效？ | README 小节 |
|------------|----------|-------------------|-------------|
| **只使用**（Host 挂公开/私有 GitHub，不改源码） | `uvx --from "git+https://github.com/shiharuharu/mcp-remote-control.git@main" …` | 否（吃 cache；远端更新要 `--refresh` 或 pin 新 ref） | [从 Git 运行](#2-从-git-运行只用) |
| **改源码开发**（editable） | 先 **clone 到本地**，再 `uvx --with-editable "$REPO" --from "$REPO" …` | 是（仍须**重启** MCP 进程） | [本地开发](#本地开发必读-with-editable) |
| **长期本机 CLI** | `uv tool install --from "git+…" …` 或 `uv pip install -e .` | tool 安装否 / `-e` 是 | [固定安装](#3-固定安装可选) / [venv](#推荐-bvenv-入口最稳host-配置最简单) |

**重要：editable 不能直接挂 GitHub URL。**

```bash
# ❌ 不要这样想：把 GitHub 地址当 editable
uvx --with-editable "git+https://github.com/shiharuharu/mcp-remote-control.git" ...

# uv 的 --with-editable / pip -e 需要的是「本地目录」
# （含 pyproject.toml 的 checkout），不是远程 URL。
```

想「对着 GitHub 上的仓改代码」时，正确路径是：

```bash
git clone https://github.com/shiharuharu/mcp-remote-control.git
cd mcp-remote-control          # 本仓库：含 pyproject.toml 的根（即本目录）
REPO="$(pwd)"
uvx --python 3.12 --with-editable "$REPO" --from "$REPO" mcp-remote-control
```

---

## 快速部署（推荐：uvx + Git）

[uv](https://docs.astral.sh/uv/) 的 **`uvx`** 按需拉包并在隔离环境运行入口，适合 MCP Host 的 `command` / `args`。

### 1. 配置目录

```bash
mkdir -p ~/.config/mcp-remote-control/{profiles,secrets,state}
export MRC_HOME="$HOME/.config/mcp-remote-control"
```

最小 `config.toml` / profile 见 [配置 MRC_HOME](#配置-mrc_home)。也可交给 Agent：`config ensure_home` → `put_secret` → `put_profile`。

### 2. 从 Git 运行（只用）

本目录即仓库根。官方地址：https://github.com/shiharuharu/mcp-remote-control（**不要** `#subdirectory=`）。

这是 **「Host 直接挂 GitHub」** 的用法：`--from git+…`，**不是** editable。

```bash
FROM="git+https://github.com/shiharuharu/mcp-remote-control.git@main"

# MCP stdio（由 Host 拉起；前台会等待协议输入）
uvx --python 3.12 --from "$FROM" mcp-remote-control

# CLI 自检
uvx --python 3.12 --from "$FROM" mcp-remote-control-cli doctor
uvx --python 3.12 --from "$FROM" mcp-remote-control-cli endpoint list
```

分支 / tag / commit / 私有仓：

```bash
uvx --python 3.12 --from "git+https://github.com/shiharuharu/mcp-remote-control.git@v0.2.0" mcp-remote-control
uvx --python 3.12 --from "git+https://github.com/shiharuharu/mcp-remote-control.git@abcdef1" mcp-remote-control
uvx --python 3.12 --from "git+ssh://git@github.com/shiharuharu/mcp-remote-control.git@main" mcp-remote-control
```

> **`uvx` 会缓存环境。** 远端或本地源码更新后工具列表仍旧（例如缺 `config`、仍有 `{"result":…}`）时：
>
> - 加 **`--refresh`**，或  
> - pin 新 commit/tag，或  
> - 开发期 **clone 后** 用下面的 **`--with-editable` 本地路径** / 直接跑 `.venv` 入口。

### 3. 固定安装（可选）

```bash
uv tool install --python 3.12 --from "git+https://github.com/shiharuharu/mcp-remote-control.git@main" mcp-remote-control
# 入口常在 ~/.local/bin/mcp-remote-control
uv tool upgrade mcp-remote-control
```

---

## 本地开发（必读：`--with-editable`）

开发时若只用：

```bash
uvx --from /path/to/this-repo mcp-remote-control
# 或：uvx --from "git+https://github.com/shiharuharu/mcp-remote-control.git@main" …
```

`uvx` 会把包**拷进 cache**，**不会**随你改 `src/` 自动更新 → Host 可能仍是旧 6 tools（无 `config`）、旧 structured 包装。

### 推荐 A：clone 后 `uvx --with-editable`（跟源码联动）

1. 从 GitHub 拉到本地（或已有 fork/checkout）。  
2. **`--with-editable` / `--from` 都填本地绝对路径**（含 `pyproject.toml` 的目录 = 本仓库根）。

```bash
# 若还没有本地树：
git clone https://github.com/shiharuharu/mcp-remote-control.git
cd mcp-remote-control          # 仓库根（本目录）

REPO="$(pwd)"                  # 或 /abs/path/to/mcp-remote-control

# 每次从源码可编辑安装再跑入口（改代码后重启 MCP 进程即可）
uvx --python 3.12 \
  --with-editable "$REPO" \
  --from "$REPO" \
  mcp-remote-control

# CLI
uvx --python 3.12 \
  --with-editable "$REPO" \
  --from "$REPO" \
  mcp-remote-control-cli doctor
```

| 参数 | 作用 |
|------|------|
| **`--from $REPO`** | 用**本地**项目提供 console script（`$REPO` 必须是目录，不是 `git+https://…`） |
| **`--with-editable $REPO`** | 以 **editable** 装本包，改 `src/` 立即反映（仍须**重启** stdio MCP 进程） |
| **`--python 3.12`** | 满足 `requires-python >= 3.11`（系统默认 3.9 会 resolve 失败） |
| **`--refresh`** | 丢掉 uv 缓存元数据后重装（排障时用） |

也可：

```bash
cd "$REPO"
uvx --python 3.12 --with-editable . --from . mcp-remote-control-cli config home
```

### 推荐 B：venv 入口（最稳、Host 配置最简单）

```bash
cd /path/to/this-repo
uv venv --python 3.12
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"

export MRC_HOME="$HOME/.config/mcp-remote-control"
mcp-remote-control-cli doctor
mcp-remote-control                 # stdio
```

Host 直接指向：

```text
/path/to/this-repo/.venv/bin/mcp-remote-control
```

### 推荐 C：`uv run`（在项目目录内）

```bash
cd /path/to/this-repo
uv run --python 3.12 mcp-remote-control-cli doctor
uv run --python 3.12 mcp-remote-control
```

### 不要这样做（开发）

| 做法 | 问题 |
|------|------|
| 仅 `uvx --from $REPO` **无** `--with-editable` | 易吃 **旧 cache 快照**，新工具/修复不出现 |
| 改完代码不重连 MCP | stdio 进程仍是旧内存；需 Host 重连 `/mcp` 或重启会话 |
| 系统 Python 3.9 调 `uvx` 不写 `--python` | `requires-python >= 3.11` 解析失败 |

---

## MCP Host 配置

`command` = 本机可执行文件（`which uvx` 或 venv 的绝对路径）。  
**配置根默认**为用户目录下的 `~/.config/mcp-remote-control`；一般**不必**在 Host JSON 里写 `MRC_HOME`。

若一定要指定，请用本机真实绝对路径（在终端 `echo "$HOME/.config/mcp-remote-control"` 后粘贴结果）。  
不要照抄文档里的示例用户名，也不要写 `~/Users/...` 这类叠路径。

### 生产 / 只用：远端 GitHub（非 editable）

Host 里写 **Git URL**，用 `--from git+…`。**不要**在 args 里写 `--with-editable` + `git+https://…`。

```json
{
  "mcpServers": {
    "mcp-remote-control": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--from",
        "git+https://github.com/shiharuharu/mcp-remote-control.git@main",
        "mcp-remote-control"
      ]
    }
  }
}
```

需要自定义配置根时再加 `env.MRC_HOME`（绝对路径）。强制刷新缓存可在 `args` 最前加 `"--refresh"`。

### 开发：本地 checkout + `--with-editable`（推荐）

先 `git clone`，在**含 `pyproject.toml` 的仓库根**取绝对路径（`pwd -P`），填入下面两处（不是 `git+https://…`）：

```json
{
  "mcpServers": {
    "mcp-remote-control": {
      "command": "uvx",
      "args": [
        "--python", "3.12",
        "--with-editable",
        "/absolute/path/to/mcp-remote-control",
        "--from",
        "/absolute/path/to/mcp-remote-control",
        "mcp-remote-control"
      ]
    }
  }
}
```

改代码后 **重连 MCP**。

### 开发：venv 绝对路径（最简单）

```json
{
  "mcpServers": {
    "mcp-remote-control": {
      "command": "/absolute/path/to/mcp-remote-control/.venv/bin/mcp-remote-control",
      "args": []
    }
  }
}
```

### Claude Desktop

编辑 `mcpServers`（macOS 常见：  
`~/Library/Application Support/Claude/claude_desktop_config.json`），键名建议 **`mcp-remote-control`**，结构同上。

### Claude Code（全局示例）

常见：`~/.claude.json` → `mcpServers`（字段同上）。改完后 **重连 MCP**，确认 tools 为 7 个且含 **`config`**。

### 部署后自检

```bash
export MRC_HOME="$HOME/.config/mcp-remote-control"
# 应列出 7 名：endpoint,exec,fs,screen,ps,console,config
uvx --python 3.12 --with-editable . --from . mcp-remote-control-cli doctor
```

Host 侧：重连后工具列表须含 `config`；调用结果应为 `@kind …` 文本，**不应**再被 `{"result":…}` 包一层。

---

## 配置 `MRC_HOME`

| 路径 | 含义 |
|------|------|
| `$MRC_HOME/config.toml` | 全局默认 |
| `$MRC_HOME/profiles/*.toml` | 每个 endpoint 一个 profile（`name` = `ep=` / `profile=`） |
| `$MRC_HOME/secrets/` | 密钥/密码文件（**勿提交 Git**） |
| `$MRC_HOME/state/` | 运行时状态 |

| 环境变量 | 含义 |
|----------|------|
| **`MRC_HOME`** | 配置根（优先） |
| `MCP_REMOTE_CONTROL_HOME` | 次选别名 |
| （皆未设） | `~/.config/mcp-remote-control` |

`transport`：`local` | `ssh` | `winrm`  
- local / ssh ≈ `exec` + `fs` + `screen`  
- winrm ≈ `exec` + `fs` + `ps`（`screen` → UNSUPPORTED）

### 手写示例

```toml
# $MRC_HOME/config.toml
[defaults]
verbosity = "normal"
```

```toml
# $MRC_HOME/profiles/local.toml
name = "local"
transport = "local"
label = "this machine"
```

```toml
# $MRC_HOME/profiles/lab-ssh.toml
name = "lab-ssh"
transport = "ssh"
host = "10.0.0.5"
port = 22
username = "deploy"
label = "lab"

[auth]
method = "private_key_path"
key_path = "secrets/id_ed25519"   # 相对 MRC_HOME 或绝对路径

[ssh]
connect_timeout_ms = 15000
keepalive_interval_s = 30
known_hosts = "none"              # 仅实验室；生产请用 known_hosts 文件
encoding = "utf-8"

[defaults]
cwd = "/home/deploy"
```

密钥：`chmod 600 $MRC_HOME/secrets/*`。密码可用 `password_path = "secrets/lab_pass"`。

### `[winrm]` 配置

WinRM profile 的传输调优键全部放在 `[winrm]`；`[auth].method = "password"` 默认映射为 `ntlm`。

```toml
# $MRC_HOME/profiles/lab-win.toml
name = "lab-win"
transport = "winrm"
host = "10.0.0.6"
username = "Administrator"

[auth]
password_path = "secrets/win_pass"

[winrm]
scheme = "http"                     # http | https
server_cert_validation = "ignore"   # 实验室自签证书；生产请留空
connect_timeout_ms = 15000
operation_timeout_s = 20
read_timeout_s = 22
encryption = "auto"
probe = "full"
probe_timeout_s = 5
reconnection_retries = 2
reconnection_backoff = 0.5

[winrm.credssp]                     # 仅 auth = "credssp" 时生效
auth_mechanism = "ntlm"
disable_tlsv1_2 = true
minimum_version = 6
```

| 键（`[winrm]`） | 取值 | 默认 | 含义 |
|-----------------|------|------|------|
| `scheme` | `http` \| `https`（亦接受 `ssl` / `true` / `1`） | `http` | 是否走 pypsrp `ssl`；`https` 且 profile 未写 `port` 时端口取 5986 |
| `ssl` | TOML 布尔 / 字符串 | `false` | `scheme` 之外的显式开关；`"false"` / `"0"` / `"no"` / `"off"` 均视为关 |
| `auth` | `ntlm` \| `basic` \| `kerberos` \| `negotiate` \| `credssp` \| `certificate` | 由 `[auth].method` 推导（password → `ntlm`） | pypsrp 认证协议；`auth_method` 为等价别名 |
| `server_cert_validation` | `ignore` / `false` / `0` / `no` 关闭校验 | 校验 | HTTPS 服务端证书校验；本键优先于 `cert_validation` |
| `cert_validation` | TOML 布尔 | `true` | 同上；两个键都未写时校验 |
| `connect_timeout_ms` | 整数毫秒 | `15000` | 建连预算；非法值回落默认 |
| `operation_timeout_s` | 整数秒 | 不写用 pypsrp 默认 `20` | WSMan `OperationTimeout`；exec 调用还会按本次 `timeout_s` 收紧 |
| `read_timeout_s` | 整数秒 | 不写时：有生效的 operation timeout 则 = 该值 `+ 2`，否则沿用 pypsrp 默认 `30` | HTTP 读超时；显式值优先。读超时必须晚于 WSMan 超时，否则客户端先读超时、拿到的是「像链路故障」的超时 |
| `message_encryption` | `auto` \| `always` \| `never` | `auto` | 消息加密；`encryption` 为等价别名；`always` 与 `basic` / `certificate` 不兼容 |
| `probe` | `skip` \| `light` \| `full`（亦接受 `true` / `false`、`1` / `0`） | `full` | open 后的身份 / 能力探测强度；`light` 跳过能力 oneshot，高延迟链路打不开时可用（丢失能力详情）；`skip` 完全跳过 |
| `probe_timeout_s` | 正数秒 | `5` | 探测预算；高延迟链路（单次命令数秒）需调大。环境变量 `MRC_WINRM_PROBE_TIMEOUT_S` 优先于本键 |
| `reconnection_retries` | 非负整数（上界 10） | `2` | pypsrp 连接层重试次数；`0` 显式禁用；未写 / 负数 / 非法值回落默认 |
| `reconnection_backoff` | 秒（非负） | `0.5` | 重试退避间隔 |
| `[winrm.credssp]` | 子表 | — | 仅 `auth = "credssp"`：`auth_mechanism` / `disable_tlsv1_2` / `minimum_version`；未写时沿用 `[auth]` 的同名字段 |

`probe` 还可在 `[defaults].winrm_probe`（profile 或全局）设置，`MRC_WINRM_PROBE` 环境变量优先级最高。

**链路保持实测提示**：WinRM 明文 HTTP + 消息加密（`scheme = "http"` 且 `encryption = "auto"`）下出现 `HTTP 400` 且 **body 为空**时，它只是一种**拒绝形态**，不是某一种病因的签名——实测它可出现在空闲后的第一个请求、拷贝中途、或重连握手中。**先查网络路径**：同一主机实测，请求经本地 HTTP 代理转发时空闲后的 `fs put` **7/7 失败**；不走代理（把目标主机加入 `NO_PROXY`，或去掉进程环境里的 `HTTP_PROXY` / `HTTPS_PROXY`）后 **18/18 成功**。「明文 HTTP 下消息加密上下文数秒空闲即失效」只是**其中一种**成因，不是 pypsrp 或明文 HTTP 的固有属性。两类拒绝含义不同：**4xx + 空 body** 是分帧层拒绝，已证明请求未被执行（重放安全）；**带 body 的拒绝**（中间方自己的错误页，实测 `502` + `Connection error: read ETIMEDOUT`）**是否已转发未知**——所以是否重放不按 body 判，只要求本次操作尚未完成过带载荷的往返（见下）。自动恢复：`exec`、`ps open` / `ps invoke` / `ps close`、以及 `endpoint open` 的身份 / 能力探测，都会在**重试不可能重复执行你的命令**时重握手并重试一次——判据是本次操作尚未成功完成过带载荷的往返（链路往返计数不可观测时只会更严，不会更宽）；`ps invoke` 首包即脚本，条件更严，只重放空 body 的 4xx 拒绝（`exec` / `ps open` / `ps close` 与探测的首包是建 shell / 建 runspace / 删 runspace / 身份探测，未完成过带载荷往返时带 body 的拒绝也会重放一次，重放不会重复你的命令）；`ps close` 的重发不会重复删除。其余链路类失败判链路失效、断开会话，重新 `endpoint open` 后再调用（下一次调用也会自动重连）；`ps close` 若删除没落地，用 `endpoint close` + `endpoint open` 收尾。`fs` 不静默重放写操作：观察到链路故障会报错并把端点标记为断开；下一次 `fs` 调用会自行重连，重新 `endpoint open` 只是可选的确认手段。改用 HTTPS（不施加消息加密）可消除加密上下文这一类成因。

**子进程输出编码实测提示（子进程写 UTF-8 → 乱码，且工具侧无从察觉）**：WinRM 的 PSRP runspace 是 Windows PowerShell 5.1，它的 **native-command 输出处理器用 runspace 的 console 码页**（实测主机 gb2312/cp936）解子进程的 stdout 字节。决定性变量只有**子进程自己的 `[Console]::OutputEncoding`**：脚本显式把它设成 UTF-8 时（触发的是脚本里那行 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`，不是「用了 pwsh 7」——pwsh 7 的 stdout 被重定向时仍按 console 码页输出），`跳过` / `完成` / `包` 回来就是 `璺宠繃` / `瀹屾垚` / `鍖?`（`?` 是 .NET 的码页替换回退，不是 U+FFFD）。这层解码在**远端**完成、pypsrp 交回的就是已解好的 `str`，所以 `exec` 与 `ps invoke`（同一 runspace 边界，两条都实测）拿到的只是「看起来正常的」乱码：行是 `ok` / `exit=0`，**无提示、无 token**——**字节证据在远端解码处就已丢失，改本工具的解码器没有任何用**。工具侧也**无法**识别它：同一段收到文本既可能来自「对端写 UTF-8 被按 cp936 解」，也可能来自「对端本来就发这些汉字」，两者逐字相同。

**两条实测有效**：① 让子进程按父进程的码页输出——把脚本里那行 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` 去掉即可（同一脚本改回默认后 `跳过` / `完成` / `包` 全部正确）；② 输出落盘再读回，**重定向必须交给 `cmd` 做**：

```text
# 落盘：cmd 的重定向把子进程 stdout 的原始字节直接写进文件（实测取回的字节与子进程输出逐字节相同）
exec  ep=<winrm-profile>  command='cmd /c "prog.exe > C:\Windows\Temp\mrc-out.txt"'
# 读回二选一：fs read 按编码探测解文本（meta 带 | encoding=…）；fs get 把原始字节取到本机
fs    op=read  ep=<winrm-profile>  path=C:\Windows\Temp\mrc-out.txt
fs    op=get   ep=<winrm-profile>  path=C:\Windows\Temp\mrc-out.txt  local=./mrc-out.txt
```

需要 stderr 时在 cmd 串里写 `2>&1`（子进程是 powershell.exe 时，它写进重定向 stderr 的是 CLIXML 记录，实测文件开头会多出 `#< CLIXML` 段，正文行不受影响）。**不要**用 PowerShell 自己的 `>` 落盘：它先按同一码页把子进程输出解成字符串再写文件，实测落盘的正是 `璺宠繃` / `瀹屾垚` / `鍖?`（UTF-16LE+BOM），坏文本从此固化。

**实测无效（别再试）**：照搬 SSH 的 `[Console]::OutputEncoding` prologue → 直接报 `句柄无效`（PSRP runspace 没有 console 句柄；SSH/WinRM 在这点上的不对称有正当理由）；只设 `$OutputEncoding` → 无效；`chcp 65001`（回显 `Active code page: 65001`）→ 无效；事后按 gb18030 重编码「修」回来 → **有损**：`.NET` 的替换回退已顶掉一个字节（`鍖?` 重编码后按 utf-8 解不开），信息不在我们手里。

### Agent 自助配置（优先）

```text
config op=ensure_home
config op=put_secret  name=id_ed25519  content=<pem 或密码>
config op=put_profile name=lab transport=ssh host=… username=…
         auth={"method":"private_key_path","key_path":"secrets/id_ed25519"}
endpoint op=open profile=lab
```

| `config` op | 作用 |
|-------------|------|
| `home` / `ensure_home` | 查看 / 创建布局 + 默认 `config.toml` |
| `get` / `list_profiles` / `get_profile` | 读配置（**无密钥正文**） |
| `put_profile` / `delete_profile` | 写/删 `profiles/*.toml`（删档案时连带删对应 notes 文件） |
| `put_secret` / `list_secrets` | 写密钥（只回路径）/ 列文件名 |
| `notes` | 主机备注：`action=read\|write\|append\|prepend\|stat\|rm`；文件在 `notes/{name}.md`；**正文只在 read** |

密钥 **只** 进 `secrets/`，响应永不回 body。备注不是密钥；不要用 `fs` 去改 MRC_HOME。

---

## 工具面（给 Agent）

### 主机

| Tool | 作用 |
|------|------|
| `endpoint` | `op=list\|open\|close`；open 用 `profile=`；close 用 `ep=` |
| `exec` | 非交互：`command` / `argv` / `script`（需 `ep=`） |
| `fs` | `list\|stat\|read\|write\|put\|get\|mkdir\|rm`（需 `ep=`）。`read` 完整 PNG/JPEG/GIF/WebP 以图片内容返回；默认上限 1 MiB，截断则报错 |
| `screen` | 真 PTY（需 `ep=`）；**不是**串口 |
| `ps` | WinRM 持久 PowerShell（需 `ep=`） |

典型：`endpoint open` → `exec` / `fs` / `screen` → `close`。

### Console（独立 — 嵌入式串口）

与 endpoint **完全分离**。不是 PTY、不是 SSH。open 即后台 CapturePump；`views` 查大缓冲。

```text
console op=list
console op=open   path=<device>  baud=115200
console op=views  id=con_01  mode=tail|since|contains
console op=send   id=con_01  data=…  newline=true
console op=close  id=con_01
```

| op | 含义 |
|----|------|
| `list` | 系统 console 设备名（禁止扫 `/dev`） |
| `open` | `path=` / `device=`，可选 `baud=`、`max_lines=`、`encoding=`（对端控制台码页，如 `gbk`；不写按 UTF-8 读。串口设备没有 profile 可探测，所以由操作者显式给出；非法码页名会警告并回落 UTF-8） |
| `send` | `id=` + `data=` / `data_b64=` |
| `views` | `mode=tail\|since\|contains` |
| `close` / `sessions` | 关闭 / 列会话 |

### Config

见上一节 [Agent 自助配置](#agent-自助配置优先)。

---

## CLI

**`mcp-remote-control-cli` 是 MCP 的平行入口**：同一套 Core，无 MCP 依赖；适合本地调试、脚本和 CI harness。业务逻辑不在 CLI 里重写。

| 入口 | 命令 | 进程模型 |
|------|------|----------|
| MCP Host | `mcp-remote-control`（stdio） | Host 拉起**一个**长进程，会话挂在该进程内 |
| CLI harness | `mcp-remote-control-cli …` | **每次调用通常是新进程**（见下方进程内限制） |

默认输出与 MCP 相同的 **Agent 文本**；加全局 **`--json`** 走机器轨。

### MCP tool ↔ CLI 对照

MCP 侧是「一个 tool + `op=`」；CLI 侧是「子命令 + 二级 op」。语义对齐，参数名多为 `--flag` 形式。

| MCP tool | CLI | 典型 op / 子命令 | 说明 |
|----------|-----|------------------|------|
| `endpoint` | `endpoint` | `list` · `open` · `close` | 主机生命周期；`--profile` / `--ep` |
| `exec` | `exec` | `--command` / `--argv` / `--script` | 非交互执行；也可用 `-- form` 后置 `command\|argv\|script` |
| `fs` | `fs` | `list` · `stat` · `read` · `write` · `put` · `get` · `mkdir` · `rm` | 需已 open 的 `--ep` |
| `screen` | `screen` | `open` · `send` · `close` · `list` | 真 PTY；**见进程内限制** |
| `ps` | `ps` | `open` · `invoke` · `close` | WinRM 持久 PS；会话进程内 |
| `console` | `console` | `list` · `open` · `send` · `views` · `close` · `sessions` | 串口，非 PTY/SSH |
| `config` | `config` | `home` · `ensure-home` · `get` · `list-profiles` · `get-profile` · `put-profile` · `delete-profile` · `put-secret` · `list-secrets` | 写 `MRC_HOME`，少人手改 TOML |

**仅 CLI / harness（MCP 无对应 tool）：**

| CLI | 用途 |
|-----|------|
| `doctor` | 离线：配置根、依赖、profile 语法；可选 `--create` |
| `selftest` | 离线 smoke：render 往返 + fixture 配置加载 |
| `replay` | PTY fixture 回放（CI；无真 TUI）。例：`replay --fixture bash_prompt --check` |

### 进程内状态限制（必读）

endpoint / screen / ps / console 的**打开会话只存在于当前 Python 进程**。

| 场景 | 结果 |
|------|------|
| MCP：Host 同一 stdio 进程内 `open` → `send` → `close` | 正常 |
| CLI：同一次命令里完成的操作 | 正常（单进程） |
| CLI：`screen open` 后，**另开 shell** 再 `screen send` | **`SCREEN_NOT_FOUND`**（新进程，注册表空） |
| 多进程 CLI 接力 endpoint/ps/console 会话 | 同样失败 |

因此：

- **人工调试 screen**：用 MCP Host 一次会话；或仓库内 **`scripts/harness/local_screen_smoke.py`** / `smoke_local.sh`（进程内 open→send→close）。
- **CI**：`ci.sh` 用 in-process smoke + `replay`，不用跨进程 CLI 拼 screen。

```bash
# ❌ 不要这样（两次进程，第二次找不到 session）
mcp-remote-control-cli screen open --ep local
mcp-remote-control-cli screen send --id scr_01 --text 'ls'

# ✅ MCP 同一连接内连续 tool call；或：
./scripts/harness/smoke_local.sh
```

### 示例

```bash
export MRC_HOME="$HOME/.config/mcp-remote-control"

# harness 专用
mcp-remote-control-cli doctor
mcp-remote-control-cli selftest
mcp-remote-control-cli replay --fixture bash_prompt --check

# 与 MCP 同构
mcp-remote-control-cli endpoint list
mcp-remote-control-cli endpoint open --profile local
mcp-remote-control-cli exec --ep local --command 'uname -a'
mcp-remote-control-cli fs list --ep local --path /tmp
mcp-remote-control-cli console list
mcp-remote-control-cli config home
mcp-remote-control-cli config ensure-home
mcp-remote-control-cli config list-profiles
mcp-remote-control-cli config put-secret --name id_ed25519 --content "$(cat ./id_ed25519)"
mcp-remote-control-cli config put-profile --name lab --transport ssh \
  --host 10.0.0.5 --username deploy \
  --auth-json '{"method":"private_key_path","key_path":"secrets/id_ed25519"}'

# 机器轨
mcp-remote-control-cli --json endpoint list
```

完整 flag：`mcp-remote-control-cli <cmd> -h` / `… <cmd> <op> -h`。

---

## 开发与 harness

**工程根 = 本目录**（`pyproject.toml` / `src/` / `tests/`）。

```bash
export MRC_HOME="$(pwd)/tests/fixtures/config"
uv venv --python 3.12 && source .venv/bin/activate
uv pip install --python .venv/bin/python -e ".[dev]"   # 含 pytest + ruff==0.15.12
ruff check src tests
./scripts/harness/ci.sh
```

`ci.sh`：pytest（无 integration）→ doctor/selftest → smoke_local / smoke_mcp → replay。

**GitHub Actions（已写好，推仓后生效）：** `.github/workflows/ci.yml`

| Job | 内容 |
|-----|------|
| `lint-and-test` | 矩阵 Python **3.11 / 3.12 / 3.13**：`ruff==0.15.12` → pytest（无 integration）→ doctor / selftest → smoke_local / smoke_mcp → replay |
| `package` | `uv build`；断言 sdist 含 **LICENSE/NOTICE**、**不含** `todo.md` |

触发：`push`/`pull_request` 到 `main`/`master`，以及 `workflow_dispatch`。  
本地等价：`./scripts/harness/ci.sh`（单版本；全量矩阵靠 Actions）。

| 可选 | 说明 |
|------|------|
| `MRC_INTEGRATION=1` + 真 `MRC_HOME` | 真机集成测 |
| `MRC_DOCKER=1 ./scripts/harness/docker_shell_matrix.sh` | Docker 方言矩阵 |

```text
.
├── LICENSE                     # Apache-2.0
├── NOTICE                      # attribution (must travel with redistributions)
├── pyproject.toml              # name = mcp-remote-control
├── README.md
├── .github/workflows/ci.yml
├── src/mcp_remote_control/
├── tests/
└── scripts/harness/
```

其它安装：

```bash
pip install "git+https://github.com/shiharuharu/mcp-remote-control.git@main"
python -m mcp_remote_control.mcp_server
```

---

## 故障排查

| 现象 | 原因 / 处理 |
|------|-------------|
| **找不到 `config` 工具** | Host 仍在跑 **旧 uvx 缓存**（仅 6 tools）。改用 **本地** `--with-editable` 或 `.venv` 入口，或 `uvx --refresh`，然后 **重连 MCP**。 |
| 想 **editable 却写了 `git+https://…`** | uv 的 editable **只接受本地路径**。先 `git clone`，再 `--with-editable "$REPO" --from "$REPO"`。只用远端请用 **`--from git+…`（非 editable）**。 |
| 输出被 **`{"result":"@exec…"}` 包一层** | 旧 SDK structured 包装；当前源码已 `structured_output=False`。`uvx --refresh` 后重连 MCP。 |
| `No module named 'mcp.server.fastmcp'` | 装到了 MCP SDK v1 API 路径但环境是旧缓存，或反过来。本项目 **0.2+ 需要 `mcp>=2`**（`MCPServer`）。`uvx --refresh`。 |
| `cwd=True` / `cd True` | 旧 probe 把 `cap_pwd` 写成路径（已修）。升级后 **close 再 open** endpoint。 |
| `uvx` / No solution · Python 版本 | 加 **`--python 3.12`**（或 ≥3.11）；确认仓库根有 `pyproject.toml`，勿乱加 `#subdirectory=`。 |
| 私有仓认证失败 | `git+ssh://…` 或配好 Git/SSH 凭据。 |
| `PROFILE_NOT_FOUND` | 检查配置根与 `profiles/<name>.toml`；或用 `config put_profile`。默认配置根：`~/.config/mcp-remote-control`。 |
| `NOTES_NOT_FOUND` | 该 profile 还没有 `notes/{name}.md`；先 `config op=notes action=write`。 |
| `NOTES_TOO_LARGE` | 备注超过 `max_body_chars`（默认 24000）；缩短后再 write/append/prepend。 |
| `NOTES_ENCODING_INVALID` | 备注文件不是 UTF-8（read/append/prepend 都会碰到）。**文件未被改动**；按 UTF-8 重编码，或用 `write` 整体覆盖。 |
| WinRM 输出中文变乱码（`跳过`→`璺宠繃`），行却是 `ok` / `exit=0` | PSRP runspace（5.1）按 console 码页解了子进程的 stdout；字节在远端已丢，工具侧无提示、也无法检测，改本工具的解码器没用。照 [WinRM 配置](#winrm-配置) 一节的「子进程输出编码实测提示」落盘再读回（或先让子进程别写 UTF-8）。 |
| `PS_CLOSE_UNCONFIRMED` | `ps close` 的 WSMan Delete 被拒，且服务端的应答没能证明远端 runspace 已消失：本进程已注销会话，但**远端 runspace 可能仍在**。先 `endpoint close` + `endpoint open` 重握手，再 `ps open`；期间把主机的 runspace 计数视为可能过时。服务端明确答「无此 shell」（selectors 不匹配该对象）属于删除已落地的证据，此时报 `ok` 而不是本码。 |
| `Permission denied` 指向奇怪家目录 | Host 里 `MRC_HOME` 若填了无效路径，删掉 `env.MRC_HOME` 用默认，或改成终端里 `echo "$HOME/.config/mcp-remote-control"` 的结果。 |
| `SCREEN_NOT_FOUND` | 会话仅在**当前** Python 进程内。勿跨两次 CLI 进程接力 `screen open` / `send`；见 [CLI · 进程内状态限制](#进程内状态限制必读)。 |
| 远端/源码已更新 Host 仍旧 | `uvx --refresh` / pin 新 commit / 开发用本地 `--with-editable`。 |
| WinRM `screen` | 预期 `UNSUPPORTED`；用 `ps`。 |
| GitHub Actions 不跑 | 工作流在 **本目录** `.github/workflows/ci.yml`；需在**本目录为 git 根**推到 GitHub（不要只推父目录 dev 树）。 |

**验证 tools 是否最新（在仓库根）：**

```bash
uvx --python 3.12 --with-editable . --from . python -c \
  "from mcp_remote_control.mcp_server import tool_names; print(tool_names())"
# 期望: ['endpoint', 'exec', 'fs', 'screen', 'ps', 'console', 'config']
```

---

## 安全提示

- 密钥只放 `$MRC_HOME/secrets/`，勿写进 profile 明文、勿提交 Git。  
- Agent 轨会 redact 常见敏感字段；**不要**把私钥/密码当普通日志贴出。  
- `put_secret` 的 content 只应在 tool 参数中传递一次，响应里不会回显 body。  
- 生产 SSH 慎用 `known_hosts = "none"`。  
- 仓库内 `tests/fixtures/config/secrets/*` 仅为 **dummy** 测试夹具（见该目录 `README.md`），不是可用密钥。

---

## 许可证

本项目以 **[Apache License 2.0](LICENSE)** 发布，归属说明见 **[NOTICE](NOTICE)**。

| 可以 | 约束（再分发时「必须带上」） |
|------|------------------------------|
| 商用、闭源产品内嵌、SaaS 使用 | 附带本 **LICENSE** 副本 |
| 修改源码并再分发 | 修改过的文件标明已变更 |
| 申请专利交叉许可（见协议正文） | **保留** 原有版权 / 专利 / 商标 / 归属声明 |
| | 若附带 **NOTICE**，衍生作品须按 4(d) **继续携带** 其中的归属信息 |

**不是 GPL：** 你的专有代码**不必**开源；Apache-2.0 不强制衍生作品整体以同一许可证发布，但**不能剥掉**本项目自带的许可与 NOTICE 要求。

---

## 名称对照

| 用途 | 名称 |
|------|------|
| 产品 / 发行包 / MCP 服务 | **mcp-remote-control** |
| MCP console script | `mcp-remote-control` |
| CLI console script | `mcp-remote-control-cli` |
| Python 包 / import | `mcp_remote_control` |
| 配置根默认 | `~/.config/mcp-remote-control` |
| 环境变量 | `MRC_HOME` |
