# Paper Collector

每天从计算机科学论文中找出真正值得看的文章，阅读全文后把原文和中文导读存入本地知识库；当天选择一篇与最近推送主题尽量不同的论文，用 `pdf2zh` 制作中英双语 PDF，打包成 ZIP 上传百度网盘，再由 Server酱把导读和分享链接推送到你的个人微信。

它不是“关键词命中后随机发一篇”的 RSS 套壳。脚本负责可复现的数据抓取、PDF、翻译、百度网盘分享、去重和投递；Codex agent 负责候选精排与全文审读。任一步失败都会保留状态，下一次从断点继续。

## 推荐逻辑

| 信号 | 小同行 `niche` | 大同行 `broad` |
|---|---:|---:|
| 兴趣相关性 | 高 | 高 |
| 全文内容与实验 | 很高，弱会也可入选 | 高，顶会同样审查 junk |
| venue | 辅助信号 | 强信号 |
| 年龄归一化引用 | 辅助信号 | 强信号 |
| 作者 h-index / 过往影响 | 不确定时参考 | 普通 venue 的重要门槛 |
| 时效性 | 强信号 | 强信号 |

第一阶段用透明的分项公式压缩候选；第二阶段让 Codex 对少量候选排序；第三阶段解析多篇 PDF 全文，要求 agent 按页核查任务定义、最强基线、公平比较、消融、数值提升、失败案例与限制。所有达到深读阈值的论文都会保存原文和导读，进入知识库待推送队列；当天选择时兼顾质量、时效性和最近若干天的主题重合度。

## 数据来源

- 会议/期刊官网：OpenReview（ICLR、TMLR）、PMLR（ICML、AISTATS、UAI、CoRL）、ACL Anthology（ACL、EMNLP、NAACL、COLING、TACL）、CVF Open Access（CVPR、ICCV、WACV）、NeurIPS Proceedings、USENIX（Security、NSDI、OSDI、ATC）、NDSS、JMLR RSS 和 PVLDB。
- 官方出版商 API：可选 IEEE Xplore（ToN、TDSC、TIFS、TNSM、TPAMI、TNNLS、TKDE、TSE、INFOCOM 等）和 Elsevier Scopus（当前限定 Computer Networks）；没有 key 时自动跳过。
- [OpenAlex API](https://docs.openalex.org/)：会议/期刊元数据、引用、作者画像和开放获取位置；同时搜索最近论文与近年高影响论文。
- [arXiv API](https://info.arxiv.org/help/api/index.html)：最新预印本与原始 PDF。
- [Semantic Scholar API](https://www.semanticscholar.org/product/api)：可选引用数、影响力引用和开放 PDF 补强；请求间隔至少 1 秒，每个本地自然日最多 30 次，用量跨进程保存在 SQLite 中。

每个官网在一次运行中只读取一次，随后针对所有兴趣在本地匹配；官网给出标题、摘要、作者、venue 和公开 PDF，OpenAlex / Semantic Scholar 再补引用、作者画像或缺失链接。某个官网改版或临时拒绝访问时只跳过该适配器，不会阻断其他来源。

不直接爬 Google Scholar：它没有稳定的公开 API，自动访问容易触发验证码和账号/地址封禁。也不模拟登录或高频抓取 ACM DL；ACM 论文由 OpenAlex 等合规元数据源覆盖。

## 快速开始

需要 macOS/Linux、`uv`、已登录的 Codex CLI、`pdf2zh` 和 `BaiduPCS-Go`。本项目使用 Codex 的非交互 `exec`、JSON Schema 输出和只读 sandbox；参见 [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)。

```bash
brew install baidupcs-go       # macOS；其他系统见 BaiduPCS-Go Releases
uv sync --extra dev --extra notes
uv run paper-collector init
cp .env.example .env
```

然后编辑 `config.toml`：

1. 把邮箱改为真实地址（OpenAlex polite pool 要求）。
2. 按需修改 `[[interests]]` 研究方向。小同行用 `scope = "niche"`，大同行用 `scope = "broad"`。
3. 在 `.env` 填 `SERVERCHAN_SENDKEY`；有 Semantic Scholar、IEEE Xplore 或 Elsevier key 也可一并填写。OpenAI-compatible 翻译还需填写 `OPENAILIKED_BASE_URL`、`OPENAILIKED_API_KEY` 和 `OPENAILIKED_MODEL`。
   OpenReview 的匿名 API 可能要求人机验证；如需稳定抓取 ICLR/TMLR，请填写 `OPENREVIEW_USERNAME` 和 `OPENREVIEW_PASSWORD`。启用 MFA 的账号可改填 `OPENREVIEW_ACCESS_TOKEN`，令牌优先于账号密码。
4. 根据领域补充 `top_venues` 和 `strong_venues`。venue 名称忽略大小写。
5. 在 `[baidupan].cookie` 填写百度网盘网页 Cookie。`doctor` 或首次上传会在 CLI 未登录时自动登录；已有有效登录状态时不会重复登录。

`config.toml` 已被 Git 忽略，可以直接使用单行 TOML 字符串：

```toml
[baidupan]
cookie = 'BDUSS=...; STOKEN=...; ...'
```

Cookie 含双引号时优先使用上面的 TOML 单引号写法。也可以保留
`cookie = "${BAIDUPAN_COOKIE}"`，把真实值放入同样被忽略的 `.env`。程序不会把 Cookie
写入日志或 Codex prompt；但 `BaiduPCS-Go` 的登录接口只支持命令参数，因此自动登录瞬间，
Cookie 会短暂存在于本机进程参数中。

`BaiduPCS-Go` 是第三方客户端，不是百度官方 SDK。若不愿把账号 Cookie 交给第三方程序，不应采用此方案；可改用百度开放平台应用，但其接入和分享权限申请明显更重。

先自检，再试跑：

```bash
uv run paper-collector doctor --config config.toml
uv run paper-collector run --config config.toml --dry-run
uv run paper-collector run --config config.toml --no-notify
uv run paper-collector run --config config.toml
```

- `--dry-run`：抓候选并调用 Codex 精排，不下载、翻译或推送。
- `--no-notify`：完成全文审读与翻译，把产物留在 `data/papers/`，不发微信。
- 正常运行：若上次已有待投递产物，优先续传，不会另选一篇。

其他命令：

```bash
uv run paper-collector discover --config config.toml
uv run paper-collector status --config config.toml
```

## 个人微信、Server酱与百度网盘

默认链路如下：

```text
英文原文 + 中英双语 PDF + 中文导读 → ZIP → 百度网盘根目录 /PaperCollector → Server酱 → 个人微信
```

1. 登录 [Server酱](https://sct.ftqq.com/)，用自己的微信完成绑定并取得 SendKey。
2. 把 SendKey 放入 `.env` 的 `SERVERCHAN_SENDKEY`，不要提交到 Git。
3. 免费额度目前每天 5 条，本项目正常每天只发送一条。
4. 微信中收到的是导读和百度网盘下载链接，不是聊天窗口里的原生附件；ZIP 内含英文原文、中英双语 PDF 和中文导读。

百度网盘目录必须是根目录下的单层文件夹，默认 `/PaperCollector`；程序会自动创建，所有推送 ZIP 都存放于此。分享有效期和固定提取码可在 `[baidupan]` 修改。提取码留空时，会根据网盘路径生成稳定的四位码；默认链接有效 365 天。百度可能对分享行为做频控或内容审核，失败时流水线不会标记已推送。

百度官方开放平台支持上传，但旧的个人“创建分享链接”接口自 2022 年起不再接受新申请，因此默认采用第三方 `BaiduPCS-Go`。参考：[百度网盘分享 FAQ](https://pan.baidu.com/union/doc/faq/%E5%88%86%E4%BA%AB%E6%96%87%E4%BB%B6/)、[BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go)。

若要切回企业微信群原生文件，在配置中设置 `channel = "wecom"`、`wecom_enabled = true`，并填写 `WECOM_WEBHOOK_KEY`。

## 每天定时

macOS 使用 `launchd`，不会依赖一直开着的终端：

```bash
uv run paper-collector schedule install --config config.toml --hour 9 --minute 0
```

日志位于 `data/logs/`。移除任务：

```bash
uv run paper-collector schedule uninstall
```

电脑在设定时间休眠时，普通 `StartCalendarInterval` 任务通常会在唤醒后运行；电脑必须能访问网络，且 Codex、BaiduPCS-Go 登录、翻译服务与 Server酱 SendKey 均有效。

### Linux 服务器：每周二、四、五

服务器需安装 `uv`、Codex CLI、`pdf2zh`、`BaiduPCS-Go` 和 `cron`，并配置好
`config.toml`、`.env` 及 Codex 的认证。服务器时区必须为北京时间（UTC+08:00）。
建议使用已验证的依赖约束安装翻译工具，避免新版 SDK 与 `pdf2zh` 不兼容：

```bash
uv tool install --python python3 --constraint deploy/pdf2zh-constraints.txt pdf2zh==1.9.11
```

```bash
uv sync --locked
uv run paper-collector doctor --config config.toml
sudo bash deploy/install-cron.sh
```

任务写入 `/etc/cron.d/paper-collector`，每周二、四、五 09:00 启动，每次最多推送一篇。
全文审读、翻译和上传需要时间，微信实际送达会晚于启动时间。
日志按日期保存在 `data/logs/cron-YYYY-MM-DD.log`，运行不依赖 SSH 会话。
迁移部署时应同时迁移 `data/`，保留已推送记录和知识库队列，避免重复推荐；
`config.toml` 和 `.env` 的文件权限应设置为 `600`。
启动脚本使用 Hugging Face 离线模式，需提前准备好 `pdf2zh` 的布局模型和中文字体缓存。

```bash
bash deploy/run-scheduled.sh --no-notify
sudo rm /etc/cron.d/paper-collector
```

第一条命令完整生成产物但不发送微信；第二条命令移除后续定时任务。
若服务器不能访问 PyPI 下载域名，可保持锁定版本和哈希校验，通过镜像安装：

```bash
uv venv --python python3
uv export --locked --extra dev --no-emit-project -o deployment-requirements.txt >/dev/null
uv pip install --python .venv/bin/python --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r deployment-requirements.txt
uv pip install --python .venv/bin/python --index-url https://pypi.tuna.tsinghua.edu.cn/simple --no-deps --editable .
.venv/bin/paper-collector doctor --config config.toml
```

若服务器运行在没有 systemd 的容器中，重启容器后需要执行 `service cron start`
恢复定时调度；也可把这条命令加入容器自己的启动入口。

## HTML 阅读笔记与推送链接

启用后，每次推送选定的论文都会额外生成一篇完整阅读笔记，格式依据本机
`~/.claude/skills/paper-reading-notes/SKILL.md` 实现；部署后的程序不依赖这个本地路径。
它不是中文导读的 HTML 转存：Codex 会重新读取整篇 PDF 和页面布局，规划全部编号图表的精确裁剪，
再检查联系表及逐张原尺寸图片，写出固定八章节的详细笔记：总览、写作组织、元数据、论证主线、
技术细节、评测协议、实验解读、来源与差异。包括带页码的证据、逐图逐表解释、
可重建的数据/输入输出记录、证据综合表与来源核对表。默认正文至少 30000 字符。

图表与样式均内嵌在单个 HTML 中，支持手机目录、阅读进度和保存后离线阅读。
推送末尾增加“阅读完整笔记（HTML）”超链接，原有百度网盘下载链接保留；新生成的 ZIP 也附带
`阅读笔记.html`。Server酱与企业微信两个通知通道均支持。

### 启用与部署

```bash
uv sync --locked --extra notes
```

在服务器的 `config.toml` 中增加以下配置，把示例 IP 换成手机实际能够访问的服务器 IP：

```toml
[reading_notes]
enabled = true
public_base_url = "http://198.51.100.25:8765"
host = "0.0.0.0"
port = 8765
timeout_seconds = 1800
min_chars = 30000
```

也可把 `public_base_url` 写成 `"${READING_NOTES_BASE_URL}"`，在 `.env` 中设置真实地址。
`public_base_url` 是通知中的对外访问地址；`host` 和 `port` 是本机监听地址，两者可能因 NAT、
容器端口映射或反向代理而不同。例如对外映射到 18765 时，URL 应用 18765，本机仍可监听 8765。
上面的 IP 是文档示例，不是 A100-2 的实际地址。SSH 能登录不代表 HTTP 端口能从微信访问；
需要另行确认公网映射、防火墙与容器端口，并用手机网络打开下面的健康检查地址。

前台启动（适合先检查连通性）：

```bash
.venv/bin/paper-collector serve-notes --config config.toml
curl http://198.51.100.25:8765/healthz
```

Linux 长驻运行（需要 root、`cron`、`flock`）：

```bash
bash deploy/install-notes-service.sh
.venv/bin/paper-collector doctor --config config.toml
```

安装器先验证配置，启动服务并检查本地健康状态，再由独立 cron watchdog 每分钟检查、退出后重启；
文件锁避免启动多个实例。不会修改原有每周二、四、五的论文推送任务。
日志为 `data/logs/notes-server.log`。无 systemd 的容器重启后，仍需从容器启动入口恢复
`service cron start`，之后笔记服务会由 watchdog 自动拉起。

为已审读过的论文补写笔记（不重发通知、不改变已投递状态）：

```bash
.venv/bin/paper-collector write-note 'arxiv:实际论文ID' --config config.toml
```

访问链接形如 `http://198.51.100.25:8765/notes/随机文件名.html`。
也可以给 `public_base_url` 加路径前缀；反向代理需保留该前缀，并为公网访问配置 HTTPS 和访问限流。
内置 HTTP 服务适合个人轻量使用，不应直接用 `python -m http.server` 暴露整个项目目录。

### A100-2 运行，VPS 只托管 HTML

所有业务始终部署并运行在 A100-2：每周二、四、五的任务、论文抓取、Codex 审读、翻译、
阅读笔记生成、SQLite 与微信通知均不迁移。若 A100-2 的 HTTP 可从公网访问，继续使用本机发布模式。
若不能访问，则在 A100-2 上配置 SSH 发布，把最终 HTML 上传到 `82.156.88.52`。
模式在部署时确认并配置；不会把生成任务自动迁移到 VPS。

```text
A100-2：选文 → 审读 → 翻译/HTML 笔记 → SSH 上传 HTML → 公网链接校验 → 微信推送
                                             ↓
                          VPS 82.156.88.52：仅提供 /notes/随机文件名.html
```

VPS 不安装 PaperCollector、Codex、pdf2zh，也不接收 `.env`、业务配置、API key、SQLite、
原始响应或知识库。独立的网页服务只依赖 Python 标准库；服务器已存在的其它站点不应被覆盖。

先在 **A100-2 上**建立到 VPS 的非交互 SSH 认证，并核验 VPS 主机指纹。不要把本地电脑的私钥
复制到服务器，也不要关闭主机指纹校验。本环境已配置 SSH 别名 `att-vps`，使用 `ubuntu` 用户及
A100-2 自有密钥；VPS 上该账号允许 sudo。实际用户名或 SSH 端口不同时应调整配置。

从已经更新代码的 A100-2 上，只复制网页服务、安装器和路由示例这三个不含凭据的文件：

```bash
cd /root/PaperCollector
scp src/paper_collector/notes_http.py deploy/install-vps-static.sh deploy/nginx-paper-notes-location.conf.example ubuntu@82.156.88.52:/tmp/
ssh ubuntu@82.156.88.52 'sudo -n env NOTES_STATIC_ROOT=/home/ubuntu/paper-collector-notes NOTES_STATIC_USER=ubuntu NOTES_STATIC_BIND=127.0.0.1 NOTES_STATIC_PREFIX=/paper-notes bash /tmp/install-vps-static.sh /tmp/notes_http.py 8765'
```

安装器只在 VPS 创建静态服务目录和独立服务，不安装论文推送定时器。上述命令使用
`/home/ubuntu/paper-collector-notes/`、非 root 的 `ubuntu` 服务账号，并仅监听 `127.0.0.1:8765`。
systemd 服务名为 `paper-collector-notes-static.service`，支持重启自动恢复。
未设置环境变量时，安装器默认使用 root 账号、`/root/paper-collector-notes/` 和公网监听；
无 systemd 的 root 部署可以退回 cron watchdog。

本次部署复用 VPS 已有 Nginx 和有效 HTTPS：在现有 443 server 中引入
`deploy/nginx-paper-notes-location.conf.example` 的独立 `/paper-notes/` 路由，保留原应用的其它路径。
后端必须收到完整 `/paper-notes/` 前缀；先执行 `nginx -t`，通过后才 reload。
无需把 8765 开放到公网，公开入口是现有 443 端口。健康检查地址为
`https://82.156.88.52/paper-notes/healthz`，应从 A100-2 和外部网络分别验证。

将 `deploy/reading-notes-vps.example.toml` 中的 `[reading_notes]` 节合入 **A100-2 的**
`config.toml`，保留所有其它配置；已有同名节时应替换，不能重复追加。关键设置是：

```toml
[reading_notes]
enabled = true
public_base_url = "https://82.156.88.52/paper-notes"
publish_ssh_host = "ubuntu@82.156.88.52"
publish_ssh_directory = "/home/ubuntu/paper-collector-notes/public"
publish_ssh_port = 22
publish_timeout_seconds = 180
```

`publish_ssh_directory` 是 VPS 网页根目录，程序会自动在其 `notes/` 子目录中发布。
不配置 `publish_ssh_host` / `publish_ssh_directory` 就维持 A100-2 本地发布，不发生远程上传。
VPS 模式不需要在 A100-2 上启动 HTTP 服务，也不要在 A100-2 上运行 `install-vps-static.sh`。

发布会先传入远端临时文件，核对 SHA-256 与文件大小，再原子替换最终 HTML，防止断线损坏已发布页面。
随后从 A100-2 请求公开笔记 URL，核对 HTTP 200、HTML 类型、文件大小及无意外跳转，成功后才保存
新链接并推送微信。上传失败或 VPS 公网端口不通时保留已验收笔记，后续重试只重新上传，不重新调用模型。

### 校验、重试与公开范围

- 图表越界、近整页截图、遗漏 caption、图表覆盖不全、视觉验收失败、缺失章节或证据页码、
  数据记录未逐字段解释、skill 禁用连接词、篇幅不足等都会阻止发布。少量禁用措辞先做局部修订，
  数值、证据页及其余段落保持不变；仍未通过时最多自动重做一次，
  仍失败时保留流水线状态，不发送缺少阅读笔记的通知。
- 已验收内容按 PDF SHA-256 缓存在知识库中，通知重试、重建丢失的公开 HTML 或修改访问域名
  不会重新调用模型。笔记会增加两阶段模型调用、token 用量和处理时间，必要时追加小型措辞修订调用；
  配置中的 timeout 为每阶段上限。
- 私有笔记、裁剪、原始响应和验收缓存位于该论文的知识库目录；SQLite 保存笔记路径和 URL。
  本地索引卡与 JSON 索引位于 `data/public_notes/`，内置 HTTP 服务不提供索引浏览或目录列表。
- HTTP 仅提供 `/healthz` 与严格匹配的 `/notes/随机文件名.html`，禁止任意路径、符号链接、
  配置、`.env`、数据库和运行日志访问。模型输出作为纯文本转义；固定进度脚本通过 CSP 哈希授权，
  不加载外部图片、字体或脚本。
- 这些笔记没有登录认证，持有链接的人均可阅读；随机 URL 不能代替访问控制。不要在笔记中发布私密材料。

开发验证：`uv sync --extra dev --extra notes` 后运行 `python -m pytest`。

## 状态、去重与隐私

`data/state.sqlite3` 是唯一事实源：

```text
discovered → ranked → reviewed → translated → shared → delivering → delivered
                         └──────────────→ rejected
任一临时失败 → failed → 下次续跑
```

- DOI 优先去重；无 DOI 时使用 arXiv ID 或规范化标题哈希。
- 同一论文跨来源出现时，优先采用官网的摘要、venue、落地页和 PDF，同时保留 OpenAlex 作者 ID 与各聚合源中较高的引用数。
- 本地知识库默认位于 `data/knowledge_base/papers/`；一篇论文一个目录，先保存原文、抽取文本和导读，被选中推送时再补双语 PDF 与最终 ZIP。
- 达标但当天未推送的论文保留为队列。选择下一篇时对最近 7 天相同兴趣、标签和标题主题降权，避免连续数天反复推同一题目；质量分仍是首要门槛。
- 百度网盘分享链接会在 Server酱投递前立即写入 SQLite；通知失败时重用链接，不会重复上传。
- Server酱确认创建推送任务后，导读与下载链接才会被原子标记为已投递。
- 企业微信备用通道仍保留“导读成功但文件失败时只重传文件”的行为。
- `config.toml`、`.env`、`data/` 默认不进入 Git；百度 Cookie 只用于本机调用 `BaiduPCS-Go`，不会进入论文审读 prompt。
- Codex 只拿到兴趣配置、公开论文元数据和抽取的论文文本；密钥不会放入 prompt。其工作目录限制在当次 `data/runs/`，shell 环境不继承主进程变量，论文中的指令会被明确当作不可信文本忽略。
- `rank.prompt.txt` 与 `review-*.prompt.txt` 会留在 `data/runs/` 便于审计。如不希望保留公开论文文本，可定期清理该目录，但不要删除 SQLite。

## 调整质量

- 推荐太泛：缩窄 `description`，加入短语型 `keywords` 和 `negative_keywords`。
- 小同行论文被 venue 压制：确认使用 `scope = "niche"`，可提高 `weight`。
- 大同行水文太多：提高 `min_recommendation_score`，完善顶会表，并在描述中明确不可接受的实验缺陷。
- PDF 来源临时返回 403/502：流水线会继续尝试精排榜单中的下一篇，直到完成配置数量的全文审读尝试；
  同一篇无需重新筛选。若整份 `shortlist_size` 中的 PDF 都不可下载，或论文未达到推荐质量阈值，则跳过本次推送，不生成虚假推荐。
- 每天没有论文：扩大 `recent_days` / `evergreen_years`，增加关键词或扩大 `shortlist_size`；
  `max_deep_reviews` 限定的是实际全文审读尝试数，不需要用来补偿下载失败。
- 相邻主题仍太像：提高 `agent.diversity_penalty` 或扩大 `agent.diversity_history_days`；若轮换过强则反向调低。
- pdf2zh 不稳定：在 `[translation]` 切换你已配置的 service。`google` 不需 key，但网络质量不可控。

## 开发

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

核心边界有单测覆盖：评分差异、DOI 去重、投递状态幂等、OpenAlex 摘要还原、导读渲染与 PDF 文件校验。
