# WorkBuddy2API-Center 多账号网关

<p align="center">
  <a href="https://github.com/flower-elf/workbuddy2api-center/releases"><img src="https://img.shields.io/github/v/release/flower-elf/workbuddy2api-center?style=flat-square&color=2496ED&label=Release" alt="Release"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

本项目把腾讯的两个服务封装成 OpenAI 兼容接口：国际版 **[www.workbuddy.ai](https://www.workbuddy.ai)** 和国内版 **[codebuddy.cn](https://www.codebuddy.cn)**。Codex、Claude Code 等客户端指向同一个地址，就可以使用这两个服务。账号池、出口、密钥和配额都在 Web 看板上管理。程序只使用 Python 标准库。

> 本项目由 **吃白饭的蓝色大肥鱼（Deepseek-V4.1-Flash）** 配合开发。人类开发者提供架构和需求，大肥鱼完成界面编写和后端改进。

> 本仓库基于上游项目 **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)** 开发，看板界面参照了 **[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)**。项目的来源与贡献者见[七、来源与贡献者](#七来源与贡献者)。

## 特性

- **三种协议**：支持 OpenAI Chat Completions（旧版）、Responses 和 Anthropic Messages。
- **流式纠错**：上游中断、报错或缺少收尾事件时，网关按各自协议返回错误收尾，上游心跳原样转发。
- **智能调度**：国际版和国内版分别调度，尽可能节省 Token。
- **出站身份**：每个账号可以在 WB、VSC、CLI 三种官方身份之间切换，出站版本与 UA 与官方客户端一致。
- **密钥分配**：每个 API Key 可以绑定出口，并设置模型白名单、有效期、Token 配额、积分配额、IP 白名单和并发上限。
- **代理槽**：账号可以绑定固定的代理出口，网关支持自动发现 mihomo 端口。
- **用量审计**：网关为每条请求记录模型、Token、积分、密钥、来源 IP、状态码和实际使用的思考档位。
- **模型目录**：按官方桌面端清单整理，剔除补全通道与专线变体，支持手动刷新与每日自动获取。
- **积分与定价**：按 OpenRouter 价格估算每次请求的等价花费，支持人民币与美元切换。「费用估算」页再按模型的官方价格比较积分支出，算出省了多少。
- **活动与自动化**：国内版 Buddy 加油站签到、成长任务与连登活跃、猫猫旅行，国际版今日活跃打卡；后台调度器按固定时刻巡检。账号被上游限制时，网关立即停止全部账号并向客户端返回 503。
- **剩余用量与限额护栏**：账号级保留积分、每日 Token、每日积分、按模型 Token 四条护栏，另有剩余用量估算。
- **本地网络工具**：网关可代替客户端执行声明的 `web_search` 和 `web_fetch` 工具。
- **零依赖**：只需要 Python 3.9 或更新版本。项目提供 Windows、macOS、Linux 启动脚本和 Docker 镜像。

## 目录

- [一、快速开始](#一快速开始)
- [二、客户端接入](#二客户端接入)
- [三、看板](#三看板)
- [四、核心机制](#四核心机制)
- [五、接口一览](#五接口一览)
- [六、命令行参数与环境变量](#六命令行参数与环境变量)
- [七、来源与贡献者](#七来源与贡献者)
- [八、免责声明](#八免责声明)

---

## 一、快速开始

### 本机运行

本机运行需要 Python 3.9 或更新版本，不需要用 `pip install` 安装任何依赖。

| 系统 | 启动方式 |
|---|---|
| Windows | 双击 `start-wb-proxy.bat`，保持窗口运行 |
| macOS | 双击 `start-wb-proxy.command`；首次启动遭到 Gatekeeper 拦截时，右键选择“打开” |
| Linux 与 macOS 终端 | 执行 `./start-wb-proxy.sh`，可以在后面加端口号，例如 `./start-wb-proxy.sh 9000` |

如果不想安装 Python，可以从 [Releases](https://github.com/flower-elf/workbuddy2api-center/releases) 下载绿色包 `wb-proxy-center-v*.zip`。绿色包只为 Windows 附带精简 Python 运行时，Windows 解压后直接双击 `start-wb-proxy.bat`。macOS 和 Linux 使用系统 Python，解压后同样用 `.command` 或 `.sh` 脚本启动。

macOS 和 Linux 的脚本按顺序查找三个位置：包内的 `python/bin/python3`、`/usr/bin/python3` 和 Homebrew 的 python3。macOS 上没有 Python 时，执行 `xcode-select --install` 或 `brew install python`。

解压 zip 后如果提示权限不足，先执行下面的命令。

```bash
chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command
```

启动后可以访问下面两个地址。

- **API 地址**：`http://127.0.0.1:8788/v1`
- **看板**：`http://127.0.0.1:8788/`，默认密码为 `admin`

首次启动时账号池为空。登录看板后，点击“账号”页的 **“添加账号 (OAuth)”**，扫码或打开授权链接完成登录，就可以开始使用。

### 面板密码

面板密码只用于打开看板，与 API Key 相互独立。默认密码为 `admin`，登录后请立即在“设置 → 面板密码”里修改，也可以在启动时用 `--panel-password` 指定。

网关以 PBKDF2-SHA256 摘要的形式，把密码保存在 `accounts/settings.json`。登录状态保存在浏览器会话里，关闭浏览器或重启网关后需要重新登录。

### 局域网共享

手机、平板或其他电脑要访问网关时，使用局域网模式启动。

- **Windows**：双击 `start-wb-proxy-lan.bat`。
- **macOS**：双击 `start-wb-proxy-lan.command`。
- **终端**：执行下面的命令。

```bash
./start-wb-proxy-lan.sh               # 端口 8788，自动生成并复用 API Key
./start-wb-proxy-lan.sh 8788 我的Key   # 自定义端口与 Key
```

局域网模式强制要求 API Key。首次启动时，网关生成一个随机 Key，保存到 `accounts/settings.json` 并打印在终端，重启后可继续使用。客户端的 Base URL 填写 `http://<本机局域网IP>:8788/v1`。

macOS 首次监听端口时，会询问是否允许 Python 接受连接，请选择“允许”。macOS 15 以上版本还要在“系统设置 → 隐私与安全性 → 本地网络”里允许终端访问。执行 `./allow-firewall.command` 可以查看防火墙状态，并把 Python 加入允许列表。

### Docker

一键脚本适用于 Linux、macOS 和 NAS，需要先安装 Docker。

```bash
curl -fsSL https://raw.githubusercontent.com/flower-elf/workbuddy2api-center/main/quick-deploy.sh | bash
```

脚本在当前目录创建 `workbuddy2api-center` 文件夹，拉取 GHCR 镜像并启动容器。再次运行同一条命令，就可以更新到最新版本，账号和用量数据都保留在文件夹里。

仓库根目录的 `docker-compose.yml` 是 Compose 模板，只拉取 GHCR 镜像，用法如下。

```bash
docker compose up -d                          # 启动
docker compose pull && docker compose up -d   # 更新；固定版本时把 image 换成具体版本号，如 :0.2.0
docker compose logs -f                        # 查看日志
```

也可以直接用 `docker run` 启动预编译镜像。正式版会同步更新 `latest` 标签，预发布版只有版本标签。

```bash
docker run -d --name wb-proxy-center --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key ghcr.io/flower-elf/workbuddy2api-center:latest
```

从本地源码构建镜像时，执行 `docker compose -f docker-compose.build.yml up -d --build`。

- **数据目录**：`./accounts` 保存账号凭证、设置和模型目录缓存，`./usage` 保存请求记录和积分变动记录，两个目录都要挂载。
- **鉴权**：容器以局域网模式启动。不传 `API_KEY` 时，网关自动生成一个，执行 `docker compose logs wb-proxy-center | grep -i "api key"` 可以查看。
- **修改端口**：`PORT` 决定容器内监听的端口，端口映射的右侧必须与它相同，例如 `-e PORT=9000 -p 9000:9000`。

### 运行测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只运行名字里含 realm 的套件
```

测试共有 156 个套件：142 个 Python 套件 + 14 个 JavaScript 套件。看板的 JavaScript 套件位于 `tests/javascript`。JavaScript 套件需要 Node.js 22 或更新版本，没有安装 Node.js 时会跳过并给出提示。

GitHub Actions 在三个环境里运行同一条命令：Ubuntu 的 Python 3.9 和 3.12，以及 Windows 的 Python 3.12。`tests/manual/_mobile_check.py` 是需要手动运行的 Playwright 布局检查器，不属于测试套件。

---

## 二、客户端接入

- **Base URL**：`http://127.0.0.1:8788/v1`，局域网为 `http://<局域网IP>:8788/v1`。
- **API Key**：本机运行且没有配置任何 Key 时，可以留空或任意填写。配置过 Key 或开启局域网模式后，请使用“密钥”页里的 Key。
- **模型名称**：填写 `/v1/models` 里列出的任意模型 ID，例如 `deepseek-v4.1-flash`、`glm-5.3`。

**（1）Codex CLI 等 Responses 客户端**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在“密钥”页添加的 API Key"
```

**（2）Claude Code 等 Messages 客户端**

**积分风险**：网关把 Anthropic Messages 转换为 WorkBuddy 的 Chat 或 Responses 请求，`cache_control` 会被忽略。上游报告过长会话的缓存命中率偏低、单次消耗偏高，因此这个入口默认关闭。确认理解风险后，在“设置 → 高级”里打开“Anthropic Messages 入口”。关闭时 `/v1/messages` 返回 400，请求不会转发到上游。

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_AUTH_TOKEN="你在“密钥”页添加的 API Key"
export ANTHROPIC_MODEL="deepseek-v4.1-flash"
```

设置页的 `messages_format` 可以选择 `openai-completions` 或 `openai-responses` 管线。两种管线都会做协议转换：`thinking` 历史块、`top_k`、`cache_control` 与 `betas` 会被忽略，`count_tokens` 只返回网关自己的估算值。

**（3）curl**

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer 你的 Key" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

所有 `/v1/...` 接口都可以去掉 `/v1` 前缀访问，例如 `/chat/completions`。这样可以兼容把 Base URL 填成根地址的客户端。

---

## 三、看板

看板地址为 `http://127.0.0.1:8788/`。底部菜单栏用于切换页面，右上角用于切换正在查看的版本（国际版或国内版），按 `Ctrl+K`（macOS 为 `⌘K`）可以搜索页面。“面板选项”里可以切换主题、调整自动刷新间隔和退出登录。

| 页面 | 内容 |
|---|---|
| 仪表盘 | 账号数、可用账号、积分余额、今日请求与 Token；近 14 天调用趋势；切换网关默认出口；调度器状态 |
| 账号 | 搜索、筛选与排序账号；每个账号的积分、有效期、出站身份、优先级、代理槽、备注与操作按钮 |
| 任务 | 调度器状态与日志、国内版任务与福利（Buddy 加油站、成长任务与猫猫日常，可选择单个账号执行）、国际版今日活跃打卡、积分变动记录 |
| 积分 | 每个账号每一笔套餐的来源、已用、剩余与到期时间；页头显示 15 天内到期的积分总数 |
| 密钥 | API Key 的出口、模型限制、有效期、配额、IP 白名单、用量与接入示例 |
| 模型 | 模型目录与能力规格；可以按账号查看，也可以选「全部账号」看每个模型的账号覆盖率；被筛选规则排除的模型默认停止处理，勾选后才会放行 |
| 测试台 | 用面板会话直接调试模型，可以指定账号 |
| 用量 | 按时间范围统计请求、Token、积分与缓存命中，并按账号、密钥与模型细分；积分卡片下方显示本月消耗 |
| 费用估算 | 按模型的官方价格折算当前范围的请求费用，再减去积分折算的支出，算出省了多少 |
| 请求日志 | 每条请求的全部字段，可以按时间、版本、密钥、状态、模型、账号与来源 IP 筛选 |
| 运行日志 | 网关控制台输出，可以复制、导出或清空 |
| 设置 | 网关行为、费用估算、代理槽、面板密码、高级功能、运维与运行信息 |

### 账号

- **添加账号**：点击“账号”页的“添加账号 (OAuth)”，选择国际版或国内版，再选择以 WB、VSC 或 CLI 哪个官方客户端登录，然后扫码或打开授权链接登录。网关自动检测回调。所选客户端就是新账号的出站身份。
- **优先级**：数字越小，越先选中，默认值为 100。
- **并发上限**：限制同一账号、同一模型同时在途的请求数，填写 0 表示不限。提示里会显示当前在途数。
- **出站身份**：在 WB、VSC、CLI 之间切换，切换后立即保存。
- **测试**：向这个账号发送一条内容为 `hi` 的真实对话，确认它能否正常回复。
- **备注**：最长 100 字，保存在账号文件里，可以用来搜索。
- **模型排除**：国内版与国际版账号都可以设置排除名单，名单里的模型不再派给这个账号，其他账号照常接单。入口是账号行上的「模型排除」按钮，账号信息里会显示已排除的数量。
- **导入与导出**：导出的文档可以再次导入，也可以手写账号列表导入。
- **今日活动**：账号页顶部显示今天的完成情况。国内版是 Buddy 加油站与成长计划连登活跃，国际版是今日活跃打卡；每个账号的状态也标在账号行上。国际版可以只给今天还没打卡的账号补跑一次，已经打过的不再重复消耗额度。

从桌面客户端导入账号的功能暂时停用。桌面客户端自 2026-09-24 起加密存储令牌，网关无法读出可用的令牌，因此看板隐藏了这个入口。相关代码保留在 `app/web/js/pages/accounts/dialogs.js` 的 `openDesktopScanDialog()` 和接口 `/accounts/import/desktop`。

### 积分

- **积分构成**：“积分”页按来源列出每个账号的套餐。同一来源的多笔到账合并成一行，展开后可以查看每一笔。
- **15 天内到期积分**：页头统计当前出口 15 天内到期的剩余额度，只算尚未过期、带到期时间且还有剩余的套餐。
- **积分到期**：账号的积分旁显示最近一笔“额度 · N 天后到期”。一天内到期的显示为红色，七天内到期的显示为黄色。
- **积分变动**：网关每次读取余额时，都与上一次的余额比较。余额增加时，网关在 `usage/credit_events.jsonl` 记录一条变动，只保留最近 30 天的记录。“任务”页分页显示这些记录，每页 20 条。

### 费用估算

“费用估算”页按模型的官方价格折算所选范围的请求费用，再减去积分折算的支出，得到省了多少。这两个口径都在“设置 → 费用估算”里填写。估算只统计已完成的请求；失败请求即使上游已经扣了积分，也不进入这张表。

- **官方价格**：单位为元 / 百万 Token，分输入、缓存命中与输出三项。缓存命中价格留空时按输入价格计费。默认只内置 deepseek-v4.1-flash 的官方高峰时段价格；没有配置价格的模型按 0 计，不参与估算。
- **积分折算单价**：1 积分折合多少元。默认 0 表示积分不计支出，省了多少等于官方价格合计。

### 高级设置

“设置 → 高级”集中放置这些功能开关，修改后立即生效。同名环境变量优先于面板设置，面板会标出它当前的取值。

- **Anthropic Messages 入口**：打开 `/v1/messages`。默认关闭，长会话可能增加积分消耗。
- **亲和调试日志**：把派生的亲和键与页内轮转写入运行日志。默认关闭，仅用于排查。
- **prompt_cache_key**：向上游附带该字段。实测没有改变缓存命中率，仅保留作实验开关。默认关闭。
- **免费模型积分回退**：免费模型额度耗尽后回退到同系列付费模型。默认关闭。
- **上游连接复用**：复用上游 TCP/TLS 连接。默认开启。
- **用量聚合缓存**：把用量聚合状态写入磁盘，重启后只读新增日志。默认开启。
- **日与小时分桶**：窗口统计使用按日/小时分桶的增量结果。默认开启。
- **最近请求索引**：为请求日志建立深分页索引。默认关闭。

“设置 → 运维”可以调整定价刷新与变体继承、积分后台刷新间隔、日志写入数据库与三条保留上限、小时层与日桶保留、上游超时、可信反向代理、提示词模式与文件、每日版本检查。

### 密钥

不同客户端可以使用不同的 Key。每个 Key 可以单独设置下面几项。

- **出口**：固定使用国际版或国内版。没有绑定出口时，跟随网关默认出口。
- **模型限制**：设置允许调用的模型，支持 `*` 通配，例如 `deepseek*`。请求列表以外的模型时，网关直接返回 400，不发往上游。
- **有效期与配额**：Key 过期时返回 401，Token 或积分配额用完时返回 429。
- **IP 白名单**：每行填写一个 IP 或 CIDR（无类别域间路由）网段。来源不在名单里时返回 403。
- **用量**：统计从上次“重置用量”起累计的用量。
- **区域校验**：Key 绑定的出口不提供所请求的模型时，网关直接返回 400 并说明原因。

Key 保存在 `accounts/settings.json`。在看板保存过 Key 之后，启动参数 `--api-key` 不再生效。只有面板会话能够读取完整的 Key。

### 模型冷却

上游对某个账号的某个模型返回 429 时，只有这个模型进入冷却，账号仍然服务其他模型。

网关按以下顺序确定恢复时间：首先取上游消息里写明的重置时刻，其次取 HTTP `Retry-After` 头，两者都没有时冷却 60 秒。冷却状态只保存在内存里，重启后清空。

上游返回 401 时，网关先刷新这个账号的令牌并重试同一个账号一次；刷新失败或重试仍是 401 才换号。同一次请求里每个账号最多刷新一次。

---

## 四、核心机制

### 账号调度

每次请求都按以下规则选择账号。

1. **可用性**：跳过停用、冷却中、余额不高于保留积分、今日 Token 已达限额的账号。
2. **会话粘性**：同一段对话固定使用同一个账号，只有这个账号无法接单时才更换账号。客户端没有给出会话标识时，网关按前两条消息识别对话。这个功能默认打开，开关在“设置 → 网关行为 → 会话粘性”。关闭后，每一轮请求都重新选择账号。
3. **分配规则**：打开“智能分配”时，网关按积分到期节奏与最近一小时的忙闲分配，剩余额度接近耗尽的账号与模型组合略优先；关闭时，按优先级与轮询顺序分配，临期积分窗口内的账号先接单。开关在“设置 → 网关行为 → 智能分配”，默认打开。

此外，下面四项设置也会影响选号。

- **保留积分**：余额不高于设定值时，账号暂停接单，这样可以避免余额用到 0 后收到上游的提醒短信。填写 0 表示关闭这项功能。网关根据最近一次查询到的余额进行判定。
- **每日 Token 限额**：今日用量达到设定值时，账号暂停接单，本地时间 0 点后恢复。填写 0 表示不限额。
- **临期积分优先窗口**：积分在这个天数内到期时优先分配请求，默认 7 天。填写 0 表示关闭。智能分配与轮询都使用这个窗口。
- **单账号单模型并发上限**：每个账号可以在账号页单独设置这个数值，填写 0 表示不限。具体规则如下。
  - 网关按“同一账号 + 同一模型”分别计数。例如，同一账号的 `glm-5.3` 和 `deepseek` 各自计数，互不占用名额。
  - 选号时，网关直接跳过名额已满的账号，把请求交给其他账号。
  - 对话绑定的账号名额暂满时，其他账号接手这个请求，对话绑定随之转到接手的账号。接手账号重新计算了完整前缀，此后缓存保存在它那里。
  - 本地网络工具的后续轮次沿用这次请求已经占用的名额。更换账号时，名额随之转到新账号。
  - 整个出口的可用账号都已满时，网关返回 429。测试台指定的账号名额已满时，同样返回 429。
  - 在途数量只保存在内存里，重启后归零。账号页的“并发上限”列会提示当前正在服务的请求数。

### 上游限制后的停止

任何一个账号被上游按风控限制时，网关立即停止，不再把请求发给其他账号。限制发生后继续轮询下一个账号，正是把整个账号池关联起来的原因。

- **判定**：HTTP 403，正文带 code 11140 或“账号存在异常行为，已被限制使用”。内容审核返回的 403 走原来的降级提示词重试，不会触发停止。
- **动作**：停用全部账号并写回账号文件，调度器、积分刷新与模型目录获取一并暂停。
- **返回**：所有对话请求返回 503，正文说明停止原因与恢复方式。
- **恢复**：网关不自动恢复。确认上游已解封后，在账号页点“全部启用”或逐个启用账号。

### 出站身份

每个账号有三种官方身份，分别对应不同的出站端点和配额。

- **WB**：WorkBuddy 桌面客户端。
- **VSC**：VSCode 插件，国内版使用 `www.workbuddy.cn`。
- **CLI**：CodeBuddy CLI。

### 设备指纹

网关用账号 UID 加固定盐值做单向哈希，派生出机器码和会话标识。同一账号每次出站都表现为同一台虚拟设备，不同账号之间互不关联。

### 模型目录

- **来源**：上游 `GET /v3/config` 的 `agents[cli].models`，与官方桌面端一致。上游新增的模型不需要发布新版本，就会出现在 `/v1/models`。
- **过滤**：去掉代码补全专用模型（如 `codewise-*`）、多云专线变体（如 `*-volc`）、档位别名、`auto`、`-sg` 和 `-x` 变体。同名模型只保留 0.00 倍率的一档。
- **档位别名**：`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model` 与国内版的 `auto` 是上游的虚拟档位，不进入 `/v1/models`。被筛选规则排除的模型默认停止处理，请求会被直接拒绝；在模型页打开「显示被排除的模型」后勾选它，才会开始转发。
- **缓存**：获取成功后，网关把目录保存到 `accounts/cache/model_catalog.json`，24 小时内直接使用。获取失败时，网关依次使用过期缓存、桌面端本地文件和内置快照。

国际版和国内版的同名模型分别调度。请求只使用当前出口，网关暂时不跨区域混合轮询。

### 定时任务

调度器按 UTC+8 时间运行，与机器的时区设置无关。

| 时间（默认） | 任务 |
|---|---|
| 每天 09:00 与 21:00 | 国内版 Buddy 加油站签到与猫猫旅行；国际版每日活跃打卡 |
| 每天 22:00 | 自动刷新令牌剩余有效期不足 2 小时的账号 |
| 每天 01:00 | 夜猫子任务 |

这四组整点都可以在“设置 → 网关行为”里单独调整：填写 0 到 23 的整点，用逗号分隔；留空表示这组不跑。默认值保持不变。

在“任务”页可以手动触发巡检，停止或启动调度器，也可以按账号手动执行 Buddy 加油站签到和今日活跃打卡。

- **Buddy 加油站**：国内版每天领取一次加油站积分。调度器在 09:00 与 21:00 补漏，签到记录同时出现在积分变动记录里。任务页可以选单个账号领取。
- **随机间隔**：调度器在处理每个账号前随机等待 2 秒到设定值之间的时间，默认 5 秒。这个上限在“设置 → 网关行为 → 任务随机间隔”里调整，最小 2 秒，最大 600 秒。
- **成长计划连登**：国内版每天上报一次活跃，点亮连续登录天数。上报失败不会影响 Buddy 加油站。
- **国内版成长任务**：自动接取任务、上报行为事件并领取奖励。
- **猫猫日常**：猫猫在家时自动派出；回来后先领奖再派出，以免奖励作废。
- **国际版每日活跃打卡**：为当天还没有活跃的账号发送一条轻量对话。每个账号每天只执行一次，执行记录保存在账号文件的 `lastDailyChat`。
- **网页通道打卡**：按网页端的流程完整执行一轮会话，领取每日 30 积分，Pro 账号为 50 积分。这项任务会消耗少量积分，所以点击按钮后会先弹出确认框。一轮最多等待 120 秒，可以用 `WB_WEB_TURN_TIMEOUT` 调整。“设置 → 网关行为”里可以关闭这项任务。

### 代理槽

代理槽在“设置 → 代理槽”里配置，然后在“账号”页为每个账号选择要绑定的槽位。

- **跟随**：槽位地址变化时，绑定的账号自动使用新地址。槽位停用或删除时，账号改为直连。
- **自动发现**：扫描同一 Docker 网络里的 mihomo 出口，默认主机为 `cli-proxy-mihomo`，端口为 `17901-17910`。扫描结果显示每个出口的公网 IP 和延迟。
- **自动分配**：把启用中的账号依次分配到各个槽位。
- **测试**：通过这个代理真实访问一次外网。

### 本地网络工具

有些客户端（如 Codex App）会声明 `web_search` 和 `web_fetch` 工具，上游无法执行这两个工具。打开“设置 → 网关行为 → 本地网络工具”后，网关在本地执行它们，再把结果交给模型。

- **搜索**：使用 DuckDuckGo HTML 版，在国内网络下可能无法连接。
- **抓取**：只访问公网地址。域名解析出本机、私网、链路本地或保留地址时，网关拒绝请求。每次重定向都会重新检查，最多重定向 5 次。单个响应最多 4 MiB，一次抓取最多 40 秒。
- **轮数**：默认最多 3 轮，可以用 `WB_MAX_WEB_ROUNDS` 调整，上限为 8 轮。每一轮都会多调用一次上游。

这个功能默认关闭。关闭时，网关原样转发工具声明。

### Messages 接口

`POST /v1/messages` 按 Anthropic Messages 协议收发，支持 `system`、多轮对话、图片、工具调用、思考内容和流式输出，错误也按这个协议的格式返回。这个入口默认关闭，在“设置 → 高级”里打开“Anthropic Messages 入口”才会启用；关闭时返回 Anthropic 格式的 400，请求不会转发到上游。

翻译方式在“设置 → 网关行为 → Messages 接口翻译”里选择。

- **openai-completions**：默认选项，翻译成对话补全请求。
- **openai-responses**：翻译成 Responses 请求。客户端使用 Responses 风格的自定义工具时，选择这一项。

### 流式转发

三种协议的收尾事件各不相同：Chat Completions 为 `data: [DONE]`，Responses 为 `response.completed`，Messages 为 `message_stop`。

上游截断数据流的情况包括连接中断、读取异常、上游自己发出错误帧，以及没有收尾事件就结束。这时网关按各自协议改发错误收尾，客户端据此把这条回答当作失败。

- **Chat Completions**：发送一个 `{"error": {...}}` 事件，不再补发 `[DONE]`。
- **Responses**：发送 `response.failed`，响应体的 `status` 为 `failed`，`error` 里带有原因和上游的错误码。
- **Messages**：发送 `error` 事件，不再发送 `message_stop`。
- **非流式请求**：返回 `502`。

已经发给客户端的正文原样保留，网关不重新请求上游，因为重新请求会再次生成、再次计费。上游在思考期间发送的 SSE 注释心跳（`: ping`）原样转发，这样客户端的空闲超时不会在长时间静默时提前断开连接。

---

## 五、接口一览

### 对外接口

对外接口使用 `Authorization: Bearer` 或 `x-api-key` 鉴权。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /v1/chat/completions | Chat Completions 协议，支持流式 |
| POST | /v1/completions | 旧版补全协议 |
| POST | /v1/responses | Responses 协议，支持流式与自定义工具 |
| POST | /v1/messages | Anthropic Messages 协议 |
| GET | /v1/models | 模型列表，含能力、规格、目录来源 `source` 与获取时间 `fetched_at` |

`GET /health` 用于外部健康检查，不需要凭据。它始终返回 `200` 和 `{"ok": true, ...}`，包含 `realm`、`accounts`、`accounts_ready` 与 `api_key_required`；即使没有可用账号也不会返回 503。已授权请求还会返回 `uid`、`domain`、`issuer`、`credential_file` 与 `expires_at`。

### 看板接口

看板接口需要面板会话，浏览器登录后会自动携带。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 看板，静态资源经 `/web/<路径>` 提供 |
| GET、POST | /realm | 读取或切换网关默认出口 |
| GET | /panel/status | 运行信息，包括版本和账号统计；没有面板会话时返回 401 |
| GET | /panel/models | 模型目录的管理视图，含已关闭的模型 |
| POST | /panel/login | 面板登录，返回会话令牌 |
| POST | /panel/logout | 退出登录 |
| POST | /panel/password | 修改面板密码 |
| GET | /accounts | 账号列表 |
| GET | /accounts/export | 导出账号文档 |
| POST | /accounts/import | 导入账号文档，支持 `dryRun` 与 `overwrite` |
| POST | /accounts/import/desktop | 只读扫描桌面端凭据；传 `path` 导入一个，传 `all` 导入全部 |
| POST | /accounts/refresh | 刷新一个或全部账号的令牌 |
| POST | /accounts/test | 测试账号，可以临时指定模型 |
| POST | /accounts/set | 修改单个字段：`enabled`、`priority`、`note`、`concurrencyLimit`、`proxy`、`proxySlot` |
| POST | /accounts/set-all | 启用或停用全部账号 |
| POST | /accounts/delete | 删除账号 |
| POST | /accounts/product | 切换出站身份：WB、VSC、CLI |
| POST | /accounts/checkin | 国内版 Buddy 加油站签到 |
| POST | /accounts/daily-chat | 国际版每日活跃，使用桌面端对话；传 `uids` 只给名单里的账号打卡 |
| POST | /accounts/daily-chat-web | 国际版每日活跃，使用网页通道 |
| POST | /accounts/login/start | 发起 OAuth 登录，`product` 取 `workbuddy`、`vscode` 或 `cli`，返回授权链接与二维码 |
| GET | /accounts/login/poll | 查询登录状态 |
| POST | /accounts/login/cancel | 取消登录 |
| GET、POST | /accounts/credits | 刷新积分；GET 刷新全部账号，POST 可以用 `uid` 或 `realm` 指定范围 |
| GET | /accounts/credit-events | 最近 30 天的积分变动记录，按 `realm` 筛选，用 `limit` 与 `offset` 分页 |
| GET | /usage | 用量汇总，可选 `realm` 与时间范围 |
| GET | /usage/recent | 请求记录，可以按 `realm`、`key`、`status`、`model`、`account`、`ip`、`range`、`since`、`until` 筛选并分页 |
| GET | /usage/trend | 调用趋势；`days=N` 按天统计，`range=today` 按小时统计 |
| GET | /usage/analytics | 按账号、模型与密钥的统计 |
| GET | /usage/cost-estimate | 费用估算，按模型返回官方价格、积分折合与省了多少；参数与 `/usage` 相同 |
| GET | /usage/month-credit | 本月消耗的积分，按 `realm` 汇总 |
| GET | /usage/by-account | 按账号的用量明细 |
| GET | /usage/perf | 首字延迟与生成速度 |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 领取国内版成长任务奖励 |
| POST | /tasks/travel | 猫猫旅行，派出或领奖 |
| GET | /scheduler | 调度器状态与日志 |
| POST | /scheduler/trigger | 立即执行一次巡检 |
| POST | /scheduler/toggle | 停止或启动调度器 |
| GET | /settings | 读取设置，以及每个 Key 的用量和状态 |
| POST | /settings/save | 保存设置；`reset_key_usage` 重置某个 Key 的用量 |
| GET | /settings/reveal | 读取一个 Key 的完整值 |
| GET、POST | /proxy/slots | 读取代理槽列表 |
| POST | /proxy/slots/save | 保存代理槽列表 |
| POST | /proxy/slots/test | 测试一个代理槽 |
| POST | /proxy/discover | 扫描 mihomo 出口，返回公网 IP 与延迟 |
| GET | /logs | 运行日志 |
| GET | /logs/export | 下载日志文件 |
| POST | /logs/clear | 清空运行日志 |

---

## 六、命令行参数与环境变量

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，默认为 `127.0.0.1`；对应环境变量 `HOST` |
| `--port` | 监听端口，默认为 `8788`；对应环境变量 `PORT` |
| `--lan` | 监听全部网卡并强制要求 API Key，相当于 `--host 0.0.0.0` |
| `--api-key` | 要求 `/v1/*` 请求携带这个 Key；在看板保存过 Key 后，网关忽略这个参数；也可以使用环境变量 `API_KEY` 或 `WB_PROXY_KEY` |
| `--panel-password` | 启动时设置面板密码 |
| `--accounts-dir` | 账号凭证与设置目录，默认为 `./accounts`；也可以使用环境变量 `ACCOUNTS_DIR` |
| `--usage-dir` | 用量记录目录，默认为 `./usage`；也可以使用环境变量 `WB_PROXY_USAGE_DIR` |
| `--import-desktop` | 导入桌面端凭据后退出 |
| `--system-prompt` | 请求没有 system 消息时注入的内容 |
| `--user-agent` | 覆盖出站 User-Agent，默认与官方 WorkBuddy 客户端相同 |
| `--info` | 指定 WorkBuddy `*.info` 凭据文件路径 |

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 监听地址 |
| `PORT` | `8788` | 监听端口 |
| `API_KEY` | 空 | 启动时固定 API Key |
| `WB_PROXY_KEY` | 空 | 与 `API_KEY` 作用相同 |
| `ACCOUNTS_DIR` | `./accounts` | 账号凭证与设置目录 |
| `WB_PROXY_USAGE_DIR` | `./usage` | 用量记录目录 |
| `WB_PROXY_CATALOG_DIR` | `./catalog` | 模型目录缓存目录 |
| `WB_PROXY_DEFAULT_REALM` | `intl` | 启动时的默认出口 |
| `WB_ENABLE_ANTHROPIC_MESSAGES` | `0` | 高级设置“Anthropic Messages 入口”；环境变量优先于面板设置 |
| `WB_ENABLE_FREE_MODEL_CREDIT_FALLBACK` | `0` | 高级设置“免费模型积分回退”；环境变量优先于面板设置 |
| `WB_PROMPT_CACHE_KEY` | `0` | 高级设置“prompt_cache_key”；环境变量优先于面板设置 |
| `WB_AFFINITY_DEBUG` | `0` | 高级设置“亲和调试日志”；环境变量优先于面板设置 |
| `WB_UPSTREAM_KEEPALIVE` | `1` | 高级设置“上游连接复用”；环境变量优先于面板设置 |
| `WB_USAGE_CACHE` | `1` | 高级设置“用量聚合缓存”；环境变量优先于面板设置 |
| `WB_USAGE_DAY_BUCKETS` | `1` | 高级设置“日与小时分桶”；环境变量优先于面板设置 |
| `WB_RECENT_INDEX` | `0` | 高级设置“最近请求索引”；环境变量优先于面板设置 |
| `WB_TRUSTED_PROXIES` | 空 | 运维设置“可信反向代理”；环境变量优先于面板设置 |
| `WB_UPDATE_CHECK_ENABLED` | 空 | 运维设置“自动检查更新”；环境变量优先于面板设置 |
| `WB_UPDATE_CHECK_INTERVAL_HOURS` | 空 | 运维设置“自动检查间隔”，单位小时，取 1～720；环境变量优先于面板设置 |
| `WB_MODEL_SCOPED_ERRORS_PENALIZE` | 空 | 设为 `1` 时恢复旧行为，所有 5xx 都记入账号故障；默认只把普通 5xx 记入账号 |
| `WB_CLIENT_TIMEOUT` | `120` | 客户端连接读写超时，单位为秒 |
| `WB_MAX_PAYLOAD_BYTES` | `16777216` | 请求体上限，即 16 MiB，超出时返回 413 |
| `WB_MAX_CONCURRENT_CHAT` | `32` | 同时处理的对话请求数；设为 `auto` 时按可用账号数计算 |
| `WB_CHAT_SLOT_WAIT` | `30` | 并发已满时排队等待的秒数 |
| `WB_MAX_WEB_ROUNDS` | `3` | 本地网络工具最多运行的轮数，上限为 8 |
| `WB_WEB_TURN_TIMEOUT` | `120` | 网页通道打卡一轮的等待上限，单位为秒 |
| `WB_WEB_ENGINES` | 内置链 | 搜索引擎顺序，逗号分隔，例如 `duckduckgo,bing` |
| `WB_WEB_COOLDOWN` | `600` | 被限流引擎的冷却秒数，0 表示不冷却 |
| `WB_WEB_CACHE_TTL` | `600` | 搜索与抓取缓存秒数，0 表示关闭 |
| `WB_WEB_MIN_INTERVAL` | `0.5` | 对外请求的最小间隔秒数 |
| `WB_WEB_BUDGET` | `25` | 单次搜索所有引擎的总预算秒数 |
| `WB_WEB_TOOLS_PROXY` | 空 | 搜索请求的出口代理；`web_fetch` 为固定解析 IP 的安全连接，不走这个代理 |
| `WB_STATS_TTL` | `15` | 用量统计缓存秒数 |
| `WB_PROXY_DISCOVER_HOST` | `cli-proxy-mihomo` | 代理槽自动发现的主机名 |
| `WB_PROXY_DISCOVER_PORTS` | `17901-17910` | 代理槽自动发现的端口范围 |
| `TZ` | `Asia/Shanghai` | 容器时区；定时任务始终按 UTC+8 时间计算 |

## 七、来源与贡献者

本仓库基于 [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub) 开发，看板视觉参考 [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)，部分交互与部署配方参考 [linux-do/cdk](https://github.com/linux-do/cdk)。

完整的贡献者名单见 [GitHub 贡献者图表](https://github.com/flower-elf/workbuddy2api-center/graphs/contributors)。

---

## 八、免责声明

1. 本项目是非官方的自托管网关，仅供技术研究和学习，以及在私有环境中使用个人合法授权的账号进行测试。
2. 本项目不提供任何账号和额度。请遵守官方服务条款，仅限个人和家庭使用，禁止恶意多并发和违规滥用。
