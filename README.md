# Paper Collector

每天从计算机科学论文中找出一篇真正值得看的文章，阅读全文后生成中文导读，用 `pdf2zh` 制作中英双语 PDF，上传百度网盘，再由 Server酱把导读和分享链接推送到你的个人微信。

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

第一阶段用透明的分项公式压缩候选；第二阶段让 Codex 对少量候选排序；第三阶段解析 PDF 全文，要求 agent 按页核查任务定义、最强基线、公平比较、消融、数值提升、失败案例与限制。只有深读评分达到阈值才会推送。

## 数据来源

- [OpenAlex API](https://docs.openalex.org/)：会议/期刊元数据、引用、作者画像和开放获取位置；同时搜索最近论文与近年高影响论文。
- [arXiv API](https://info.arxiv.org/help/api/index.html)：最新预印本与原始 PDF。
- [Semantic Scholar API](https://www.semanticscholar.org/product/api)：可选引用数、影响力引用和开放 PDF 补强。

不直接爬 Google Scholar：它没有稳定的公开 API，自动访问容易触发验证码和账号/地址封禁。会议官网也没有统一结构；当前用 OpenAlex 的 venue 元数据覆盖历年论文。后续若某个方向需要官方 accepted-paper 页面，可在 `sources.py` 中添加独立 source，不影响状态机。

## 快速开始

需要 macOS/Linux、`uv`、已登录的 Codex CLI、`pdf2zh` 和 `BaiduPCS-Go`。本项目使用 Codex 的非交互 `exec`、JSON Schema 输出和只读 sandbox；参见 [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)。

```bash
brew install baidupcs-go       # macOS；其他系统见 BaiduPCS-Go Releases
uv sync --extra dev
uv run paper-collector init
cp .env.example .env
```

然后编辑 `config.toml`：

1. 把邮箱改为真实地址（OpenAlex polite pool 要求）。
2. 删除两个示例 `[[interests]]`，按你的研究方向填写。小同行用 `scope = "niche"`，大同行用 `scope = "broad"`。
3. 在 `.env` 填 `SERVERCHAN_SENDKEY`；有 Semantic Scholar key 也一并填写。
4. 根据领域补充 `top_venues` 和 `strong_venues`。venue 名称忽略大小写。
5. 登录一次 `BaiduPCS-Go`，登录状态会保存在当前 macOS 用户目录中，之后定时任务复用。

建议不要把百度 Cookie 直接写进命令历史或配置文件。可以从百度网盘网页取得 Cookie 后，用隐藏输入传给客户端：

```bash
read -s "BAIDUPAN_COOKIES?百度网盘 Cookie: "
BaiduPCS-Go login -cookies="$BAIDUPAN_COOKIES"
unset BAIDUPAN_COOKIES
BaiduPCS-Go who
```

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
dual PDF → 百度网盘 /PaperCollector → 带提取码分享链接 → Server酱 → 个人微信
```

1. 登录 [Server酱](https://sct.ftqq.com/)，用自己的微信完成绑定并取得 SendKey。
2. 把 SendKey 放入 `.env` 的 `SERVERCHAN_SENDKEY`，不要提交到 Git。
3. 免费额度目前每天 5 条，本项目正常每天只发送一条。
4. 微信中收到的是导读和百度网盘下载链接，不是聊天窗口里的原生附件；打开链接即可下载双语 PDF。

百度网盘默认目录、分享有效期和固定提取码可在 `[baidupan]` 修改。提取码留空时，会根据远程路径生成稳定的四位码；默认链接有效 365 天。百度可能对分享行为做频控或内容审核，失败时流水线不会标记已推送。

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

## 状态、去重与隐私

`data/state.sqlite3` 是唯一事实源：

```text
discovered → ranked → reviewed → translated → shared → delivering → delivered
                         └──────────────→ rejected
任一临时失败 → failed → 下次续跑
```

- DOI 优先去重；无 DOI 时使用 arXiv ID 或规范化标题哈希。
- 百度网盘分享链接会在 Server酱投递前立即写入 SQLite；通知失败时重用链接，不会重复上传。
- Server酱确认创建推送任务后，导读与下载链接才会被原子标记为已投递。
- 企业微信备用通道仍保留“导读成功但文件失败时只重传文件”的行为。
- `config.toml`、`.env`、`data/` 默认不进入 Git；百度 Cookie 由 `BaiduPCS-Go` 存在用户目录，不经过本项目。
- Codex 只拿到兴趣配置、公开论文元数据和抽取的论文文本；密钥不会放入 prompt。其工作目录限制在当次 `data/runs/`，shell 环境不继承主进程变量，论文中的指令会被明确当作不可信文本忽略。
- `rank.prompt.txt` 与 `review-*.prompt.txt` 会留在 `data/runs/` 便于审计。如不希望保留公开论文文本，可定期清理该目录，但不要删除 SQLite。

## 调整质量

- 推荐太泛：缩窄 `description`，加入短语型 `keywords` 和 `negative_keywords`。
- 小同行论文被 venue 压制：确认使用 `scope = "niche"`，可提高 `weight`。
- 大同行水文太多：提高 `min_recommendation_score`，完善顶会表，并在描述中明确不可接受的实验缺陷。
- 每天没有论文：扩大 `recent_days` / `evergreen_years`，增加关键词，或把 `max_deep_reviews` 从 3 调高。
- pdf2zh 不稳定：在 `[translation]` 切换你已配置的 service。`google` 不需 key，但网络质量不可控。

## 开发

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```

核心边界有单测覆盖：评分差异、DOI 去重、投递状态幂等、OpenAlex 摘要还原、导读渲染与 PDF 文件校验。
